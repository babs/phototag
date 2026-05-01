.PHONY: install install-all lint test test-slow run clean

install:
	uv sync

install-all:
	uv sync --extra all

lint:
	pre-commit run --all-files

test:
	uv run pytest

test-cov:
	uv run pytest --cov=phototag --cov-report=term-missing --cov-report=xml --cov-report=html

test-slow:
	uv run pytest -m slow

run:
	uv run phototag --help

clean:
	rm -rf __pycache__ .mypy_cache .pytest_cache .ruff_cache .coverage htmlcov
