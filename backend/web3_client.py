"""
web3_client.py

Client for the PaperCustody contract.

Reads the deployed contract address and ABI from contract_deployment.json.
For a local Hardhat node, BOARD_PRIVATE_KEY can be omitted and an unlocked
node account is used. For Sepolia, set RPC_URL and BOARD_PRIVATE_KEY in the
backend environment; transactions are signed locally before being broadcast.

Important: BOARD_PRIVATE_KEY must belong to the account that deployed the
contract, because that account becomes examBoard.
"""

import config
import json
import os
import time
from pathlib import Path
from typing import Any

from web3 import Web3

DEPLOYMENT_PATH = Path(__file__).parent / "contract_deployment.json"
DEFAULT_RPC_URL = "http://127.0.0.1:8545"


def poa_compatible(w3):
    """QBFT/IBFT blocks carry a long extraData (the validator list). web3.py rejects that unless
    the proof-of-authority middleware is installed. Harmless on chains that don't need it."""
    try:
        from web3.middleware import ExtraDataToPOAMiddleware as _poa     # web3 v7+
    except ImportError:
        from web3.middleware import geth_poa_middleware as _poa          # web3 v6
    w3.middleware_onion.inject(_poa, layer=0)
    return w3


def _raw_signed_transaction(signed: Any) -> bytes:
    """Support eth-account versions with either attribute name."""
    raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raw = getattr(signed, "rawTransaction", None)
    if raw is None:
        raise AttributeError("Signed transaction has no raw transaction bytes.")
    return raw


