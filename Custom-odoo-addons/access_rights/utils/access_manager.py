from odoo.http import request

class AccessManager:
    @staticmethod
    def check_module_access(user, module_name):
        """
        Check if the user has access to a specific module.
        Super Admin, Admin and Regular Users (no role) have module-level access.
        Operator access is determined by module.access entries.
        """
        if not user.company_role:
            # Regular users have implicitly granted module access;
            # their data visibility is restricted by get_company_domain.
            return True
        if user.company_role == 'operator':
            access = request.env['module.access'].sudo().search([
                ('user_id', '=', user.id),
                ('module_name', '=', module_name),
                ('read_access', '=', True)
            ], limit=1)
            return bool(access)
        if user.company_role in ['super_admin', 'admin']:
            return True
        return False

    @staticmethod
    def get_company_domain(user, customer_field='customer_id'):
        """
        Return a domain filter that restricts records to the user's company or ownership.
        - Super Admin/Admin/Operator: See all records in their company.
        - Regular User (no role): See only their own records (customer_field == partner_id).
        """
        company_id = user.company_id.id
        partner_id = user.partner_id.id

        # Regular users without a role are restricted to their own partner_id
        if not user.company_role:
            return [(customer_field, '=', partner_id)]

        # Partner IDs of all users in the same company
        company_partner_ids = request.env['res.users'].sudo().search([
            ('company_id', '=', company_id)
        ]).mapped('partner_id.id')
        
        # Handle legacy records for privileged roles: 
        # Show if company matches OR if it's a legacy record (company_id is False or 1) and the customer belongs to our company
        main_company = request.env.ref('base.main_company', raise_if_not_found=False)
        main_company_id = main_company.id if main_company else 1
        
        return [
            '|',
            ('company_id', '=', company_id),
            '&',
            ('company_id', 'in', [False, main_company_id]),
            (customer_field, 'in', company_partner_ids)
        ]

    @staticmethod
    def get_record_domain(user, module_name):
        """
        Return a domain filter for records based on user role and record access entries.
        Super Admin and Admin see all records for their company.
        Operator sees only assigned records.
        """
        if not user.company_role or user.company_role in ['super_admin', 'admin']:
            return []
        
        if user.company_role == 'operator':
            access_records = request.env['record.access'].sudo().search([
                ('user_id', '=', user.id),
                ('module_name', '=', module_name)
            ])
            record_ids = access_records.mapped('record_id')
            return [('id', 'in', record_ids)]
        
        return [('id', '=', -1)] # Deny all if no role matches

    @staticmethod
    def validate_user_operation(user, target_user, operation):
        """
        Validate if a user can perform an operation on another user.
        Rules:
        - Admin cannot delete Super Admin or other Admins.
        - Super Admin cannot be deleted.
        """
        if operation == 'delete':
            if target_user.company_role == 'super_admin':
                return False
            if user.company_role == 'admin' and target_user.company_role == 'admin':
                return False
        return True

    # Mapping from frontend node names to Odoo node_type selection keys
    _NODE_NAME_MAP = {
        'all': None,
        'validator': 'validator',
        'archive': 'archive',
        'archieve': 'archive',   # handle common typo from frontend
        'rpc': 'rpc',
        'full': 'rpc',           # "Full" nodes are RPC in Odoo
    }

    @staticmethod
    def get_node_type_domain(user):
        """
        Return a domain filter for subscription.node based on the operator's
        node_access_type and specific_nodes fields.
        Supports both node types (rpc, validator, etc.) and specific node_identifiers.
        """
        import json as _json
        if not user.company_role or user.company_role in ['super_admin', 'admin']:
            return []
        if user.node_access_type != 'specific':
            return []
        try:
            raw = user.specific_nodes or '[]'
            names = _json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            return [('id', '=', -1)]
        
        types = []
        identifiers = []
        for name in names:
            name = (name or '').strip()
            if not name: continue
            key = AccessManager._NODE_NAME_MAP.get(name.lower())
            if key:
                if key not in types: types.append(key)
            else:
                if name not in identifiers: identifiers.append(name)
        
        domain = []
        if types:
            domain.append(('node_type', 'in', types))
        if identifiers:
            if domain:
                domain = ['|'] + domain + [('node_identifier', 'in', identifiers)]
            else:
                domain = [('node_identifier', 'in', identifiers)]
        
        return domain if domain else [('id', '=', -1)]

    @staticmethod
    def get_rollup_type_domain(user):
        """
        Return a domain filter for rollup.service based on the operator's
        node_access_type and specific_rollups fields.
        Supports both rollup types (Arbitrum, zkSync, etc.) and specific service_ids.
        """
        import json as _json
        if not user.company_role or user.company_role in ['super_admin', 'admin']:
            return []
        if user.node_access_type != 'specific':
            return []
        try:
            raw = user.specific_rollups or '[]'
            names = _json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            return [('id', '=', -1)]
        
        if not names:
            return [('id', '=', -1)]
            
        # Distinguish between type names and service identifiers (UUIDs)
        import re
        uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
        
        types = []
        identifiers = []
        for name in names:
            name = (name or '').strip()
            if not name: continue
            if uuid_pattern.match(name):
                identifiers.append(name)
            else:
                types.append(name)
        
        domain = []
        if types:
            domain.append(('type_id.name', 'in', types))
        if identifiers:
            if domain:
                domain = ['|'] + domain + [('service_id', 'in', identifiers)]
            else:
                domain = [('service_id', 'in', identifiers)]
                
        return domain if domain else [('id', '=', -1)]

