.PHONY: test lint format
test:
	pytest
lint:
	ruff check .
format:
	ruff format .
