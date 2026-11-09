def pytest_configure(config):
    config.addinivalue_line("markers", "slow: takes more than ~1 s (starts a local server); excluded by -m \"not slow\"")
