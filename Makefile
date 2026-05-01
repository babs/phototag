.PHONY: install install-all lint test test-slow run clean js-build js-watch

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

# JS bundle: static/src/*.js -> static/ui.js (single ES2020 IIFE).
# Falls back to a friendly install hint when esbuild isn't on PATH yet.
js-build:
	@if [ ! -x node_modules/.bin/esbuild ] && ! command -v esbuild >/dev/null 2>&1; then \
		echo "esbuild not found — run 'npm install' first (esbuild is a devDependency in package.json)."; \
		exit 1; \
	fi
	npx esbuild static/src/main.js --bundle --outfile=static/ui.js --target=es2020 --format=iife --legal-comments=none

js-watch:
	@if [ ! -x node_modules/.bin/esbuild ] && ! command -v esbuild >/dev/null 2>&1; then \
		echo "esbuild not found — run 'npm install' first (esbuild is a devDependency in package.json)."; \
		exit 1; \
	fi
	npx esbuild static/src/main.js --bundle --outfile=static/ui.js --target=es2020 --format=iife --legal-comments=none --watch
