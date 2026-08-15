"""Where the local service listens, and where a client reaches it.

Serving and running are the two sides of one default: change where the server
binds and the run command must follow, or an operator who ran `serve` with no
arguments cannot run anything on it. They live here, and not with the server,
because reading this pair must not cost the client the whole server graph.
"""

from __future__ import annotations

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8422
DEFAULT_SERVICE_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
