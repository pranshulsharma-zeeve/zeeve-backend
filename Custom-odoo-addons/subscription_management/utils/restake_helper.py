"""Helper utilities to enable Restake workflows within Odoo."""
from __future__ import annotations

import base64
import json
import logging
import random
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

import requests
from github import Github, GithubException
from github.InputGitAuthor import InputGitAuthor
import time
from odoo import _, fields
from odoo.exceptions import UserError
import time
import random

from .mnemonic_service import encrypt_mnemonic, generate_mnemonic_and_address
from ..utils.subscription_helpers import _lcd_get_json

_logger = logging.getLogger(__name__)

GITHUB_ACCESS_TOKEN_KEY = "restake.github_access_token"
GITHUB_USERNAME_KEY = "restake.github_username"
GITHUB_REPO_NAME_KEY = "restake.github_repo_name"
GITHUB_MAIN_OWNER_KEY = "restake.github_main_owner"
GITHUB_BASE_BRANCH_KEY = "restake.github_base_branch"

ZABBIX_URL_KEY = "restake.zabbix_url"
ZABBIX_BEARER_TOKEN_KEY = "restake.zabbix_bearer_token"


def _get_config_param(env, key: str, required: bool = True, default: Optional[str] = None) -> str:
    value = env["ir.config_parameter"].sudo().get_param(key, default or "")
    if required and not value:
        raise UserError(_("System parameter '%s' is not configured for Restake.") % key)
    return _decode_if_base64(value)


def _decode_if_base64(value: str) -> str:
    """Decode base64-encoded configuration values (best-effort)."""
    if not value:
        return value
    candidates = value.strip()
    if not candidates:
        return candidates
    if not _looks_like_base64(candidates):
        return candidates
    try:
        decoded = base64.b64decode(candidates).decode("utf-8")
        # Only return when decoding produces printable text
        if decoded.strip():
            return decoded
    except Exception:  # pylint: disable=broad-except
        return value
    return value


def _looks_like_base64(text: str) -> bool:
    if len(text) % 4 != 0:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    return all(ch in allowed for ch in text)


def _get_github_client(env) -> Github:
    token = _get_config_param(env, GITHUB_ACCESS_TOKEN_KEY)
    return Github(token)


def _get_repositories(env, client: Github):
    fork_owner = _get_config_param(env, GITHUB_USERNAME_KEY)
    repo_name = _get_config_param(env, GITHUB_REPO_NAME_KEY)
    main_owner = _get_config_param(env, GITHUB_MAIN_OWNER_KEY)
    fork_repo = client.get_repo(f"{fork_owner}/{repo_name}")
    upstream_repo = client.get_repo(f"{main_owner}/{repo_name}")
    base_branch = _get_config_param(env, GITHUB_BASE_BRANCH_KEY, required=False, default="master") or "master"
    return fork_repo, upstream_repo, fork_owner, repo_name, base_branch


def sanitize_folder_name(name: str, fallback: str) -> str:
    sanitized = re.sub(r"[^\w]", "", name or "")
    return sanitized or fallback


def generate_mnemonic_and_address(env, is_testnet: bool) -> Tuple[str, str]:
    wallet = generate_mnemonic_and_address(env,testnet=is_testnet)
    mnemonic = wallet["mnemonic"]
    bot_address = wallet["address"]
    encrypted = encrypt_mnemonic(env, mnemonic)
    return bot_address, encrypted


def create_branch(fork_repo, base_branch: str, new_branch: str) -> None:
    try:
        fork_repo.get_git_ref(f"heads/{new_branch}")
        raise UserError(_("GitHub branch '%s' already exists.") % new_branch)
    except GithubException as exc:
        if exc.status != 404:
            raise
    base_ref = fork_repo.get_git_ref(f"heads/{base_branch}")
    fork_repo.create_git_ref(ref=f"refs/heads/{new_branch}", sha=base_ref.object.sha)


def add_file(repo, path: str, content: str, message: str, branch: str, author: InputGitAuthor):
    return repo.create_file(
        path=path,
        message=message,
        content=content,
        branch=branch,
        committer=author,
        author=author,
    )


def create_pull_request(upstream_repo, title: str, body: str, head: str, base_branch: str) -> int:
    pr = upstream_repo.create_pull(title=title, body=body, head=head, base=base_branch)
    return pr.number

