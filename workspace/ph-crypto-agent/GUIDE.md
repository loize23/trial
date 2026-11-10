# Building an AI Crypto Trading Agent from the Philippines

*A practical, honest guide. Last updated 2026-11-10.*

> **Read this before anything else.** I am not giving you financial advice and neither is
> anyone selling you a trading bot. Everything below is engineering and risk management.
> Most retail algo traders lose money. The ones who don't lose money usually have an edge
> in infrastructure, data, or latency — not in "AI". Assume your first bot will lose money,
> and build it so that losing money is a small, bounded, educational event.

---

## 1. What "an AI agent that trades for me" actually means

People say this sentence meaning four very different things. Pick yours deliberately:

| # | What you build | Who decides | Backtestable? | Realistic outcome |
|---|---|---|---|---|
| A | **Rules bot** (EMA/RSI/grid/DCA) | Deterministic code | ✅ Yes | The sane starting point |
| B | **ML signal bot** (gradient boosting / LSTM on features) | A trained model | ✅ Yes, with care | Needs lots of data + discipline; overfits constantly |
| C | **LLM-in-the-loop** (GPT/Claude reads news + charts, emits trades) | A language model | ❌ No | Consistently loses after fees in every honest test |
| D | **LLM as analyst** (bot trades on rules; LLM writes briefings, journals trades, audits your code) | Code decides, LLM narrates | ✅ Yes | **Genuinely useful. This is what the kit in this repo does.** |

The trap: most people ask for C because it sounds like the future, and C is the one that
cannot be tested, cannot be reproduced, and hallucinates prices. An LLM will happily tell
you "BTC is showing bullish divergence" about a chart it never actually saw.

**Recommended architecture (what I built you):**

```
        market data (public REST/WS)
                 │
                 ▼
      ┌──────────────────────┐        ┌────────────────────────┐
      │  STRATEGY (rules)    │───────▶│  RISK MANAGER          │
      │  deterministic,      │ intent │  can only say NO or    │
      │  backtestable        │        │  SMALLER. Never yes+.  │
      └──────────────────────┘        └───────────┬────────────┘
                 ▲                                │ approved order
                 │                                ▼
      ┌──────────┴─────────┐            ┌──────────────────┐
      │ LLM (optional)     │            │ BROKER           │
      │ briefing, journal, │            │ paper OR live    │
      │ code audit         │            │ (ccxt)           │
      │ NO ORDER ACCESS    │            └────────┬─────────┘
      └────────────────────┘                     │
                                        ┌────────▼─────────┐
                                        │ alerts (Telegram)│
                                        │ logs, CSV, state │
                                        └──────────────────┘
```

The single most important arrow in that diagram is the one the LLM **doesn't** have.

---

## 2. The Philippines-specific part (this is the bit most guides skip)

Crypto is legal to own and trade in the PH. What changed recently is *who is allowed to
serve you*.

**The regulatory picture as of late 2026:**

- The **SEC** regulates Crypto-Asset Service Providers (CASPs). SEC Memorandum Circulars
  No. 4 and No. 5, Series of 2025 took effect **5 July 2025** — registration, disclosure,
  custody and marketing rules for anyone offering crypto services to Filipinos.
- The **BSP** separately regulates virtual asset service providers (wallets, exchanges
  handling fiat) — this is why Coins.ph and PDAX hold BSP licences.
- In **August 2025** the SEC named **OKX, Bybit, MEXC, KuCoin, Bitget** (and others) as
  unregistered platforms accessible from the PH, warned the public, and signalled possible
  blocking/access-restriction action. Using them from PH is a real, personal risk —
  not a theoretical one: access can be cut off with your funds inside.
- **Binance** has been blocked since a 2024 SEC order. As of mid-2026 it is pursuing
  re-entry through the SEC regulatory sandbox, with testing starting late 2026 — status
  can change, verify before you rely on it.
- In **June 2026** the PH barred licensed platforms from listing privacy coins and
  tightened listing rules.
