
# Release Instructions

1. bump `thor.__version__` in `thor/__init__.py`.
2. git tag `"thor-%s" % thor.__version__` 
3. push tag to server
4. python setup.py sdist upload
