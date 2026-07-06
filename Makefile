PYTHON ?= python
CSV ?= runway_sample.csv
IMAGE ?= pab-pricer

.PHONY: install install-dev price web clean start stop restart docker-build docker-price \
        test test-unit test-api test-e2e audit docker-test-build docker-test

# --- native (host Python) ---

install:
	$(PYTHON) -m pip install -r requirements.txt

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt
	$(PYTHON) -m playwright install --with-deps chromium

price:
	$(PYTHON) -m pab_pricer.cli --input "$(CSV)"

web:
	$(PYTHON) -m uvicorn webapp.main:app --reload --port 8000

clean:
	rm -rf outputs/*.csv

# --- tests (see plan/results-page-overhaul.md for the staged test strategy) ---
# `make test` is the single command that must cover everything: unit, API,
# and end-to-end browser tests. None of these hit the real lego.com -- the
# pricing fetcher is monkeypatched to a small fake catalog (tests/conftest.py).

test-unit:
	$(PYTHON) -m pytest tests/unit -v

test-api:
	$(PYTHON) -m pytest tests/api -v

test-e2e:
	$(PYTHON) -m pytest tests/e2e -v

test: test-unit test-api test-e2e

audit:
	$(PYTHON) -m pip_audit -r requirements.txt -r requirements-dev.txt

# --- Docker (recommended: avoids host Python/version issues) ---

docker-build:
	docker compose build

start: docker-build
	docker compose up -d

stop:
	docker compose down

restart: stop start

docker-price: docker-build
	docker compose run --rm pab-pricer python -m pab_pricer.cli --input "$(CSV)"

# --- Dockerized tests: same `make test` suite, run inside a container so ---
# --- "works on my machine" isn't a factor. Separate image from the ---
# --- runtime one (Dockerfile) since Playwright/Chromium's OS deps have no ---
# --- place in production. ---

docker-test-build:
	docker build -f Dockerfile.test -t pab-pricer-test .

docker-test: docker-test-build
	docker run --rm pab-pricer-test
