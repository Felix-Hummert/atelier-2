# Product status

Audience: humans and agents deciding what Atelier currently is. This index owns
the status partition; each linked section owns its subject's landed status.
Requirements own intended behaviour and decisions own technical choices.

## Stage: prototype

Operator ruling 01.09.2026: *"das atelier ist prototyp. wir müssen uns um nix
gedanken machen!"* — *"keine backwards compatibility nötig! atelier prototyp!"*

One live instance, one user, and nothing outside this repository holding
Atelier's data or shapes. A successor therefore owes its predecessor no
transition: a key may be renamed, a store recreated, stored browser state
dropped, a surface simply removed. "Existing data or state would be lost" is
not by itself a defect, and compatibility layers, migration ceremony, and
deprecation paths are not owed. `AGENTS.md` already refuses compatibility
layers without a current caller; the stage names why no such caller exists.

The stage frees the work from ceremony, not from rigour. Correctness,
security, honest failure classes, behavioural tests, clean ownership, and
deleting a predecessor when its successor lands are untouched: a prototype may
be unfinished, it may not lie. Work the operator is doing right now — a
running run, a conversation in progress — is not prototype data; losing it is
a real defect, because he is the user.

## Sections

- [Product intent](product/intent.md)
- [Runtime](product/runtime.md)
- [Workflow execution](product/workflow.md)
- [Interfaces](product/interfaces.md)
- [Operations and command line](product/operations.md)
- [Governance and projects](product/governance.md)
