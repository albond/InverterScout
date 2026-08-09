.PHONY: install lint format test test-cov run showcase docker-build

PYTHON ?= python

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests

format:
	$(PYTHON) -m ruff check --fix src tests
	$(PYTHON) -m ruff format src tests

test:
	$(PYTHON) -m pytest

test-cov:
	$(PYTHON) -m pytest --cov=inverterscout --cov-report=term-missing

run:
	$(PYTHON) -m inverterscout

showcase:
	$(PYTHON) -m inverterscout.devtools.showcase --port 2301

docker-build:
	docker compose build
