# AGENTS.md

This repository contains the KRX market-monitoring project under [kospi_v3_krx/README.md](kospi_v3_krx/README.md). Treat that folder as the active app unless a task clearly targets deployment or repo-level automation files.

## Project shape

- Application code: [kospi_v3_krx/02_engine/](kospi_v3_krx/02_engine/)
- Backtesting: [kospi_v3_krx/03_backtest/](kospi_v3_krx/03_backtest/)
- Data providers: [kospi_v3_krx/04_data/providers/](kospi_v3_krx/04_data/providers/)
- Legacy reference code: [kospi_v3_krx/99_archive/](kospi_v3_krx/99_archive/)
- Deployment entrypoint: [kospi_v3_krx/deploy.sh](kospi_v3_krx/deploy.sh)
- Runtime config: [kospi_v3_krx/requirements.txt](kospi_v3_krx/requirements.txt)

## Working model

- The app is a Streamlit dashboard for monitoring KOSPI/market conditions and signals; it does not execute live trades by itself.
- Real data is fetched through provider adapters in [kospi_v3_krx/04_data/providers/](kospi_v3_krx/04_data/providers/). The default path is KRX first, with mock fallback if all providers fail.
- Keep data-source failures explicit: the UI is designed to surface provider errors and fallback reasons instead of silently hiding them.
- Follow the existing naming and folder conventions: `02_engine`, `03_backtest`, `04_data`, and `99_archive`.

## Run and validate

Use the project’s documented commands, not ad-hoc scripts:

- Install dependencies:
  - `pip install -r kospi_v3_krx/requirements.txt`
- Run the app locally:
  - `python -m streamlit run kospi_v3_krx/02_engine/kospi_engine_v3_realtime.py`
- Deploy or rollback:
  - `cd kospi_v3_krx && ./deploy.sh deploy stable`
  - `cd kospi_v3_krx && ./deploy.sh rollback`
  - `cd kospi_v3_krx && ./deploy.sh status`
- Validate before release:
  - `python3 kospi_v3_krx/03_backtest/dry_run_test.py --cycles 240 --interval 15`

## Architecture and guardrails

- [kospi_v3_krx/02_engine/kospi_engine_v3_realtime.py](kospi_v3_krx/02_engine/kospi_engine_v3_realtime.py) is the main runtime entrypoint and contains the live signal engine.
- [kospi_v3_krx/02_engine/burst_early_warning.py](kospi_v3_krx/02_engine/burst_early_warning.py) contains the burst-warning logic. The current accepted rule is the explicit two-condition gate: compressed market condition plus a burst in volume. Keep that rule consistent if you change burst logic.
- The deploy script enforces safety checks around market hours and active PID management; do not bypass those expectations unless the task explicitly requires it.
- `.env` files are expected for KRX credentials; keep them out of version control.
- Prefer minimal, targeted changes to the existing engine flow and provider interfaces.

## Editing expectations

- When adding or updating a provider, preserve the provider contract used by the engine and keep `fetch_tick()` and `source_note()` behavior consistent.
- If you modify the burst logic, document the reason in code comments and preserve the final-gate logic described in [kospi_v3_krx/README.md](kospi_v3_krx/README.md).
- If you add new dependencies, update [kospi_v3_krx/requirements.txt](kospi_v3_krx/requirements.txt) instead of relying on hidden environment state.
- Keep safety notes intact: this project is an analysis/decision-support tool, not a live execution system for direct order placement.

## Useful references

- [kospi_v3_krx/README.md](kospi_v3_krx/README.md)
- [kospi_v3_krx/deploy.sh](kospi_v3_krx/deploy.sh)
- [kospi_v3_krx/04_data/providers/base.py](kospi_v3_krx/04_data/providers/base.py)
- [kospi_v3_krx/04_data/providers/krx_provider.py](kospi_v3_krx/04_data/providers/krx_provider.py)
