from odoo import models, fields, api
from datetime import timedelta
import logging
import time
import concurrent.futures
from psycopg2 import IntegrityError
from odoo.tools import mute_logger

from ..utils.subscription_helpers import (
    _collect_validator_performance_snapshot,
    _normalize_protocol_name,
)

_logger = logging.getLogger(__name__)


class ValidatorPerformanceSnapshot(models.Model):
    _name = 'validator.performance.snapshot'
    _description = 'Validator Performance Snapshot'
    _order = 'snapshot_date desc'

    valoper = fields.Char(string='Validator Address', required=True, index=True)
    valcons_addr = fields.Char(string='Consensus Address')
    height = fields.Integer(string='Block Height')
    missed_counter = fields.Integer(string='Missed Blocks Counter')
    window_size = fields.Integer(string='Slashing Window Size')
    snapshot_date = fields.Datetime(string='Snapshot Date', default=fields.Datetime.now, index=True)
    protocol_key = fields.Char(string='Protocol Key', default='coreum', index=True)
    protocol_id = fields.Many2one('protocol.master', string='Protocol', index=True)
    node_id = fields.Many2one('subscription.node', string='Node', index=True, ondelete='cascade')
    expected_blocks=fields.Integer(string='Expected Blocks')
    produced_blocks=fields.Integer(string='Produced Blocks')
    signed_blocks = fields.Integer(string='Signed Blocks', compute='_compute_signed_blocks', store=False)
    
    _sql_constraints = [
        ('valoper_node_date_unique', 'unique(node_id, valoper, snapshot_date)', 'Snapshot for this validator and date already exists')
    ]
    
    @api.depends('window_size', 'missed_counter')
    def _compute_signed_blocks(self):
        """Compute signed blocks from window size and missed counter"""
        for record in self:
            if record.window_size and record.missed_counter is not None:
                record.signed_blocks = record.window_size - record.missed_counter
            else:
                record.signed_blocks = 0
    
    def snapshot_all_validator_performance(self):
        """
        Cron job method to process pending validators from the queue.
        Processes validators in batches of 20 using parallel threads until queue is empty
        or max_runtime is reached.
        """
        _logger.info("Starting performance snapshot processing from queue")
        
        start_time = time.time()
        max_runtime = 90  # seconds safety limit
        batch_limit = 20
        # Fetch max_workers from config, default to 5 if not set
        max_workers = int(self.env['ir.config_parameter'].sudo().get_param('subscription_management.validator_max_workers', 5))
        
        queue_model = self.env['validator.performance.queue']
        
        total_success = 0
        total_error = 0
        total_processed = 0
        
        while True:
            # Check for global timeout before starting a new batch
            elapsed = time.time() - start_time
            if elapsed >= max_runtime:
                _logger.warning(f"Max runtime ({max_runtime}s) reached. Stopping processing with {total_processed} records handled so far.")
                break

            # Fetch pending queue records
            pending_records = queue_model.search(
                [('state', '=', 'pending')],
                order='create_date ASC',
                limit=batch_limit
            )
            
            if not pending_records:
                _logger.info("No pending validators in queue")
                break
            
            batch_size = len(pending_records)
            _logger.info(f"Processing batch of {batch_size} pending validators")
            
            # 1. PREPARE TASKS & LOCK RECORDS
            tasks = []
            valid_records = []
            
            for queue_record in pending_records:
                total_processed += 1
                
                # Update state to processing
                queue_record.write({
                    'state': 'processing',
                    'last_attempt_date': fields.Datetime.now(),
                })
                
                # Validate Data
                protocol = queue_record.protocol_id
                error_msg = None
                
                if not protocol:
                     error_msg = 'Protocol information missing for queue record'
                
                protocol_key = _normalize_protocol_name(protocol.name or "") if protocol else None
                
                if not protocol_key and not error_msg:
                     error_msg = 'Protocol information missing for queue record'
    
                if not queue_record.rpc_url and not error_msg:
                     error_msg = 'RPC URL missing for validator snapshot'
                
                if error_msg:
                    queue_record.write({
                        'state': 'failed',
                        'error_message': error_msg,
                    })
                    total_error += 1
                    self.env.cr.commit()
                    continue
    
                # Add to tasks
                valid_records.append(queue_record)
                tasks.append({
                    'record_id': queue_record.id,
                    'protocol_key': protocol_key,
                    'valoper': queue_record.valoper,
                    'rpc_url': queue_record.rpc_url,
                    'protocol_id': protocol.id,
                    'node_id': queue_record.node_id.id if queue_record.node_id else False,
                    'validator_info': queue_record.node_id.validator_info if queue_record.node_id else None,
                })
            
            self.env.cr.commit()

            if not tasks:
                continue
    
            # 2. PARALLEL EXECUTION (Network I/O only)
            _logger.info(f"Starting parallel execution for {len(tasks)} tasks with {max_workers} threads")
            results = {}
    
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_id = {
                    executor.submit(
                        _collect_validator_performance_snapshot,
                        task['protocol_key'],
                        task['valoper'],
                        task['rpc_url'],
                        task.get('validator_info')
                    ): task['record_id'] for task in tasks
                }
    
                for future in concurrent.futures.as_completed(future_to_id):
                    record_id = future_to_id[future]
                    try:
                        data = future.result()
                        results[record_id] = data
                    except Exception as e:
                        _logger.error(f"Thread execution failed for record {record_id}: {e}")
                        results[record_id] = {'error': 'thread_error', 'note': str(e)}
    
            # 3. WRITE RESULTS (Main Thread DB Ops)
            for queue_record in valid_records:
                result = results.get(queue_record.id, {'error': 'unknown', 'note': 'No result returned'})
                try:
                    with self.env.cr.savepoint():
                        if 'error' in result:
                            # Mark as failed
                            queue_record.write({
                                'state': 'failed',
                                'error_message': f"{result.get('error')}: {result.get('note')}",
                            })
                            total_error += 1
                            _logger.warning(
                                f"Failed to fetch performance for {queue_record.valoper}: "
                                f"{result.get('note')} (error: {result.get('error')})"
                            )
                        else:
                            # Create snapshot record
                            try:
                                with self.env.cr.savepoint(), mute_logger('odoo.sql_db'):
                                    self.create({
                                        'valoper': queue_record.valoper,
                                        'valcons_addr': result.get('valconsAddr'),
                                        'height': result.get('height'),
                                        'missed_counter': result.get('missedCounter'),
                                        'window_size': result.get('windowSize'),
                                        'protocol_key': _normalize_protocol_name(queue_record.protocol_id.name or "") or 'coreum',
                                        'protocol_id': queue_record.protocol_id.id,
                                        'expected_blocks': result.get('expectedBlocks'),
                                        'produced_blocks': result.get('producedBlocks'),
                                        'node_id': queue_record.node_id.id if queue_record.node_id else False,
                                    })
                            except IntegrityError:
                                _logger.warning(f"Snapshot already exists for {queue_record.valoper}, marking as done.")
                            
                            # Mark as completed
                            queue_record.write({
                                'state': 'completed',
                                'last_snapshot_date': fields.Datetime.now(),
                                'error_message': False,
                            })
                            total_success += 1
                            _logger.info(
                                f"Successfully created performance snapshot for {queue_record.valoper} at height {result.get('height')}"
                            )
                    
                    self.env.cr.commit()
                    
                except Exception as e:
                    _logger.error(f"Error saving result for {queue_record.valoper}: {e}", exc_info=True)
                    try:
                         with self.env.cr.savepoint():
                            queue_record.write({'state': 'failed', 'error_message': str(e)})
                    except Exception as e2:
                         _logger.error(f"Failed to write error status for {queue_record.valoper}: {e2}")
                         pass
                    
                    try:
                         self.env.cr.commit()
                    except:
                         pass
                    total_error += 1

        # Cleanup old snapshots
        try:
            cutoff_date = fields.Datetime.now() - timedelta(days=60)
            old_snapshots = self.search([('snapshot_date', '<', cutoff_date)])
            deleted_count = len(old_snapshots)
            if deleted_count > 0:
                old_snapshots.unlink()
                _logger.info(f"Cleaned up {deleted_count} old performance snapshots")
        except Exception as e:
            _logger.error(f"Error cleaning up old performance snapshots: {str(e)}", exc_info=True)
        
        elapsed_total = time.time() - start_time
        _logger.info(
            f"Performance snapshot processing completed. "
            f"Total Success: {total_success}, Total Errors: {total_error}, "
            f"Total Processed: {total_processed}, "
            f"Time: {elapsed_total:.1f}s"
        )
        
        # Check if we need to chain the cron job (if there are still pending records)
        remaining_pending = queue_model.search_count([('state', '=', 'pending')])
        if remaining_pending > 0:
            _logger.info(f"Cron timed out with {remaining_pending} records remaining. Chaining next execution immediately.")
            try:
                self.env.ref('subscription_management.cron_snapshot_validator_performance')._trigger()
            except Exception as e:
                _logger.error(f"Failed to chain cron job: {e}")

        return True

    def snapshot_all_coreum_validators(self):
        """
        Backward compatibility method for old cron jobs.
        Redirects to snapshot_all_validator_performance.
        """
        _logger.warning("Method 'snapshot_all_coreum_validators' is deprecated. Please update the cron job to use 'snapshot_all_validator_performance'.")
        return self.snapshot_all_validator_performance()
    
    def link_snapshots_to_nodes(self):
        """
        Utility method to link existing snapshots to nodes by matching valoper addresses.
        Searches subscription.node records, extracts valoper from validator_info JSON field,
        and updates snapshot records to set the corresponding node_id.
        """
        _logger.info("Starting legacy snapshot linking to nodes")
        
        import json
        
        # Get all validator nodes
        nodes = self.env['subscription.node'].search([
            ('node_type', '=', 'validator'),
            ('validator_info', '!=', False),
        ])
        
        total_updated = 0
        total_errors = 0
        
        for node in nodes:
            try:
                # Parse validator_info JSON
                validator_info = json.loads(node.validator_info)
                
                # Extract valoper from various possible keys
                valoper = (
                    validator_info.get('validatorAddress') or
                    validator_info.get('valoperAddress') or
                    validator_info.get('valoper_address') or
                    validator_info.get('validator_address') or
                    validator_info.get('valoper') or
                    validator_info.get('address') or
                    validator_info.get('nodeId') or
                    validator_info.get('nodeIdentifier') or
                    validator_info.get('validatorNodeId') or
                    validator_info.get('node_id')
                )
                
                if not valoper:
                    _logger.warning(f"No valoper found in validator_info for node {node.id}")
                    continue
                
                # Update performance snapshots
                performance_snapshots = self.search([
                    ('valoper', '=', valoper),
                    ('node_id', '=', False),
                ])
                if performance_snapshots:
                    performance_snapshots.write({'node_id': node.id})
                    total_updated += len(performance_snapshots)
                    _logger.info(f"Linked {len(performance_snapshots)} performance snapshots to node {node.id} (valoper: {valoper})")
                
                # Update rewards snapshots
                rewards_snapshots = self.env['validator.rewards.snapshot'].search([
                    ('valoper', '=', valoper),
                    ('node_id', '=', False),
                ])
                if rewards_snapshots:
                    rewards_snapshots.write({'node_id': node.id})
                    total_updated += len(rewards_snapshots)
                    _logger.info(f"Linked {len(rewards_snapshots)} rewards snapshots to node {node.id} (valoper: {valoper})")
                
            except json.JSONDecodeError as e:
                _logger.error(f"Failed to parse validator_info for node {node.id}: {e}")
                total_errors += 1
            except Exception as e:
                _logger.error(f"Error linking snapshots for node {node.id}: {e}", exc_info=True)
                total_errors += 1
        
        _logger.info(f"Legacy snapshot linking completed. Total updated: {total_updated}, Total errors: {total_errors}")
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Legacy Data Linking Complete',
                'message': f'Updated: {total_updated} snapshots, Errors: {total_errors}',
                'type': 'success' if total_errors == 0 else 'warning',
                'sticky': False,
            }
        }
