PROJECT=thor
GITHUB_STEP_SUMMARY ?= throwaway


##########################################################################################
## Tests

.PHONY: test
test:
	PYTHONPATH=.:$(VENV) $(VENV)/pytest --md $(GITHUB_STEP_SUMMARY) --workers auto test
	rm -f throwaway

.PHONY: test/*.py
test/*.py:
	PYTHONPATH=.:$(VENV) $(VENV)/pytest $@


#############################################################################
## Tasks

.PHONY: cli
cli: venv
	PYTHONPATH=$(VENV) $(VENV)/pip install .
	PYTHONPATH=$(VENV):. sh

.PHONY: clean
clean:
	find . -d -type d -name __pycache__ -exec rm -rf {} \;
	rm -rf build dist MANIFEST $(PROJECT).egg-info .venv .mypy_cache *.log

.PHONY: tidy
tidy: venv
	$(VENV)/black $(PROJECT) test

.PHONY: lint
lint: venv
	PYTHONPATH=$(VENV) $(VENV)/pylint --output-format=colorized $(PROJECT)

.PHONY: typecheck
typecheck: venv
	PYTHONPATH=$(VENV) $(VENV)/python -m mypy $(PROJECT)

.PHONY: loop_type
loop_type:
	PYTHONPATH=$(VENV) $(VENV)/python -c "import thor.loop; print(thor.loop._loop.__class__)"

#############################################################################
## Distribution

.PHONY: version
version: venv
	$(eval VERSION=$(shell $(VENV)/python -c "import $(PROJECT); print($(PROJECT).__version__)"))

.PHONY: build
build: clean venv
	$(VENV)/python -m build

.PHONY: upload
upload: build test version venv
	git tag $(PROJECT)-$(VERSION)
	git push
	git push --tags origin
	$(VENV)/python -m twine upload dist/*


include Makefile.venv
