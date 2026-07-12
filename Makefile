.PHONY: install start doctor build test

install:
	./simplex install

start:
	./simplex start

doctor:
	./simplex doctor

build:
	npm --prefix frontend run build

test:
	PYTHONPATH=. .venv/bin/pytest -q
	npm --prefix frontend run lint
	npm --prefix frontend run build
