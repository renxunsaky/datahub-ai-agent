# Local development entry points. Requires uv (https://docs.astral.sh/uv/).
UV ?= uv

.PHONY: install lint format type arch test coverage check

install:
	$(UV) sync

lint:
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

format:
	$(UV) run ruff check --fix src tests
	$(UV) run ruff format src tests

type:
	$(UV) run mypy src tests

arch:
	$(UV) run lint-imports

test:
	$(UV) run pytest -q

# Coverage gate for the security-critical packages (>= 90 %).
coverage:
	$(UV) run pytest -q --cov=data_navigator.sqlproc --cov=data_navigator.starburst \
		--cov-report=term-missing --cov-fail-under=90

check: lint type arch coverage
