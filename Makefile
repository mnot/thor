PY_TESTS=test/test_*.py

all:
	@echo "make dist to 1) push and tag to github, and 2) upload to pypi."

# for running from IDEs (e.g., TextMate)
.PHONY: run
run: test


##########################################################################################
## Tasks

.PHONY: tidy
tidy: venv
	$(VENV)/black thor test

.PHONY: lint
lint: venv
	PYTHONPATH=$(VENV) $(VENV)/pylint --output-format=colorized --rcfile=test/pylintrc thor

.PHONY: typecheck
typecheck: venv
	PYTHONPATH=$(VENV) $(VENV)/python -m mypy --config-file=test/mypy.ini thor

.PHONY: clean
clean:
	rm -rf build dist MANIFEST thor.egg-info .venv
	find . -type f -name \*.pyc -exec rm {} \;
	find . -d -type d -name __pycache__ -exec rm -rf {} \;


##########################################################################################
## Tests

.PHONY: test
test:
	PYTHONPATH=.:$(VENV) $(VENV)/pytest test


#############################################################################
## Distribution

.PHONY: version
version: venv
	$(eval VERSION=$(shell $(VENV)/python -c "import thor; print(thor.__version__)"))

.PHONY: build
build: clean venv
	$(VENV)/python -m build

.PHONY: upload
upload: build typecheck test version
	git tag thor-$(VERSION)
	git push
	git push --tags origin
	$(VENV)/python -m twine upload dist/*


include Makefile.venv
