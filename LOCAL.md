# Local testing (B: rules-api, rates-refresh, kb-refresh)

Run everything from the repo root. `make test` runs all unit tests with the `.venv` (Python 3.12).

## One-time: create the virtualenv

```bash
make setup    # uv venv --python 3.12 .venv, then installs every service's requirements.txt plus pytest
```

Without `uv`: `python3.12 -m venv .venv`, then `.venv/bin/pip install -r services/rules-api/requirements.txt -r jobs/rates-refresh/requirements.txt -r jobs/kb-refresh/requirements.txt pytest`.
(`PY=/path/to/python` overrides the interpreter that every target uses.)

## One-time: your own `.env`

`rates-refresh` calls Gemini (only for banks whose terms allow automated fetching; currently none, so it fetches nothing and rates are updated by hand, see `RATES_UPDATE.md`). To try it, create `.env` yourself (it is gitignored):

```bash
cp .env.example .env
# edit .env and set GEMINI_API_KEY=...   (GEMINI_MODEL is optional: leave it commented out to use the code default)
```

With an allowed bank configured but no key, `rates` / `local-rates` fail loudly (`KeyError: 'GEMINI_API_KEY'` recorded as that bank's failure, non-zero exit when every attempted bank failed) and write no rates snapshot. A `job_runs` record of the failed run is still written.

## Mode 1: pure Python (no Docker)

```bash
make test          # all unit tests
make local-api     # uvicorn on :8080, DATA_BACKEND=local (reads kb/seed + data/rates_seed.json)
make smoke         # in another shell: /healthz + /v1/rules, expects loan 880000, lvr 88.0, dti 6.07
make rates-fetch   # fetch pages of tos_status=allowed banks only (none today); no Gemini, no writes
make local-rates   # full rates-refresh (needs .env); skips non-allowed banks; output in jobs/rates-refresh/out/
python scripts/validate_rates_seed.py   # check the hand-maintained data/rates_seed.json
make local-kb      # kb-refresh; prints quote_found per KB item, report in out/
```

## Mode 2: Docker + Firestore emulator

Needs the Docker daemon running.

```bash
make up      # build + start firestore emulator (:8085) and rules-api (:8080, DATA_BACKEND=firestore)
make seed    # write kb seed + data/rates_seed.json into the emulator (runs on the host via .venv)
make smoke   # same check as above, now served from Firestore
make kb      # kb-refresh job in Docker; writes last_check into the emulator
make rates   # rates-refresh job in Docker (reads GEMINI_API_KEY from .env)
make down    # stop everything
```

Notes:
- `smoke` before `seed` returns empty rates/KB; always `seed` after a fresh `up` (emulator data is not persisted).
- The jobs are in compose profile `jobs`; `make kb` / `make rates` use `docker compose run`.
- `smoke` takes `BASE=http://host:port` to point at another instance.
