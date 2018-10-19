PYTHON=python3
PYTHONPATH=./
version=$(shell python -c "import thor; print thor.__version__")
PY_TESTS=test/test_*.py

all:
	@echo "make dist to 1) push and tag to github, and 2) upload to pypi."

# for running from IDEs (e.g., TextMate)
.PHONY: run
run: test

.PHONY: dist
dist: test
	git tag thor-$(version)
	git push
	git push --tags origin
	$(PYTHON) -m twine upload dist/*

.PHONY: lint
lint:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pylint --rcfile=test/pylintrc thor

.PHONY: test
test: $(PY_TESTS)

.PHONY: $(PY_TESTS)
$(PY_TESTS):
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) $@

.PHONY: typecheck
typecheck:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m mypy --config-file=test/mypy.ini thor

.PHONY: clean
clean:
	rm -rf build dist MANIFEST
	find . -type f -name \*.pyc -exec rm {} \;
	find . -d -type d -name __pycache__ -exec rm -rf {} \;
