"""One scripted tracker answer for scenarios that run no tracker."""

from __future__ import annotations

from dataclasses import dataclass, field

from atelier2.contracts.effect_requests import HeadBranch
from atelier2.ports.effects import (
    HeadBranchPullRequestState,
    NoPullRequestOpenOnHeadBranch,
)


@dataclass
class FakeHeadBranchPullRequests:
    """Answers every branch the same way, and keeps which branches were asked."""

    answer: HeadBranchPullRequestState = field(
        default_factory=NoPullRequestOpenOnHeadBranch
    )
    asked: list[HeadBranch] = field(default_factory=list)

    def open_pull_requests_on(
        self, head_branch: HeadBranch
    ) -> HeadBranchPullRequestState:
        self.asked.append(head_branch)
        return self.answer
