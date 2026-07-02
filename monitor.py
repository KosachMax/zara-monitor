"""Entrypoint for Zara Stock Monitor.

The implementation lives in the `zara_monitor` package. Public imports are
re-exported here for backwards compatibility with existing tests/scripts that
import `monitor` directly.
"""

from __future__ import annotations

import asyncio

from zara_monitor import *  # noqa: F403
from zara_monitor.app import main

if __name__ == "__main__":
    asyncio.run(main())
