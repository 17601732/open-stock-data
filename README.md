# open-stock-data

[![CI](https://github.com/irachex/open-stock-data/actions/workflows/ci.yml/badge.svg)](https://github.com/irachex/open-stock-data/actions/workflows/ci.yml)
[![Update Symbols](https://github.com/irachex/open-stock-data/actions/workflows/update-symbols.yml/badge.svg)](https://github.com/irachex/open-stock-data/actions/workflows/update-symbols.yml)
[![Update CN Bars](https://github.com/irachex/open-stock-data/actions/workflows/update-bars-cn.yml/badge.svg)](https://github.com/irachex/open-stock-data/actions/workflows/update-bars-cn.yml)
[![Update HK Bars](https://github.com/irachex/open-stock-data/actions/workflows/update-bars-hk.yml/badge.svg)](https://github.com/irachex/open-stock-data/actions/workflows/update-bars-hk.yml)
[![Update US Bars](https://github.com/irachex/open-stock-data/actions/workflows/update-bars-us.yml/badge.svg)](https://github.com/irachex/open-stock-data/actions/workflows/update-bars-us.yml)

Daily-updated stock symbols and OHLCV bars for CN, HK, and US markets, powered by GitHub Actions.

## Markets & Exchanges

| Market | Exchanges | Symbols Source | Bars Source |
|--------|-----------|---------------|-------------|
| CN | SSE, SZSE, BSE | Official exchange APIs | BaoStock (SSE/SZSE) + AKShare (BSE) |
| HK | HKEX | Official HKEX list | AKShare |
| US | NASDAQ, NYSE, AMEX | NASDAQ Screener API | AKShare |

## Data Structure

### Symbols (Git-tracked CSV files)

```
symbols/
  SSE.csv      # Shanghai Stock Exchange
  SZSE.csv     # Shenzhen Stock Exchange
  BSE.csv      # Beijing Stock Exchange
  HKEX.csv     # Hong Kong Exchange
  NASDAQ.csv   # NASDAQ
  NYSE.csv     # New York Stock Exchange
  AMEX.csv     # American Stock Exchange
```

Each CSV has columns: `code, region, name, exchange, type`

### Bars (Published via GitHub Releases)

| Release Tag | Asset | Description |
|-------------|-------|-------------|
| `data-cn-bars` | `cn_bars.parquet` | CN A-share daily bars |
| `data-hk-bars` | `hk_bars.parquet` | HK equity daily bars |
| `data-us-bars` | `us_bars.parquet` | US equity daily bars |

Parquet schema: `date, code, open, high, low, close, volume, amount, turnover`

All prices are **unadjusted** (raw). Users can compute adjusted prices as needed.

## Setup

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync

# Install dev dependencies
uv sync --group dev
```

## Backfill

For initial historical data loading:

```bash
# Backfill CN bars (local only)
uv run python -m scripts.backfill --market cn --start-year 2020 --end-year 2024 --local-only

# Backfill and upload to GitHub Releases
export GITHUB_REPOSITORY=owner/repo
export GITHUB_TOKEN=ghp_xxx
uv run python -m scripts.backfill --market cn --start-year 2020

# Backfill HK / US
uv run python -m scripts.backfill --market hk --start-year 2022 --local-only
uv run python -m scripts.backfill --market us --start-year 2023 --local-only
```

## GitHub Actions

| Workflow | Schedule | Description |
|----------|----------|-------------|
| `update-symbols` | UTC 17:00 Mon-Fri | Fetch symbols from all exchanges |
| `update-bars-cn` | UTC 09:00 Mon-Fri | Fetch CN daily bars after market close |
| `update-bars-hk` | UTC 09:00 Mon-Fri | Fetch HK daily bars after market close |
| `update-bars-us` | UTC 22:00 Mon-Fri | Fetch US daily bars after market close |
| `update-radar` | UTC 10:30 Mon-Fri | A-share top/bottom radar snapshot (see below) |
| `ci` | On push/PR | Lint (ruff) + test (pytest) |

All workflows support `workflow_dispatch` for manual triggering.

## A-Share Top/Bottom Radar Snapshot

`update-radar` runs every trading day at **UTC 10:30 (Beijing 18:30, after close)** and can be
triggered manually (`workflow_dispatch`, inputs `full` / `with_holders`). It fetches incremental
inputs, computes all indicators, the 0-100 composite score and the event-study backtest, then
commits a single snapshot file:

```
data/radar/snapshot.json
```

**Why a raw file on `main` instead of Pages or a Release asset:** the Android app only needs one
stable, read-only, CDN-cached URL with no credentials:

```
https://raw.githubusercontent.com/17601732/open-stock-data/main/data/radar/snapshot.json
```

GitHub Pages would need branch/publishing configuration, and Release assets need an API call that
returns a redirect URL. Raw files are the simplest thing the app can fetch with zero auth on a
public repo. Private repos would require a token with `contents:read`.

The snapshot carries metadata: `data_cutoff`, `generated_at`, per-source coverage (including the
actual PCR start date), missing trading dates, missing indicators and anomaly flags. Raw
Parquet/CSV inputs are **not** committed (see `.gitignore`) — they are kept in the Actions cache
so daily runs only fetch increments and the repository only grows by one ~380 KB JSON per day.

Indicator coverage and update limits:

| Indicator | Source | Snapshot range / note |
|-----------|--------|----------------------|
| Valuation (HS300 PE/PB, all-A PE, ERP) | Legulegu | HS300 PE/PB monthly, forward-filled to daily; all-A PB has no public API and is excluded |
| Sentiment (turnover, amount/float-cap, limit net, margin ratio) | Sample pool + SSE/SZSE margin | Pool = 800 current constituents of HS300+CSI500 (survivorship bias) |
| Breadth (advancers 5d, above MA60, 52w high-low) | Full market via TDX `hsjday.zip` | 2014-01-02 onward, ~5,500 active stocks; per-stock API only as fallback |
| TRIN | TDX full-market breadth | Recomputed daily from the package; `trin_2020_now.csv` only as fallback |
| PCR | SSE option daily stats (510050 + 510300) | Real range only, **2019-12-23** start, no forward fill |
| Net holder increase | Eastmoney announcements | 2014 onward, weekly cumulative 20 weeks, unit = 10k shares |
| Trend (year-line bias, 20d vol) | HS300 index | Full range |

All series are standardized with a rolling 10-year percentile (no look-ahead) and combined with
configurable weights in `scripts/radar/config.py`; `top>=85`, `bottom<=15`.

See [`RADAR.md`](RADAR.md) for the full methodology, backtest design (in-sample vs out-of-sample,
2015/2018/2024 out-of-distribution periods) and known limitations.

### TDX full-market package (replaces per-stock API fetches)

`update-radar` downloads `https://data.tdx.com.cn/vipdoc/hsjday.zip` inside Actions and parses it
directly — no manual Release upload. The site has a JS anti-bot gate; the script sends
`Cookie: __tst_status=...; EO_Bot_Ssid=...` first, then retries without it, and resumes partial
downloads with HTTP Range.

Package layout and `.day` record (verified by an actual download, little-endian, 32 bytes fixed):

| Offset | Type | Field | Encoding |
|--------|------|-------|----------|
| 0 | int32 | date | YYYYMMDD |
| 4 | int32 | open | 0.01 CNY |
| 8 | int32 | high | 0.01 CNY |
| 12 | int32 | low | 0.01 CNY |
| 16 | int32 | close | 0.01 CNY |
| 20 | float32 | amount | CNY |
| 24 | int32 | volume | shares |
| 28 | int32 | reserved | 65536 for stocks, unused |

Measured on a local run (Actions uses the same code path):

| Item | Result |
|------|--------|
| Zip size / download | 525.7 MB / 22 s (cookie path) |
| Parsed stock files | 5,918 (SH/SZ/BJ, strict code-prefix filter; 5,571 active) |
| Parse + aggregation | 124 s (numpy `frombuffer`, vectorized `bincount`) |
| Total wall clock | 165 s (download → parquet write) |
| Detail output | 3,927,371 rows × 5,684 codes (3 years), `tdx_stock.parquet` ≈ 82 MB |
| Breadth output | 3,118 sessions since 2014-01-02, `tdx_breadth.parquet` ≈ 261 KB |

The zip stays in the Actions cache (24 h TTL, `.part` + Range resume) and is **never committed**;
only the slim parquet aggregates (`tdx_breadth.parquet`, `board_members.parquet`) are committed.
The job has `timeout-minutes: 45`, well above the measured 165 s; disk use peaks around 1.4 GB
(zip + extracted members + parquet), under the runner limit.

OHLCV validation against the per-stock interface (20 symbols × 20 recent sessions = 400 records):
open/high/low/close `max_abs_diff = 0.0`, volume/amount ratio = 1.0 on every record.

**Adjustment (复权) handling:** `.day` prices are unadjusted. Same-day shape metrics (upper shadow,
amplitude, volume spike) are unaffected. Cross-day returns / moving averages / N-day cumulative
returns would be contaminated on ex-rights days, so ex-dates are taken from Eastmoney's dividend
calendar (`stock_fhps_em` → 除权除息日; precision and recall ≈ 95% against a manually labeled
sample) and every ex-rights row is flagged (`ex_rights`). Board backtests drop any sample whose
forward window crosses an ex-date instead of silently using unadjusted prices. The gap-heuristic
detector remains only as a fallback when the calendar is unavailable (it scored 0/18 precision,
0/37 recall on the validation sample and should not be trusted alone).

Per-stock API fetches remain behind `meta.stock_source = "api"` and only run when TDX download or
parsing fails; the snapshot always records which path was used.

### Board "volume spike + long upper shadow" risk alert

Parameters (all configurable; defaults in `scripts/radar/pattern_signal.py`):

- Long upper shadow: `upper_shadow / day_range ≥ 0.5` **and** `upper_shadow ≥ 2 × body`.
  0.5 means the candle gave back more than half of its intraday range; 2× body separates a
  rejection from a normal trend candle.
- Volume spike: `volume ≥ 2.0 × MA20(volume)` (prior 20 sessions, shifted by 1 to avoid look-ahead).
- Optional position filter: within 5% of the 60-day high, or up-then-reverse close (off by default).
- Board trigger: ≥3 hits **or** ≥10% of the board's eligible stocks on the same session.

Board membership: Shenwan level-1 industry (31 boards, 4,897 codes, 87.8% of active stocks;
Beijing exchange not covered). Concept/theme membership has no clean point-in-time source, so it is
**not** provided rather than fabricating a mapping. Membership is a current snapshot, not history.

Effectiveness (2023-11-02 → 2026-11-02, 3,643 board signals, excess vs HS300):

| Group | Window | n | Mean | Down share | Max DD | Excess |
|-------|--------|---|------|------------|--------|--------|
| Signal | 5d | 3,615 | +0.34% | 46.7% | -2.58% | +0.09% |
| Signal | 10d | 3,589 | +0.72% | 45.5% | -4.76% | +0.23% |
| Signal | 20d | 3,562 | +1.59% | 44.4% | -7.71% | +0.56% |
| Volume only | 5d | 16,472 | +0.24% | 48.0% | -2.44% | +0.07% |
| Shadow only | 5d | 19,407 | +0.21% | 47.4% | -2.30% | +0.12% |
| All board-days | 5d | 22,413 | +0.22% | 47.6% | -2.34% | +0.11% |

The pattern has **no significant downside predictive power**: forward returns are slightly higher
than the all-board-day baseline, win rates slightly lower, and excess return nearly identical.
It is therefore presented as an attention/risk-review prompt, not a bearish alarm, and should be
down-weighted in any scoring use. The App exposes the volume multiple and board-count thresholds.

## Development

This project follows **TDD** (Test-Driven Development):

1. Define interface (function signatures + type hints + docstrings)
2. Write tests
3. Implement functionality

Before every commit:

```bash
# Lint
uv run ruff check .

# Test
uv run pytest
```

Both must pass before committing.

## License

MIT
