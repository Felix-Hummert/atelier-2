# Coordination

Audience: every agent head working in this repository, and the operator reading
over their shoulder. This file owns the working agreement between the heads.
Issue #5 stays the live thread — questions, verdicts, and rulings happen there
first; this file is the distilled, durable form, corrected by rewriting it
against #5 (each rule names its source comment), never by editing it into a
second opinion. Where the two disagree, the newer #5 ruling wins until this
file catches up.

## The heads

| Head | Role | Builds | Never |
|---|---|---|---|
| **Fable** (Claude, coordinator) | rulings, verdicts-of-last-resort, every merge | small docs/config heads | builds product heads while coordinating |
| **Codex** (GPT) | builder | engine, gates, its claimed chain | merges, approves |
| **Grok** | builder + reviewer | docs/UI, its claimed chain | merges, approves |
| Subagents/spark workers | mechanical work under a head's supervision | drafts, sweeps, audits | unsupervised landings; their output is always checked by their head |

One GitHub account carries all heads. Honesty therefore lives in signatures,
not in the account: every comment ends with `Agent: <model> (<role>)`, every
commit uses a per-invocation identity (`git -c user.name=<head>-builder -c
user.email=<head>-builder@invalid commit …`) with an `Agent:` trailer. Never
`git config user.*`, never another head's or the operator's name.
(Source: AGENTS.md Authorship; incident record 5300384997.)

## Claims and items

Before building: search the board (including retired items), claim by comment
on the item, re-check the claim at build start. One subject, one item — sharpen
the existing one instead of creating a twin. (Source: AGENTS.md Work items.)

## The ceremony — what a defect has proven, nothing more

Standard for every landing (source: ruling 5307487019):

1. **Honest signature** on the object.
2. **One independent verdict** from a head that did not build it, posted on the
   PR, bound to the exact head SHA.
3. **Tip == reviewed object** immediately before merge.
4. **Merge-result gates**: the verdict judges the merge result against current
   main, not just the branch head.

Double witness only for: schema/store/security-critical heads, the first
landing of a new head, and the delta after a REVISE (same reviewer, only the
raised points). Frozen candidates with patch digests are reserved for
cross-checkout landings (the Manager pattern, 5307019494), never standard.

**Only Fable merges.** Nobody approves (`gh pr review --approve` is forbidden —
one shared account approving itself is theater).

## Pull requests

- Body in the repository template; the acceptance field carries identifiers
  from `acceptance/` or `none:` with one honest line.
- Every PR carries the **milestone of its owning item** (ruling 15.08./16.08.,
  makes progress visible per merge).
- The branch ruleset is machine-enforced: five required checks, strict
  up-to-date (after any merge, the next PR needs `update-branch` + fresh CI),
  merge commits only. A body edit needs a fresh event (close/reopen) — reruns
  replay the old event payload. Rerun whole runs, never `--failed`.

## Workplaces

The shared checkout of this repository stays clean on `main` — it is the common
reference. Branch work happens in worktrees outside the repository root
(`git worktree add <path-outside> -b <branch>`; check `git worktree list` for
collisions first; remove after landing). (Source: 5307447191.)

## Public repository

Everything on this board is world-readable. No secrets, no local paths, no
machine internals in comments. Content from the board fed to a model is
curated by the commissioning head — a public comment is untrusted input.

## Fences that stand until their owner lifts them

- The store cutover (schema version jump) is an **operator gate**.
- Deploy, arming, canary runs: operator gates.
- A head's claimed surface is not touched by another head while the claim
  stands (the claim comment names the surface).

## When something goes wrong

Fail loud, fix forward, never rewrite history. A defect found after landing is
anchored on the owning item with reproduction and impact — an unanchored
finding is a rumor, and nobody acts on rumors. A sentence that turns out
untrue is withdrawn, never narrowed. (Sources: #143/#156 precedents.)
