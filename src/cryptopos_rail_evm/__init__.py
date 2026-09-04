"""Complete Ethereum Sepolia native-ETH and ERC-20 payment rails.

The provider is verified with ``eth_chainId`` before every baseline and
observation. Reads are bounded, integer quantities are parsed exactly, and a
transaction is credited only after its receipt reports success. The adapter
polls blocks/logs for truth; it does not claim a mempool view it does not have.
"""

# The distribution version is declared here as the single source of truth.
# It previously also appeared as a literal in pyproject.toml, and those two
# declarations drifted apart by a patch release; Hatch now derives the
# distribution metadata from this module string.
__version__ = "0.1.1"


import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping

from cryptopos_core.addresses import OK, validate
from cryptopos_core.errors import AddressRefused, InvalidRailPlugin, RailProviderError
from cryptopos_core.plugin import (
	ADDRESS_VALIDATION,
	NEEDS_REVIEW,
	OBSERVATION,
	PAYMENT_REQUEST,
	PENDING,
	SETTLED,
	SETTLEMENT,
	Asset,
	Network,
	ObservationBatch,
	PaymentIntent,
	PaymentRequest,
	Readiness,
	RecipientBaseline,
	SettlementDecision,
	TransferObservation,
)
from cryptopos_core.rails import RAILS, USDC_ON_AMOY, USDC_ON_SEPOLIA
from cryptopos_core.uri import build_uri

SEPOLIA_CHAIN_ID = 11_155_111
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MAX_RESPONSE_BYTES = 4_000_000
MAX_BLOCKS_PER_OBSERVATION = 256
MAX_TRANSACTIONS_PER_BLOCK = 20_000
MAX_LOGS_PER_OBSERVATION = 20_000
DEFAULT_TIMEOUT_SECONDS = 5.0

_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_QUANTITY = re.compile(r"^0x(?:0|[1-9a-f][0-9a-f]*)$")
_DATA_WORD = re.compile(r"^0x[0-9a-f]{64}$")
_BYTECODE = re.compile(r"^0x(?:[0-9a-f]{2})+$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, request, file_pointer, code, message, headers, new_url):
		return None


def _default_user_agent():
	from . import __version__

	return f"cryptopos-rail-evm/{__version__}"


class _JsonRpcTransport:
	def __init__(self):
		self._opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))

	def post(self, url, body, timeout, max_bytes):
		request = urllib.request.Request(
			url,
			data=body,
			headers={
				"Accept": "application/json",
				"Content-Type": "application/json",
				"User-Agent": _default_user_agent(),
			},
			method="POST",
		)
		with self._opener.open(request, timeout=timeout) as response:
			payload = response.read(max_bytes + 1)
		if len(payload) > max_bytes:
			raise ValueError("response exceeded the safety limit")
		return payload


def _configuration(configuration, rail_key):
	if not isinstance(configuration, Mapping):
		raise RailProviderError(rail_key, "configuration must be a mapping")
	endpoint = configuration.get("endpoint")
	if not isinstance(endpoint, str) or not endpoint.strip():
		raise RailProviderError(rail_key, "an explicit JSON-RPC endpoint is required")
	parts = urllib.parse.urlsplit(endpoint.strip())
	if (
		parts.scheme != "https"
		or not parts.hostname
		or parts.username is not None
		or parts.password is not None
		or parts.query
		or parts.fragment
	):
		raise RailProviderError(
			rail_key,
			"endpoint must be an HTTPS URL without credentials, query text, or a fragment",
		)
	base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
	transport = configuration.get("transport") or _JsonRpcTransport()
	if not callable(getattr(transport, "post", None)):
		raise RailProviderError(rail_key, "transport must provide a post method")
	timeout = configuration.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
	if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
		raise RailProviderError(rail_key, "timeout_seconds must be greater than 0 and at most 30")
	return base, transport, float(timeout)


