.PHONY: install lint format test run dry-run report status test-destinations clean

install:
	python -m pip install -e ".[dev]"

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

test:
	pytest

run:
	map-violation-monitor run

dry-run:
	map-violation-monitor run --dry-run

report:
	map-violation-monitor report --dry-run --force

status:
	map-violation-monitor status

test-destinations:
	map-violation-monitor test-destinations

clean:
	rm -rf build dist src/*.egg-info .pytest_cache .ruff_cache answers
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
