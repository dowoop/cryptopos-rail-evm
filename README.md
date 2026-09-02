# cryptopos-rail-evm

EVM payment rails for [cryptopos-core](https://github.com/dowoop/cryptopos-core) — Ethereum Sepolia and Polygon Amoy, native coin and ERC-20 — read over plain JSON-RPC.

They hold **no keys and never spend**. They are watchers: the customer's own
wallet is the payer, and this package only tells you what the chain says.

```bash
pip install cryptopos-rail-evm
```

Installing it *is* the integration — all four rails register themselves through
the `cryptopos.rails` entry-point group, and a host that calls `discover()`
finds them with no code change.

> ### ⚠ The binding on these rails is the weakest this project supports
>
> Unless the host derives a fresh receiving address per sale, all four EVM rails
> receive at a **static account**, and settlement credits the **running total**
> of unclaimed, timely transfers — it settles as soon as that total reaches the
> invoice. It does **not** match the amount. That is not merely imprecise, and
> it fails without an attacker:
>
> 1. Sale A and sale B are open at the same address. Their amounts need not match.
> 2. B's customer pays B's amount.
> 3. A polls first, sees an unclaimed timely transfer, and its total covers A's invoice.
> 4. **A settles on B's money** — even if A invoiced a thousandth of it.
> 5. B, whose customer actually paid, ends `needs-review` credited nothing.
>
> Reproduced against these adapters, not theorised: a 1 wei invoice settles on a
> 10¹⁵ wei transfer. Transfers also **sum**, so making each sale's amount unique
> is not a remedy.
>
> A payment carries a transaction id, so a host that keeps a claimed-transaction
> set can stop the same transaction being credited twice. Nothing here can tell
> two concurrent sales apart, whatever their amounts. **If you accept real money
> on these rails, [derive a per-sale address](#3-give-every-sale-its-own-address)
> — the host, not this package, owns that.**

> **Not yet proven through this published wheel.** The adapters have settled real
> testnet money in the parent project, where they shipped built into
> `cryptopos-core`. They have not yet settled a payment in this extracted,
> installed form. This project has four recorded incidents of a green suite over
> a deployment that could not take a payment, so the distinction is stated rather
> than glossed.

**Not audited.** No external security audit; never used with mainnet funds.

## Rails

| entry point | rail key | settles at |
|---|---|---|
| `ethereum-sepolia-eth` | `ethereum:sepolia/native:eth` | 3 confirmations |
| `ethereum-sepolia-usdc` | `ethereum:sepolia/erc20:0x1c7d…7238` | 3 confirmations |
| `polygon-amoy-pol` | `polygon:amoy/native:pol` | at or below the `finalized` block tag |
| `polygon-amoy-usdc` | `polygon:amoy/erc20:0x41e9…7582` | at or below the `finalized` block tag |

---

# Cookbook

The five-call sequence, the settlement states, and the four host obligations are
in [cryptopos-core's cookbook](https://github.com/dowoop/cryptopos-core#the-five-calls).
This file covers only what is specific to EVM chains.

## 1. Configure it

```python
configuration = {
    "endpoint": "https://rpc-amoy.polygon.technology",   # any JSON-RPC HTTPS URL
    "timeout_seconds": 10,                               # optional, per request
}
```

`readiness` verifies the chain id **and exercises the actual method the rail
needs** — the full-block read for a native rail, the `eth_getLogs` token query
for an ERC-20 one — not merely `eth_chainId`. A provider that answers the cheap
call and refuses the expensive one is caught at start-up rather than mid-sale.
It still cannot prove that a provider is honest.

## 2. Charge a sale

<!-- readme: new -->
```python
from cryptopos_core.plugin import PaymentIntent, RecipientBaseline
from cryptopos_rail_evm import ethereum_sepolia, usdc_polygon_amoy

ethereum_sepolia.key                     # -> 'ethereum:sepolia/native:eth'
usdc_polygon_amoy.key
#   -> 'polygon:amoy/erc20:0x41e94eb019c0762f9bfcf9fb1e58725bfb0e7582'
```

The rail key carries the token contract, so a USDC rail and an ETH rail on the
same chain are different rails and can never be confused for one another. A
sale's rail key pins the contract for the life of that sale.

```python
address = "0x4B7115aD9623A528f1845eaf85D166dE1E869BFB"

def intent_for(rail, amount_native):
    baseline = RecipientBaseline(rail.key, address, "json-rpc", tip=100)
    return PaymentIntent("sale-1042", rail.key, address, amount_native,
                         1_787_100_000, 1_787_101_800, baseline=baseline)

ethereum_sepolia.create_request(intent_for(ethereum_sepolia, 125_000)).uri
#   -> 'ethereum:0x4B7115aD9623A528f1845eaf85D166dE1E869BFB@11155111?value=125000'
```

That is [ERC-681](https://eips.ethereum.org/EIPS/eip-681), and note two things.
The chain id is **in the URI** (`@11155111`), so unlike BIP-21 the payer's
wallet cannot pay it on the wrong network. And the amount is the **integer
native** amount — wei, not ether. Sending the decimal form here is not a
rounding difference, it is off by 10¹⁸.

A token payment looks quite different, and the difference matters:

```python
usdc_polygon_amoy.create_request(intent_for(usdc_polygon_amoy, 125_000)).uri
#   -> 'ethereum:0x41E94Eb019C0762f9Bfcf9Fb1E58725BfB0e7582@80002/transfer?address=0x4B7115aD9623A528f1845eaf85D166dE1E869BFB&uint256=125000'
```

The URI targets the **token contract**, and the merchant is a parameter of a
`transfer` call. Your receiving address appears in the query string, not as the
URI's subject. A reviewer eyeballing the URI for "my address" will not find it
where they expect it — that is correct, not a bug.

Against a live endpoint:

<!-- readme: skip -->
```python
baseline = rail.capture_baseline(address, configuration)
batch = rail.observe(intent, configuration)
while not batch.complete:                       # EVM rails genuinely page
    batch = rail.observe(intent, configuration, batch)
decision = rail.settle(intent, batch, claimed_transaction_ids=already_credited)
```

**The loop is not optional here.** EVM reads are bounded by block range, so
`observe` returns a cumulative batch and resumes from it until the provider's
tip has been covered. Settling on the first batch settles on a fraction of the
window. Once a batch reports `complete`, start the *next* poll without it, so
the rail revalidates the current canonical chain instead of carrying a stale
snapshot forward.

## 3. Give every sale its own address

This is the remedy for the warning at the top of this file, and it is the host's
job. `cryptopos-core` ships watch-only BIP-32 derivation:

```python
from cryptopos_core import hd

XPUB = ("xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJ"
        "oCu1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8")

account = hd.parse_extended_key(XPUB)

def address_for_sale(index):
    """One fresh receiving address per sale, from a key that cannot spend."""
    return hd.evm_address(hd.derive_path(account, f"0/{index}"))

address_for_sale(0)                      # -> '0x4B7115aD9623A528f1845eaf85D166dE1E869BFB'
address_for_sale(1)                      # -> '0xEb5A8aE75e395Ef05c96839a3FB088B2f65E7662'
```

Addresses come back in [EIP-55](https://eips.ethereum.org/EIPS/eip-55) mixed-case
checksum form.

**Derive from the account key**, not the master key — the xpub a wallet exports
for `m/44'/60'/0'`, so `0/index` is its external chain. The example above uses a
BIP-32 test vector's master key, which is convenient for a checkable example and
wrong for a real deployment.

**An index is spent the moment it is shown, and can never be reused.** A
payment instruction cannot be withdrawn: a customer who kept an old QR can pay
it tomorrow, and if that address now belongs to another sale, that sale settles
on their money. No cooldown helps. **And here you have no backstop at all** —
the Bitcoin rail refuses a recipient that already has transaction history, so
it at least catches reuse of a *paid* address. EVM accounts have history by
design, so there is no anomaly for this package to detect and nothing will stop
you. The discipline is entirely yours.

**Keep the allocation counter durable, and mind the gap limit.** Never reusing
means abandoned checkouts consume indices, while a wallet restoring from the
seed scans only until it meets a run of unused addresses — commonly **20**,
BIP-44's gap limit. Keep the watching wallet's limit above your realistic run
of unpaid sales, persist the next-index counter across restarts, and apply
backpressure if you cannot. Reconcile late payments by transaction id rather
than assuming they cannot happen.

The module accepts extended **public** keys only — no private derivation, no
signing operation — so a host deriving addresses this way still holds nothing
that can spend:

<!-- readme: raises -->
```python
hd.parse_extended_key("xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi")   # InvalidExtendedKey - a private key has no business here
```

Unlike the Bitcoin rail, **nothing in this package refuses a reused address**.
EVM accounts have history by design and an account that has been paid before is
not anomalous, so there is no equivalent of the Bitcoin history check to lean
on. The per-sale address is the whole of your binding.

## 4. The two chains do not settle the same way, and the difference is large

Polygon settles on the **`finalized` block tag**, which cannot be reorganised
away. Measured on Amoy, the finalized head trails the tip by about **one block,
one second**, so that guarantee costs essentially nothing.

Ethereum cannot do the same. Sepolia's finalized head trails the tip by about
**82 blocks — seventeen minutes** — longer than a fifteen-minute price lock, so
gating on it would permanently fail honest, immediately-paid sales. The Sepolia
rails therefore settle at **three confirmations**, and a sale booked that way
*can* be reorganised away afterwards. That exposure is structural: a property of
the chain against a point-of-sale timing budget, and no amount of care in this
adapter removes it.

**Prefer the Polygon-class rails where you need settlement that is both fast and
final.**

## Binding, precisely

Both chains receive at a **static account** unless the host derives a fresh
address per sale, so the default binding is the weakest this project supports: a
payment is credited by **running total inside the lock window, not by amount
match**. Whichever open sale polls first takes whatever unclaimed money covers
its invoice; two deposits SUM, so giving each sale a unique amount is not a
remedy — the test is a total, not an equality.

A payment is tied to a transaction id, so a host's claimed-transaction set stops
the same transaction being credited twice. It cannot tell two concurrent sales
at the same address apart.

*(This section said "matched by amount inside the lock window" until 2026-08-31.
That is a stronger claim than the code makes and it is false; the sentence
survived into a shipped wheel's metadata.)*

## What this package does not decide

Pricing, which rails a deployment offers, whether a rail is switched on, and
what an endpoint URL should be are **host** questions. They change per
deployment and are edited by someone with a login. This package answers only
what is true about the chain.

## Testing

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
python3 tools/readme.py --wheel   # every example above, against the wheel
```

No test in this package opens a socket.

## Licence

MIT.