- **Tax:** crypto is treated as property, not currency. Gains are taxable; the exchange
  owes VAT on its fees. Keeping a clean trade CSV (the kit writes one automatically) is
  worth real money at filing time. Confirm current treatment with a PH accountant —
  rates and BIR guidance move.

**What this means for your bot — pick one path:**

| Path | Compliance | API for retail bots? | Verdict |
|---|---|---|---|
| **Coins.ph / Coins.ph Pro** | ✅ BSP + SEC-registered | ✅ Yes — REST API at `api.pro.coins.ph`, spot trading endpoints, API-key management, Python/JS/Java/Go SDKs | **Best default.** Regulated, PHP on/off-ramp via InstaPay, real spot API. Rate limits are tight (120 req/min per IP) so poll, don't hammer |
| **PDAX** | ✅ Licensed, homegrown | ⚠️ API is via the **PDAX Partners** portal (institutional/business) — not a self-serve retail bot API | Great for buying/selling and PHP pairs; weak for automation unless you qualify as a partner |
| **OKX / Bybit / Bitget / MEXC / KuCoin** | ❌ SEC-flagged as unregistered in PH | ✅ Excellent APIs, deep liquidity, ccxt support | Technically the easiest to automate; **regulatorily the riskiest.** Access can be restricted with your money on the platform. If you go here, keep balances small and withdraw often |
| **Binance** | ❌ Blocked since 2024 (sandbox re-entry pending) | ✅ | Don't build your business on a platform that's currently inaccessible |
| **DEX / on-chain bots** (Uniswap etc.) | Grey; some protocols were SEC-flagged | ✅ | Adds smart-contract risk, gas, MEV and rug exposure on top of everything else. Not a beginner project |

> **Honest summary:** the *safest* route (Coins.ph) has the least liquid, most
> rate-limited API. The *best* APIs belong to platforms the PH SEC has flagged. That
> tension is the real decision you're making. My suggestion: **paper trade on Binance
> public data (no account needed, no legal exposure), then deploy real money on the
> regulated local venue** once your strategy survives contact with reality.

---

## 3. Three ways to build it — choose your level

### Route 1 — No-code / hosted (fastest, least control)
3Commas, Cryptohopper, Pionex grid bots, exchange-native "copy trading" and grid bots.

- **Pros:** running in 30 minutes, mobile UI, no server.
- **Cons:** monthly USD subscription (often more than your profits), you can't see or
  test the logic, "AI" branding is usually marketing, and **copy trading means you inherit
  someone else's drawdown at the exact moment they blow up.** Also: connecting these to an
  SEC-flagged exchange compounds your risk.
- **Verdict:** fine for a simple grid/DCA bot. Not how you learn anything durable.

### Route 2 — Open-source self-hosted (best value for a technical person)
**Freqtrade** is the mature default (~55k GitHub stars): Python strategies, built-in
backtesting, hyperopt, dry-run mode, Telegram control, FreqUI dashboard, works with ccxt.
Alternatives: **Hummingbot** (market making), **Jesse**, **OctoBot**, **Passivbot**.

```bash
# On a $5-10/mo VPS or your own machine
git clone https://github.com/freqtrade/freqtrade
cd freqtrade && ./setup.sh -i          # interactive installer
source .venv/bin/activate
freqtrade create-userdir --userdir user_data
freqtrade new-config --config user_data/config.json   # set "dry_run": true
freqtrade new-strategy --strategy MyFirstStrategy
freqtrade download-data --exchange binance --pairs BTC/USDT ETH/USDT -t 5m 1h --days 400
freqtrade backtesting --strategy MyFirstStrategy --timerange 20250101-
freqtrade trade --dry-run --strategy MyFirstStrategy   # paper trade, live prices
```

- **Pros:** free, battle-tested, huge community, dry-run mode is exactly what you need.
- **Cons:** Python required; TA-Lib install can be annoying; it will not make you money
  by itself — it's an execution+testing harness, not an edge.

