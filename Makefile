PYTHON=python3
PYTHONPATH=./
version=$(shell PYTHONPATH=$(PYTHONPATH) $(PYTHON) -c "import thor; print(thor.__version__)")
PY_TESTS=test/test_*.py

all:
	@echo "make dist to 1) push and tag to github, and 2) upload to pypi."

# for running from IDEs (e.g., TextMate)
.PHONY: run
run: test

.PHONY: version
version:
	@echo $(version)

.PHONY: dist
dist: clean typecheck test
	git tag thor-$(version)
	git push
	git push --tags origin
	$(PYTHON) setup.py sdist
	$(PYTHON) -m twine upload dist/*

.PHONY: tidy
tidy:
	black thor

.PHONY: lint
lint:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pylint --output-format=colorized --rcfile=test/pylintrc thor

.PHONY: test
test: $(PY_TESTS)

.PHONY: $(PY_TESTS)
$(PY_TESTS):
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) $@ -v

.PHONY: typecheck
typecheck:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m mypy --config-file=test/mypy.ini thor

.PHONY: clean
clean:
	rm -rf build dist MANIFEST thor.egg-info
	find . -type f -name \*.pyc -exec rm {} \;
	find . -d -type d -name __pycache__ -exec rm -rf {} \;
