# TrainHub — commandes de développement.
SHELL := /bin/bash
UV ?= uv
NPM ?= npm
PORT ?= 8420

export TRAINHUB_PORT := $(PORT)

.PHONY: help install dev dev-local dev-sim backend backend-local backend-sim web test test-py test-web e2e lint lint-py lint-web typecheck format build build-web image fake-train-image sim-up sim-down sim-keys openapi clean

help:  ## Affiche cette aide
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Installe les dépendances Python et front
	$(UV) sync
	cd web && $(NPM) install

# -- développement --------------------------------------------------------------------

backend:  ## Backend seul en mode fake (rechargement auto)
	TRAINHUB_EXECUTOR=fake TRAINHUB_INVENTORY=dev/inventory.dev.yaml TRAINHUB_DATA_DIR=.trainhub/dev \
	  $(UV) run trainhub serve --reload

backend-local:
	TRAINHUB_EXECUTOR=local TRAINHUB_INVENTORY=dev/inventory.local.yaml TRAINHUB_DATA_DIR=.trainhub/local \
	  $(UV) run trainhub serve --reload

backend-sim:
	TRAINHUB_EXECUTOR=ssh TRAINHUB_INVENTORY=dev/inventory.sim.yaml TRAINHUB_DATA_DIR=.trainhub/sim \
	  $(UV) run trainhub serve --reload

web:  ## Front seul (Vite + HMR, proxifie /api, /ws et /mcp)
	cd web && $(NPM) run dev

dev:  ## Backend fake + Vite HMR, une seule commande
	@trap 'kill 0' EXIT; $(MAKE) --no-print-directory backend & $(MAKE) --no-print-directory web & wait

dev-local: fake-train-image  ## Idem en mode local (Docker local)
	@trap 'kill 0' EXIT; $(MAKE) --no-print-directory backend-local & $(MAKE) --no-print-directory web & wait

dev-sim: sim-up  ## Idem avec les machines SSH simulées (docker compose)
	@trap 'kill 0' EXIT; $(MAKE) --no-print-directory backend-sim & $(MAKE) --no-print-directory web & wait

# -- qualité ------------------------------------------------------------------------------

test: test-py test-web  ## Tous les tests (Python + front)

test-py:  ## Tests Python (pytest)
	$(UV) run pytest

test-web:  ## Tests front (Vitest)
	cd web && $(NPM) run test

e2e: build-web  ## Parcours Playwright (mode fake)
	cd web && $(NPM) run e2e

lint: lint-py lint-web  ## ruff, mypy, eslint/tsc

lint-py:
	$(UV) run ruff check src tests dev
	$(UV) run ruff format --check src tests dev
	$(UV) run mypy

lint-web:
	cd web && $(NPM) run lint && $(NPM) run typecheck

format:  ## Formate le code
	$(UV) run ruff format src tests dev
	$(UV) run ruff check --fix src tests dev
	cd web && $(NPM) run format

openapi:  ## Régénère web/src/lib/api-types.ts depuis l'OpenAPI
	$(UV) run python -m trainhub.api.export_openapi web/openapi.json
	cd web && npx openapi-typescript openapi.json -o src/lib/api-types.ts

# -- build -------------------------------------------------------------------------------

build-web:  ## Compile le front (web/dist)
	cd web && $(NPM) run build

build: build-web  ## Front + paquet Python
	$(UV) build

image:  ## Image Docker de TrainHub
	docker build -t trainhub:latest .

fake-train-image:  ## Image de test trainhub-fake-train (mode local / sim)
	docker build -t trainhub-fake-train dev/fake-train

sim-keys:  ## Génère la clé SSH de test des machines simulées
	./dev/gen-ssh-key.sh

sim-up: sim-keys fake-train-image  ## Démarre les machines SSH simulées
	docker compose -f dev/docker-compose.yml up -d --build

sim-down:  ## Arrête les machines SSH simulées
	docker compose -f dev/docker-compose.yml down

clean:
	rm -rf .trainhub web/dist dist .pytest_cache .mypy_cache .ruff_cache