def _rpc(rail_key, provider, method, params):
	base, transport, timeout = provider
	request = json.dumps(
		{"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
		separators=(",", ":"),
	).encode()
	try:
		payload = transport.post(base, request, timeout=timeout, max_bytes=MAX_RESPONSE_BYTES)
	except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exception:
		raise RailProviderError(rail_key, f"{method} failed: {exception}") from None
	if not isinstance(payload, bytes):
		raise RailProviderError(rail_key, f"{method} returned non-byte data")
	if len(payload) > MAX_RESPONSE_BYTES:
		raise RailProviderError(rail_key, f"{method} exceeded the response safety limit")
	try:
		response = json.loads(payload.decode("utf-8"))
	except (UnicodeDecodeError, json.JSONDecodeError) as exception:
		raise RailProviderError(rail_key, f"{method} did not return valid JSON: {exception}") from None
	if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != 1:
		raise RailProviderError(rail_key, f"{method} returned a malformed JSON-RPC envelope")
	if response.get("error") is not None:
		raise RailProviderError(rail_key, f"{method} returned JSON-RPC error {response['error']!r}")
	if "result" not in response:
		raise RailProviderError(rail_key, f"{method} returned no result")
	return response["result"]


def _quantity(rail_key, value, field):
	if not isinstance(value, str) or not _QUANTITY.fullmatch(value):
		raise RailProviderError(rail_key, f"{field} was not a canonical hexadecimal quantity")
	return int(value, 16)


class EthereumSepoliaRail:
	"""One Sepolia asset; ``token_contract=None`` selects native ETH."""

	network = Network("ethereum", "sepolia", True)
	chain_id = SEPOLIA_CHAIN_ID
	max_blocks_per_observation = MAX_BLOCKS_PER_OBSERVATION

	def __init__(self, legacy_key, asset, token_contract=None):
		self.legacy_key = legacy_key
		self.asset = asset
		self.token_contract = token_contract
		self.key = f"{self.network.key}/{asset.key}"
		self.binding_category = RAILS[legacy_key]["binding_category"]
		self.capabilities = frozenset({ADDRESS_VALIDATION, PAYMENT_REQUEST, OBSERVATION, SETTLEMENT})

	def validate_recipient(self, recipient):
		return validate(self.legacy_key, recipient, "testnet")

	def readiness(self, configuration):
		ready = {ADDRESS_VALIDATION, PAYMENT_REQUEST, SETTLEMENT}
		unavailable = []
		try:
			provider = self._provider(configuration)
			self._verify_network(provider)
			tip = self._tip(provider)
			self._probe_observation(provider, tip)
			self._finalized_tip(provider, tip)
		except RailProviderError as exception:
			unavailable.append((OBSERVATION, exception.reason))
		else:
			ready.add(OBSERVATION)
		return Readiness(self.key, frozenset(ready), tuple(unavailable))

	def capture_baseline(self, recipient, configuration):
		self._verified_recipient(recipient)
		provider = self._provider(configuration)
		self._verify_network(provider)
		return RecipientBaseline(self.key, recipient, provider[0], self._tip(provider))

	def create_request(self, intent):
		self._intent(intent)
		self._verified_recipient(intent.recipient)
		if intent.baseline is None:
			raise InvalidRailPlugin(f"{self.network.key} requires a block baseline before request creation")
		uri = build_uri(
			self.legacy_key,
			{"address": intent.recipient},
			intent.amount_native,
			"testnet",
		)
		return PaymentRequest(self.key, uri, intent.recipient, intent.amount_native)

	def observe(self, intent, configuration, previous=None):
		self._intent(intent)
		if intent.baseline is None or intent.baseline.tip is None:
			raise InvalidRailPlugin(f"{self.network.key} observation requires a captured baseline")
		provider = self._provider(configuration)
		if provider[0] != intent.baseline.provider:
			raise RailProviderError(self.key, "observation endpoint differs from the baseline endpoint")
		self._verify_network(provider)
		tip = self._tip(provider)
		if tip < intent.baseline.tip:
			raise RailProviderError(self.key, "provider tip is behind the captured baseline")
		cursor = intent.baseline.tip
		if previous is not None:
			if not isinstance(previous, ObservationBatch):
				raise InvalidRailPlugin("previous observations have an unknown shape")
			previous.require_intent(intent)
			if previous.complete:
				raise InvalidRailPlugin(
					"previous observations are complete; begin a fresh observation cycle to revalidate them"
				)
			cursor = previous.observed_through_tip
		if tip < cursor:
			raise RailProviderError(self.key, "provider tip is behind the observation cursor")
		through = min(tip, cursor + self.max_blocks_per_observation)
		# Read the finalized tip HERE, beside the tip it is checked against, and
		# not after the scan below.
		#
		# `_native_transfers` costs one `eth_getBlockByNumber` per block. On a
		# two-second chain a few hundred blocks of catch-up is minutes of
		# sequential calls, during which the chain keeps finalizing -- so a
		# finalized tip read afterwards is compared against a `tip` from minutes
		# ago and is legitimately above it. That raised "finalized block is above
		# the latest block" on every native Amoy observation, while the token
		# rail on the same chain survived only because `eth_getLogs` is one call
		# and leaves almost no window. Both numbers now describe one instant.
		#
		# Reading it earlier can only make the gate more conservative: an older
		# finalized height matures fewer transfers, never more.
		finalized_tip = self._finalized_tip(provider, tip)
		if through == cursor:
			transfers = []
		elif self.token_contract is None:
			transfers = self._native_transfers(provider, intent.recipient, cursor + 1, through, tip)
		else:
			transfers = self._token_transfers(provider, intent.recipient, cursor + 1, through, tip)
		page = ObservationBatch(
			self.key,
			intent.intent_id,
			intent.recipient,
			provider[0],
			intent.baseline.tip,
			tip,
			cursor,
			through,
			tuple(transfers),
			finalized_tip=finalized_tip,
		)
		return page if previous is None else previous.extend(page)

	def settle(self, intent, observations, claimed_transaction_ids=frozenset()):
		self._intent(intent)
		if not isinstance(observations, ObservationBatch):
			raise InvalidRailPlugin("observations have an unknown shape")
		observations.require_intent(intent)
		if not observations.complete:
			raise InvalidRailPlugin("settlement requires observations through the provider tip")
		if not isinstance(claimed_transaction_ids, frozenset) or any(
			not isinstance(transaction_id, str) for transaction_id in claimed_transaction_ids
		):
			raise InvalidRailPlugin("claimed transaction ids must be a frozenset of text")
		claimed = [t for t in observations.transfers if t.transaction_id in claimed_transaction_ids]
		available = [t for t in observations.transfers if t.transaction_id not in claimed_transaction_ids]
		sighted = sum(t.amount_native for t in available)
		mature = [t for t in available if self._is_mature(t, observations)]
		timely = [
			transfer
			for transfer in mature
			if transfer.block_time_epoch is not None and transfer.block_time_epoch <= intent.expires_at_epoch
		]
		late = [transfer for transfer in mature if transfer not in timely]
		credited = sum(t.amount_native for t in timely)
		if credited >= intent.amount_native:
			return SettlementDecision(
				SETTLED,
				credited,
				sighted,
				tuple(sorted(transfer.transaction_id for transfer in timely)),
				self._settled_reason(),
			)
		if claimed and sum(t.amount_native for t in claimed) + sighted >= intent.amount_native:
			return SettlementDecision(
				NEEDS_REVIEW,
				credited,
				sighted,
				reason="one or more observed transactions are already claimed by another intent",
			)
		if late and credited + sum(transfer.amount_native for transfer in late) >= intent.amount_native:
			return SettlementDecision(
				NEEDS_REVIEW,
				credited,
				sighted,
				reason="payment arrived after expiry or lacks a trustworthy block time",
			)
		reason = "payment is below the invoice amount" if sighted else "no payment observed"
		if sighted >= intent.amount_native:
			reason = self._pending_reason()
		return SettlementDecision(PENDING, credited, sighted, reason=reason)

	def _provider(self, configuration):
		return _configuration(configuration, self.key)

	def _verify_network(self, provider):
		chain_id = _quantity(self.key, _rpc(self.key, provider, "eth_chainId", []), "chain id")
		if chain_id != self.chain_id:
			raise RailProviderError(self.key, f"chain id {chain_id} is not {self.network.key}")

	def _tip(self, provider):
		return _quantity(self.key, _rpc(self.key, provider, "eth_blockNumber", []), "block number")

	def _probe_observation(self, provider, tip):
		"""Exercise the provider method this rail needs, not only network identity."""
		if self.token_contract is None:
			block = _rpc(self.key, provider, "eth_getBlockByNumber", [hex(tip), True])
			if not isinstance(block, dict):
				raise RailProviderError(self.key, "latest block result was not an object")
			if _quantity(self.key, block.get("number"), "latest block number") != tip:
				raise RailProviderError(self.key, "provider returned the wrong latest block")
			block_hash = block.get("hash")
			if not isinstance(block_hash, str) or not _HASH.fullmatch(block_hash):
				raise RailProviderError(self.key, "latest block hash was malformed")
			_quantity(self.key, block.get("timestamp"), "latest block timestamp")
			transactions = block.get("transactions")
			if not isinstance(transactions, list) or len(transactions) > MAX_TRANSACTIONS_PER_BLOCK:
				raise RailProviderError(self.key, "latest block transactions were malformed or excessive")
			return
		code = _rpc(self.key, provider, "eth_getCode", [self.token_contract, "latest"])
		if not isinstance(code, str) or not _BYTECODE.fullmatch(code):
			raise RailProviderError(self.key, "configured token contract has no readable bytecode")
		zero_recipient = "0x" + "0" * 64
		logs = _rpc(
			self.key,
			provider,
			"eth_getLogs",
			[
				{
					"fromBlock": hex(tip),
					"toBlock": hex(tip),
					"address": self.token_contract,
					"topics": [TRANSFER_TOPIC, None, zero_recipient],
				}
			],
		)
		if not isinstance(logs, list) or len(logs) > MAX_LOGS_PER_OBSERVATION:
			raise RailProviderError(self.key, "transfer-log readiness probe was malformed or excessive")

	def _finalized_tip(self, provider, tip):
		return None

	def _is_mature(self, transfer, observations):
		return transfer.block_height is not None and observations.tip - transfer.block_height + 1 >= 3

	def _settled_reason(self):
		return "successful receipt and three-confirmation Sepolia gate passed"

	def _pending_reason(self):
		return "payment is awaiting the three-confirmation gate"

	def _receipt_success(self, provider, transaction_id, expected_height, expected_block_hash):
		receipt = _rpc(self.key, provider, "eth_getTransactionReceipt", [transaction_id])
		if not isinstance(receipt, dict) or receipt.get("transactionHash") != transaction_id:
			raise RailProviderError(self.key, "transaction receipt was malformed or mismatched")
		if _quantity(self.key, receipt.get("blockNumber"), "receipt block number") != expected_height:
			raise RailProviderError(self.key, "transaction receipt moved to a different block")
		block_hash = receipt.get("blockHash")
		if not isinstance(block_hash, str) or block_hash != expected_block_hash:
			raise RailProviderError(self.key, "transaction receipt block hash does not match the observation")
		return _quantity(self.key, receipt.get("status"), "receipt status") == 1

	def _native_transfers(self, provider, recipient, start, end, tip):
		transfers = []
		for height in range(start, end + 1):
			block = _rpc(self.key, provider, "eth_getBlockByNumber", [hex(height), True])
			if not isinstance(block, dict):
				raise RailProviderError(self.key, "block result was not an object")
			if _quantity(self.key, block.get("number"), "block number") != height:
				raise RailProviderError(self.key, "provider returned the wrong block")
			block_hash = block.get("hash")
			if not isinstance(block_hash, str) or not _HASH.fullmatch(block_hash):
				raise RailProviderError(self.key, "block hash was malformed")
			timestamp = _quantity(self.key, block.get("timestamp"), "block timestamp")
			transactions = block.get("transactions")
			if not isinstance(transactions, list) or len(transactions) > MAX_TRANSACTIONS_PER_BLOCK:
				raise RailProviderError(self.key, "block transactions were malformed or excessive")
			for transaction in transactions:
				if not isinstance(transaction, dict):
					raise RailProviderError(self.key, "block transaction was not an object")
				to = transaction.get("to")
				if to is None:
					continue
				if not isinstance(to, str) or not _ADDRESS.fullmatch(to):
					raise RailProviderError(self.key, "transaction recipient was malformed")
				if to.lower() != recipient.lower():
					continue
				value = _quantity(self.key, transaction.get("value"), "transaction value")
				if value == 0:
					continue
				txid = transaction.get("hash")
				if not isinstance(txid, str) or not _HASH.fullmatch(txid):
					raise RailProviderError(self.key, "transaction hash was malformed")
				if self._receipt_success(provider, txid, height, block_hash):
					transfers.append(
						TransferObservation(txid, value, True, tip - height + 1, height, timestamp)
					)
		return transfers

	def _token_transfers(self, provider, recipient, start, end, tip):
		topic_recipient = "0x" + "0" * 24 + recipient[2:].lower()
		query = {
			"fromBlock": hex(start),
			"toBlock": hex(end),
			"address": self.token_contract,
			"topics": [TRANSFER_TOPIC, None, topic_recipient],
		}
		logs = _rpc(self.key, provider, "eth_getLogs", [query])
		if not isinstance(logs, list) or len(logs) > MAX_LOGS_PER_OBSERVATION:
			raise RailProviderError(self.key, "transfer logs were malformed or excessive")
		by_transaction = {}
		seen_logs = set()
		for log in logs:
			if not isinstance(log, dict) or log.get("removed") is not False:
				raise RailProviderError(self.key, "transfer log was malformed or removed")
			address = log.get("address")
			if not isinstance(address, str) or address.lower() != self.token_contract.lower():
				raise RailProviderError(self.key, "transfer log came from the wrong contract")
			topics = log.get("topics")
			if not isinstance(topics, list) or len(topics) != 3:
				raise RailProviderError(self.key, "transfer log topics were malformed")
			if topics[0] != TRANSFER_TOPIC or topics[2] != topic_recipient:
				raise RailProviderError(self.key, "transfer log topics did not match the request")
			if not isinstance(topics[1], str) or not _DATA_WORD.fullmatch(topics[1]):
				raise RailProviderError(self.key, "transfer sender topic was malformed")
			data = log.get("data")
			if not isinstance(data, str) or not _DATA_WORD.fullmatch(data):
				raise RailProviderError(self.key, "transfer amount was not one ABI word")
			amount = int(data, 16)
			if amount == 0:
				continue
			height = _quantity(self.key, log.get("blockNumber"), "log block number")
			if height < start or height > end:
				raise RailProviderError(self.key, "transfer log block is outside the requested range")
			txid = log.get("transactionHash")
			if not isinstance(txid, str) or not _HASH.fullmatch(txid):
				raise RailProviderError(self.key, "transfer log transaction hash was malformed")
			block_hash = log.get("blockHash")
			if not isinstance(block_hash, str) or not _HASH.fullmatch(block_hash):
				raise RailProviderError(self.key, "transfer log block hash was malformed")
			log_index = _quantity(self.key, log.get("logIndex"), "transfer log index")
			log_identity = (block_hash, log_index)
			if log_identity in seen_logs:
				raise RailProviderError(self.key, "provider returned a duplicate transfer log")
			seen_logs.add(log_identity)
			previous = by_transaction.get(txid)
			if previous is None:
				by_transaction[txid] = [amount, height, block_hash]
			elif previous[1:] != [height, block_hash]:
				raise RailProviderError(self.key, "one transaction appeared at two block heights")
			else:
				previous[0] += amount
		transfers = []
		block_times = {}
		for txid, (amount, height, block_hash) in by_transaction.items():
			if self._receipt_success(provider, txid, height, block_hash):
				block_identity = (height, block_hash)
				timestamp = block_times.get(block_identity)
				if timestamp is None:
					timestamp = self._block_timestamp(provider, height, block_hash)
					block_times[block_identity] = timestamp
				transfers.append(TransferObservation(txid, amount, True, tip - height + 1, height, timestamp))
		return transfers

	def _block_timestamp(self, provider, height, expected_hash):
		block = _rpc(self.key, provider, "eth_getBlockByNumber", [hex(height), False])
		if not isinstance(block, dict):
			raise RailProviderError(self.key, "transfer block result was not an object")
		if _quantity(self.key, block.get("number"), "transfer block number") != height:
			raise RailProviderError(self.key, "provider returned the wrong transfer block")
		if block.get("hash") != expected_hash:
			raise RailProviderError(self.key, "transfer block hash does not match the log")
		return _quantity(self.key, block.get("timestamp"), "transfer block timestamp")

	def _verified_recipient(self, recipient):
		verdict, reason = self.validate_recipient(recipient)
		if verdict != OK:
			raise AddressRefused(self.legacy_key, recipient, verdict, reason)

	def _intent(self, intent):
		if not isinstance(intent, PaymentIntent) or intent.rail_key != self.key:
			raise InvalidRailPlugin("payment intent belongs to another rail")


ethereum_sepolia = EthereumSepoliaRail("eth", Asset("native", "eth", "SepoliaETH", 18))
usdc_ethereum_sepolia = EthereumSepoliaRail(
	"usdc-eth",
	Asset("erc20", USDC_ON_SEPOLIA.lower(), "USDC", 6),
	token_contract=USDC_ON_SEPOLIA,
)


class PolygonAmoyRail(EthereumSepoliaRail):
	"""One Amoy asset, settled only once the chain reports the block finalized.

	The gate is a property of the *chain*, not of the asset: `eth_getBlockByNumber
	("finalized")` knows nothing about which token moved. So this class serves
	native POL and any Amoy ERC-20 alike, exactly as its Sepolia base class does
	for confirmation counting -- `token_contract=None` selects native.
	"""

	network = Network("polygon", "amoy", True)
	chain_id = 80_002
	max_blocks_per_observation = 5_000

	def _finalized_tip(self, provider, tip):
		block = _rpc(self.key, provider, "eth_getBlockByNumber", ["finalized", False])
		if not isinstance(block, dict):
			raise RailProviderError(self.key, "finalized block result was not an object")
		finalized = _quantity(self.key, block.get("number"), "finalized block number")
		if finalized > tip:
			raise RailProviderError(self.key, "finalized block is above the latest block")
		return finalized

	def _is_mature(self, transfer, observations):
		return (
			observations.finalized_tip is not None
			and transfer.block_height is not None
			and transfer.block_height <= observations.finalized_tip
		)

	def _settled_reason(self):
		return "successful receipt and Polygon finalized-block gate passed"

	def _pending_reason(self):
		return "payment is awaiting Polygon finalized-block inclusion"


usdc_polygon_amoy = PolygonAmoyRail(
	"usdc-pol",
	Asset("erc20", USDC_ON_AMOY.lower(), "USDC", 6),
	token_contract=USDC_ON_AMOY,
)

# Native POL on Amoy. This was a `RequestRail` in `catalog.py` until 2026-08-24,
# carrying the blocker "the provider-specific observer has not been extracted
# into this package" -- so the terminal could build a payment request for POL and
# could never see one arrive. Nothing needed extracting in the end: native
# observation is the `token_contract=None` path this class already inherits from
# Sepolia, and the maturity gate is the Amoy one directly above. The rail was
# request-only because nobody had composed the two halves, not because either
# half was missing.
polygon_amoy = PolygonAmoyRail("pol", Asset("native", "pol", "AmoyPOL", 18))
