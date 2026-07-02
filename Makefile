PYTHON ?= python
CSV ?= runway_sample.csv
IMAGE ?= pab-pricer

.PHONY: install price web clean start stop restart docker-build docker-price

# --- native (host Python) ---

install:
	$(PYTHON) -m pip install -r requirements.txt

price:
	$(PYTHON) -m pab_pricer.cli --input "$(CSV)"

web:
	$(PYTHON) -m uvicorn webapp.main:app --reload --port 8000

clean:
	rm -rf outputs/*.csv

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
