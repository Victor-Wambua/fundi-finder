.PHONY: install run seed test lint typecheck check reset

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
