"""InverterScout process entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from inverterscout.security.logging import sensitive_values, stdout_handler
from inverterscout.settings.wizard import run_setup_wizard
from inverterscout.storage.encrypted import setup_is_complete


def main() -> None:
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        handlers=[stdout_handler(log_format, sensitive_values())],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if not setup_is_complete():
        asyncio.run(run_setup_wizard())

    from inverterscout.storage.encrypted import load_settings

    timezone = str(load_settings().get("timezone", "UTC"))
    os.environ["TZ"] = timezone
    if hasattr(time, "tzset"):
        time.tzset()

    from inverterscout.interfaces.telegram import main as run_application

    try:
        run_application()
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("InverterScout stopped")


if __name__ == "__main__":
    main()
