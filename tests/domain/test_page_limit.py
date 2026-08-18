from __future__ import annotations

import pytest

from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS, PageLimit


def test_a_page_limit_is_an_integer_inside_the_owned_bound() -> None:
    assert PageLimit(1).value == 1
    assert PageLimit(MAXIMUM_PAGE_ITEMS).value == MAXIMUM_PAGE_ITEMS
    with pytest.raises(ValueError, match="from 1 to 100"):
        PageLimit(0)
    with pytest.raises(ValueError, match="from 1 to 100"):
        PageLimit(MAXIMUM_PAGE_ITEMS + 1)
    with pytest.raises(ValueError, match="from 1 to 100"):
        PageLimit(True)  # type: ignore[arg-type]


@pytest.mark.proves("a-persisted-bound-is-written-once-and-derived-everywhere")
def test_the_page_bound_is_written_once() -> None:
    assert MAXIMUM_PAGE_ITEMS == 100