### Route 3 — Build your own agent (what's in this repo)
You own every line. Best for learning, and the only way to get an LLM in the loop safely.
Start here, graduate to Freqtrade for serious backtesting infrastructure, or stay.

---

## 4. What is in this repo

```
ph-crypto-agent/
├── run.py                    # CLI: selftest, backtest, cycle, loop, status, brief, journal, reset
├── config/
│   ├── config.example.json   # every knob, documented inline
│   └── config.json           # YOURS (gitignored — create it from the example)
├── agent/
│   ├── indicators.py         # SMA, EMA, RSI, ATR — pure stdlib, unit-tested
│   ├── datafeed.py           # public REST candles (Binance/Bybit fallback), optional ccxt
│   ├── strategy.py           # TrendPullback: long-only, ATR stops. EDIT THIS.
│   ├── risk.py               # ★ the guardrails. Read it. Can only reject or shrink.
│   ├── broker.py             # PaperBroker (sim fills w/ fees+slippage) & LiveBroker (ccxt)
│   ├── agent.py              # the loop: exits first, then entries, then risk gate
│   ├── backtest.py           # no-lookahead backtester (signal on close, fill next open)
│   └── llm.py                # optional LLM: briefing / journal / code audit — NO order access
└── data/                     # state.json, trades.csv, backtest results
```

**Zero mandatory dependencies.** Python 3.9+ and an internet connection. `ccxt` is only
needed for live trading. That means it runs on a ₱300/mo VPS, a Raspberry Pi, or a
free-tier container.

### Try it right now (5 minutes, no account, no money)

```bash
cd ph-crypto-agent
python3 run.py selftest                          # verify indicators + every risk guardrail
python3 run.py backtest --limit 1000             # strategy vs buy & hold on real history
python3 run.py cycle                             # ONE paper cycle on live prices
python3 run.py status                            # your simulated portfolio
python3 run.py loop                              # keep running (Ctrl-C to stop)
```

Expected first-run backtest output looks like this (BTC/ETH/SOL, 1h, a down-choppy period):

```
BTC/USDT  strategy return : -1.34%   (buy & hold: -11.79%)   max DD 1.59%   30 trades
ETH/USDT  strategy return : -1.51%   (buy & hold: -22.99%)   max DD 1.72%   29 trades
SOL/USDT  strategy return : -1.69%   (buy & hold: -28.98%)   max DD 2.10%   27 trades
```

**That is a good result, and I want you to notice why.** The strategy *lost* money — but it
lost 1.5% while the assets fell 12–29%. It kept max drawdown near 2% by sitting in cash.
This is what risk management actually looks like: not making money in a downtrend, but
surviving it with almost no damage. Any bot that shows you +400% in a backtest is either
curve-fit or lying.

The default strategy is a *starting point*, not an edge. Your job is to replace
`strategy.py` with something you understand and have tested across regimes.

### The guardrails, concretely

Every one of these is enforced in code and verified by `selftest`:

1. **Position size comes from risk, not conviction.** `size = (equity × 1%) ÷ distance-to-stop`,
   then capped at 10% of equity. A stronger signal can make a trade *smaller*, never bigger.
2. **No stop loss → no trade.** Physically refused.
3. **Max 3 open positions.** Concentration kills accounts.
4. **Daily loss limit (4%)** → flat for the rest of the UTC day.
5. **Total drawdown halt (15%)** → stops until a human calls `human_reset()`.
6. **4 consecutive losses** → halt. The strategy is probably out of regime.
7. **Kill switch:** `touch KILL` in the project dir and the agent refuses to trade.
   Delete the file to resume. Panic button that works even if the code is confused.
8. **Exits are always processed before entries** in every cycle.
9. **Paper by default.** Live mode requires `"mode": "live"` in config *and*
   `I_UNDERSTAND_I_CAN_LOSE_MONEY=yes` in the environment *and* real API keys.
10. **All fees + slippage modelled** in both paper and backtest.

