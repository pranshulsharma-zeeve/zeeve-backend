"""
Ad-hoc probe script to fetch Skale validator info via Ethereum JSON-RPC.

Usage:
    python skale_probe.py --validator-address <eth address> [--rpc-url <url>] [--max-delegations 5]

Requires `web3` and `eth-abi` installed (pip install web3 eth-abi).
The script reads the Skale Manager ABI from `controllers/skaleAbi.json`.
"""

import argparse
import json
import os
import sys
from pathlib import Path


VALIDATOR_SERVICE_ADDRESS = "0x840C8122433A5AA7ad60C1Bcdc36AB9DcCF761a5"
DELEGATION_CONTROLLER_ADDRESS = "0x06dD71dAb27C1A3e0B172d53735f00Bf1a66Eb79"
DEFAULT_RPC_URL = "https://eth.llamarpc.com"


def _require_web3():
    try:
        from web3 import Web3  # type: ignore
    except ImportError:
        print("Missing dependency: pip install web3 eth-abi", file=sys.stderr)
        sys.exit(1)
    return Web3


def load_abi():
    base_dir = Path(__file__).resolve().parent.parent / "controllers"
    abi_path = base_dir / "skaleAbi.json"
    if not abi_path.exists():
        print(f"ABI file not found at {abi_path}", file=sys.stderr)
        sys.exit(1)
    with abi_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    try:
        validator_abi = data["validator_service_abi"]
        delegation_abi = data["delegation_controller_abi"]
    except KeyError:
        print("ABI JSON missing required keys validator_service_abi/delegation_controller_abi", file=sys.stderr)
        sys.exit(1)
    return validator_abi, delegation_abi


def _to_checksum(w3, addr: str) -> str:
    try:
        return w3.to_checksum_address(addr)
    except Exception:
        print(f"Invalid address: {addr}", file=sys.stderr)
        sys.exit(1)


def fetch_validator(w3, validator_contract, delegation_contract, validator_address: str, validator_id: int, max_delegations: int):
    if validator_id is None:
        validator_id = validator_contract.functions.getValidatorId(validator_address).call()
        if validator_id == 0:
            raise RuntimeError("Validator not found for provided address")

    validator = validator_contract.functions.getValidator(validator_id).call()
    # validator tuple: name, validatorAddress, requestedAddress, description, feeRate (per mille), registrationTime, minimumDelegationAmount, acceptNewRequests
    fee_rate_per_mille = validator[4]
    commission_pct = float(fee_rate_per_mille) / 10

    delegated_now = delegation_contract.functions.getAndUpdateDelegatedToValidatorNow(validator_id).call()
    delegated_total = int(delegated_now)

    delegations_len = delegation_contract.functions.getDelegationsByValidatorLength(validator_id).call()
    sample_count = min(max_delegations, delegations_len)
    delegation_items = []
    for idx in range(sample_count):
        deleg_id = delegation_contract.functions.delegationsByValidator(validator_id, idx).call()
        delegation = delegation_contract.functions.getDelegation(deleg_id).call()
        # delegation tuple: holder, validatorId, amount, delegationPeriod, created, started, finished, info
        delegation_items.append(
            {
                "id": int(deleg_id),
                "holder": delegation[0],
                "amount_wei": str(delegation[2]),
                "delegationPeriod": int(delegation[3]),
                "created": int(delegation[4]),
                "info": delegation[7],
            }
        )

    summary = {
        "validatorId": int(validator_id),
        "name": validator[0],
        "validatorAddress": validator[1],
        "description": validator[3],
        "commissionPct": commission_pct,
        "minimumDelegationAmountWei": str(validator[6]),
        "acceptNewRequests": bool(validator[7]),
        "totalDelegatedWei": str(delegated_total),
        "delegationsCount": int(delegations_len),
        "delegationsSample": delegation_items,
    }
    return summary


def main():
    parser = argparse.ArgumentParser(description="Probe Skale validator data via Ethereum RPC.")
    parser.add_argument("--rpc-url", default=os.environ.get("SKALE_RPC_URL", DEFAULT_RPC_URL), help="Ethereum RPC URL (default: https://eth.llamarpc.com)")
    parser.add_argument("--validator-address", help="Validator owner address (checksummed or hex).")
    parser.add_argument("--validator-id", type=int, help="Validator ID (if known).")
    parser.add_argument("--max-delegations", type=int, default=5, help="Number of delegations to sample.")
    args = parser.parse_args()

    if not args.validator_address and args.validator_id is None:
        print("Provide --validator-address or --validator-id", file=sys.stderr)
        sys.exit(1)

    Web3 = _require_web3()
    w3 = Web3(Web3.HTTPProvider(args.rpc_url))
    if not w3.is_connected():
        print(f"Failed to connect to RPC at {args.rpc_url}", file=sys.stderr)
        sys.exit(1)

    validator_abi, delegation_abi = load_abi()
    validator_contract = w3.eth.contract(
        address=_to_checksum(w3, VALIDATOR_SERVICE_ADDRESS),
        abi=validator_abi,
    )
    delegation_contract = w3.eth.contract(
        address=_to_checksum(w3, DELEGATION_CONTROLLER_ADDRESS),
        abi=delegation_abi,
    )

    validator_addr = _to_checksum(w3, args.validator_address) if args.validator_address else None
    summary = fetch_validator(
        w3,
        validator_contract,
        delegation_contract,
        validator_addr,
        args.validator_id,
        max(args.max_delegations, 0),
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
