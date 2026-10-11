# TrainHub — development commands.
SHELL := /bin/bash
UV ?= uv
NPM ?= npm
PORT ?= 8420

export TRAINHUB_PORT := $(PORT)

.PHONY: help install dev dev-local dev-sim backend backend-local backend-sim web test test-py test-web e2e lint lint-py lint-web typecheck format build build-web image fake-train-image sim-up sim-down sim-keys openapi zip clean

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Install Python and front-end dependencies
	$(UV) sync
	cd web && $(NPM) install

# -- development ----------------------------------------------------------------------

backend:  ## Backend only in fake mode (auto reload)
	TRAINHUB_EXECUTOR=fake TRAINHUB_INVENTORY=dev/inventory.dev.yaml TRAINHUB_DATA_DIR=.trainhub/dev \
	  $(UV) run trainhub serve --reload

backend-local:
	TRAINHUB_EXECUTOR=local TRAINHUB_INVENTORY=dev/inventory.local.yaml TRAINHUB_DATA_DIR=.trainhub/local \
	  $(UV) run trainhub serve --reload

backend-sim:
	TRAINHUB_EXECUTOR=ssh TRAINHUB_INVENTORY=dev/inventory.sim.yaml TRAINHUB_DATA_DIR=.trainhub/sim \
	  $(UV) run trainhub serve --reload

web:  ## Front end only (Vite + HMR, proxies /api, /ws and /mcp)
	cd web && $(NPM) run dev

dev:  ## Fake backend + Vite HMR, a single command
	@trap 'kill 0' EXIT; $(MAKE) --no-print-directory backend & $(MAKE) --no-print-directory web & wait

dev-local: fake-train-image  ## Same in local mode (local Docker)
	@trap 'kill 0' EXIT; $(MAKE) --no-print-directory backend-local & $(MAKE) --no-print-directory web & wait

dev-sim: sim-up  ## Same with the simulated SSH machines (docker compose)
	@trap 'kill 0' EXIT; $(MAKE) --no-print-directory backend-sim & $(MAKE) --no-print-directory web & wait

# -- quality ------------------------------------------------------------------------------

test: test-py test-web  ## All tests (Python + front end)

test-py:  ## Python tests (pytest)
	$(UV) run pytest

test-web:  ## Front-end tests (Vitest)
	cd web && $(NPM) run test

e2e: build-web  ## Playwright flows (fake mode)
	cd web && $(NPM) run e2e

lint: lint-py lint-web  ## ruff, mypy, eslint/tsc

lint-py:
	$(UV) run ruff check src tests dev scripts
	$(UV) run ruff format --check src tests dev scripts
	$(UV) run mypy

lint-web:
	cd web && $(NPM) run lint && $(NPM) run typecheck

format:  ## Format the code
	$(UV) run ruff format src tests dev scripts
	$(UV) run ruff check --fix src tests dev scripts
	cd web && $(NPM) run format

zip:  ## Zip the repository without ignored files (../trainhubX.Y.Z.zip)
	$(UV) run python scripts/make_zip.py

openapi:  ## Regenerate web/src/lib/api-types.ts from the OpenAPI
	$(UV) run python -m trainhub.api.export_openapi web/openapi.json
	cd web && npx openapi-typescript openapi.json -o src/lib/api-types.ts

# -- build -------------------------------------------------------------------------------

build-web:  ## Build the front end (web/dist)
	cd web && $(NPM) run build

build: build-web  ## Front end + Python package
	$(UV) build

image:  ## TrainHub Docker image
	docker build -t trainhub:latest .

fake-train-image:  ## trainhub-fake-train test image (local / sim mode)
	docker build -t trainhub-fake-train dev/fake-train

sim-keys:  ## Generate the test SSH key of the simulated machines
	./dev/gen-ssh-key.sh

sim-up: sim-keys fake-train-image  ## Start the simulated SSH machines
	docker compose -f dev/docker-compose.yml up -d --build

sim-down:  ## Stop the simulated SSH machines
	docker compose -f dev/docker-compose.yml down

clean:
	rm -rf .trainhub web/dist dist .pytest_cache .mypy_cache .ruff_cache
