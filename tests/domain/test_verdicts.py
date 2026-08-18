"""The verdict vocabulary and the contract an answer says it under, held together.

A verdict is the one thing a node produces that decides which edge runs next, so
two questions have to have the same answer: which words this product owns, and
which words the schema an answer is judged by admits. They are one owner here,
and these tests are where a second opinion would die.
"""

from __future__ import annotations

import pytest

from atelier2.contracts.schemas_v3 import (
    InstanceAccepted,
    SchemaAccepted,
    read_instance_document,
    read_schema_document,
)
from atelier2.contracts.verdicts import (
    VERDICT_ANSWER_SCHEMA,
    Verdict,
    VerdictUnreadable,
    read_verdict,
)


@pytest.fixture(scope="module")
def contract() -> SchemaAccepted:
    """The published contract, read by the same owner a run reads it with."""
    schema = read_schema_document(VERDICT_ANSWER_SCHEMA.document)
    assert isinstance(schema, SchemaAccepted)
    return schema


def answer(verdict: str) -> bytes:
    return f'{{"verdict":"{verdict}"}}'.encode()


@pytest.mark.proves("the-verdict-vocabulary-and-its-published-contract-are-one-owner")
@pytest.mark.parametrize("verdict", tuple(Verdict), ids=[str(v) for v in Verdict])
def test_every_verdict_this_product_owns_is_admitted_by_its_own_contract(
    verdict: Verdict, contract: SchemaAccepted
) -> None:
    """The vocabulary is the schema's source, so no token can be unpublishable."""
    judged = read_instance_document(answer(verdict.value), contract)

    assert isinstance(judged, InstanceAccepted)
    assert read_verdict(answer(verdict.value)) is verdict


@pytest.mark.proves("the-verdict-vocabulary-and-its-published-contract-are-one-owner")
@pytest.mark.parametrize(
    "written",
    (
        b'{"verdict":"refused"}',
        b'{"verdict":"accepted","reason":"and here is why"}',
        b'{"reason":"nothing about a verdict"}',
        b'"accepted"',
    ),
    ids=(
        "a word this build cannot honour",
        "an answer carrying something nobody reads",
        "an answer naming no verdict",
        "an answer that is no object",
    ),
)
def test_an_answer_this_contract_does_not_admit_never_reaches_an_edge(
    written: bytes, contract: SchemaAccepted
) -> None:
    """The seam every output passes is where each of these ends.

    That matters most for the second case: the reader takes only the member it
    needs, so an answer carrying more than a verdict is refused by the contract
    and by nothing else. One owner of what an answer may be, rather than a
    reader growing an opinion of its own.
    """
    assert not isinstance(read_instance_document(written, contract), InstanceAccepted)


@pytest.mark.proves("the-verdict-vocabulary-and-its-published-contract-are-one-owner")
@pytest.mark.parametrize(
    "written",
    (
        b'{"verdict":"refused"}',
        b'{"reason":"nothing about a verdict"}',
        b'"accepted"',
        b"not a document at all",
    ),
    ids=(
        "a word this build cannot honour",
        "an answer naming no verdict",
        "an answer that is no object",
        "bytes that are no document",
    ),
)
def test_bytes_carrying_no_verdict_are_named_rather_than_raised_over(
    written: bytes,
) -> None:
    """Reaching the reader with these means the store and the contract disagree.

    It cannot happen while the schema above is what judged the bytes, and it is
    still the loud boundary it looks like -- because the alternative is choosing
    an edge out of an answer nobody could read.
    """
    with pytest.raises(VerdictUnreadable):
        read_verdict(written)
