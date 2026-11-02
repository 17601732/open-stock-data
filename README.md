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
| Breadth (advancers 5d, above MA60, 52w high-low) | Sample pool | Pool scope, not full market |
| TRIN | `trin_2020_now.csv` (existing full-market script) | Reused as-is, 2020-01-02 onward; falls back to pool estimate if the CSV is missing |
| PCR | SSE option daily stats (510050 + 510300) | Real range only, **2019-12-23** start, no forward fill |
| Net holder increase | Eastmoney announcements | 2014 onward, weekly cumulative 20 weeks, unit = 10k shares |
| Trend (year-line bias, 20d vol) | HS300 index | Full range |

All series are standardized with a rolling 10-year percentile (no look-ahead) and combined with
configurable weights in `scripts/radar/config.py`; `top>=85`, `bottom<=15`.

See [`RADAR.md`](RADAR.md) for the full methodology, backtest design (in-sample vs out-of-sample,
2015/2018/2024 out-of-distribution periods) and known limitations.

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