### Where the LLM fits (if you want one)

Set `llm.enabled: true`, export `LLM_API_KEY`, then:

```bash
python3 run.py brief      # plain-English briefing on regime + risk flags
python3 run.py journal    # reads your trades.csv and critiques your PROCESS
```

Both return text. Neither can place an order — there is no code path from `llm.py` to
`broker.py`, and you should keep it that way. Two more high-value LLM uses:

- **Code audit:** point an LLM at your own `strategy.py` and ask it to hunt for look-ahead
  bias, off-by-one errors and unhandled exceptions (`llm.code_audit`). Cheap, catches real bugs.
- **Coding assistant:** use an LLM to *write and test* the bot. That's where AI genuinely
  compresses months of work into days. Just verify every line — an LLM-written strategy
  with a subtle look-ahead bug will produce a gorgeous backtest and lose money live.

---

## 5. Going live — a staged plan, not a leap

**Stage 0 — Backtest honestly (week 1).** Run `backtest` across 3+ symbols, 2+ timeframes,
and at least one full bull/bear cycle (`--limit 1000` on `4h` or `1d` for longer history).
Then change one parameter by 20%. If results collapse, you were curve-fitting.

**Stage 1 — Paper trade (weeks 2–5).** `python3 run.py loop` on a VPS with Telegram alerts.
Goal is **not** profit. Goal is: does it run 500 cycles without crashing? Does it handle
API timeouts, restarts, and a weekend gap? Do the stops actually fire? Compare live paper
results to your backtest — a big gap means your backtest is wrong.

**Stage 2 — Live with money you can burn (weeks 6+).**
- Amount: **₱5,000–₱15,000 (~$90–$270)** or less. Enough to feel real, small enough that
  losing all of it changes nothing in your life. Never rent money, never use credit,
  never touch emergency funds or 529/tuition money.
- Run it for a full month at that size before adding a peso.

**Stage 3 — Scale only with evidence.** Add capital in steps, only after 3+ months of live
results that match your backtest within reason.

### API key security — non-negotiable checklist

- [ ] **Withdrawals DISABLED.** Always. This one setting is the difference between a bad
      week and an empty account. A trading bot never needs to withdraw.
- [ ] **IP allowlist** = only your server's static IP.
- [ ] Keys in **environment variables** (`EXCHANGE_API_KEY`, `EXCHANGE_API_SECRET`) or a
      secrets manager — **never** committed to git, never pasted into chat, never in a
      screenshot.
- [ ] Separate **sub-account** with only the funds the bot needs, not your main balance.
- [ ] Server: SSH keys only (no password), `ufw`/security group allowing only SSH,
      automatic security updates, no other services running.
- [ ] Back up your state files and trade CSV off-box.
- [ ] **Never give any bot, "support agent", or website your seed phrase or wallet key.**
      No legitimate trading bot ever asks. This is the #1 way Filipinos lose crypto to scams.

### Common scams targeting people who want trading bots (all very active in PH)

- Telegram/Discord/FB "signals + bot" groups showing screenshots of profit → they profit
  from your subscription and your referral link.
- "Guaranteed 1-3% daily" / arbitrage bots that need you to deposit into *their* platform →
  Ponzi. If returns are promised, it's not trading.
- Fake exchange apps and clone sites with working-looking dashboards → funds never existed.
- "AI trading" Chrome extensions / npm / pip packages that steal keys on install →
  audit anything you install, pin versions, read the source.
- Someone DMs offering to "set it up for you" for a share of profits → they get your keys.

Rule of thumb: **if the edge were real, they would trade it silently with their own money
instead of selling it to you for ₱2,500/month.**

---

## 6. Realistic costs (monthly, PH)

