.PHONY: install run seed test lint typecheck check reset demo demo-test

install:
	uv sync

run:
	uv run flask --app fundifinder run --debug

seed:
	uv run flask --app fundifinder seed

reset:
	rm -rf instance
	uv run flask --app fundifinder seed

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

check: lint typecheck test

demo:
	python3 demo/build.py
	python3 -m http.server 8950 -d demo/_site

demo-test:
	python3 demo/build.py
	cd demo && npm ci && npx playwright install chromium && npm test
