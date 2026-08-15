VENV       := .venv
PYTHON     := $(VENV)/bin/python3
PIP        := $(VENV)/bin/pip
PYRIGHT    := $(VENV)/bin/pyright

.PHONY: help install cli temporal worker check clean docker-build docker-run

help:
	@echo "make install      - create .venv, install deps, copy .env.example -> .env"
	@echo "make cli          - run the CLI review (main.py, no Temporal)"
	@echo "make temporal     - start the local Temporal dev server (terminal 1 for the CI path)"
	@echo "make worker       - start the Temporal worker              (terminal 2)"
	@echo "make check        - run pyright across the project"
	@echo "make clean        - remove __pycache__/*.pyc"
	@echo "make docker-build - build temporal_worker.py's image (terraform+snyk+uv baked in)"
	@echo "make docker-run   - run that image, env from .env (needs a reachable Temporal)"

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip -q
	$(PIP) install -r requirements.txt
	@[ -f .env ] || cp .env.example .env
	@echo "Installed. Edit .env with your API keys/tokens, then see README.md for how to run."

cli:
	$(PYTHON) main.py

temporal:
	temporal server start-dev

worker:
	$(PYTHON) temporal_worker.py

check:
	$(PYRIGHT) graph.py main.py temporal_worker.py agents/ ci_graph.py ci_workflow.py trigger_workflow.py

clean:
	find . -name '__pycache__' -type d -exec rm -rf {} +
	find . -name '*.pyc' -delete

docker-build:
	docker build -t agentic-devsecops-worker .

docker-run:
	docker run --rm --env-file .env agentic-devsecops-worker
