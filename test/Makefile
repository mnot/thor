
python=`which python`
PY_TESTS=test_*.py

.PHONY: test
test: $(PY_TESTS)

.PHONY: $(PY_TESTS)
$(PY_TESTS):
	PYTHONPATH=.:../ $(python) $@

.PHONY: lint
lint:
	PYTHONPATH=../ pylint-2.7 -i y \
	-d C0103,R0801,W0612,W0613 \
	thor

