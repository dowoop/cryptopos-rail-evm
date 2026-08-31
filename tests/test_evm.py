"""Ethereum Sepolia native and ERC-20 rails share verified JSON-RPC truth."""

import json
import unittest

from cryptopos_core.errors import AddressRefused, RailProviderError
from cryptopos_rail_evm import (
	SEPOLIA_CHAIN_ID,
	TRANSFER_TOPIC,
	ethereum_sepolia,
	usdc_ethereum_sepolia,
	usdc_polygon_amoy,
)
from cryptopos_core.plugin import PENDING, SETTLED, PaymentIntent

ENDPOINT = "https://rpc.example"
RECIPIENT = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
TX_A = "0x" + "a" * 64
TX_B = "0x" + "b" * 64
BLOCK_A = "0x" + "1" * 64
BLOCK_B = "0x" + "2" * 64


class Transport:
	def __init__(self, handler):
		self.handler = handler
		self.calls = []

	def post(self, url, body, timeout, max_bytes):
		request = json.loads(body)
		self.calls.append((url, request["method"], request["params"], timeout, max_bytes))
		result = self.handler(request["method"], request["params"])
		return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()


def base_handler(tip):
	def answer(method, params):
		if method == "eth_chainId":
			return hex(SEPOLIA_CHAIN_ID)
		if method == "eth_blockNumber":
			return hex(tip)
		if method == "eth_getBlockByNumber":
			return {
				"number": hex(tip),
				"hash": BLOCK_A,
				"timestamp": "0x4b0",
				"transactions": [],
			}
		raise AssertionError((method, params))

	return answer


def configuration(transport):
	return {"endpoint": ENDPOINT, "transport": transport, "timeout_seconds": 2}


def intent(rail, baseline, amount=1500):
	return PaymentIntent("sale-1", rail.key, RECIPIENT, amount, 1000, 2000, baseline=baseline)


