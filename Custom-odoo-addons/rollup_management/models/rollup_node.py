"""Rollup node model."""

import json
import uuid

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class RollupNode(models.Model):
    """Infrastructure node linked to a rollup service."""

    _name = "rollup.node"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = "Rollup Node"
    _rec_name = "node_name"

    nodid = fields.Char(
        string="Node Identifier",
        default=lambda self: str(uuid.uuid4()),
        copy=False,
        required=True,
        index=True,
        tracking=True,
    )
    node_name = fields.Char(string="Node Name", required=True)
    service_id = fields.Many2one(
        "rollup.service",
        string="Rollup Service",
        required=True,
        ondelete="cascade",
        tracking=True,
    )
    node_type = fields.Selection(
        selection=[
            ("sequencer", "Sequencer"),
            ("prover", "Prover"),
            ("rpc", "RPC"),
            ("indexer", "Indexer"),
            ("zksync", "ZkSync"),
            ("zkevm", "ZkEVM"),
            ("dac", "DAC"),
            ("das", "DAS"),
            ("validator", "Validator"),
            ("other", "Other"),
        ],
        string="Node Type",
        required=True,
        default="other",
        tracking=True,
    )
    status = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("requested", "Requested"),
            ("provisioning", "Provisioning"),
            ("syncing", "Syncing"),
            ("ready", "Ready"),
            ("suspended", "Suspended"),
            ("deleted", "Deleted"),
        ],
        string="Node Status",
        required=True,
        default="draft",
        tracking=True,
    )
    endpoint_url = fields.Char(
        string="Endpoint URL",
        help="Service endpoint shared after deployment is completed.",
        tracking=True,
    )
    metadata_json = fields.Json(
        string="Node Metadata",
        default=dict,
        copy=False,
        help="System generated information regarding the node.",
        tracking=False,
        readonly=False,
    )
    metadata_json_tracking = fields.Text(
        string="Metadata (Tracked)",
        copy=False,
        tracking=True,
        help="Serialized snapshot of metadata_json stored for chatter tracking only.",
    )
    metadata_json_text = fields.Text(
        string="Metadata JSON (Editable)",
        compute="_compute_metadata_json_text",
        inverse="_inverse_metadata_json_text",
        store=False,
        copy=False,
        help="Human editable JSON that syncs with metadata_json.",
    )
    node_created_date = fields.Datetime(
        string="Node Created Date",
        store=True,
        tracking=True,
        readonly=False
    )

    _sql_constraints = [
        ("rollup_node_nodid_unique", "unique(nodid)", "Node identifier must be unique."),
    ]

    @api.model
    def _valid_statuses(self):
        return {value for value, _label in self._fields["status"].selection}

    @api.model
    def _legacy_status_mapping(self):
        return {
            "running": "ready",
            "stopped": "suspended",
            "degraded": "suspended",
            "retired": "deleted",
        }
    
    @api.model
    def _normalise_status_value(self, status_value):
        """Return a safe status choice handling legacy values."""

        valid_statuses = self._valid_statuses()
        if not status_value:
            return "draft"
        status_value = str(status_value)
        if status_value in valid_statuses:
            return status_value
        legacy_map = self._legacy_status_mapping()
        return legacy_map.get(status_value, "draft")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.setdefault("nodid", str(uuid.uuid4()))
            if "status" in vals:
                vals["status"] = self._normalise_status_value(vals.get("status"))
            else:
                vals["status"] = "draft"
            metadata_value = self._normalise_metadata_payload(vals.get("metadata_json"))
            vals["metadata_json"] = metadata_value
            if "metadata_json_tracking" not in vals:
                vals["metadata_json_tracking"] = self._serialize_metadata_for_tracking(metadata_value)
        records = super().create(vals_list)
        # Set node_created_date to create_date
        for record in records:
            if record.create_date and not record.node_created_date:
                record.write({"node_created_date": record.create_date})
        return records

    def write(self, vals):
        if "status" in vals:
            vals = dict(vals)
            vals["status"] = self._normalise_status_value(vals.get("status"))
        if "metadata_json" in vals:
            vals = dict(vals)
            metadata_value = self._normalise_metadata_payload(vals.get("metadata_json"))
            vals["metadata_json"] = metadata_value
            if "metadata_json_tracking" not in vals and not self.env.context.get("skip_metadata_tracking"):
                vals["metadata_json_tracking"] = self._serialize_metadata_for_tracking(metadata_value)
        elif "metadata_json_tracking" not in vals and not self.env.context.get("skip_metadata_tracking"):
            vals = dict(vals)
            metadata_value = self._normalise_metadata_payload(self.metadata_json)
            vals["metadata_json_tracking"] = self._serialize_metadata_for_tracking(metadata_value)
        return super().write(vals)

    def _compute_metadata_json_text(self):
        for node in self:
            metadata_value = node._normalise_metadata_payload(node.metadata_json)
            if not metadata_value:
                node.metadata_json_text = "{}"
                continue
            try:
                node.metadata_json_text = json.dumps(metadata_value, indent=2, sort_keys=True)
            except Exception:
                node.metadata_json_text = "{}"

    def _inverse_metadata_json_text(self):
        for node in self:
            raw_value = (node.metadata_json_text or "").strip()
            if not raw_value:
                node.metadata_json = {}
                continue
            try:
                parsed = json.loads(raw_value)
            except Exception as exc:
                raise ValidationError("Metadata JSON must be valid JSON text.") from exc
            if not isinstance(parsed, dict):
                raise ValidationError("Metadata JSON must be an object with key/value pairs.")
            node.metadata_json = parsed

    def _normalise_metadata_payload(self, value):
        """Ensure metadata stored in JSON column is a dictionary."""

        if not value:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    def _serialize_metadata_for_tracking(self, value):
        """Return a string representation suitable for the tracked field."""

        if not value:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, sort_keys=True)
        except Exception:
            return str(value)

    def get_metadata_dict(self):
        """Expose metadata_json as a dictionary for serializers."""

        return self._normalise_metadata_payload(self.metadata_json)

    @api.constrains("nodid")
    def _check_nodid_uuid(self):
        """Ensure node identifiers are stored as canonical UUID4 strings."""

        for node in self:
            try:
                uuid_obj = uuid.UUID(str(node.nodid), version=4)
            except (ValueError, AttributeError, TypeError):
                raise ValidationError("Node ID must be a valid UUID4 value.")
            if str(uuid_obj) != node.nodid:
                raise ValidationError("Node ID must be a canonical UUID4 value.")

    def init(self):
        """Normalise legacy node statuses during module install/updates."""

        super().init()
        legacy_map = self._legacy_status_mapping()
        if not legacy_map:
            return
        cr = self.env.cr
        for legacy_value, target_value in legacy_map.items():
            cr.execute(
                "UPDATE rollup_node SET status=%s WHERE status=%s",
                (target_value, legacy_value),
            )
        cr.execute(
            """
            UPDATE rollup_node
               SET node_created_date = create_date
             WHERE node_created_date IS NULL
               AND create_date IS NOT NULL
            """
        )
