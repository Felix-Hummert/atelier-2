"""The one bounded, thread-safe, process-lifetime cache every content-addressed
memoization owner shares (#937 round 3, extracted as its own owner round 4)."""

from __future__ import annotations

from atelier2.application.bounded_process_cache import BoundedProcessCache


def test_a_remembered_key_answers_on_every_later_lookup() -> None:
    cache: BoundedProcessCache[str, str] = BoundedProcessCache(capacity=2)

    cache.remember("a", "value-a")

    assert cache.found("a") == "value-a"
    assert cache.found("b") is None


def test_a_new_key_past_capacity_is_never_remembered_but_existing_keys_still_answer() -> (
    None
):
    """Round 4 review finding 3: the cache silently declines a new key once its
    capacity is spent, rather than storing whatever it is given. This pins that
    admission policy as observable behavior, not just the docstring's word."""
    cache: BoundedProcessCache[str, str] = BoundedProcessCache(capacity=1)
    cache.remember("a", "value-a")

    cache.remember("b", "value-b")

    assert cache.found("a") == "value-a", (
        "a key remembered before the cap filled up still answers"
    )
    assert cache.found("b") is None, (
        "a key offered after the cap filled up is never remembered"
    )