def _cleanup_github_pr_and_branch(upstream_repo, fork_repo, pr_number: Optional[int], branch_name: Optional[str]) -> None:
    """Close PR and delete branch safely. Handles both merged and open PRs. """
    try:
        if not pr_number and not branch_name:
            return
        
        # Step 1: Close the PR if it's still open (allows branch deletion even if PR is open)
        if pr_number:
            try:
                pr = upstream_repo.get_pull(pr_number)
                if pr.state == "open":
                    pr.edit(state="closed")
                    _logger.info("Closed PR #%s", pr_number)
                else:
                    _logger.info("PR #%s already in state: %s", pr_number, pr.state)
            except GithubException as exc:
                if exc.status == 404:
                    _logger.info("PR #%s not found (already closed/merged or doesn't exist)", pr_number)
                else:
                    _logger.warning("Failed to close PR #%s: %s", pr_number, exc)
            except Exception as exc:
                _logger.warning("Unexpected error closing PR #%s: %s", pr_number, exc)
        
        # Step 2: Delete the branch (works for both merged and unmerged after PR is closed)
        if branch_name:
            try:
                fork_repo.get_git_ref(f"heads/{branch_name}").delete()
                _logger.info("Deleted GitHub branch: %s", branch_name)
            except GithubException as exc:
                if exc.status == 404:
                    _logger.info("Branch %s already deleted", branch_name)
                else:
                    _logger.warning("Failed to delete branch %s: %s", branch_name, exc)
            except Exception as exc:
                _logger.warning("Unexpected error deleting branch %s: %s", branch_name, exc)
    except Exception as exc:
        _logger.error("Unexpected error during PR/branch cleanup: %s", exc)



def _cleanup_github_pr_and_branch(upstream_repo, fork_repo, pr_number: Optional[int], branch_name: Optional[str]) -> None:
    """Close PR and delete branch safely. Handles both merged and open PRs. """
    try:
        if not pr_number and not branch_name:
            return
        
        # Step 1: Close the PR if it's still open (allows branch deletion even if PR is open)
        if pr_number:
            try:
                pr = upstream_repo.get_pull(pr_number)
                if pr.state == "open":
                    pr.edit(state="closed")
                    _logger.info("Closed PR #%s", pr_number)
                else:
                    _logger.info("PR #%s already in state: %s", pr_number, pr.state)
            except GithubException as exc:
                if exc.status == 404:
                    _logger.info("PR #%s not found (already closed/merged or doesn't exist)", pr_number)
                else:
                    _logger.warning("Failed to close PR #%s: %s", pr_number, exc)
            except Exception as exc:
                _logger.warning("Unexpected error closing PR #%s: %s", pr_number, exc)
        
        # Step 2: Delete the branch (works for both merged and unmerged after PR is closed)
        if branch_name:
            try:
                fork_repo.get_git_ref(f"heads/{branch_name}").delete()
                _logger.info("Deleted GitHub branch: %s", branch_name)
            except GithubException as exc:
                if exc.status == 404:
                    _logger.info("Branch %s already deleted", branch_name)
                else:
                    _logger.warning("Failed to delete branch %s: %s", branch_name, exc)
            except Exception as exc:
                _logger.warning("Unexpected error deleting branch %s: %s", branch_name, exc)
    except Exception as exc:
        _logger.error("Unexpected error during PR/branch cleanup: %s", exc)


def check_pull_request_status(env, pr_number: int, upstream_repo=None) -> bool:
    if not pr_number:
        return False
    try:
        if upstream_repo is None:
            github_client = _get_github_client(env)
            _, upstream_repo, _, _, _ = _get_repositories(env, github_client)
        pr = upstream_repo.get_pull(pr_number)
        return pr.is_merged()
    except GithubException:
        return False


def _build_github_files(validator_name: str, validator_identity: str, validator_address: str,
                        bot_address: str, interval: int, minimum_reward: int, is_testnet: bool) -> Dict[str, str]:
    profile = {
        "$schema": "../profile.schema.json",
        "name": validator_name,
        "identity": validator_identity,
    }
    chain_name = "coreumtestnet" if is_testnet else "coreum"
    chain = {
        "$schema": "../chains.schema.json",
        "name": validator_name,
        "chains": [
            {
                "name": chain_name,
                "address": validator_address,
                "restake": {
                    "address": bot_address,
                    "run_time": f"every {interval} hours",
                    "minimum_reward": minimum_reward,
                },
            }
        ],
    }
    return {
        "profile.json": json.dumps(profile, indent=4),
        "chains.json": json.dumps(chain, indent=4),
    }

