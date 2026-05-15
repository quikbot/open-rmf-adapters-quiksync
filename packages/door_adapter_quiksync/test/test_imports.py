"""Smoke test: package imports cleanly and exposes a non-empty `__version__`."""

def test_import():
    import door_adapter_quiksync
    assert door_adapter_quiksync.__version__
