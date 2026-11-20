.PHONY: test syntax clean all

test:
	python3 -m pytest -v tests/

syntax:
	python3 -m compileall -q topology baselines monitoring routing failure_detection recovery controller traffic experiments common tests

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

all: syntax test