#-------------------------------------------------------------------------------
#-------- Cron job management for restake cycles --------
#-------------------------------------------------------------------------------
# def _ensure_restake_cron(env, subscription, interval: int, next_run_time) -> Any:
#     cron_model = env['ir.cron'].sudo()
#     cron_name = f"Restake - Subscription {subscription.id}"
#     cron = cron_model.search([('name', '=', cron_name)], limit=1)
#     model = env['ir.model'].sudo().search([('model', '=', 'subscription.subscription')], limit=1)
#     if not model:
#         raise UserError(_("Unable to locate model configuration for subscriptions."))
#     code = f"env['subscription.subscription'].sudo().browse([{subscription.id}])._run_restake_cycle()"
#     values = {
#         'name': cron_name,
#         'model_id': model.id,
#         'state': 'code',
#         'code': code,
#         'interval_number': interval,
#         'interval_type': 'hours',
#         'numbercall': -1,
#         'doall': False,
#         'nextcall': next_run_time,
#     }
#     try:
#         admin_user = env.ref('base.user_root')
#         values['user_id'] = admin_user.id
#     except ValueError:
#         values['user_id'] = env.user.id
#     if cron:
#         cron.write(values)
#         return cron
#     return cron_model.create(values)

def _post(env, payload, log_label):
    try:
        zabbix_url = _get_config_param(env, ZABBIX_URL_KEY).strip()
        zabbix_token = _get_config_param(env, ZABBIX_BEARER_TOKEN_KEY).strip()
    except UserError as exc:
        _logger.error("Zabbix configuration error: %s", exc)
        raise UserError(_("Zabbix restake activation failed")) from exc


    endpoint = f"{zabbix_url.rstrip('/')}/api_jsonrpc.php"
    headers = {
        "Authorization": f"Bearer {zabbix_token}",
        "Content-Type": "application/json-rpc",
    }
    response = requests.post(endpoint, json=payload, headers=headers, timeout=15)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        _logger.error("Zabbix %s returned invalid JSON: %s", log_label, exc)
        raise UserError(_("Zabbix restake activation failed")) from exc
    if data.get("error"):
        _logger.error("Zabbix %s error: %s", log_label, data["error"])
        raise UserError(_("Zabbix restake activation failed"))
    return data.get("result")

def _invoke_zabbix_script(env, interval_hours: int, minimum_reward_value: str, host_id, validator_address) -> None:
    try:
    
        # Locate the Zabbix item to ensure it is enabled.
        item_name = "Docker Container Execution Script*"
        item_get_payload = {
            "jsonrpc": "2.0",
            "method": "item.get",
            "params": {
                "hostids": [host_id],
                "search": {"name": item_name},
                "searchWildcardsEnabled": True,
                "output": ["itemid", "name", "status"],
                "limit": 1,
            },
            "id": 2,
        }
        item_result = _post(env, item_get_payload, "item.get")
        if not item_result:
            _logger.error("Zabbix item not found for pattern %s on host %s", item_name, host_id)
            raise UserError(_("Zabbix restake activation failed"))
        item_id = item_result[0]["itemid"]

        item_enable_payload = {
            "jsonrpc": "2.0",
            "method": "item.update",
            "params": {
                "itemid": item_id,
                "status": 0,
            },
            "id": 3,
        }
        _post(env, item_enable_payload, "item.update")

        payload = {
            "jsonrpc": "2.0",
            "method": "host.update",
            "params": {
                "hostid": host_id,
                "macros": [
                    {"macro": "{$VALIDATOR_ADDRESS}", "value": validator_address},
                    {"macro": "{$RESTART_DELAY}", "value": f"{interval_hours}h"},
                    {"macro": "{$REWARD}", "value": minimum_reward_value},
                ],
            },
            "id": 3,
        }
        _post(env, payload, "host.update")
    except requests.RequestException as exc:
        _logger.error("Zabbix host.update request failed: %s", exc)
        raise UserError(_("Zabbix restake activation failed")) from exc
    except UserError:
        raise
    except Exception as exc:
        _logger.error("Zabbix host.update unexpected error: %s", exc)
        raise UserError(_("Zabbix restake activation failed")) from exc

