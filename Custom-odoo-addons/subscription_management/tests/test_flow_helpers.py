import importlib.util
import datetime as dt
from decimal import Decimal
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parents[2]
MODULE_ROOT = BASE_DIR / "subscription_management"

if "odoo" not in sys.modules:
    odoo_module = types.ModuleType("odoo")
    http_module = types.ModuleType("odoo.http")
    http_module.request = types.SimpleNamespace(env=None)
    http_module.root = object()
    http_module.Controller = type("DummyController", (), {})
    http_module.route = lambda *a, **k: (lambda func: func)
    fields_module = types.ModuleType("odoo.fields")
    class _DummyDatetimeField:
        @staticmethod
        def now():
            return dt.datetime.utcnow()

        @staticmethod
        def to_string(value):
            return str(value)

        def __call__(self, *args, **kwargs):
            return _field(*args, **kwargs)

    def _field(*args, **kwargs):
        return types.SimpleNamespace(args=args, kwargs=kwargs)

    for _name in [
        "Char",
        "Many2one",
        "Integer",
        "Float",
        "Boolean",
        "Selection",
        "Date",
        "Text",
        "Binary",
    ]:
        setattr(fields_module, _name, _field)

    def _fields_getattr(name):
        return _field

    fields_module.__getattr__ = _fields_getattr
    fields_module.Datetime = _DummyDatetimeField()
    exceptions_module = types.ModuleType("odoo.exceptions")
    class _DummyUserError(Exception):
        pass
    exceptions_module.UserError = _DummyUserError
    class _DummyAccessDenied(Exception):
        pass
    exceptions_module.AccessDenied = _DummyAccessDenied
    class _DummyValidationError(Exception):
        pass
    exceptions_module.ValidationError = _DummyValidationError
    class _DummyAccessError(Exception):
        pass
    exceptions_module.AccessError = _DummyAccessError
    sys.modules["odoo.exceptions"] = exceptions_module
    odoo_module._ = lambda message, *args, **kwargs: message
    dummy_model = type("DummyModel", (), {})

    def _identity_decorator(*dargs, **dkwargs):
        def _wrap(func):
            return func

        return _wrap

    odoo_module.api = types.SimpleNamespace(
        model=lambda *a, **k: dummy_model,
        depends=_identity_decorator,
        model_create_multi=_identity_decorator,
        constrains=_identity_decorator,
        onchange=_identity_decorator,
        returns=_identity_decorator,
    )
    odoo_module.models = types.SimpleNamespace(
        Model=dummy_model,
        AbstractModel=dummy_model,
        TransientModel=dummy_model,
    )
    tools_module = types.ModuleType("odoo.tools")
    misc_module = types.ModuleType("odoo.tools.misc")
    mimetypes_module = types.ModuleType("odoo.tools.mimetypes")
    safe_eval_module = types.ModuleType("odoo.tools.safe_eval")
    float_utils_module = types.ModuleType("odoo.tools.float_utils")
    def _format_date(env, date, lang=None, tz=None):
        return str(date)
    misc_module.format_date = _format_date
    def _format_datetime(env, date, tz=None):
        return str(date)
    misc_module.format_datetime = _format_datetime
    def _float_is_zero(value, precision_digits=6, precision_rounding=None):
        threshold = 10 ** (-(precision_digits or 6))
        return abs(value or 0) < threshold
    def _format_amount(env, amount, currency, lang=None, from_currency=None):
        symbol = getattr(currency, "symbol", getattr(currency, "name", ""))
        return f"{amount} {symbol}".strip()
    def _format_lang(env, value, digits=None):
        return str(value)
    def _html2plaintext(payload):
        return payload
    class _DummyConfig(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    config_module = _DummyConfig()
    mimetypes_module.guess_mimetype = lambda *a, **k: ("application/octet-stream", None)
    def _safe_eval(expr, globals_dict=None, locals_dict=None):
        return {}
    safe_eval_module.safe_eval = _safe_eval
    tools_module.misc = misc_module
    tools_module.mimetypes = mimetypes_module
    tools_module.config = config_module
    tools_module.formatLang = _format_lang
    tools_module.float_is_zero = _float_is_zero
    tools_module.format_amount = _format_amount
    tools_module.html2plaintext = _html2plaintext
    float_utils_module.float_is_zero = _float_is_zero
    sys.modules["odoo.tools"] = tools_module
    sys.modules["odoo.tools.misc"] = misc_module
    sys.modules["odoo.tools.mimetypes"] = mimetypes_module
    sys.modules["odoo.tools.safe_eval"] = safe_eval_module
    sys.modules["odoo.tools.float_utils"] = float_utils_module
    odoo_module.tools = tools_module
    odoo_module.exceptions = exceptions_module
    odoo_module.http = http_module
    odoo_module.fields = fields_module
    odoo_module.release = types.SimpleNamespace(version="16.0")
    odoo_module.__path__ = [str(BASE_DIR.parent)]
    sys.modules["odoo"] = odoo_module
    sys.modules["odoo.http"] = http_module
    sys.modules["odoo.fields"] = fields_module

rollup_root_name = "odoo.rollup_management"
if rollup_root_name not in sys.modules:
    rollup_root = types.ModuleType(rollup_root_name)
    rollup_root.__path__ = [str(BASE_DIR / "rollup_management")]
    sys.modules[rollup_root_name] = rollup_root

rollup_utils_name = f"{rollup_root_name}.utils"
if rollup_utils_name not in sys.modules:
    rollup_utils_module = types.ModuleType(rollup_utils_name)
    rollup_utils_module.__path__ = [str((BASE_DIR / "rollup_management" / "utils"))]
    sys.modules[rollup_utils_name] = rollup_utils_module

deployment_full_name = f"{rollup_utils_name}.deployment_utils"
if deployment_full_name not in sys.modules:
    deployment_stub = types.ModuleType(deployment_full_name)
    deployment_stub._as_date = lambda value: value
    sys.modules[deployment_full_name] = deployment_stub

top_rollup = "rollup_management"
if top_rollup not in sys.modules:
    top_rollup_module = types.ModuleType(top_rollup)
    top_rollup_module.__path__ = [str(BASE_DIR / "rollup_management")]
    sys.modules[top_rollup] = top_rollup_module

top_rollup_utils = f"{top_rollup}.utils"
if top_rollup_utils not in sys.modules:
    top_rollup_utils_module = types.ModuleType(top_rollup_utils)
    top_rollup_utils_module.__path__ = [str(BASE_DIR / "rollup_management" / "utils")]
    sys.modules[top_rollup_utils] = top_rollup_utils_module

top_rollup_deployment = f"{top_rollup_utils}.deployment_utils"
if top_rollup_deployment not in sys.modules:
    top_rollup_deployment_module = types.ModuleType(top_rollup_deployment)
    top_rollup_deployment_module._as_date = lambda value: value
    sys.modules[top_rollup_deployment] = top_rollup_deployment_module

if "odoo.addons" not in sys.modules:
    addons_module = types.ModuleType("odoo.addons")
    addons_module.__path__ = [str(BASE_DIR)]
    sys.modules["odoo.addons"] = addons_module

web_addon_root = "odoo.addons.web"
if web_addon_root not in sys.modules:
    web_module = types.ModuleType(web_addon_root)
    web_module.__path__ = []
    sys.modules[web_addon_root] = web_module

web_controllers = f"{web_addon_root}.controllers"
if web_controllers not in sys.modules:
    web_controllers_module = types.ModuleType(web_controllers)
    web_controllers_module.__path__ = []
    sys.modules[web_controllers] = web_controllers_module

web_database = f"{web_controllers}.database"
if web_database not in sys.modules:
    web_database_module = types.ModuleType(web_database)
    web_database_module.Database = type("DummyDatabase", (), {})
    sys.modules[web_database] = web_database_module

web_home = f"{web_controllers}.home"
if web_home not in sys.modules:
    web_home_module = types.ModuleType(web_home)
    web_home_module.SIGN_UP_REQUEST_PARAMS = ()
    web_home_module.Home = type("WebHome", (http_module.Controller,), {})
    sys.modules[web_home] = web_home_module

auth_signup_root = "odoo.addons.auth_signup"
if auth_signup_root not in sys.modules:
    auth_signup_module = types.ModuleType(auth_signup_root)
    auth_signup_module.__path__ = []
    sys.modules[auth_signup_root] = auth_signup_module

auth_signup_controllers = f"{auth_signup_root}.controllers"
if auth_signup_controllers not in sys.modules:
    auth_signup_controllers_module = types.ModuleType(auth_signup_controllers)
    auth_signup_controllers_module.__path__ = []
    sys.modules[auth_signup_controllers] = auth_signup_controllers_module

auth_signup_main = f"{auth_signup_controllers}.main"
if auth_signup_main not in sys.modules:
    auth_signup_main_module = types.ModuleType(auth_signup_main)
    auth_signup_main_module.AuthSignupHome = type("AuthSignupHome", (http_module.Controller,), {})
    sys.modules[auth_signup_main] = auth_signup_main_module

addon_pkg_name = "odoo.addons.subscription_management"
if addon_pkg_name not in sys.modules:
    addon_module = types.ModuleType(addon_pkg_name)
    addon_module.__path__ = [str(MODULE_ROOT)]
    sys.modules[addon_pkg_name] = addon_module

utils_pkg_name = f"{addon_pkg_name}.utils"
if utils_pkg_name not in sys.modules:
    utils_module = types.ModuleType(utils_pkg_name)
    utils_module.__path__ = [str(MODULE_ROOT / "utils")]
    sys.modules[utils_pkg_name] = utils_module

rollup_pkg_name = f"{addon_pkg_name}.rollup_management"
if rollup_pkg_name not in sys.modules:
    rollup_module = types.ModuleType(rollup_pkg_name)
    rollup_module.__path__ = [str(MODULE_ROOT / "rollup_management")]
    sys.modules[rollup_pkg_name] = rollup_module

rollup_utils_pkg_name = f"{rollup_pkg_name}.utils"
if rollup_utils_pkg_name not in sys.modules:
    rollup_utils = types.ModuleType(rollup_utils_pkg_name)
    rollup_utils.__path__ = [str(MODULE_ROOT / "rollup_management" / "utils")]
    sys.modules[rollup_utils_pkg_name] = rollup_utils

deployment_pkg_name = f"{rollup_utils_pkg_name}.deployment_utils"
if deployment_pkg_name not in sys.modules:
    deployment_module = types.ModuleType(deployment_pkg_name)
    deployment_module._as_date = lambda value: value
    sys.modules[deployment_pkg_name] = deployment_module

helpers_path = MODULE_ROOT / "utils" / "subscription_helpers.py"
spec = importlib.util.spec_from_file_location(
    "odoo.addons.subscription_management.utils.subscription_helpers",
    helpers_path,
)
helpers = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
spec.loader.exec_module(helpers)


class TestFlowHelpers(unittest.TestCase):
    """Validate Flow helper fallbacks and error translation."""

    def test_missing_node_translates_to_not_found(self):
        node_id = "f" * 64
        error_message = (
            "Flow scripts request failed (400): Invalid Flow argument: FlowIDTableStaking.NodeInfo"
        )
        flow_error = helpers.LCDRequestError(error_message, status=400)

        with patch(
            "odoo.addons.subscription_management.utils.subscription_helpers._flow_exec_script",
            side_effect=["0.05", flow_error],
        ):
            with self.assertRaises(helpers.LCDRequestError) as captured:
                helpers._flow_fetch_validator_details(
                    "https://flowscan.io",
                    node_id,
                    "mainnet",
                )

        self.assertEqual(captured.exception.status, 404)
        self.assertEqual(
            str(captured.exception),
            "Flow validator node was not found on this network",
        )

    def test_delegators_loaded_without_owner_address(self):
        node_id = "a" * 64

        node_info_payload = {
            "value": {
                "fields": [
                    {
                        "name": "tokensRewarded",
                        "value": "3.0",
                    }
                ]
            }
        }

        delegator_payload = [
            {
                "value": {
                    "fields": [
                        {"name": "nodeID", "value": node_id},
                        {"name": "id", "value": {"value": "7"}},
                        {"name": "tokensCommitted", "value": "80.0"},
                        {"name": "tokensStaked", "value": "75.0"},
                        {"name": "tokensRewarded", "value": "1.5"},
                        {"name": "tokensUnstaking", "value": "0.0"},
                        {"name": "tokensUnstaked", "value": "0.0"},
                        {"name": "tokensRequestedToUnstake", "value": "0.0"},
                    ]
                }
            }
        ]

        with patch(
            "odoo.addons.subscription_management.utils.subscription_helpers._flow_exec_script",
            side_effect=[
                "0.05",  # reward cut
                "100.0",  # total with delegators
                "40.0",  # total without delegators
                node_info_payload,
                delegator_payload,
            ],
        ):
            details = helpers._flow_fetch_validator_details(
                "https://rest-mainnet.onflow.org",
                node_id,
                "mainnet",
            )

        self.assertEqual(details["delegation_count"], 1)
        self.assertEqual(len(details["delegators"]), 1)
        self.assertEqual(details["delegators"][0]["delegator_id"], 7)
        self.assertEqual(details["delegators"][0]["tokens_staked"], Decimal("75.0"))

    def test_collect_delegators_accepts_nested_values(self):
        node_id = "92dab49c5d89fa2f0619f99131d6406a94c5f214a198aafab41241322f9bf173"
        payload = {
            "value": [
                {
                    "value": {
                        "fields": [
                            {"name": "id", "value": {"value": "4", "type": "UInt32"}},
                            {"name": "nodeID", "value": {"value": node_id, "type": "String"}},
                            {"name": "tokensCommitted", "value": {"value": "0.00000000", "type": "UFix64"}},
                            {"name": "tokensStaked", "value": {"value": "4999999.00000000", "type": "UFix64"}},
                            {"name": "tokensUnstaking", "value": {"value": "0.00000000", "type": "UFix64"}},
                            {"name": "tokensRewarded", "value": {"value": "414552.33108969", "type": "UFix64"}},
                            {"name": "tokensUnstaked", "value": {"value": "0.00000000", "type": "UFix64"}},
                            {"name": "tokensRequestedToUnstake", "value": {"value": "0.00000000", "type": "UFix64"}},
                        ],
                        "id": "A.8624b52f9ddcd04a.FlowIDTableStaking.DelegatorInfo",
                    },
                    "type": "Struct",
                }
            ],
            "type": "Array",
        }

        results = helpers._flow_collect_delegators(payload, node_id)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["delegator_id"], 4)
        self.assertEqual(results[0]["tokens_staked"], Decimal("4999999.00000000"))
