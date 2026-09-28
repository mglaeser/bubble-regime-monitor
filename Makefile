.PHONY: install dev lock lint type test run build up deploy migrate sensitivity

# Installs come from the locks, hash-checked: the image and CI get exactly the
# versions that were tested. `make lock` re-resolves after pyproject.toml changes.
install:
	pip install --require-hashes -r requirements.lock
	pip install --no-deps .

dev:
	pip install --require-hashes -r requirements-dev.lock
	pip install --no-deps -e .

# uv 0.12.13. The existing lock is uv's preference, so re-locking moves only
# what pyproject.toml now demands; add --upgrade-package NAME to move one on purpose.
LOCK_TARGET = --generate-hashes --python-version 3.12 --python-platform x86_64-manylinux_2_28
lock:
	uv pip compile pyproject.toml --extra parquet $(LOCK_TARGET) -o requirements.lock
	uv pip compile pyproject.toml --extra dev -c requirements.lock $(LOCK_TARGET) -o requirements-dev.lock

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

# One-command update & deploy: pull -> build -> migrate -> recreate -> health-check.
deploy:
	./deploy.sh

# Apply DB migrations to head against the local DB_URL (no container).
migrate:
	alembic upgrade head

sensitivity:
	python scripts/sensitivity.py
