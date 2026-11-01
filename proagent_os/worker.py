"""Scheduler worker process for proactive monitors."""
from __future__ import annotations

import asyncio
import logging

from proagent_os.config import get_settings
from proagent_os.db import get_db
from proagent_os.observability import shutdown_observability
from proagent_os.oidc import require_auth_config
from proagent_os.proactive.scheduler import run_worker_forever
from proagent_os.services import get_services


def prepare() -> None:
    """Fail closed on incomplete OIDC settings, then open the database."""
    require_auth_config(get_settings())
    get_db()
    get_services()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    prepare()
    try:
        asyncio.run(run_worker_forever(poll_seconds=15))
    finally:
        shutdown_observability()


if __name__ == "__main__":
    main()