def enable_restake(env, host_id : Any, node_identifier: int, minimum_reward: Any, interval: Any,
                   partner_id: Optional[int] = None, user_email: Optional[str] = None) -> Dict[str, Any]:
    """Enable Restake for a given subscription."""
    try:
        subscription_model = env['subscription.subscription'].sudo()
        node_model = env['subscription.node'].sudo()
        current_user = env.user
        node_record = node_model.search([('node_identifier', '=', node_identifier)], limit=1)
        if node_record:
            subscription = node_record.subscription_id
            selected_node = node_record
        else:
            subscription = subscription_model.search([('subscription_uuid', '=', node_identifier)], limit=1)
            selected_node = subscription.node_ids[:1]
        if not subscription:
            raise UserError(_("Subscription not found."))
        if subscription.subscription_type != 'validator':
            raise UserError(_("Restake is only available for validator subscriptions."))
        is_admin_user = current_user.has_group('access_rights.group_admin')
        if partner_id and subscription.customer_name.id != partner_id and not is_admin_user:
            raise UserError(_("You do not have access to this subscription."))
        try:
            minimum_reward_decimal = Decimal(str(minimum_reward))
        except (InvalidOperation, TypeError):
            raise UserError(_("Invalid minimum reward supplied."))
        if minimum_reward_decimal <= 0:
            raise UserError(_("Minimum reward must be greater than zero."))
        if minimum_reward_decimal % Decimal(1) != 0:
            raise UserError(_("Minimum reward must be an integer value."))
        minimum_reward_int = int(minimum_reward_decimal)
        try:
            interval_int = int(interval)
        except (TypeError, ValueError):
            raise UserError(_("Interval must be an integer value."))
        if interval_int <= 0:
            raise UserError(_("Interval must be greater than zero."))

        restake_data = {}
        if selected_node.metadata_json:
            try:
                restake_data = json.loads(selected_node.metadata_json)
            except Exception:  # pragma: no cover - corrupted JSON
                restake_data = {}
        
        if restake_data.get('is_active'):
            raise UserError(_("Restake is already enabled for this subscription."))

        validator_info = {}
        if selected_node.validator_info:
            try:
                validator_info = json.loads(selected_node.validator_info)
            except Exception:  # pragma: no cover - corrupted JSON
                validator_info = {}
        validator_address = (
            validator_info.get('validatorAddress')
        )
        primary_node = selected_node or subscription.get_primary_node()
        network_name = (
            primary_node.network_selection_id.name.lower()
            if primary_node and primary_node.network_selection_id and primary_node.network_selection_id.name
            else ''
        )
        if network_name == "testnet":
            rpc_base_url = (subscription.protocol_id.web_url_testnet or "").strip()
        else:
            rpc_base_url = (subscription.protocol_id.web_url or "").strip()
        validator_payload = _lcd_get_json(
        f"/cosmos/staking/v1beta1/validators/{validator_address}",
        allow_404=True,
        base_url=rpc_base_url,
        )
        if not validator_payload or "validator" not in validator_payload:
            raise UserError(_("Unable to fetch validator information from the blockchain."))
        
        validator_public_details = validator_payload["validator"]
        description = validator_public_details.get("description") or {}
        moniker = (description.get("moniker") or "").strip()
        if not validator_address:
            raise UserError(_("Validator address is missing for this subscription."))

        validator_name = moniker
        validator_identity = (
            validator_info.get('validatorIdentity') or
            ''
        )
        network_type = (
            primary_node.network_selection_id.name
            if primary_node and primary_node.network_selection_id
            else ''
        )
        is_testnet = str(network_type or '').lower() in {'testnet', 'devnet', 'qa', 'staging'}
        bot_address, encrypted_mnemonic = validator_info.get('wallet'), validator_info.get('mnemonic')
        timestamp = str(int(time.time()))[-6:]  # Last 6 digits of timestamp
        folder_name = sanitize_folder_name(validator_name, f"validator-{timestamp}")
        random_num = random.randint(10000, 99999)
        branch_name = f"{folder_name.lower()}-{subscription.id}-{random_num}"[:250]

        user_email = user_email or subscription.customer_name.email
        if not user_email:
            raise UserError(_("User e-mail is required to enable Restake."))
        
        if restake_data.get('github_pr_number') and restake_data.get('is_active'):
            # PR already exists, just invoke Zabbix agent and update metaData
            now = fields.Datetime.now()
            next_run = now + timedelta(minutes=int(interval))
            _invoke_zabbix_script(env, int(interval), str(int(minimum_reward)), host_id, validator_address)
            # Update metaData to ensure is_active is True and next_run_time is updated
            restake_data['is_active'] = True
            restake_data['next_run_time'] = fields.Datetime.to_string(next_run)
            selected_node.sudo().write({"metadata_json": json.dumps(restake_data)})
            return restake_data

        github_client = _get_github_client(env)
        fork_repo, upstream_repo, fork_owner, repo_name, base_branch = _get_repositories(env, github_client)

        author = InputGitAuthor(validator_name, user_email)

        try:
            create_branch(fork_repo, base_branch, branch_name)
            files = _build_github_files(
                validator_name=validator_name,
                validator_identity=validator_identity or '',
                validator_address=validator_address,
                bot_address=bot_address,
                interval=interval_int,
                minimum_reward=minimum_reward_int,
                is_testnet=is_testnet,
            )
            add_file(
                fork_repo,
                path=f"{folder_name}/chains.json",
                content=files['chains.json'],
                message=f"chains.json for {validator_name}",
                branch=branch_name,
                author=author,
            )
            add_file(
                fork_repo,
                path=f"{folder_name}/profile.json",
                content=files['profile.json'],
                message=f"profile.json for {validator_name}",
                branch=branch_name,
                author=author,
            )
            pr_number = create_pull_request(
                upstream_repo,
                title=f"chains.json for {validator_name}",
                body="pull request for restake operator",
                head=f"{fork_owner}:{branch_name}",
                base_branch=base_branch,
            )
        except GithubException as exc:
            _logger.exception("GitHub operation failed for subscription %s", subscription.id)
            raise UserError(_("GitHub operation failed: %s") % getattr(exc, 'data', {}).get('message', str(exc)))

        now = fields.Datetime.now()
        next_run = now + timedelta(hours=interval_int)
        # cron = _ensure_restake_cron(env, subscription, interval_int, next_run)
        _invoke_zabbix_script(env, interval_int, str(minimum_reward_int), host_id, validator_address)

        restake_record = {
            "subscription_id": subscription.id,
            "bot_address": bot_address,
            "mnemonic": encrypted_mnemonic,
            "interval": interval_int,
            "minimum_reward": minimum_reward_int,
            "github_pr_number": pr_number,
            "is_active": True,
            "github_branch_name": branch_name,
            "next_run_time": fields.Datetime.to_string(next_run),
            "is_pr_merged": False,
            # "cron_id": cron.id,
        }

        subscription.sudo().write({"metaData": json.dumps(restake_record)})
        selected_node.sudo().write({"metadata_json": json.dumps(restake_record)})

        return restake_record
    except Exception as exc:
        _logger.exception("Failed to enable Restake for node %s: %s", node_identifier, str(exc))
        raise UserError(_("Failed to enable Restake: %s") % str(exc)) from exc


