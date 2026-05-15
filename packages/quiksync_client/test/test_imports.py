"""Smoke test: package imports cleanly and exposes a non-empty `__version__`."""

def test_import():
    import quiksync_client
    assert quiksync_client.__version__
