"""Smoke test: package imports without error.

Pinned at v1 scaffold so  has at least one passing test per
package. Per-package real tests land alongside the implementation.
"""

def test_import():
    import lift_adapter_quiksync
    assert lift_adapter_quiksync.__version__