def _disable_restake(env, host_id) -> None:
    try:
        # Locate the Zabbix item to ensure it is enabled.
        item_name = "Docker Container Execution Script*"
        item_get_payload = {
            "jsonrpc": "2.0",
            "method": "item.get",
            "params": {
                "hostids": [host_id],
                "search": {"name": item_name},
                "searchWildcardsEnabled": True,
                "output": ["itemid", "name", "status"],
                "limit": 1,
            },
            "id": 2,
        }
        item_result = _post(env, item_get_payload, "item.get")
        if not item_result:
            _logger.error("Zabbix item not found for pattern %s on host %s", item_name, host_id)
            raise UserError(_("Zabbix restake disable failed"))
        item_id = item_result[0]["itemid"]
        item_status = item_result[0]["status"]

        item_disable_payload = {
            "jsonrpc": "2.0",
            "method": "item.update",
            "params": {
                "itemid": item_id,
                "status": 1,
            },
            "id": 3,
        }
        _post(env, item_disable_payload, "item.update")

        # Return item id and status before disabling
        return {"item_id": item_id, "status": item_status}

    except requests.RequestException as exc:
        _logger.error("Zabbix host.update request failed: %s", exc)
        raise UserError(_("Zabbix restake disable failed")) from exc
    except UserError:
        raise
    except Exception as exc:
        _logger.error("Zabbix host.update unexpected error: %s", exc)
        raise UserError(_("Zabbix restake disable failed")) from exc
