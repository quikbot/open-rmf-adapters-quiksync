"""Smoke test: package imports without error.

Pinned at v1 scaffold so  has at least one passing test per
package. Per-package real tests land alongside the implementation.
"""

def test_import():
    import quiksync_client
    assert quiksync_client.__version__
