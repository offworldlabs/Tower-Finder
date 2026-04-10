# Legacy test files use sys.exit() pattern — skip them in pytest collection.
# They still run standalone: python tests/test_custody.py
collect_ignore = ["test_custody.py", "test_integration.py"]
