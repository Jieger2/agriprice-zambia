def pytest_addoption(parser):
    parser.addoption("--env", action="store", default="dev", help="environment whose outputs to test")
