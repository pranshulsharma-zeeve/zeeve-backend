# -*- coding: utf-8 -*-
import base64
import csv
import io
import logging
import time

from psycopg2 import errors

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..utils.subscription_utils import SubscriptionUtils
from ..utils.invoice_utils import InvoiceImportUtils

_logger = logging.getLogger(__name__)

_IMPORT_QUIET_CONTEXT = {
    'no_reset_password': True,
    'mail_create_nolog': True,
    'mail_create_nosubscribe': True,
    'mail_notrack': True,
    'tracking_disable': True,
    'mail_auto_subscribe_no_notify': True,
    'mail_activity_automation_skip': True,
    'mail_post_autofollow': False,
    'mail_create_no_notify': True,
    'mail_channel_no_subscribe': True,
    'mail_channel_no_emails': True,
}

class DataImporter(models.Model):
    """Generic CSV importer with special handling for res.users."""

    _name = 'data.importer'
    _description = 'Data Importer'

    name = fields.Char(required=True, string='Import Name')
    model_id = fields.Many2one('ir.model', required=True, string='Model', ondelete='cascade')
    file = fields.Binary(string='CSV File', attachment=False)
    filename = fields.Char(string='Filename')
    state = fields.Selection(
        [('draft', 'Draft'), ('done', 'Done')],
        default='draft',
    )
    log = fields.Text(readonly=True)
    chunk_size = fields.Integer(
        string='Chunk Size',
        default=0,
        help='Optional limit to process only this many rows per run when importing generic models.',
    )
    chunk_position = fields.Integer(
        string='Rows Processed',
        default=0,
        readonly=True,
        help='Internal pointer tracking how many rows of the current file have been imported.',
    )

    @api.onchange('model_id')
    def _onchange_model_id(self):
        if self.state != 'draft':
            self.state = 'draft'
        self.log = False
        self.chunk_position = 0

    @api.onchange('file')
    def _onchange_file(self):
        """Reset chunk progress when uploading a new file."""
        self.chunk_position = 0

    def action_download_template(self):
        self.ensure_one()
        if not self.model_id:
            raise UserError(_('Select a model before downloading the template.'))

        headers = self._get_template_headers()
        if not headers:
            raise UserError(
                _('No importable fields found for the model %s.') % self.model_id.name
            )

        content = io.StringIO()
        writer = csv.writer(content)
        writer.writerow(headers)
        csv_data = content.getvalue()
        content.close()

        filename = '%s_template.csv' % (self.model_id.model.replace('.', '_'),)
        encoded = base64.b64encode(csv_data.encode('utf-8'))
        return {
            'type': 'ir.actions.act_url',
            'url': '/data_importer/download_template/%s' % self.id,
            'target': 'self',
        }

    def action_import_file(self):
        self.ensure_one()
        if not self.file:
            raise UserError(_('Please upload a CSV file to import.'))

        if self.model_id.model == 'subscription.subscription':
            csv_text = self._decode_csv_text()

            reader = csv.DictReader(io.StringIO(csv_text, newline=''))
            if not reader.fieldnames:
                raise UserError(_('The CSV file does not contain any headers.'))

            log_lines = []
            success_count = 0
            partial_count = 0
            skipped_count = 0
            error_count = 0

            row_iter = enumerate(reader, start=2)
            for row_number, row in row_iter:
                try:
                    outcome = SubscriptionUtils.handle_subscription_row(self.env, row)
                except Exception as row_error:  # pylint: disable=broad-except
                    error_count += 1
                    message = _('Row %(row)s failed: %(error)s') % {
                        'row': row_number,
                        'error': row_error,
                    }
                    log_lines.append(message)
                    _logger.exception('Row %s failed during subscription import', row_number)
                    continue

                status = outcome.get('status')
                detail = outcome.get('message', '')

                if status == 'success':
                    success_count += 1
                    log_lines.append(
                        _('Row %(row)s imported: %(detail)s')
                        % {'row': row_number, 'detail': detail}
                    )
                elif status == 'partial':
                    partial_count += 1
                    log_lines.append(
                        _('Row %(row)s imported with warnings: %(detail)s')
                        % {'row': row_number, 'detail': detail}
                    )
                elif status == 'skipped':
                    skipped_count += 1
                    log_lines.append(
                        _('Row %(row)s skipped: %(detail)s')
                        % {'row': row_number, 'detail': detail}
                    )
                else:
                    error_count += 1
                    log_lines.append(
                        _('Row %(row)s failed: %(detail)s')
                        % {'row': row_number, 'detail': detail}
                    )

            summary = _(
                'Subscriptions import summary - %(success)d succeeded, %(partial)d partially imported, %(skipped)d skipped, %(errors)d failed.'
            ) % {
                'success': success_count,
                'partial': partial_count,
                'skipped': skipped_count,
                'errors': error_count,
            }
            log_lines.insert(0, summary)

            if success_count == 0 and partial_count == 0:
                self.write(
                    {
                        'state': 'draft',
                        'log': '\n'.join(log_lines),
                    }
                )
                raise UserError(_('No subscriptions were imported successfully.'))

            notification_type = 'success'
            if error_count or skipped_count or partial_count:
                notification_type = 'warning'

            self.write(
                {
                    'state': 'done',
                    'log': '\n'.join(log_lines),
                }
            )

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Subscription Import'),
                    'message': summary,
                    'sticky': False,
                    'type': notification_type,
                },
            }

        if self.model_id.model == 'account.move':
            return self._import_zoho_invoices()
        
        if self.model_id.model == 'rollup.service':
            return self._import_zoho_rollups()

        csv_text = self._decode_csv_text()

        reader = csv.DictReader(io.StringIO(csv_text, newline=''))
        if not reader.fieldnames:
            raise UserError(_('The CSV file does not contain any headers.'))

        Model = self.env[self.model_id.model].sudo()
        log_lines = []
        success_count = 0
        error_count = 0

        chunk_limit = max(int(self.chunk_size or 0), 0)
        offset = max(int(self.chunk_position or 0), 0)
        processed_in_run = 0
        has_more_rows = False

        row_iter = enumerate(reader, start=2)
        try:
            with self.env.cr.savepoint():
                for row_number, row in row_iter:
                    absolute_index = row_number - 2
                    if offset and absolute_index < offset:
                        continue
                    if chunk_limit and processed_in_run >= chunk_limit:
                        has_more_rows = True
                        break

                    cleaned_row = self._sanitize_row(row)
                    if not cleaned_row:
                        continue
                    try:
                        with self.env.cr.savepoint():
                            record, action = self._import_single_row(Model, cleaned_row)
                        success_count += 1
                        processed_in_run += 1
                        log_lines.append(
                            _('Row %(row)s: %(action)s record %(record)s')
                            % {
                                'row': row_number,
                                'action': action,
                                'record': record.display_name,
                            }
                        )
                    except Exception as row_error:  # pylint: disable=broad-except
                        error_count += 1
                        processed_in_run += 1
                        message = _('Row %(row)s failed: %(error)s') % {
                            'row': row_number,
                            'error': row_error,
                        }
                        log_lines.append(message)
                        _logger.exception('Row %s failed during data import', row_number)
                if success_count == 0 and error_count == 0:
                    raise UserError(_('No records were imported successfully.'))
        except UserError:
            self.write(
                {
                    'state': 'draft',
                    'log': '\n'.join(log_lines),
                }
            )
            raise
        except Exception as error:  # pylint: disable=broad-except
            log_lines.append(_('Import aborted: %s') % error)
            self.write(
                {
                    'state': 'draft',
                    'log': '\n'.join(log_lines),
                }
            )
            raise UserError(_('Unexpected error during import: %s') % error) from error

        summary = _(
            'Import completed with %(success)d successes and %(errors)d errors.'
        ) % {'success': success_count, 'errors': error_count}
        if chunk_limit and has_more_rows:
            next_offset = offset + processed_in_run
            chunk_msg = _(
                'Processed %(count)d row(s) this run (rows %(start)d-%(end)d). '
                'Re-run the import to continue from row %(next)d.'
            ) % {
                'count': processed_in_run,
                'start': offset + 2,
                'end': offset + processed_in_run + 1,
                'next': next_offset + 2,
            }
            summary = chunk_msg + ' ' + summary
            self._write_with_retry(
                {
                    'state': 'draft',
                    'chunk_position': next_offset,
                    'log': '\n'.join([summary] + log_lines),
                }
            )
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Data Importer'),
                    'message': summary,
                    'sticky': False,
                    'type': 'warning',
                },
            }

        log_lines.insert(0, summary)
        self._write_with_retry({'state': 'done', 'log': '\n'.join(log_lines), 'chunk_position': 0})

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Data Importer'),
                'message': summary,
                'sticky': False,
                'type': 'success' if error_count == 0 else 'warning',
            },
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _import_zoho_invoices(self):
        csv_text = self._decode_csv_text()

        reader = csv.DictReader(io.StringIO(csv_text, newline=''))
        if not reader.fieldnames:
            raise UserError(_('The CSV file does not contain any headers.'))

        log_lines = []
        success_count = 0
        partial_count = 0
        skipped_count = 0
        error_count = 0

        for row_number, row in enumerate(reader, start=2):
            invoice_label = row.get('Invoice Number') or row.get('Invoice ID') or f"row {row_number}"
            try:
                outcome = InvoiceImportUtils.handle_invoice_row(self.env, row)
            except Exception as row_error:  # pylint: disable=broad-except
                error_count += 1
                log_lines.append(
                    _('Row %(row)s (%(invoice)s) failed: %(error)s')
                    % {'row': row_number, 'invoice': invoice_label, 'error': row_error}
                )
                _logger.exception('Invoice row %s failed during import', row_number)
                continue

            status = outcome.get('status')
            detail = outcome.get('message', '')
            if status == 'success':
                success_count += 1
                log_lines.append(
                    _('Row %(row)s (%(invoice)s) imported: %(detail)s')
                    % {'row': row_number, 'invoice': invoice_label, 'detail': detail}
                )
            elif status == 'partial':
                partial_count += 1
                log_lines.append(
                    _('Row %(row)s (%(invoice)s) imported with warnings: %(detail)s')
                    % {'row': row_number, 'invoice': invoice_label, 'detail': detail}
                )
            elif status == 'skipped':
                skipped_count += 1
                log_lines.append(
                    _('Row %(row)s (%(invoice)s) skipped: %(detail)s')
                    % {'row': row_number, 'invoice': invoice_label, 'detail': detail}
                )
            else:
                error_count += 1
                log_lines.append(
                    _('Row %(row)s (%(invoice)s) failed: %(detail)s')
                    % {'row': row_number, 'invoice': invoice_label, 'detail': detail}
                )

        summary = _(
            'Invoice import summary - %(success)d succeeded, %(partial)d left in draft, %(skipped)d skipped, %(errors)d failed.'
        ) % {
            'success': success_count,
            'partial': partial_count,
            'skipped': skipped_count,
            'errors': error_count,
        }
        log_lines.insert(0, summary)

        if success_count == 0 and partial_count == 0:
            self.write({'state': 'draft', 'log': '\n'.join(log_lines)})
            raise UserError(_('No invoices were imported successfully.'))

        notification_type = 'success'
        if error_count or partial_count or skipped_count:
            notification_type = 'warning'

        self.write({'state': 'done', 'log': '\n'.join(log_lines)})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Invoice Import'),
                'message': summary,
                'sticky': False,
                'type': notification_type,
            },
        }

    def _import_zoho_rollups(self):
        from ..utils.rollup_utils import RollupImportUtils
        csv_text = self._decode_csv_text()

        reader = csv.DictReader(io.StringIO(csv_text, newline=''))
        if not reader.fieldnames:
            raise UserError(_('The CSV file does not contain any headers.'))

        log_lines = []
        success_count = 0
        error_count = 0
        skipped_count = 0

        for row_number, row in enumerate(reader, start=2):
            try:
                if not any(row.values()):
                    skipped_count += 1
                    log_lines.append(
                        _('Row %(row)s skipped: Empty row') % {'row': row_number}
                    )
                    continue
                record = RollupImportUtils.handle_rollup_row(self.env, row)
                success_count += 1
                log_lines.append(
                    _('Row %(row)s imported: %(record)s') % {'row': row_number, 'record': record.display_name}
                )
            except Exception as row_error:  # pylint: disable=broad-except
                error_count += 1
                message = _('Row %(row)s failed: %(error)s') % {
                    'row': row_number,
                    'error': row_error,
                }
                log_lines.append(message)
                _logger.exception('Row %s failed during rollup import', row_number)
                continue

        summary = _(
            'Rollup import summary - %(success)d succeeded, %(skipped)d skipped, %(errors)d failed.'
        ) % {
            'success': success_count,
            'skipped': skipped_count,
            'errors': error_count,
        }
        log_lines.insert(0, summary)

        if success_count == 0:
            self.write({'state': 'draft', 'log': '\n'.join(log_lines)})
            raise UserError(_('No rollup records were imported successfully.'))

        notification_type = 'success' if error_count == 0 else 'warning'

        self.write({'state': 'done', 'log': '\n'.join(log_lines)})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Rollup Import'),
                'message': summary,
                'sticky': False,
                'type': notification_type,
            },
        }

    def _decode_csv_text(self):
        """Return decoded CSV text while tolerating common encodings."""
        if not self.file:
            raise UserError(_('Please upload a CSV file to import.'))
        decoded = base64.b64decode(self.file)
        encodings = ['utf-8-sig', 'utf-8', 'cp1252', 'latin-1']
        errors = []
        for encoding in encodings:
            try:
                return decoded.decode(encoding)
            except UnicodeDecodeError as exc:
                errors.append(f"{encoding}: {exc}")
        raise UserError(
            _('Failed to decode the CSV file using encodings %s. Last error: %s')
            % (', '.join(encodings), errors[-1] if errors else _('unknown error'))
        )

    def _write_with_retry(self, vals, retries=3, delay=1.0):
        """Write helper that retries on serialization failures to avoid chunk import crashes."""
        attempt = 0
        while True:
            try:
                return super(DataImporter, self).write(vals)
            except errors.SerializationFailure:
                attempt += 1
                if attempt > retries:
                    raise
                self.env.cr.rollback()
                time.sleep(delay)
    
    def _sanitize_row(self, row):
        """Return a cleaned dictionary or False when empty."""
        cleaned = {}
        for key, value in row.items():
            if value is None:
                continue
            text = value.strip() if isinstance(value, str) else value
            if text in ('', None):
                continue
            cleaned[key] = text
        return cleaned

    def _import_single_row(self, Model, row):
        if self.model_id.model == 'res.users':
            return self._import_res_users_row(row)
        return self._import_generic_row(Model, row)

    def _import_generic_row(self, Model, row):
        record = self._find_existing_record(Model, row)
        vals = self._prepare_generic_vals(Model, row)
        if not vals:
            raise UserError(_('No valid fields were provided for row: %s') % row)

        if record:
            record.write(vals)
            return record, _('Updated')
        record = Model.create(vals)
        return record, _('Created')

    def _import_res_users_row(self, row):
        import_context = dict(self.env.context)
        import_context.update(_IMPORT_QUIET_CONTEXT)
        Users = self.env['res.users'].sudo().with_context(**import_context)
        Partners = self.env['res.partner'].sudo().with_context(**import_context)

        login = row.get('login')
        email = row.get('email')
        name = row.get('name')
        first_name = row.get('first_name')
        last_name = row.get('last_name')
        mobile = row.get('mobile')
        address = row.get('address')
        account_id = row.get('account_id')
        oauth_provider = row.get('oauth_provider')
        node_bcrypt_hash = row.get('legacy_password_bcrypt') 
        utm_info = row.get('meta') 
        print(login,email)

        if not login and not email:
            raise UserError(_('Login or Email is required to identify a user.'))

        user = False
        if login:
            user = Users.search([('login', '=', login)], limit=1)
        if not user and email:
            user = Users.search([('email', '=', email)], limit=1)

        partner_vals = {}
        if first_name:
            partner_vals['first_name'] = first_name
        if last_name:
            partner_vals['last_name'] = last_name
        if email:
            partner_vals['email'] = email
        if mobile:
            partner_vals['mobile'] = mobile
        if address:
            partner_vals['street'] = address
        computed_name = name or ' '.join(part for part in [first_name, last_name] if part)
        if computed_name:
            partner_vals['name'] = computed_name
        if account_id:
            partner_vals['account_id'] = account_id
        if oauth_provider:
            partner_vals['oauth_provider'] = oauth_provider.lower()
        if utm_info:
            partner_vals['utm_info'] = utm_info

        if user:
            update_vals = {}
            if computed_name:
                update_vals['name'] = computed_name
            if login:
                update_vals['login'] = login
            if email:
                update_vals['email'] = email
            if node_bcrypt_hash:
                update_vals['legacy_password_bcrypt'] = node_bcrypt_hash

            if partner_vals and user.partner_id:
                user.partner_id.write(partner_vals)
            elif partner_vals:
                partner_vals.setdefault(
                    'name',
                    computed_name or login or email or _('Unnamed Partner'),
                )
                partner = Partners.create(partner_vals)
                update_vals['partner_id'] = partner.id

            if update_vals:
                user.write(update_vals)
            return user, _('Updated')

        partner_vals.setdefault(
            'name',
            computed_name or login or email or _('Unnamed Partner'),
        )
        partner_vals.setdefault('email_verified', True)
        partner = Partners.create(partner_vals)

        user_vals = {
            'name': computed_name or partner.name,
            'login': login or email,
            'partner_id': partner.id,
            'legacy_password_bcrypt': node_bcrypt_hash if node_bcrypt_hash else False,
        }
        if email:
            user_vals['email'] = email
        portal_group = self.env.ref('base.group_portal')

        user = Users.create(user_vals)
        user.sudo().write({
                'groups_id': [(6, 0, [portal_group.id])]
            })

        if user.partner_id != partner:
            user.write({'partner_id': partner.id})

        return user, _('Created')

    def _prepare_generic_vals(self, Model, row):
        vals = {}
        for column, value in row.items():
            if column not in Model._fields:
                continue
            field = Model._fields[column]
            if field.type in ('one2many', 'many2many'):
                continue
            prepared = self._prepare_field_value(field, value)
            if prepared is not None:
                vals[column] = prepared
        return vals

    def _prepare_field_value(self, field, value):  # pylint: disable=too-many-branches
        if value in (None, '') and field.type != 'boolean':
            return None

        if field.type in ('char', 'text', 'html'):  # simple text
            return value
        if field.type in ('integer', 'many2one_reference'):
            return int(value)
        if field.type in ('float', 'monetary'):  # convert to float
            return float(value)
        if field.type == 'boolean':
            if isinstance(value, bool):
                return value
            return value.strip().lower() in ('1', 'true', 'yes', 'y')
        if field.type == 'date':
            return fields.Date.to_date(value)
        if field.type == 'datetime':
            return fields.Datetime.to_datetime(value)
        if field.type == 'selection':
            selection = dict(field.selection)
            if value in selection:
                return value
            for key, label in field.selection:
                if label and value.lower() == label.lower():
                    return key
            raise UserError(
                _('Value %(value)s is not valid for field %(field)s')
                % {'value': value, 'field': field.name}
            )
        if field.type == 'many2one':
            return self._resolve_many2one(field, value)
        return value

    def _resolve_many2one(self, field, value):
        comodel = self.env[field.comodel_name].sudo()
        if isinstance(value, str) and value.isdigit():
            record = comodel.browse(int(value))
            if record.exists():
                return record.id

        rec_name = comodel._rec_name or 'name'
        if rec_name in comodel._fields:
            record = comodel.search([(rec_name, '=', value)], limit=1)
            if record:
                return record.id

        if 'name' in comodel._fields and rec_name != 'name':
            record = comodel.search([('name', '=', value)], limit=1)
            if record:
                return record.id

        if rec_name not in comodel._fields:
            raise UserError(
                _('Unable to create related record for field %(field)s.')
                % {'field': field.name}
            )

        create_vals = {rec_name: value}
        record = comodel.create(create_vals)
        return record.id

    def _find_existing_record(self, Model, row):
        lookup_fields = [
            'id',
            'xml_id',
            'external_id',
            'email',
            'login',
            'name',
            'code',
            Model._rec_name,
        ]
        for field_name in lookup_fields:
            if not field_name:
                continue
            if field_name not in row:
                continue
            value = row[field_name]
            if not value:
                continue
            if field_name == 'id' and str(value).isdigit():
                record = Model.browse(int(value))
                if record.exists():
                    return record
            elif field_name in ('xml_id', 'external_id'):
                record = self.env.ref(value, raise_if_not_found=False)
                if record and record._name == Model._name:
                    return record
            elif field_name in Model._fields:
                record = Model.search([(field_name, '=', value)], limit=1)
                if record:
                    return record
        return Model.browse()

    def _get_template_headers(self):
        if self.model_id.model == 'res.users':
            return ['first_name','last_name', 'login', 'legacy_password_bcrypt', 'email', 'mobile', 'address','account_id','oauth_provider','meta']

        Model = self.env[self.model_id.model]
        headers = []
        for field in self.model_id.field_id:
            if not field.store:
                continue
            if field.ttype in ('binary', 'one2many', 'many2many'):  # skip unsupported
                continue
            if field.name in ('create_date', 'write_date', '__last_update'):
                continue
            if field.readonly:
                continue
            if field.name not in Model._fields:
                continue
            headers.append(field.name)
        return headers
