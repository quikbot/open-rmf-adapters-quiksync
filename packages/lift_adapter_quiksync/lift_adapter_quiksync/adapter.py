"""QuikSync lift adapter entry point — v1 stub.

In v1 this binary compiles but does nothing. The real implementation lands
in v2. Exposed in the v1 release so docker-compose configs don't need to
special-case "fleet only" — the lift adapter container starts, logs a
deferred-to-v2 notice, and idles.
"""

from __future__ import annotations

import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lift_adapter_quiksync")


def main(argv: list[str] | None = None) -> int:
    log.info(
        "lift_adapter_quiksync: v1 stub — lift adapter ships in v2. "
        "Idling so the container doesn't exit-loop in docker-compose."
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("shutdown requested")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
