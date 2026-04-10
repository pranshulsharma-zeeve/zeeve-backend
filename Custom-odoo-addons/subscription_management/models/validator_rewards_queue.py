from odoo import models, fields, api
import logging
from ..utils.subscription_helpers import (
    _extract_validator_address,
    _normalize_protocol_name,
    SUPPORTED_VALIDATOR_HISTORY_PROTOCOLS,
)

_logger = logging.getLogger(__name__)


class ValidatorRewardsQueue(models.Model):
    _name = 'validator.rewards.queue'
    _description = 'Validator Rewards Queue'
    _order = 'create_date asc'
    
    valoper = fields.Char(string='Validator Address', required=True, index=True)
    rpc_url = fields.Char(string='RPC URL', required=True)
    protocol_id = fields.Many2one('protocol.master', string='Protocol', required=True, index=True)
    node_id = fields.Many2one('subscription.node', string='Node', index=True, ondelete='cascade')
    state = fields.Selection([
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed')
    ], string='State', default='pending', required=True, index=True)
    create_date = fields.Datetime(string='Created Date', default=fields.Datetime.now, index=True)
    last_attempt_date = fields.Datetime(string='Last Attempt Date')
    last_snapshot_date = fields.Datetime(string='Last Snapshot Date')
    error_message = fields.Text(string='Error Message')
    
    def populate_rewards_queue(self):
        """
        Populate the queue with validators from supported protocols (Coreum, Avalanche, etc.).
        Deduplicates by ensuring only one pending/failed record exists per validator/protocol pair.
        Returns counts of created, updated, and deleted records.
        """
        _logger.info("Starting queue population for validator rewards")
        
        nodes = self.env['subscription.node'].search([
            ('node_type', '=', 'validator'),
            ('state', '=', 'ready'),
            ('subscription_id.protocol_id', '!=', False),
        ])
        
        _logger.info(f"Evaluating {len(nodes)} validator nodes for rewards queue")
        
        # Extract validator information
        validators = []
        for node in nodes:
            validator_info, valoper = _extract_validator_address(node)
            protocol = node.subscription_id.protocol_id
            protocol_id = protocol.id
            protocol_key = _normalize_protocol_name(protocol.name if protocol else "")

            if protocol_key not in SUPPORTED_VALIDATOR_HISTORY_PROTOCOLS:
                continue
            
            # Get RPC URL based on network selection
            network_selection = node.network_selection_id
            network_name = (network_selection.name or "").strip().lower() if network_selection else "mainnet"
            
            if network_name == "testnet":
                rpc_url = (protocol.web_url_testnet or "").strip()
            else:
                rpc_url = (protocol.web_url or "").strip()
            
            if rpc_url:
                rpc_url = rpc_url.rstrip("/")
            
            if not rpc_url:
                _logger.warning(f"No RPC URL found for node {node.id}, skipping")
                continue
                
            if valoper and rpc_url and protocol_id:
                validators.append((valoper, rpc_url, protocol_id, node.id))
        
        _logger.info(f"Extracted {len(validators)} validators with addresses across supported protocols")
        
        created_count = 0
        updated_count = 0
        deleted_count = 0
        
        for valoper, rpc_url, protocol_id, node_id in validators:
            try:
                # Search for existing pending/failed records for this node
                existing_records = self.search([
                    ('node_id', '=', node_id),
                    ('state', 'in', ['pending', 'failed'])
                ])
                
                if len(existing_records) > 1:
                    # Multiple records found - keep first, delete rest
                    records_to_delete = existing_records[1:]
                    deleted_count += len(records_to_delete)
                    records_to_delete.unlink()
                    _logger.info(f"Deleted {len(records_to_delete)} duplicate records for node {node_id}")
                
                if len(existing_records) >= 1:
                    # Update the first/remaining record
                    record = existing_records[0]
                    record.write({
                        'valoper': valoper,
                        'rpc_url': rpc_url,
                        'protocol_id': protocol_id,
                        'node_id': node_id,
                        'state': 'pending',
                        'error_message': False,
                    })
                    updated_count += 1
                    _logger.debug(f"Updated queue record for node {node_id}")
                else:
                    # No existing record - create new one
                    self.create({
                        'valoper': valoper,
                        'rpc_url': rpc_url,
                        'protocol_id': protocol_id,
                        'node_id': node_id,
                        'state': 'pending',
                    })
                    created_count += 1
                    _logger.debug(f"Created queue record for node {node_id}")
                    
            except Exception as e:
                _logger.error(f"Error processing node {node_id} (valoper: {valoper}): {str(e)}", exc_info=True)
                continue
        
        _logger.info(
            f"Queue population completed. Created: {created_count}, Updated: {updated_count}, Deleted: {deleted_count}"
        )
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Queue Population Complete',
                'message': f'Created: {created_count}, Updated: {updated_count}, Deleted: {deleted_count}',
                'type': 'success',
                'sticky': False,
            }
        }
