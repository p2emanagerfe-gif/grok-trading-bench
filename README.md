# grok-trading-desk

A crypto-only trading system for Solana launches, orchestrated by Grok. The desk
streams pump.fun launches, scores them with live-search-backed agents, applies
hard vetoes in code, and logs every decision to JSONL.

The design principle throughout: **a model that fails is a model that says no.**

---

## Architecture

```
                      ┌─────────────────────────────┐
                      │          desk.py            │
                      │   3 concurrent asyncio loops│
                      └──────────────┬──────────────┘
             ┌───────────────────────┼───────────────────────┐
             │                       │                       │
      ╔══════▼══════╗        ╔══════▼══════╗        ╔═══════▼══════╗
      ║ crypto_loop ║        ║  exit_loop  ║        ║allocator_loop║
      ║  continuous ║        ║   every 4h  ║        ║   every 24h  ║
      ╚══════╤══════╝        ╚══════╤══════╝        ╚═══════╤══════╝
             │                       │                       │
        ┌────▼─────┐            ┌────▼─────┐           ┌────▼─────┐
        │ 1 scout  │            │7 exit mgr│           │6 alloc.  │
        │  (code)  │            │  (fast)  │           │  (code)  │
        └────┬─────┘            └──────────┘           └──────────┘
             │
      ┌──────▼───────┬────────┬────────────────┐
      │2 auditor     │3 narr. │4 crypto_pulse  │
      │  (fast)      │ (fast) │ (fast, 15m)    │
      └──────┬───────┴────┬───┴────────┬───────┘
             │            │            │
         ┌───▼────────────▼────────────▼───┐
         │ 5 crypto_scoring (hard vetoes)  │
         └───────────────┬─────────────────┘
                         │
                 ┌───────▼────────┐
                 │ crypto_checker │
                 │  (deep model)  │
                 └───────┬────────┘
                         │
                 ┌───────▼────────┐
                 │ crypto_executor│
                 │ STUB (wire it) │
                 └────────────────┘
```

Shared modules:
- `shared/risk.py` — one portfolio, crypto-only allocation
- `shared/log.py` — append-only JSONL event log
- `shared/memory.py` — feeds past realized outcomes back into decisions

---

## The bots

1. `crypto/scout.py` — two-stage pump.fun launch intake and watch window.
2. `crypto/auditor.py` — wallet-level manipulation audit.
3. `crypto/narrative.py` — meme potential and social story.
4. `crypto/crypto_pulse.py` — Solana memecoin regime read with caching.
5. `crypto/crypto_checker.py` — adversarial final approval gate.
6. `shared/allocator.py` — locks the desk to 100% crypto allocation.
7. `shared/exit_manager.py` — HOLD / TIGHTEN / TRIM / CLOSE on open crypto positions.

Hard vetoes live in code, not in prompts:
- `coordinated_buys` or `wash_trading` → skip
- `pulse.go_signal < 0.3` → pause new crypto entries

---

## Models, live data and cost

Current defaults:

| tier | model | who |
|---|---|---|
| `fast` | `grok-4.3` | auditor, narrative, crypto pulse, exit manager |
| `deep` | `grok-4.6` | crypto checker |

Live search is enabled for the agents that need current market/news/social data.
Every call records billed usage, and `scripts/replay.py` reports PnL net of
inference cost.

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
$EDITOR config.yaml          # xAI key, Solana wallet, risk limits

python -m src.desk --config config.yaml --dry-run

python scripts/dashboard.py --log logs/desk.jsonl
python scripts/replay.py    --log logs/desk.jsonl --days 7
```

Run the tests:

```bash
pytest -v
```

### Going live

Paper trading is the default and stays the default unless both conditions hold:

```yaml
mode: "live"
```

```bash
python -m src.desk --config config.yaml --i-understand-the-risk
```

Crypto execution is intentionally left as a stub in `src/crypto/crypto_executor.py`;
wire in your own signing and submission path before using real funds.

---

## Configuration

`config.yaml` is gitignored; only `config.example.yaml` ships in the repo.

| Section | What it controls |
|---|---|
| `mode` | `paper` or `live` |
| `grok` | model slugs, structured outputs, live search, timeouts, retries |
| `solana` | RPC, wallet key, Jito settings, priority fee, slippage |
| `pump_fun` | launch stream and watch-window settings |
| `risk` | total budget, daily loss limit, crypto caps, sizing |
| `crypto_launch_filter` | stage-one launch thresholds |
| `crypto_filter` | stage-two watch-window thresholds |
| `scoring_weights` | crypto scoring weights and buy threshold |
| `pulse` | crypto pulse cache and market gate |
| `exits` | exit loop defaults |
| `debate` | optional debate stage before the checker |
| `memory` | realized-outcome recall window |
| `logging` | JSONL path, stdout echo, cost-report interval |

### Outcome memory

`shared/memory.py` joins each `buy` record to its `close` record and injects
comparable past crypto trades into the checker and exit manager.

---

## Logging

One JSONL line per event, append-only, never rewritten.

| type | fields |
|---|---|
| `buy` | market, symbol, score, all_agent_scores, amount, tx_id |
| `skip` | market, symbol, reason, detail |
| `close` | market, symbol, pnl, hold_time |
| `action` | symbol, action, reason |
| `allocation` | crypto_pct, reason |
| `cost` | cumulative calls, spend, cache hit rate, sources, per-agent |

---

## Disclaimer

This is experimental software that decides how to spend money, driven by
language models that are wrong on a regular basis. Nothing here is financial
advice. Run it on paper first and understand every line before wiring live
execution.
