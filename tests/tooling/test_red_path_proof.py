from __future__ import annotations


def test_ci_red_path_proof_must_fail() -> None:
    assert False, (
        "deliberate red-path proof for issue #10 - this failure is the success "
        "condition"
    )
