"""Scheduler worker process for proactive monitors."""
from __future__ import annotations

import asyncio
import logging

from proagent_os.db import get_db
from proagent_os.observability import shutdown_observability
from proagent_os.proactive.scheduler import run_worker_forever
from proagent_os.services import get_services


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    get_db()
    get_services()
    try:
        asyncio.run(run_worker_forever(poll_seconds=15))
    finally:
        shutdown_observability()


if __name__ == "__main__":
    main()
