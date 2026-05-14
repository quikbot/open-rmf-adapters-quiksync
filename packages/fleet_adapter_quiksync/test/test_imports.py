"""Smoke test: package imports without error.

Pinned at v1 scaffold so  has at least one passing test per
package. Per-package real tests land alongside the implementation.
"""

def test_import():
    import fleet_adapter_quiksync
    assert fleet_adapter_quiksync.__version__
