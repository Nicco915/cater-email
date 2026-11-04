"""Explicit socket event loop for the Windows local administration server.

CATER uses one web worker and no asyncio subprocesses. The selector loop avoids
Proactor socket-shutdown callbacks raising WinError 10054 when a local HTTP
client resets a connection. Other exceptions remain visible; no exception
handler is suppressed or monkey-patched.
"""
from __future__ import annotations

import asyncio


def new_server_loop() -> asyncio.AbstractEventLoop:
    """Uvicorn custom loop factory (resolved by import string)."""
    return asyncio.SelectorEventLoop()
