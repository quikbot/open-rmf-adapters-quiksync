"""Smoke test: package imports cleanly and exposes a non-empty `__version__`."""

def test_import():
    import fleet_adapter_quiksync
    assert fleet_adapter_quiksync.__version__
