
version=$(shell python -c "import thor; print thor.__version__")

all:
	@echo "make dist to 1) push and tag to github, and 2) upload to pypi."

.PHONY: dist
dist: test
	git tag thor-$(version)
	git push --tags origin
	python setup.py sdist upload

test:
	cd test; make
