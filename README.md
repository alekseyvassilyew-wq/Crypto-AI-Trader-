# Crypto AI Trader — Kraken PAPER release

A single-instance Kraken Spot market scanner and PAPER-trading service for Render. It reads Kraken public market data, optionally verifies the private API key using the read-only balance endpoint, evaluates a transparent indicator strategy, simulates fills locally, and provides owner-only Telegram controls.

## Safety boundary

This archive is a **PAPER-only release**. It is not a live trading bot and must not be treated as ready to place real orders. Changing `TRADING_MODE` to `LIVE` is rejected at startup. LIVE execution needs a separate implementation and a separate verification phase; it is intentionally not enabled here.

- **PAPER only.** `TRADING_MODE` must be `PAPER`; startup rejects every other value.
- This release has **no exchange order-placement, withdrawal, deposit, transfer, or funding code**. The API key is used only for the private balance read (`/0/private/Balance`).
- The API key may have `Query Funds` for the startup read-only account check; it does not need `Create & modify orders` or `Cancel/close orders` for this release. The private balance check does not fund or synchronize the virtual PAPER wallet. Disable trading and withdrawal permissions on Kraken for this PAPER build.
- No leverage, derivatives, margin, borrowing, shorting, or withdrawals.
- PAPER fills are simplified estimates, not a prediction of real execution or profitability.

## Included

- Kraken Spot public ticker and OHLC data (REST API).
- Optional private account-balance verification using HMAC-SHA512 signing.
- Dynamic full-market scanner: discovers all online Kraken Spot crypto markets and retrieves their tickers in one request; candle analysis rotates through the configured per-cycle budget across liquid markets with a valid EUR valuation route.
- Multi-currency PAPER cash, FX conversion, and positions persisted in SQLite.
- PAPER buys are simulated at the ticker ask and sells at the ticker bid (spread-aware), with stop checks based on the executable bid; includes stop-loss, take-profit and trailing-stop exits, score-ranked entries, per-trade cap, max-position cap, and daily equity-loss pause.
- Fee- and slippage-aware single-market and multi-market portfolio historical backtests with next-candle-open fills, conservative stop/target ordering, net P/L, profit factor, win rate and maximum drawdown; available as Telegram `/backtest SYMBOL` and `/backtest portfolio`.
- Strictly increasing private API nonces within the running process; malformed API responses are rejected.
- Persistent SQLite state and a safe paused-on-restart behavior.
- Telegram owner-only commands: `/start`, `/help`, `/status`, `/scan`, `/account`, `/portfolio`, `/interval`, `/run`, `/pause`, `/stop`.
- Render Background Worker blueprint with Persistent Disk mounted at `/var/data`.

## Kraken API key setup

Set these Render environment variables (never commit them to GitHub):

- `KRAKEN_API_KEY`
- `KRAKEN_API_SECRET`

For this release, only the Kraken **Query Funds** permission is used for the account-balance check. Public ticker/OHLC endpoints do not require a key. Do not enable withdrawal, deposit, trading, staking/earn, or export permissions for this PAPER build. If you already created a broader key, edit its permissions in Kraken and remove unused permissions. Query Funds is optional and is used only for a read-only balance check at service startup; Telegram `/account` shows the separate virtual PAPER wallet.

IP restriction can remain disabled for this first connection test. This means the key is not bound to a Render egress IP; protect the secret carefully and consider IP restrictions later if a stable outbound IP is configured.

## Deploy on Render

1. Upload the contents of this ZIP to the GitHub repository's `main` branch (the repository root must contain `render.yaml`, `requirements.txt`, and `src/`).
2. In Render, create a Blueprint from that repository, or create a Background Worker manually.
3. Confirm the Persistent Disk is mounted at `/var/data`. Keep the service to **one instance** because SQLite is local to that disk.
4. Set `BOT_TOKEN` and numeric `ADMIN_ID`.
5. Set `KRAKEN_API_KEY` and `KRAKEN_API_SECRET` as secret environment variables.
6. Keep `TRADING_MODE=PAPER`. Deploy, then message the Telegram bot with `/start`, `/status`, `/account`, and `/scan`.
7. The bot starts paused after every restart. Use `/run` only when you want the local paper simulator to run; `/pause` or `/stop` halts simulated actions. All online Kraken Spot pairs are monitored by ticker. Candle analysis rotates across liquid pairs with a valid EUR valuation route; PAPER entries may use quote currencies funded from the multi-currency virtual wallet through simulated FX conversions.

The declared Render worker plan and Persistent Disk may incur charges. Review Render's current pricing before applying the Blueprint.

## Local setup and tests

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # set Telegram values and Kraken credentials
pytest -q
python -m src.trader.main
```

## Default PAPER settings

- Initial simulated cash: €20
- Max simulated buy: €5
- Max daily simulated equity loss: €2
- Max open positions: 2
- Market monitoring: all online Kraken Spot pairs (auto-discovered hourly)
- Indicator/OHLC analysis: up to 10 liquid markets per scan with a valid EUR valuation route; the rotating cursor spreads coverage across the universe
- PAPER entries: markets with an available EUR valuation route; quote cash may be funded through simulated FX conversions. Initial USD/USDT balances are virtual allocations, not copies of the real Kraken wallet
- Scan interval: 60 seconds
- Simulated fee estimate: 0.26%
- Stop loss: 2%; take profit: 4%

These are configurable simulation assumptions, not recommendations. Verify Kraken's current minimum order sizes, pair availability, fees, and API behavior before any future live-trading implementation.

## Known limitations

- No LIVE mode, order placement, or demo-order execution is implemented in this ZIP. Do not change the mode variable expecting real trades.
- Strategy is deterministic technical-indicator logic, not a trained AI/ML model; it has not been demonstrated profitable. The private Kraken balance check is read-only and does not import real holdings into PAPER. The PAPER wallet is a separate simulation. `/backtest SYMBOL` evaluates up to 720 available closed 1-hour candles for one market. `/backtest portfolio` evaluates at most five liquid markets on shared timestamps; it uses aligned Kraken EUR FX candles when a direct EUR pair exists; if historical FX data is unavailable, it falls back to the current rate and identifies that currency in the report. Neither command is evidence of future profitability.
- Kraken market tickers are fetched for the complete online Spot universe. To keep REST usage controlled, OHLC indicators are calculated for at most `MAX_CANDLE_PAIRS` (default 10) eligible liquid pairs per cycle; the scan cursor rotates this budget. Markets without a valid EUR valuation route are monitored but cannot be considered for entries. A passing unit test suite does not verify live connectivity or permissions on your Kraken account.
- `/interval` changes the in-memory scan interval until service restart; it does not reconfigure Render scheduling.
- A single SQLite-backed worker is intended. Do not scale to multiple instances.
