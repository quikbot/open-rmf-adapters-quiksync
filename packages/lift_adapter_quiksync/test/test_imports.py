"""Smoke test: package imports cleanly and exposes a non-empty `__version__`."""

def test_import():
    import lift_adapter_quiksync
    assert lift_adapter_quiksync.__version__
