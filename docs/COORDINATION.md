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
| **Fable** (Claude) | coordinator, reviewer, merger | none | builds heads while coordinating |
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

### A stacked head has four states, and only the last one releases its child

`BUILT` → `CI GREEN` → `REVIEW PASS` → `LANDED ON MAIN`. Green is not
landable and a passed review is not landed: a child head stays **FROZEN**
until its parent is on `main`, because a parent that moves after the child
was measured makes the child's proof a statement about an object nobody will
merge.

Fable's pre-check before a verdict therefore reads the object, not the
promise: `baseRefName == main`, the exact head and tree, CI on that exact
head, and the merge result recomputed against the `main` of that moment.
(Sources: #179 stacked-PR rule, #182 frozen behind it.)

Verdicts are the one thing a head may not give itself. When the reviewer a
head is waiting for is bound elsewhere past a documented threshold, another
head may take the verdict as a **fleet fallback** — and says so in the
verdict, naming who was waited for and how long. A head that repairs an
object stops being that object's independent reviewer and says that too.
(Sources: fleet fallback exercised on #174 and #179, 16.08.)

### Updating a head and rebinding its body is one handgrip

The pull-request body is the landing binding the acceptance gate reads, so a
body describing an older head is a false record at the moment it lands. Rebind
it in the same move that updates the branch — head, tree, merge result,
measurements — because the update is the only event that makes the gate read
the body again.

Answer the acceptance field in the form the template shows; a body the gate
cannot read costs a red run and a fresh event, never a rerun.

## Reading your own tools

A gate that is read wrongly is worse than one that was never run, because the
head then carries a claim nobody checked.

- **Read a gate's whole output, never its last line.** `ruff check .` prints its
  findings before the summary, so `| tail -1` can report success over real
  errors.
- **Never `git checkout -- <path>` to undo a mutation.** It restores from the
  index, so unstaged work in that file is discarded silently; keep a copy and
  restore from it, and stage finished work so an accident cannot reach it.
- **Verify a claim you are about to repeat.** Recompute the fingerprint, run the
  probe, diff the two heads — a review that forwards a builder's number has
  checked nothing.

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

- Build and isolated proof are free; after proof, replacing the prototype store is free; only unattended arming remains an **operator gate**. (Sources: #63 comments 5307533025 and 5307545004.)
- Deploy, arming, canary runs: operator gates.
- A head's claimed surface is not touched by another head while the claim
  stands (the claim comment names the surface).

## When something goes wrong

Fail loud, fix forward, never rewrite history. A defect found after landing is
anchored on the owning item with reproduction and impact — an unanchored
finding is a rumor, and nobody acts on rumors. A sentence that turns out
untrue is withdrawn, never narrowed. (Sources: #143/#156 precedents.)
