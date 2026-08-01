.PHONY: test lint format api cli
test:
	pytest
lint:
	ruff check .
format:
	ruff format .
api:
	PYTHONPATH=apps/api:packages/core:packages/runtime uvicorn alios_api.main:app --reload
cli:
	PYTHONPATH=apps/cli:packages/core python -m alios_cli.main doctor
