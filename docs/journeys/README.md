# Journeys

Audience: a human or an agent who wants to see how a requirement feels on the
surfaces that exist today.

This directory owns the journeys. A journey **illustrates** one or more
requirement sentences. It **binds nothing**. Acceptance sentences and their
tests bind. If a journey and a requirement disagree, the requirement document
is the closer reading, and the issue behind it still wins.

## Convention

- Each journey is a short narrated walk of a path that has landed on `main`.
- Each journey names the `REQ-…` identifiers it illustrates, and only
  identifiers that already exist in `docs/requirements/`.
- A journey does not invent a requirement, a surface, a control, or a next
  step. If the proof ran through a door the cockpit does not yet show, the
  journey says so.
- The optional `Journeys:` field on a requirement sentence may point here. An
  empty field is honest. Filling it does not bind the sentence.

## Index

- [Start a named run](start-a-named-run.md) — picker, named agent, material,
  start, live graph
- [A run waits for a person](a-run-waits-for-a-person.md) — `WAITING_INPUT`,
  the operator answers, the run ends
