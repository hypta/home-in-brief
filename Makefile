VENV ?= .venv
PY ?= $(VENV)/bin/python
EMU_HOST ?= localhost:8085
BASE ?= http://localhost:8080
SEED_ENV = FIRESTORE_EMULATOR_HOST=$(EMU_HOST) GOOGLE_CLOUD_PROJECT=local-dev
RATES_PP = PYTHONPATH=jobs/rates-refresh:shared

.PHONY: setup test test-b test-property-api test-web up seed rates rates-fetch kb smoke down local-api local-rates local-kb

# One-time: create the venv (Python 3.12) and install every service's dependencies plus pytest.
# Needs uv (https://docs.astral.sh/uv/). Without uv: python3.12 -m venv .venv, then .venv/bin/pip install
# -r <each requirements.txt> pytest
setup:
	uv venv --python 3.12 $(VENV)
	uv pip install --python $(PY) -r services/rules-api/requirements.txt -r jobs/rates-refresh/requirements.txt -r jobs/kb-refresh/requirements.txt -r services/property-api/requirements.txt -r services/web/requirements.txt pytest

# Each service has its own top-level `app` package, so suites run in separate pytest processes.
test: test-b test-property-api test-web

test-b:
	$(PY) -m pytest -q services/rules-api jobs/rates-refresh jobs/kb-refresh kb

test-property-api:
	cd services/property-api && ../../$(PY) -m pytest tests -q

test-web:
	cd services/web && ../../$(PY) -m pytest tests -q

# --- Docker + Firestore emulator ---
up:
	docker compose up -d --build firestore rules-api

seed:
	$(SEED_ENV) $(PY) scripts/seed_firestore.py
	$(SEED_ENV) $(PY) scripts/seed_rates.py

rates:
	docker compose --profile jobs run --rm --build rates-refresh

rates-fetch:
	$(RATES_PP) $(PY) -m rates_refresh --fetch-only

kb:
	docker compose --profile jobs run --rm --build kb-refresh

smoke:
	$(PY) scripts/smoke.py $(BASE)

down:
	docker compose --profile jobs down --remove-orphans

# --- No Docker: plain .venv, local files ---
local-api:
	DATA_BACKEND=local PYTHONPATH=shared:services/rules-api $(PY) -m uvicorn app.main:app --port 8080

local-rates:
	set -a; [ -f .env ] && . ./.env; set +a; DATA_BACKEND=local $(RATES_PP) $(PY) -m rates_refresh

local-kb:
	DATA_BACKEND=local $(PY) jobs/kb-refresh/main.py
