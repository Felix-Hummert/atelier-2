"""One bounded, thread-safe, process-lifetime cache, shared by every owner
that memoizes a content-addressed answer for the rest of this process's life.

#937 round 3 gave a parsed workflow revision this shape, keyed by its
content hash, so every reader after the first pays a dict lookup instead of
reparsing an immutable published document. Round 4 gives a validated schema
document the same shape. Both keys are the content hash of bytes that cannot
change once published, so a value found once never goes stale for the rest
of the process's life -- there is nothing to invalidate, only a capacity to
respect so a pathological amount of distinct keys cannot grow this cache
without bound: once that capacity is spent, `remember` silently declines a
new key -- every lookup for it stays a miss and the owner recomputes it on
every call -- while every key already remembered keeps answering for the
rest of the process.

`found` and `remember` each hold the lock only for their own dict access, not
across whatever the caller does between them. Two callers racing on the same
still-uncached key can therefore both recompute and both `remember`: that is
allowed on purpose rather than serialized, because every value this cache is
asked to hold is a pure function of the key's own immutable content, so a
duplicate computation can never disagree with the first -- it only repeats
work one unlucky race already decided to pay, which costs less than making
every unrelated key's lookup wait behind one key's computation.

Whether a failed or refused answer is worth remembering at all is each
owner's own decision, never this cache's: an owner that calls `remember`
only for its accepted outcomes keeps a transient failure retryable instead of
turning it into process-lifetime durable-state corruption.
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
