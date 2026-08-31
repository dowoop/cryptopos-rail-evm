# cryptopos-rail-evm

EVM payment rails for [CryptoPoS](https://github.com/dowoop/cryptopos-core) — Ethereum Sepolia and Polygon Amoy, native coin and ERC-20 — read over plain JSON-RPC.

> ### ⚠ The binding on these rails is the weakest this project supports
>
> Unless the host derives a fresh receiving address per sale, all four EVM rails
> receive at a **static account** and match a payment by amount inside the lock
> window. That is not merely imprecise, and it fails without an attacker:
>
> 1. Sale A and sale B use the same address and the same amount.
> 2. A's customer broadcasts a transaction that does not confirm before A expires.
> 3. B captures its baseline, and the transaction confirms inside B's window.
> 4. B sees an unclaimed, timely, sufficient transfer and settles.
> 5. **B's customer paid nothing.**
>
> A payment carries a transaction id, so a host that keeps a claimed-transaction
> set can stop the same transaction being credited twice. Nothing here can tell
> two sales of the same amount apart. If you accept real money on these rails,
> derive a per-sale address — the host, not this package, owns that.

> **Not yet proven through this published wheel.** The adapter has settled
> real testnet money in the parent project, where it shipped built into
> `cryptopos-core`. It has not yet settled a payment in this extracted,
> installed form. This project has four recorded incidents of a green suite
> over a deployment that could not take a payment, so the distinction is
> stated rather than glossed.

**Not audited.** No external security audit; never used with mainnet funds.

Install it beside `cryptopos-core` and it registers itself through the
`cryptopos.rails` entry-point group — a host that discovers rails finds it with
no code change:

```bash
pip install cryptopos-rail-evm
```

```python
from importlib import metadata

for point in metadata.entry_points(group="cryptopos.rails"):
    rail = point.load()
    print(point.name, rail.key, sorted(rail.capabilities))
```

## What it is

A `PaymentRail` implementation: it validates a recipient, builds a payment
request, observes the chain for arriving money, and returns a settlement
decision. It holds **no keys and never spends** — every rail here is a watcher,
and the customer's own wallet is the payer.

Zero runtime dependencies beyond `cryptopos-core`.

## Rails

| entry point | rail key | maturity gate |
|---|---|---|
| `ethereum-sepolia-eth` | `ethereum:sepolia/native:eth` | 3 confirmations |
| `ethereum-sepolia-usdc` | `ethereum:sepolia/erc20:0x1c7d…7238` | 3 confirmations |
| `polygon-amoy-pol` | `polygon:amoy/native:pol` | at or below the `finalized` block tag |
| `polygon-amoy-usdc` | `polygon:amoy/erc20:0x41e9…7582` | at or below the `finalized` block tag |

## The two chains do not settle the same way, and the difference is large

Polygon settles on the **`finalized` block tag**, which cannot be reorganised
away. Measured on Amoy, the finalized head trails the tip by about **one block,
one second**, so that guarantee costs essentially nothing.

Ethereum cannot do the same. Sepolia's finalized head trails the tip by about
**82 blocks — seventeen minutes** — which is longer than a fifteen-minute price
lock, so gating on it would fail honest, immediately-paid sales permanently.
The Sepolia rails therefore settle at **three confirmations**, and a sale booked
that way *can* be reorganised away afterwards. That exposure is structural: it
is a property of the chain against a point-of-sale timing budget, and no amount
of care in this adapter removes it.

Prefer the Polygon-class rails where you need settlement that is both fast and
final.

## Binding

Both chains receive at a **static account** unless the host derives a fresh
address per sale, so the default binding is the weakest one this project
supports: a payment is matched by amount inside the lock window. A payment is
tied to a transaction id, so a host's claimed-transaction set can stop the same
transaction being credited twice — but two sales for the same amount at the
same address are not distinguishable by this rail alone.

## What this package does not decide

Pricing, which rails a deployment offers, whether a rail is switched on, and
what an endpoint URL should be are **host** questions. They change per
deployment and are edited by someone with a login. This package answers only
what is true about the chain.

## Testing

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
```

No test in this package opens a socket.

## Licence

MIT.
