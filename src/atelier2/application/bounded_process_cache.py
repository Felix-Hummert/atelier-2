"""One bounded, thread-safe, process-lifetime cache, shared by every owner
that memoizes a content-addressed answer for the rest of this process's life.

#937 round 3 gave a parsed workflow revision this shape, keyed by its
content hash, so every reader after the first pays a dict lookup instead of
reparsing an immutable published document. Round 4 gives a validated schema
document the same shape. Both keys are the content hash of bytes that cannot
change once published, so a value found once never goes stale for the rest
of the process's life -- there is nothing to invalidate, only a capacity to
respect so a pathological amount of distinct keys cannot grow this cache
without bound. The API can execute reads in a worker pool, so the lock makes
lookup and insertion safe across threads.

Whether a failed or refused answer is worth remembering is each owner's own
decision, never this cache's: an owner that calls `remember` only for its
accepted outcomes keeps a transient failure retryable instead of turning it
into process-lifetime durable-state corruption; this cache stores whatever
it is given and forgets nothing until the process ends.
"""

from __future__ import annotations

import threading
from collections.abc import Hashable


class BoundedProcessCache[Key: Hashable, Value]:
    """Values this process has already computed for a content-addressed key."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._found: dict[Key, Value] = {}
        self._lock = threading.Lock()

    def found(self, key: Key) -> Value | None:
        with self._lock:
            return self._found.get(key)

    def remember(self, key: Key, value: Value) -> None:
        with self._lock:
            if len(self._found) < self._capacity:
                self._found[key] = value
