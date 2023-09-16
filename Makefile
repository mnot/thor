PROJECT=thor
GITHUB_STEP_SUMMARY ?= throwaway


##########################################################################################
## Tests

.PHONY: test
test: venv
	PYTHONPATH=.:$(VENV) $(VENV)/pytest --md $(GITHUB_STEP_SUMMARY) --workers auto test
	rm -f throwaway

.PHONY: test/*.py
test/*.py: venv
	PYTHONPATH=.:$(VENV) $(VENV)/pytest $@


#############################################################################
## Tasks

.PHONY: cli
cli: venv
	PYTHONPATH=$(VENV) $(VENV)/pip install .
	PYTHONPATH=$(VENV):. sh

.PHONY: clean
clean: clean_py

.PHONY: tidy
tidy: tidy_py

.PHONY: lint
lint: lint_py

.PHONY: typecheck
typecheck: typecheck_py

.PHONY: loop_type
loop_type:
	PYTHONPATH=$(VENV) $(VENV)/python -c "import thor.loop; print(thor.loop._loop.__class__)"

#############################################################################
## Distribution

.PHONY: upload
upload: build test version venv
	git tag $(PROJECT)-$(VERSION)
	git push
	git push --tags origin
	$(VENV)/python -m twine upload dist/*


include Makefile.pyproject