class PaperCustodyClient:
    def __init__(self, rpc_url: str | None = None, account_index: int = 0):
        if not DEPLOYMENT_PATH.exists():
            raise FileNotFoundError(
                "contract_deployment.json not found. Deploy the contract first, "
                "then make sure deploy.js writes the address and ABI to this file."
            )

        with open(DEPLOYMENT_PATH, "r", encoding="utf-8") as file:
            deployment = json.load(file)

        rpc_url = (
            rpc_url
            or config.env("CHAIN_RPC_URL")      # private chain; wins over any leftover Sepolia setting
            or config.env("SEPOLIA_RPC_URL")
            or config.env("RPC_URL")
            or DEFAULT_RPC_URL
        )
        self.w3 = poa_compatible(Web3(Web3.HTTPProvider(rpc_url)))
        if not self.w3.is_connected():
            raise ConnectionError(f"Could not connect to node at {rpc_url}")

        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(deployment["address"]),
            abi=deployment["abi"],
        )

        self.private_key = config.env("BOARD_PRIVATE_KEY") or None
        if self.private_key:
            self.account = self.w3.eth.account.from_key(self.private_key).address
        else:
            accounts = self.w3.eth.accounts
            if account_index < 0 or account_index >= len(accounts):
                raise IndexError(f"account_index {account_index} is not available on this node.")
            self.account = Web3.to_checksum_address(accounts[account_index])

        # (chain timestamp, local wall-clock timestamp) calibration for chain_now().
        self._ref: tuple[int, float] | None = None

    # ------------------------------------------------------------------
    # Transaction helpers
    # ------------------------------------------------------------------

    def _fee_fields(self) -> dict:
        """Return fee fields compatible with legacy and EIP-1559 networks."""
        latest_block = self.w3.eth.get_block("latest")
        base_fee = latest_block.get("baseFeePerGas")
        if base_fee is None:
            return {"gasPrice": self.w3.eth.gas_price}

        if int(base_fee) == 0:
            # Free-gas private chain (Besu zeroBaseFee): the 1 gwei tip would demand ETH nobody holds.
            return {"maxFeePerGas": 0, "maxPriorityFeePerGas": 0}

        priority_fee = Web3.to_wei(1, "gwei")
        return {
            "maxFeePerGas": int(base_fee) * 2 + priority_fee,
            "maxPriorityFeePerGas": priority_fee,
        }

    def is_free_gas(self) -> bool:
        """True on a zero-base-fee chain, where accounts need no ETH to send transactions."""
        return self.w3.eth.get_block("latest").get("baseFeePerGas") == 0

    def is_dev_node(self) -> bool:
        """True only for Hardhat / Anvil / Ganache, which support evm_increaseTime and friends."""
        try:
            version = str(self.w3.client_version).lower()
        except Exception:
            return False
        return any(name in version for name in ("hardhat", "anvil", "ganache", "edr"))

    def _wait(self, tx_hash: Any) -> str:
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=240)
        if receipt.status != 1:
            raise RuntimeError("Transaction was mined but failed on-chain.")
        return receipt.transactionHash.hex()

    def _send(self, function_call: Any) -> str:
        """Send a contract transaction, signing locally when BOARD_PRIVATE_KEY is set."""
        if not self.private_key:
            # Unlocked accounts are intended for local development nodes only.
            tx_hash = function_call.transact({"from": self.account})
            return self._wait(tx_hash)

        transaction = function_call.build_transaction({
            "from": self.account,
            "nonce": self.w3.eth.get_transaction_count(self.account, "pending"),
            "chainId": self.w3.eth.chain_id,
            **self._fee_fields(),
        })
        signed = self.w3.eth.account.sign_transaction(transaction, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(_raw_signed_transaction(signed))
        return self._wait(tx_hash)

    def fund_if_needed(self, address: str, amount_eth: float) -> bool:
        """Send `amount_eth` to `address` if its balance is below half of that. True if sent."""
        target = Web3.to_wei(amount_eth, "ether")
        if target <= 0 or self.is_free_gas():
            return False
        address = Web3.to_checksum_address(address)
        if self.w3.eth.get_balance(address) >= target // 2:
            return False

        if not self.private_key:
            # Unlocked account: local development node only.
            tx_hash = self.w3.eth.send_transaction({"from": self.account, "to": address, "value": target})
            self._wait(tx_hash)
            return True

        transaction = {
            "from": self.account,
            "to": address,
            "value": target,
            "nonce": self.w3.eth.get_transaction_count(
                self.account, "pending"
            ),
            "chainId": self.w3.eth.chain_id,
            **self._fee_fields(),
        }

        transaction["gas"] = self.w3.eth.estimate_gas({
            "from": transaction["from"],
            "to": transaction["to"],
            "value": transaction["value"],
        }) * 120 // 100
        signed = self.w3.eth.account.sign_transaction(transaction, self.private_key)
        self._wait(self.w3.eth.send_raw_transaction(_raw_signed_transaction(signed)))
        return True

    # ------------------------------------------------------------------
    # Writes (exam board / authorized printer)
    # ------------------------------------------------------------------

    def register_paper(
        self,
        center_id: str,
        blob_hash_hex: str,
        variant_id: str,
        exam_start_time: int,
        printer_address: str,
        expected_copies: int,
    ) -> str:
        """Register a paper's ciphertext hash and authorized printer; no AES key is sent."""
        if not isinstance(exam_start_time, int) or exam_start_time <= 0:
            raise ValueError("exam_start_time must be a positive Unix timestamp.")
        if not isinstance(expected_copies, int) or expected_copies <= 0:
            raise ValueError("expected_copies must be a positive integer.")

        function_call = self.contract.functions.registerPaper(
            center_id,
            Web3.to_bytes(hexstr=self._as_bytes32_hex(blob_hash_hex)),
            variant_id,
            exam_start_time,
            Web3.to_checksum_address(printer_address),
            expected_copies,
        )
        return self._send(function_call)

    def release_key(self, center_id: str, wrapped_key: bytes) -> str:
        """
        Publish the key encrypted/wrapped for the authorized printer.

        The contract remains responsible for enforcing:
        block.timestamp >= examStartTime. This client does not bypass or
        alter that on-chain time lock.
        """
        if not isinstance(wrapped_key, (bytes, bytearray)) or not wrapped_key:
            raise ValueError("wrapped_key must be non-empty bytes.")
        return self._send(
            self.contract.functions.releaseKey(center_id, bytes(wrapped_key))
        )

    def log_custody_event(
        self, center_id: str, stage: str, hash_at_event_hex: str
    ) -> str:
        function_call = self.contract.functions.logCustodyEvent(
            center_id,
            stage,
            Web3.to_bytes(hexstr=self._as_bytes32_hex(hash_at_event_hex)),
        )
        return self._send(function_call)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_state(self, center_id: str) -> dict:
        registered, key_released, printer, expected, printed, start = (
            self.contract.functions.getCenterState(center_id).call()
        )
        return {
            "registered": registered,
            "key_released": key_released,
            "printer": printer,
            "expected_copies": expected,
            "printed_count": printed,
            "exam_start_time": start,
        }

    def is_registered(self, center_id: str) -> bool:
        return self.get_state(center_id)["registered"]

    def get_key(self, center_id: str) -> bytes:
        """Return the wrapped key; the contract only allows this after release."""
        return bytes(self.contract.functions.getKey(center_id).call())

    def get_blob_hash(self, center_id: str) -> bytes:
        return bytes(self.contract.functions.getBlobHash(center_id).call())

    def get_variant_id(self, center_id: str) -> str:
        return self.contract.functions.getVariantId(center_id).call()

    def is_key_released(self, center_id: str) -> bool:
        return self.get_state(center_id)["key_released"]

    def get_custody_log(self, center_id: str) -> list:
        return self.contract.functions.getCustodyLog(center_id).call()

    # ------------------------------------------------------------------
    # Chain clock and local Hardhat development helpers
    # ------------------------------------------------------------------

    def chain_time(self) -> int:
        """Timestamp of the latest block."""
        return int(self.w3.eth.get_block("latest").timestamp)

    def mine_fresh_block(self) -> int:
        """
        Mine a fresh block on development nodes and calibrate chain_now().
        On networks that reject evm_mine, simply read the latest block time.
        """
        try:
            self.w3.provider.make_request("evm_mine", [])
        except Exception:
            pass

        timestamp = self.chain_time()
        self._ref = (timestamp, time.time())
        return timestamp

    def chain_now(self) -> int:
        """Estimate chain time between blocks from the latest calibration."""
        if self._ref is None:
            self.mine_fresh_block()
        timestamp, wall_clock = self._ref
        return int(timestamp + (time.time() - wall_clock))

    def fast_forward(self, seconds: int) -> int:
        """DEV ONLY: advance time on a local Hardhat node and mine a block."""
        if not isinstance(seconds, int) or seconds < 0:
            raise ValueError("seconds must be a non-negative integer.")
        if not self.is_dev_node():
            raise RuntimeError(
                "This chain's clock cannot be moved (it is a real/private network, not a Hardhat dev node). "
                "Register the paper with a start time a minute or two ahead and wait for it."
            )
        response = self.w3.provider.make_request("evm_increaseTime", [seconds])
        if response.get("error"):
            raise RuntimeError(f"Could not advance chain time: {response['error']}")
        return self.mine_fresh_block()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _as_bytes32_hex(hex_str: str) -> str:
        """Validate a 32-byte hex value; never silently pad or truncate a hash."""
        if not isinstance(hex_str, str):
            raise TypeError("Expected a 32-byte hex string.")
        value = hex_str[2:] if hex_str.startswith(("0x", "0X")) else hex_str
        if len(value) != 64:
            raise ValueError(
                f"Expected a 32-byte hex value (64 hex characters), got {len(value)}."
            )
        try:
            bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError("Value contains non-hexadecimal characters.") from exc
        return "0x" + value.lower()
