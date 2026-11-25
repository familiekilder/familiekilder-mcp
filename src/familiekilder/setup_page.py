"""Constants shared by the local setup wizard and the public setup page."""

from __future__ import annotations

# The public page asks this one port, so it must never change once released.
SETUP_PORT = 28417
PAGES_ORIGIN = "https://familiekilder.github.io"
PAGES_URL = f"{PAGES_ORIGIN}/familiekilder-mcp/oppsett/"
STATUS_PATH = "/status"
STATUS_APP = "familiekilder-mcp"
STATUS_PROTOCOL = 1
