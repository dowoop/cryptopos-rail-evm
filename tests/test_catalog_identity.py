"""Rail identity and payment-request shape, moved out of cryptopos-core 2.0.

These asserted the EVM adapters while they were built into core. The adapters
live here now, so the assertions do too. Pinning a decimal scale or a chain id
in a package that no longer contains the code is how a wrong exponent survives
a green suite -- and a wrong exponent misprices every sale on the rail by a
factor of ten.
"""

import unittest

from cryptopos_core.plugin import PaymentIntent, RecipientBaseline

from cryptopos_rail_evm import (
	ethereum_sepolia,
	polygon_amoy,
	usdc_ethereum_sepolia,
	usdc_polygon_amoy,
)

EVM = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"


def intent(rail, recipient, amount, reference="", baseline=None):
	return PaymentIntent("sale-1", rail.key, recipient, amount, 100, 200, reference, baseline)


class EvmCatalogIdentity(unittest.TestCase):
	def test_chargeable_asset_atomic_scales_are_pinned(self):
		"""The scale of a rail that can actually take money.

		Native POL was pinned only in the request-only test above until
		2026-08-24. When it became chargeable it left that list, and its scale
		stopped being asserted anywhere -- a mutation of `18` to `19` survived
		the whole suite. A wrong exponent here misprices every sale on the rail
		by a factor of ten, so it is pinned where the rail now lives.
		"""
		self.assertEqual(
			{
				rail.key: (rail.asset.decimals, rail.asset.symbol)
				for rail in (
					ethereum_sepolia,
					usdc_ethereum_sepolia,
					polygon_amoy,
					usdc_polygon_amoy,
				)
			},
			{
				"ethereum:sepolia/native:eth": (18, "SepoliaETH"),
				"ethereum:sepolia/erc20:0x1c7d4b196cb0c7b01d743fbc6116a902379c7238": (6, "USDC"),
				"polygon:amoy/native:pol": (18, "AmoyPOL"),
				"polygon:amoy/erc20:0x41e94eb019c0762f9bfcf9fb1e58725bfb0e7582": (6, "USDC"),
			},
		)

	def test_evm_native_and_token_requests_carry_sepolia_identity(self):
		native_baseline = RecipientBaseline(ethereum_sepolia.key, EVM, "https://rpc.example", 100)
		token_baseline = RecipientBaseline(usdc_ethereum_sepolia.key, EVM, "https://rpc.example", 100)
		native = ethereum_sepolia.create_request(
			intent(ethereum_sepolia, EVM, 10**15, baseline=native_baseline)
		)
		token = usdc_ethereum_sepolia.create_request(
			intent(usdc_ethereum_sepolia, EVM, 6_250_000, baseline=token_baseline)
		)
		self.assertIn("@11155111?value=", native.uri)
		self.assertIn(usdc_ethereum_sepolia.asset.reference, token.uri.lower())

	def test_polygon_token_request_carries_amoy_chain_and_contract(self):
		baseline = RecipientBaseline(usdc_polygon_amoy.key, EVM, "https://rpc.example", 100)
		request = usdc_polygon_amoy.create_request(
			intent(usdc_polygon_amoy, EVM, 6_250_000, baseline=baseline)
		)
		self.assertIn("@80002/transfer", request.uri)
		self.assertIn(usdc_polygon_amoy.asset.reference, request.uri.lower())