class EthereumSepoliaTest(unittest.TestCase):
	def baseline(self, rail, tip=100):
		return rail.capture_baseline(RECIPIENT, configuration(Transport(base_handler(tip))))

	def test_readiness_proves_chain_id_not_provider_branding(self):
		readiness = ethereum_sepolia.readiness(configuration(Transport(base_handler(100))))
		self.assertTrue(readiness.chargeable)

		def wrong(method, params):
			return "0x1" if method == "eth_chainId" else "0x64"

		refused = ethereum_sepolia.readiness(configuration(Transport(wrong)))
		self.assertFalse(refused.chargeable)
		self.assertIn("not ethereum:sepolia", refused.reason_for("observation"))

		def missing_block_reads(method, params):
			if method == "eth_chainId":
				return hex(SEPOLIA_CHAIN_ID)
			if method == "eth_blockNumber":
				return "0x64"
			if method == "eth_getBlockByNumber":
				return None
			raise AssertionError((method, params))

		incomplete = ethereum_sepolia.readiness(configuration(Transport(missing_block_reads)))
		self.assertFalse(incomplete.chargeable)
		self.assertIn("latest block", incomplete.reason_for("observation"))

	def test_full_checksum_is_required_even_on_testnet(self):
		with self.assertRaises(AddressRefused):
			ethereum_sepolia.capture_baseline(RECIPIENT.lower(), configuration(Transport(base_handler(100))))

	def test_request_carries_the_concrete_chain_id(self):
		baseline = self.baseline(ethereum_sepolia)
		request = ethereum_sepolia.create_request(intent(ethereum_sepolia, baseline))
		self.assertIn("@11155111?value=1500", request.uri)

	def test_successful_native_transfer_settles_at_three_confirmations(self):
		baseline = self.baseline(ethereum_sepolia)

		def answer(method, params):
			if method == "eth_chainId":
				return hex(SEPOLIA_CHAIN_ID)
			if method == "eth_blockNumber":
				return "0x67"  # 103
			if method == "eth_getBlockByNumber":
				height = int(params[0], 16)
				transactions = []
				if height == 101:
					transactions = [{"hash": TX_A, "to": RECIPIENT, "value": hex(1500)}]
				return {
					"number": hex(height),
					"hash": BLOCK_A if height == 101 else BLOCK_B,
					"timestamp": hex(1100 + height),
					"transactions": transactions,
				}
			if method == "eth_getTransactionReceipt":
				return {
					"transactionHash": params[0],
					"blockNumber": "0x65",
					"blockHash": BLOCK_A,
					"status": "0x1",
				}
			raise AssertionError((method, params))

		observations = ethereum_sepolia.observe(
			intent(ethereum_sepolia, baseline), configuration(Transport(answer))
		)
		decision = ethereum_sepolia.settle(intent(ethereum_sepolia, baseline), observations)
		self.assertEqual(decision.state, SETTLED)
		self.assertEqual(decision.credited_native, 1500)
		self.assertEqual(decision.transaction_id, TX_A)

	def test_failed_receipt_is_never_credited(self):
		baseline = self.baseline(ethereum_sepolia)

		def answer(method, params):
			if method == "eth_chainId":
				return hex(SEPOLIA_CHAIN_ID)
			if method == "eth_blockNumber":
				return "0x65"
			if method == "eth_getBlockByNumber":
				return {
					"number": params[0],
					"hash": BLOCK_A,
					"timestamp": "0x500",
					"transactions": [{"hash": TX_A, "to": RECIPIENT, "value": hex(1500)}],
				}
			if method == "eth_getTransactionReceipt":
				return {
					"transactionHash": TX_A,
					"blockNumber": "0x65",
					"blockHash": BLOCK_A,
					"status": "0x0",
				}
			raise AssertionError((method, params))

		observations = ethereum_sepolia.observe(
			intent(ethereum_sepolia, baseline), configuration(Transport(answer))
		)
		self.assertEqual(observations.transfers, ())

	def test_token_logs_are_contract_recipient_and_receipt_bound(self):
		baseline = self.baseline(usdc_ethereum_sepolia)
		recipient_topic = "0x" + "0" * 24 + RECIPIENT[2:].lower()
		amount_word = "0x" + format(1_500_000, "064x")

		def answer(method, params):
			if method == "eth_chainId":
				return hex(SEPOLIA_CHAIN_ID)
			if method == "eth_blockNumber":
				return "0x67"
			if method == "eth_getLogs":
				query = params[0]
				self.assertEqual(query["topics"], [TRANSFER_TOPIC, None, recipient_topic])
				return [
					{
						"address": usdc_ethereum_sepolia.token_contract,
						"topics": [TRANSFER_TOPIC, "0x" + "0" * 64, recipient_topic],
						"data": amount_word,
						"blockNumber": "0x65",
						"blockHash": BLOCK_A,
						"logIndex": "0x0",
						"transactionHash": TX_B,
						"removed": False,
					}
				]
			if method == "eth_getTransactionReceipt":
				return {
					"transactionHash": TX_B,
					"blockNumber": "0x65",
					"blockHash": BLOCK_A,
					"status": "0x1",
				}
			if method == "eth_getBlockByNumber":
				return {"number": "0x65", "hash": BLOCK_A, "timestamp": "0x4b0"}
			raise AssertionError((method, params))

		payment = intent(usdc_ethereum_sepolia, baseline, 1_500_000)
		observations = usdc_ethereum_sepolia.observe(payment, configuration(Transport(answer)))
		decision = usdc_ethereum_sepolia.settle(payment, observations)
		self.assertEqual(decision.state, SETTLED)
		self.assertEqual(decision.credited_native, 1_500_000)
		self.assertEqual(observations.transfers[0].block_time_epoch, 1200)

	def test_split_payment_returns_every_transaction_id(self):
		from cryptopos_core.plugin import ObservationBatch, TransferObservation

		baseline = self.baseline(ethereum_sepolia)
		payment = intent(ethereum_sepolia, baseline, 1500)
		observations = ObservationBatch(
			ethereum_sepolia.key,
			payment.intent_id,
			payment.recipient,
			ENDPOINT,
			100,
			103,
			100,
			103,
			(
				TransferObservation(TX_A, 900, True, 3, 101, 1200),
				TransferObservation(TX_B, 600, True, 3, 101, 1200),
			),
		)
		decision = ethereum_sepolia.settle(payment, observations)
		self.assertEqual(decision.state, SETTLED)
		self.assertEqual(decision.transaction_ids, (TX_A, TX_B))

	def test_late_token_payment_needs_review(self):
		from cryptopos_core.plugin import NEEDS_REVIEW, ObservationBatch, TransferObservation

		baseline = self.baseline(usdc_ethereum_sepolia)
		payment = intent(usdc_ethereum_sepolia, baseline, 1500)
		observations = ObservationBatch(
			usdc_ethereum_sepolia.key,
			payment.intent_id,
			payment.recipient,
			ENDPOINT,
			100,
			103,
			100,
			103,
			(TransferObservation(TX_A, 1500, True, 3, 101, 2001),),
		)
		decision = usdc_ethereum_sepolia.settle(payment, observations)
		self.assertEqual(decision.state, NEEDS_REVIEW)
		self.assertIn("expiry", decision.reason)

	def test_observations_cannot_cross_payment_intents(self):
		from cryptopos_core.errors import InvalidRailPlugin
		from cryptopos_core.plugin import ObservationBatch, TransferObservation

		baseline = self.baseline(ethereum_sepolia)
		first = intent(ethereum_sepolia, baseline)
		observations = ObservationBatch(
			ethereum_sepolia.key,
			first.intent_id,
			first.recipient,
			ENDPOINT,
			100,
			103,
			100,
			103,
			(TransferObservation(TX_A, 1500, True, 3, 101, 1200),),
		)
		other = PaymentIntent("sale-2", ethereum_sepolia.key, RECIPIENT, 1500, 1000, 2000, baseline=baseline)
		with self.assertRaises(InvalidRailPlugin):
			ethereum_sepolia.settle(other, observations)

	def test_a_payment_waits_until_the_three_confirmation_gate(self):
		baseline = self.baseline(usdc_ethereum_sepolia)
		from cryptopos_core.plugin import ObservationBatch, TransferObservation

		observations = ObservationBatch(
			usdc_ethereum_sepolia.key,
			"sale-1",
			RECIPIENT,
			ENDPOINT,
			100,
			101,
			100,
			101,
			(TransferObservation(TX_A, 1500, True, 1, 101, 1200),),
		)
		decision = usdc_ethereum_sepolia.settle(intent(usdc_ethereum_sepolia, baseline), observations)
		self.assertEqual(decision.state, PENDING)
		self.assertEqual(decision.credited_native, 0)

	def test_duplicate_provider_logs_are_refused_instead_of_double_credited(self):
		baseline = self.baseline(usdc_ethereum_sepolia)
		recipient_topic = "0x" + "0" * 24 + RECIPIENT[2:].lower()
		log = {
			"address": usdc_ethereum_sepolia.token_contract,
			"topics": [TRANSFER_TOPIC, "0x" + "0" * 64, recipient_topic],
			"data": "0x" + format(1500, "064x"),
			"blockNumber": "0x65",
			"blockHash": BLOCK_A,
			"logIndex": "0x0",
			"transactionHash": TX_A,
			"removed": False,
		}

		def answer(method, params):
			if method == "eth_chainId":
				return hex(SEPOLIA_CHAIN_ID)
			if method == "eth_blockNumber":
				return "0x65"
			if method == "eth_getLogs":
				return [log, dict(log)]
			raise AssertionError((method, params))

		with self.assertRaises(RailProviderError) as caught:
			usdc_ethereum_sepolia.observe(
				intent(usdc_ethereum_sepolia, baseline), configuration(Transport(answer))
			)
		self.assertIn("duplicate", caught.exception.reason)

	def test_observation_window_is_bounded_and_resumable(self):
		baseline = self.baseline(ethereum_sepolia)

		def answer(method, params):
			if method == "eth_chainId":
				return hex(SEPOLIA_CHAIN_ID)
			if method == "eth_blockNumber":
				return "0x190"  # 400
			if method == "eth_getBlockByNumber":
				height = int(params[0], 16)
				return {
					"number": hex(height),
					"hash": BLOCK_A,
					"timestamp": hex(1200 + height),
					"transactions": [],
				}
			raise AssertionError((method, params))

		payment = intent(ethereum_sepolia, baseline)
		first = ethereum_sepolia.observe(payment, configuration(Transport(answer)))
		self.assertFalse(first.complete)
		self.assertEqual(first.observed_through_tip, 356)
		from cryptopos_core.errors import InvalidRailPlugin

		with self.assertRaises(InvalidRailPlugin):
			ethereum_sepolia.settle(payment, first)
		second = ethereum_sepolia.observe(payment, configuration(Transport(answer)), first)
		self.assertTrue(second.complete)
		self.assertEqual(second.observed_after_tip, 100)
		self.assertEqual(second.observed_through_tip, 400)
		with self.assertRaises(InvalidRailPlugin):
			ethereum_sepolia.observe(payment, configuration(Transport(answer)), second)

	def test_polygon_usdc_waits_for_finalized_not_a_confirmation_count(self):
		def baseline_answer(method, params):
			if method == "eth_chainId":
				return hex(80_002)
			if method == "eth_blockNumber":
				return "0x64"
			raise AssertionError((method, params))

		baseline = usdc_polygon_amoy.capture_baseline(RECIPIENT, configuration(Transport(baseline_answer)))
		recipient_topic = "0x" + "0" * 24 + RECIPIENT[2:].lower()

		def observe_with(finalized):
			def answer(method, params):
				if method == "eth_chainId":
					return hex(80_002)
				if method == "eth_blockNumber":
					return "0x6e"  # 110
				if method == "eth_getLogs":
					return [
						{
							"address": usdc_polygon_amoy.token_contract,
							"topics": [TRANSFER_TOPIC, "0x" + "0" * 64, recipient_topic],
							"data": "0x" + format(1500, "064x"),
							"blockNumber": "0x69",  # 105
							"blockHash": BLOCK_A,
							"logIndex": "0x0",
							"transactionHash": TX_A,
							"removed": False,
						}
					]
				if method == "eth_getTransactionReceipt":
					return {
						"transactionHash": TX_A,
						"blockNumber": "0x69",
						"blockHash": BLOCK_A,
						"status": "0x1",
					}
				if method == "eth_getBlockByNumber" and params == ["0x69", False]:
					return {"number": "0x69", "hash": BLOCK_A, "timestamp": "0x4b0"}
				if method == "eth_getBlockByNumber" and params == ["finalized", False]:
					return {"number": hex(finalized)}
				raise AssertionError((method, params))

			payment = intent(usdc_polygon_amoy, baseline)
			observations = usdc_polygon_amoy.observe(payment, configuration(Transport(answer)))
			return usdc_polygon_amoy.settle(payment, observations)

		self.assertEqual(observe_with(104).state, PENDING)
		self.assertEqual(observe_with(105).state, SETTLED)


if __name__ == "__main__":
	unittest.main()