| Item | Cost | Notes |
|---|---|---|
| VPS (DigitalOcean/Hetzner/Vultr/AWS free tier) | ₱0–600 ($0–10) | 1 vCPU / 1GB is plenty for this kit |
| Exchange trading fees | 0.1% per side typical | A bot trading 60×/month on ₱50k ≈ ₱600 in fees. **Fees are the #1 killer of small strategies** |
| LLM API (optional, briefings only) | ₱0–500 | Not needed for the trading loop at all |
| Market data | ₱0 | Public candles are free and sufficient |
| Your time | Large | The real cost. Budget 20–40 hours to get competent |

---

## 7. A 30-day plan

| Days | Do this |
|---|---|
| 1–2 | Run `selftest`, `backtest`, `cycle`. Read `risk.py` line by line until you can explain every rule |
| 3–5 | Set up Coins.ph (or your chosen venue), complete KYC, deposit a **small** amount, place 3 manual trades to learn the fee/slippage reality |
| 6–10 | Modify `strategy.py`. Break it. Fix it. Backtest each version. Keep a written log of what you changed and why |
| 11–14 | Deploy on a VPS with `systemd` + Telegram alerts, paper mode |
| 15–30 | Let it run untouched. Review weekly with `python3 run.py journal`. Count crashes, missed cycles, and any divergence from your backtest |
| 30 | Decide: is the *process* working? Only then consider Stage 2 |

---

## 8. Decision tree

```
Can you write/modify Python?
├─ No  → Route 1 (hosted grid/DCA bot on a regulated venue), OR
│        hire someone, OR learn Python first (2-3 months, free).
│        Do NOT buy a "signals + AI bot" subscription.
└─ Yes → Do you want to learn how this actually works?
         ├─ Yes → Route 3 (this repo) for 2 weeks, then Route 2 (Freqtrade)
         │        for serious infrastructure. Use an LLM as your pair programmer
         │        and code auditor — that's the real "AI agent" win.
         └─ No, I just want automation → Route 2 (Freqtrade dry-run → live small)
```

---

## 9. Sources / verify before you act

- SEC PH — Rules on Crypto-Asset Service Providers, MC No. 4 & 5 s.2025 (effective 5 July 2025):
  `cryptoslate.com/crypto-laws/philippines-sec-casp-rules-guidelines-2025/`, `bitpinas.com/regulation/sec-crypto-rules-date/`
- SEC PH advisory naming OKX/Bybit/MEXC/KuCoin/Bitget (Aug 2025):
  `sg.finance.yahoo.com/news/philippines-sec-cracks-down-unregistered-140948520.html`,
  `taxcalculator.com.ph/crypto/sec-named-platforms`, `kkinvesting.io/en/posts/bybit-exchange-guide-philippines/`
- Binance blocked 2023–24, sandbox re-entry 2026:
  `reuters.com/technology/philippines-sec-block-access-worlds-largest-crypto-exchange-binance-2023-11-29/`,
  `binance.com/en/square/post/327305288790881`
- PH privacy-coin listing ban (June 2026): `binance.com/en/square/post/334627550761642`
- Legality overview: `respicio.ph/commentaries/are-cryptocurrency-exchanges-legal-in-the-philippines`
- Coins.ph REST API docs (spot trading, API keys, SDKs): `docs.coins.ph/rest-api/`
- PDAX licensing / partners API: `pdax.ph/caas/`, `docs.ripple.com/.../connecting-to-digital-asset-exchanges`
- Freqtrade: `freqtrade.io`, `github.com/freqtrade/freqtrade`

Regulation moves fast. Re-check SEC and BSP announcements before you commit real funds —
and confirm your tax treatment with a Philippine accountant.

---

## 10. Final word

You do not need an AI to trade crypto for you. You need (1) a rules-based strategy you
understand completely, (2) a risk layer that makes blowing up your account arithmetically
hard, (3) months of paper trading, and (4) the discipline to keep the size small.

AI's real value here is as your **analyst, pair programmer and code auditor** — not your
portfolio manager. Build it in that order and you'll be ahead of 95% of the people who
search "AI crypto trading bot Philippines".

*Kabuhayan mo, desisyon mo. Start on paper.*
