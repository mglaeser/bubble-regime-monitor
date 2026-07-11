.PHONY: install dev lint type test run build up sensitivity

install:
	pip install .

dev:
	pip install -e ".[dev]"

lint:
	ruff check app tests scripts

type:
	mypy app

test:
	pytest -q

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000

build:
	podman build -t bubblegauge -f Containerfile .

up:
	podman-compose up -d

sensitivity:
	python scripts/sensitivity.py
