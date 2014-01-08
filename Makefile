
version=$(shell python -c "import thor; print thor.__version__")

all:
	@echo "make dist to 1) push and tag to github, and 2) upload to pypi."

# for running from IDEs (e.g., TextMate)
.PHONY: run
run: test

.PHONY: dist
dist: test
	git tag thor-$(version)
	git push --tags origin
	python setup.py sdist upload

.PHONY: test
test:
	cd test; make
