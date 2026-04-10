from odoo import models, fields, api
from datetime import timedelta
import logging
import time
import concurrent.futures
from psycopg2 import IntegrityError
from odoo.tools import mute_logger

from ..utils.subscription_helpers import (
    _collect_validator_reward_snapshot,
    _extract_validator_address,
    _extract_delegation_address,
    _flow_extract_owner_address,
    _normalize_protocol_name,
)

_logger = logging.getLogger(__name__)


class ValidatorRewardsSnapshot(models.Model):
    _name = 'validator.rewards.snapshot'
    _description = 'Validator Rewards Snapshot'
    _order = 'snapshot_date desc'
    valoper = fields.Char(string='Validator Identifier', required=True, index=True)
    outstanding_rewards = fields.Float(string='Outstanding Rewards', help='Converted outstanding rewards in main token units')
    total_stake = fields.Float(string='Total Stake', required=True, help='Total validator stake in main token units')
    owned_stake = fields.Float(string='Owned Stake', help='Validator self-bonded stake in main token units')
    total_rewards = fields.Float(string='Total Rewards', help='Total validator rewards')
    delegator_count = fields.Integer(string='Delegator Count', required=True, help='Number of delegators')
    snapshot_date = fields.Datetime(string='Snapshot Date', default=fields.Datetime.now, index=True)
    protocol_id = fields.Many2one('protocol.master', string='Protocol', required=True, index=True)
    protocol_key = fields.Char(string='Protocol Key', index=True)
    node_id = fields.Many2one('subscription.node', string='Node', index=True, ondelete='cascade')
    epoch = fields.Integer(string='Epoch', help='Blockchain epoch number (used by Solana)')
    commission_pct = fields.Float(string='Commission', help='Validator commission percentage')
    apr_pct = fields.Float(string='APR', help='Validator APR percentage')
    network_apr = fields.Float(string='Network APR', help='Network-wide APR percentage')

    _sql_constraints = [
        ('valoper_node_date_unique', 'unique(node_id, valoper, snapshot_date)', 'Snapshot for this validator and date already exists')
    ]
    
    def snapshot_all_validators_rewards(self):
        """
        Cron job method to process pending validators from the queue.
        Processes validators in batches of 20 using parallel threads until queue is empty
        or max_runtime is reached.
        """
        _logger.info("Starting rewards snapshot processing from queue")
        
        start_time = time.time()
        max_runtime = 90  # seconds safety limit
        batch_limit = 20
        # Fetch max_workers from config, default to 5 if not set
        max_workers = int(self.env['ir.config_parameter'].sudo().get_param('subscription_management.validator_max_workers', 5))
        
        # Fetch Dune API config once in main thread (before spawning workers)
        dune_api_key = (self.env['ir.config_parameter'].sudo().get_param('DUNE_API_KEY') or '').strip() or None
        dune_api_base = (self.env['ir.config_parameter'].sudo().get_param('DUNE_API_BASE') or '').strip() or None
        ewx_reward_url = (self.env['ir.config_parameter'].sudo().get_param('ewx_reward_url') or '').strip() or None
        energy_web_api_key = (self.env['ir.config_parameter'].sudo().get_param('energy_web_api_key') or '').strip() or None
        
        _logger.info(f"Dune API config: key={'SET' if dune_api_key else 'NOT SET'}, base={'SET' if dune_api_base else 'NOT SET'}")
        _logger.info(f"EWX reward URL: {'SET' if ewx_reward_url else 'NOT SET'}")
        _logger.info(f"EWX API key: {'SET' if energy_web_api_key else 'NOT SET'}")
        
        queue_model = self.env['validator.rewards.queue']
        
        total_success = 0
        total_error = 0
        total_processed = 0
        
        while True:
            # Check for global timeout before starting a new batch
            elapsed = time.time() - start_time
            if elapsed >= max_runtime:
                _logger.warning(f"Max runtime ({max_runtime}s) reached. Stopping processing with {total_processed} records handled so far.")
                break

            # Fetch pending queue records (lock them immediately in main thread)
            pending_records = queue_model.search(
                [('state', '=', 'pending')],
                order='create_date ASC',
                limit=batch_limit
            )
            
            if not pending_records:
                _logger.info("No more pending validators in queue.")
                break
            
            batch_size = len(pending_records)
            _logger.info(f"Processing batch of {batch_size} pending validators")
            
            # 1. PREPARE TASKS & LOCK RECORDS
            tasks = []
            valid_records = []
            
            for queue_record in pending_records:
                total_processed += 1
                
                # Update state to processing immediately (Main Thread DB Op)
                queue_record.write({
                    'state': 'processing',
                    'last_attempt_date': fields.Datetime.now(),
                })

                # Validate Data
                protocol = queue_record.protocol_id
                protocol_key = _normalize_protocol_name(protocol.name if protocol else "")
                
                error_msg = None
                if not protocol or not protocol_key:
                    error_msg = 'Protocol information missing for queue record'
                elif not queue_record.rpc_url:
                    error_msg = 'RPC URL missing for validator snapshot'
                
                if error_msg:
                    queue_record.write({
                        'state': 'failed',
                        'error_message': error_msg,
                    })
                    total_error += 1
                    self.env.cr.commit() # Commit failures immediately
                    continue

                # Add to tasks
                valid_records.append(queue_record)

                # Extract delegation/owner address from node validator info (needed for NEAR)
                task_owner_address = None
                if queue_record.node_id:
                    try:
                        node_validator_info, _ = _extract_validator_address(queue_record.node_id)
                        task_owner_address = _extract_delegation_address(node_validator_info)
                    except Exception:
                        pass

                tasks.append({
                    'record_id': queue_record.id,
                    'protocol_key': protocol_key,
                    'valoper': queue_record.valoper,
                    'rpc_url': queue_record.rpc_url,
                    'protocol_id': protocol.id,
                    'node_id': queue_record.node_id.id if queue_record.node_id else False,
                    'dune_api_key': dune_api_key,
                    'dune_api_base': dune_api_base,
                    'ewx_reward_url': ewx_reward_url,
                    'energy_web_api_key': energy_web_api_key,
                    'owner_address': task_owner_address,
                })
                
            self.env.cr.commit() # Commit the 'processing' state
            
            if not tasks:
                continue # Go to next batch if all failed validation

            # 2. PARALLEL EXECUTION (Network I/O only)
            _logger.info(f"Starting parallel execution for {len(tasks)} tasks with {max_workers} threads")
            results = {}
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Map future to record_id
                network_key = None
                owner_address = None
                if protocol_key == "flow":
                    network_key, owner_address = self._resolve_flow_context(queue_record)

                    
                    future_to_id = {executor.submit(
                            _collect_validator_reward_snapshot, 
                            task['protocol_key'], 
                            task['valoper'], 
                            task['rpc_url'],
                            network_key=network_key,
                            owner_address=owner_address
                        ): task['record_id'] for task in tasks}
                else:
                    future_to_id = {
                    executor.submit(
                        _collect_validator_reward_snapshot,
                        task['protocol_key'],
                        task['valoper'],
                        task['rpc_url'],
                        task['dune_api_key'],
                        task['dune_api_base'],
                        ewx_reward_url=task['ewx_reward_url'],
                        ewx_api=task['energy_web_api_key'],
                        owner_address=task.get('owner_address'),
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
                protocol_key = _normalize_protocol_name(queue_record.protocol_id.name or "")

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
                                f"Failed to fetch rewards for {queue_record.valoper} ({protocol_key}): "
                                f"{result.get('note')} (error: {result.get('error')})"
                            )
                        else:
                            # Create snapshot
                            try:
                                with self.env.cr.savepoint(), mute_logger('odoo.sql_db'):
                                    snapshot_vals = {
                                        'valoper': queue_record.valoper,
                                        'outstanding_rewards': result.get('outstanding_rewards', 0.0),
                                        'total_rewards': result.get('total_rewards', 0.0),
                                        'total_stake': result.get('tokens', 0.0),
                                        'owned_stake': result.get('owned_stake', result.get('ownedStake', 0.0)),
                                        'delegator_count': result.get('delegator_count', 0),
                                        'protocol_id': queue_record.protocol_id.id,
                                        'protocol_key': protocol_key,
                                        'node_id': queue_record.node_id.id if queue_record.node_id else False,
                                    }
                                    if result.get('epoch') is not None:
                                        snapshot_vals['epoch'] = result['epoch']
                                    if result.get('commission_pct') is not None:
                                        snapshot_vals['commission_pct'] = result['commission_pct']
                                    if result.get('apr_pct') is not None:
                                        snapshot_vals['apr_pct'] = result['apr_pct']
                                    if result.get('network_apr') is not None:
                                        snapshot_vals['network_apr'] = result['network_apr']
                                    self.create(snapshot_vals)
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
                                f"Successfully captured rewards for {queue_record.valoper} ({protocol_key})"
                            )
                    
                    self.env.cr.commit()

                except Exception as e:
                     _logger.error(f"Error saving result for {queue_record.valoper}: {e}", exc_info=True)
                     # Try to mark failed
                     try:
                         with self.env.cr.savepoint():
                            queue_record.write({'state': 'failed', 'error_message': str(e)})
                     except Exception as e2: 
                         _logger.error(f"Failed to write error status for {queue_record.valoper}: {e2}")
                         pass
                     
                     # Commit the failure status if successful (or just the previous clean state)
                     try:
                         self.env.cr.commit()
                     except:
                         pass
                     
                     total_error += 1
        
        # Cleanup old snapshots (run once after all batches)
        try:
            cutoff_date = fields.Datetime.now() - timedelta(days=60)
            old_snapshots = self.search([('snapshot_date', '<', cutoff_date)])
            deleted_count = len(old_snapshots)
            if deleted_count > 0:
                old_snapshots.unlink()
                _logger.info(f"Cleaned up {deleted_count} old rewards snapshots")
        except Exception as e:
            _logger.error(f"Error cleaning up old rewards snapshots: {str(e)}")

        elapsed_total = time.time() - start_time
        _logger.info(
            f"Rewards snapshot processing completed. "
            f"Total Success: {total_success}, Total Errors: {total_error}, "
            f"Total Processed: {total_processed}, "
            f"Time: {elapsed_total:.1f}s"
        )
        
        # Check if we need to chain the cron job (if there are still pending records)
        remaining_pending = queue_model.search_count([('state', '=', 'pending')])
        if remaining_pending > 0:
            _logger.info(f"Cron timed out with {remaining_pending} records remaining. Chaining next execution immediately.")
            try:
                self.env.ref('subscription_management.cron_snapshot_validator_rewards')._trigger()
            except Exception as e:
                _logger.error(f"Failed to chain cron job: {e}")

        return True

    def _resolve_flow_context(self, queue_record):
        """Return Flow network key and owner address for a queue record."""
        node_model = self.env['subscription.node'].sudo()
        domain = [
            ('node_type', '=', 'validator'),
            ('subscription_id.protocol_id', '=', queue_record.protocol_id.id),
            ('validator_info', '!=', False),
            ('validator_info', 'ilike', queue_record.valoper or ''),
        ]
        nodes = node_model.search(domain)
        for node in nodes:
            validator_info, valoper = _extract_validator_address(node)
            if valoper != queue_record.valoper:
                continue
            network_name = (node.network_selection_id.name or '').strip().lower()
            network_key = 'testnet' if network_name == 'testnet' else 'mainnet'
            owner_address = _flow_extract_owner_address(validator_info)
            return network_key, owner_address
        return None, None
