# ADR 0018: Provider-bound plugins, neutral roles — how agents, skills, MCP and workflows enter the catalog and reach a run

- Status: PROPOSED 2026-08-25 — awaiting independent review + operator approval.
  This record decides a model. Decision 6 is the only place that says what
  exists; everything else is proposed and claims nothing about the tree.
- Date: 2026-08-25
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
  (files in git are the source of truth; a run configuration pins exactly; no
  credential value in a workflow, prompt, event, receipt, log or API resource)
- Decision authority: [Issue #660](https://github.com/FlexOr2/atelier-2/issues/660),
  whose operator rulings of 25.08.2026 this record formalizes. Two comments carry
  the final model and supersede the earlier ones in that thread:
  [the neutrality correction](https://github.com/FlexOr2/atelier-2/issues/660#issuecomment-5409294913)
  (the role is neutral, not the file; the atelier translates nothing) and
  [the plugin-as-unit ruling](https://github.com/FlexOr2/atelier-2/issues/660#issuecomment-5409299513).
  The rulings on detection paths, the hook mapping and the intake-order review
  stand where they do not contradict those two.
- Depends on: [ADR 0007](0007-catalog-identity.md) (decision 2 — authoring lives
  in operator-owned sources, three acts, no auto-intake; decision 4 — the bytes
  are what a definition says; decision 8 — the admission gate), which this record
  **amends** by adding one kind token (§5); [ADR 0006](0006-node-vocabulary.md)
  (`skills:`/`tools:` are `{ref, revision}`; role, profile and skill are three
  different things); [ADR 0009](0009-runner-trust.md) (§6 — Atelier is never a
  secret-distribution channel; §7 — a binding needs a runner that attests it);
  [ADR 0011](0011-project-isolation.md) (the project store an admission is scoped
  by); [ADR 0017](0017-account-credential-model.md) (the Account a source's
  credential reference points at)
- Names, never decides: [#557](https://github.com/FlexOr2/atelier-2/issues/557)
  (casting), [#66](https://github.com/FlexOr2/atelier-2/issues/66) (agent as a
  Markdown file; its Phase C/D is the configuration→definition→launch seam §6
  orders against), [#659](https://github.com/FlexOr2/atelier-2/issues/659) (the
  catalog window and the file-import doors), [#6](https://github.com/FlexOr2/atelier-2/issues/6)
  (the publish gate), [#8](https://github.com/FlexOr2/atelier-2/issues/8) (the
  scorecard), [#16](https://github.com/FlexOr2/atelier-2/issues/16) (durable
  failure tokens, which every refusal named here must become)

## Context

The operator asked four questions in one sitting: how does the atelier recognize
an agent, a skill, an MCP server or a hook in a foreign repository; can he run a
Codex agent on Claude; is the import unit a file or a whole plugin; and does the
answer stay low-maintenance. The first answer built a **translation layer** —
parse both provider formats into one neutral agent contract, map model names to
tier intents, map provider-only fields onto our capability bounds. His own test
("low-maintenance, working, provider-neutral *and* simple — does the concept
carry?") broke it: every provider field a translation models is a field the
atelier must keep chasing, and the translated result would still be a guess
about a prompt written for another provider. That idea is withdrawn here (see
*Supersedes*).

What made translation look necessary was a real observation: both providers
model an agent as a **composition** — identity, model, tools, skills, MCP
servers and bounds — so an agent is a recipe whose skills and MCP servers are
ingredients that must exist wherever it runs. The observation survives; only its
conclusion changes. The composition does not have to be *understood* by us; it
has to be *complete* where the provider reads it.

ADR 0007 already owns where authored content lives, the three acts that take it
in, and that the bytes are the truth; ADR 0009 owns the trust boundary. What
nothing owns is the unit that enters, what the atelier promises about it, and
where provider-neutrality actually lives. That is this record's gap.

## Decision

### 1. The role is neutral, not the file

Provider-neutrality lives in two places, and in neither of them is it a property
of an imported file:

- **In the workflow.** A node declares a portable `role`, a workflow-generic
  instruction, what the occurrence **gets** (typed inputs, pinned source) and
  what it **may do** (capability, tools, grants). That is ADR 0006's model and
  `docs/VISION.md`'s worker/occurrence sentence, unchanged.
- **In the casting.** At run start a role binds to exactly one
  `AgentConfigurationRevision`, which names the provider, the model, the auth
  profile and the executor (#557's layering: the installation owns Accounts, the
  project casts roles, the workflow declares them).

An imported agent or plugin is therefore **provider-bound, and that is
acceptable**: a Claude Markdown agent runs on Claude, a Codex agent on Codex.
**The atelier translates nothing** — no model-name mapping, no composition
parsing, no neutral mirror of a provider's agent schema. The intake reads only
the **frontmatter minimum** it needs for two jobs of its own: the catalog window
(name, description, provider kind, origin) and reference resolution inside the
package (§2). Everything else travels as bytes and is read by the provider that
wrote the format. A file may propose a model; the casting sets it.

**Switching provider means casting the role with a different agent**, not
porting a file. The catalog shows each agent's provider so the operator knows
the bet he is taking, and #8's scorecard measures which casting performs better:
competition at the role rather than asserted portability. This record makes no
"runs anywhere" claim.

**What the atelier writes neutrally is what it owns** — the conductor and the
house's own core workflows (`host/conductor_workflow.py` is exactly that: a
provider-neutral one-node workflow whose fulfilling provider is a binding
decision). Imported material stays provider-bound, and the two are kept visibly
apart.

### 2. The plugin is the intake unit

A **plugin** is a provider-bound package: its agents, its skills, its MCP
declaration and its manifest, in the layout its provider already expects. It is
taken in **whole** and later rendered **whole** (§4). Nothing is decomposed into
atelier-shaped parts and nothing is reassembled.

Two consequences follow, and both simplify rather than complicate:

- **Reference resolution is package-internal.** An agent referencing a skill or
  an MCP server refers to something in the same package; the intake resolves it
  there and nowhere else. A reference that leaves the package is a named refusal
  at intake, never a silent start without the ingredient.
- **A single-file import is the exception path**: a loose agent file is a
  one-file plugin, and it carries the same rule — if it references an ingredient
  it does not bring, it is refused by name.

### 3. Git is the only authoring truth; the catalog is a window

This is ADR 0007 decision 2 applied, not re-decided.

- A **source is installation configuration**, not a store shape:
  `(source id, kind=git, location, ref, credential reference, selections)`, a
  selection being `(path pattern, kind token)`. The credential is an ADR 0017
  Account reference; material never enters the configuration (ADR 0009 §6).
- **A file's kind is configured, never inferred.** The de-facto standard layouts
  below are what an operator's selections *usually* say — they are defaults he
  writes, never a guess the atelier makes, and a file matching two selections is
  refused naming both. A source needs no atelier-specific structure.

| Ingredient | Usual pattern | Marker | Kind token |
| --- | --- | --- | --- |
| Agent | `agents/*.md`, `.claude/agents/*.md` | frontmatter carrying name and description | `agent_definition` |
| Skill | `skills/<name>/SKILL.md` | the directory plus SKILL.md frontmatter | `skill` |
| Workflow | `workflows/*.yaml` | the V3 grammar | `workflow` |
| MCP server | `.mcp.json`, `mcp.json` | an `mcpServers` mapping | `mcp_server` (§5, new) |

- **Connecting a source is one attributed first intake** — the operator's click
  is the actor. After that, **scanning is automatic and writes nothing**: it
  reads the source and shows drift ("a newer version is available"). **Intake
  happens on click.** There is no auto-intake, for ADR 0007 decision 2's stated
  reason: content the operator has never read must not become the head his next
  authored binding resolves to.
- **Provenance is `catalog_source_intakes`** — ADR 0007's provenance shape
  `(revision hash, source id, path, source position, actor, taken_at)`. No column
  is added beside the bytes, and none restates what the bytes say (ADR 0007
  decision 4).
- **`published_revisions(kind, revision_hash, document)` is already the
  content-addressed evidence store.** The snapshot is not a second copy; it is
  that table plus the intake record. Bytes are held because a run must prove
  which exact bytes drove it after a force-push, a moved branch or a deleted
  repository — git objects cannot carry that proof, since the repository is
  precisely what may vanish. A second reason is future and marked as such: a cage
  without network egress cannot fetch from git, so Core hands it the bytes.

### 4. Rendering is provider-native, inside the attempt's containment vector

The plugin is written into the **attempt's scratch root** in the layout its
provider expects (Claude: its plugin/agent directory; Codex: its own structure),
and **the provider loads its own agents and skills**. Around it stands the
containment vector the atelier doors already use
(`adapters/claude_subscription.py`): no built-in tools (`--tools=`), an explicit
`--allowedTools` list, `--strict-mcp-config` with an explicit `--mcp-config`, no
foreign discovery directories, `--disable-slash-commands`, and
`--setting-sources=` so no user, project or local settings file is read. MCP
reaches the process only after admission (§5). `--safe-mode` is deliberately
absent by measurement, not preference: safe mode prevents any `--mcp-config`
server from spawning, so it and a door cannot coexist.

**Hooks are not executed.** A hook is arbitrary shell code inside the agent
process with everything the agent can see, invisible and receiptless, and
`--setting-sources=` switches it off process-wide anyway. The intent behind a
hook has honest owners in the graph:

| Hook intent | Graph owner |
| --- | --- |
| "check after the work" | a verification node, or the `run-project-verification` grant — exit code plus receipt |
| "judge it, maybe repeat" | a declared verdict (ADR 0015) steering a bounded loop (ADR 0013) |
| "when X happens, do Y" | a workflow edge |
| "record that this happened" | receipts and attention events |

**Intake does not refuse a plugin for carrying hooks.** The agent is taken in and
the catalog shows the notice — "hooks (`<name>`) are not executed in the atelier;
model them as a verification node" — visibly, never silently. Should a hook need
ever appear that no node can express, the path is a blessed, containerized hook
**with** a receipt, never the raw provider hook.

### 5. An MCP server is a new published kind, and its blessing is admission plus attestation

**This is an amendment to ADR 0007**, stated as one: that record closed the kind
token set and said adding a token is an amendment. `mcp_server` is added, and it
is added for a named need — an MCP declaration must be publishable by hash before
anything may reference or spawn it. `RevisionKind` carries no such member today.

**Blessing is not a new card.** It is the two gates that already exist:

- **Admission** — #6's publish gate, per project store (ADR 0011). An unadmitted
  MCP revision is never spawned, so a foreign repository cannot slip a process
  in by pushing.
- **Executor attestation** — ADR 0009 §7. A runner that does not attest the
  binding refuses it (`no-runner-attests-binding`) before any process starts.

Two constraints are named because they already bind and must not be quietly
widened: `MAXIMUM_REDEEMED_TOOL_GRANTS = 1` (`contracts/workflows_v3.py`) — one
node pins one grant — and Claude Code's `mcp__<server>__<tool>` allowlist
grammar, which is how an admitted server's tools are named and bounded.

**`.mcp.json` environment values are reference-only.** A declaration whose `env`
carries a value rather than a reference name is **refused at intake**, not
sanitized and not stored: a secret must never enter the store, an event, a
prompt or a receipt (ADR 0009 §6, ADR 0017 invariant 1), and an intake that
"cleans" one has already written it.

### 6. Order by consumer: build for a reader that exists

The intake order follows the consumer, not the inventory:

1. **`workflow` intake first**, because runs already consume workflow revisions.
2. **Agents and skills reach a run only through #66 Phase C/D** —
   configuration → definition → launch. Today `AgentConfigurationRevision` carries
   model, auth profile, executor and capability and no definition link, and a V3
   document declaring `skills:` is refused at start, because `skills` stands in
   `V3_UNBOUND_AUTHORED_FORMS`: nothing binds it. Building catalog plumbing for
   ingredients no executor reads is effort without usage evidence.
3. **Plugin intake needs an executor that loads a plugin from the scratch root.**
   That executor is the next real seam, and it is the same seam as Phase C/D.

**Built today**, verifiable in the tree at this record's date: the containment
vector and the atelier doors (`adapters/claude_subscription.py`); the publish
doors, including the agent-definition route landed as
[PR #630](https://github.com/FlexOr2/atelier-2/pull/630) (#66 Phase A — exact
authored bytes in, `agent_definition` revision out, reconstructible by hash);
`published_revisions` and the catalog lineage tables. The catalog window is in
flight (#659).

**Proposed, none of it built**: the git source configuration, scan and
click-intake; `catalog_source_intakes`, which ADR 0007 names as a store shape and
no schema carries yet; the `mcp_server` kind; plugin rendering into the scratch
root; and the executor that loads it.

## Refusals

An ingredient can be wrong in three ways, each with its own boundary, so a
failure is never discovered at run start and never silently: **parse** (the file
does not satisfy the kind its selection declared), **reference resolution** (the
package lacks something it refers to — "agent X needs skill Y — not in the
plugin"), and **admission** (the ingredient is not blessed for this project
store). The catalog shows, per entry, its kind, name, origin (plugin, source,
commit), hash and an honest startability state.

The names below are proposed with the model; where one becomes durable it
becomes one of #16's tokens. ADR 0007's own source, scan, intake and admission
refusals are not renamed and not restated here.

| Name | Raised when | Boundary |
| --- | --- | --- |
| `plugin-ingredient-missing` | an agent references a skill or MCP server the package does not contain | reference resolution |
| `plugin-reference-escapes-package` | a reference points outside the intaken package | reference resolution |
| `mcp-declaration-carries-secret` | an `.mcp.json` `env` entry carries a value instead of a reference name | intake |
| `mcp-server-not-admitted` | a binding names an MCP revision this project store never admitted | binding resolution |

## Threat model

| Threat | Covering control |
| --- | --- |
| A foreign repository plants an MCP server so a run spawns its process | inert until an attributed intake, unspawnable until admission (§5) and until a runner attests the binding (ADR 0009 §7); connecting the source is the operator's recorded trust decision, and no auto-intake exists to bypass it |
| A foreign repository plants hooks to run shell code inside the agent process | hooks are never executed: `--setting-sources=` disables them process-wide, and the catalog shows the notice rather than hiding the gap (§4) |
| A secret is smuggled into the store through `.mcp.json` `env` | refused at intake by name, never stored and never sanitized (§5) |
| A plugin references material outside itself and pulls in unreviewed bytes | package-internal resolution only; an escaping reference is refused (§2, *Refusals*) |
| An agent reaches tools or servers nobody granted | `--tools=` removes every built-in, `--allowedTools` is the whole grant, `--strict-mcp-config` bounds MCP to the explicit config, one node pins one grant (§4, §5) |
| A moved branch, force-push or deleted repository destroys a run's proof | the run's bytes live in `published_revisions` by hash, with `catalog_source_intakes` naming where they came from (§3) |
| Content arrives that the operator never read and becomes the next binding's head | intake is a click; scanning writes nothing (§3, ADR 0007 decision 2) |

## Consequences

- **No translation to maintain.** Provider-format drift — new fields, renamed
  bounds, a changed agent schema — is the provider's problem. The atelier's
  surface against a provider format is the frontmatter minimum plus the bytes.
- **The price is honesty about portability.** A Codex agent does not become a
  Claude agent. Cross-provider comparison happens by casting the same role
  differently and letting #8's scorecard measure it. This record makes no "runs
  anywhere" claim, and no surface may.
- **The `agent_definition` format's closed frontmatter key set becomes too
  narrow.** `contracts/agent_definitions.py` admits exactly `name`,
  `description`, `model` and `tools` and refuses anything else with
  `field-unknown` — so a real provider agent file carrying its own composition
  keys is refused today. Pass-through requires that door to carry unmodelled keys
  while the required fields stay required; the change is named here and owned by
  #66's format owner.
- **One owner per unit.** The plugin is the intake unit, the source is the wire,
  the bytes are the evidence, the role is the neutral seam — no second editor, no
  write-back into git, no per-attribute column beside the bytes.
- **Hooks cost the operator a modelling step**, and buy what a hook never had:
  attribution, a receipt and a bound.
- **`RevisionKind` gains one member.** The kind token travels outside the
  revision hash (`contracts/revisions_v3.py`), so existing revisions keep their
  identities.

## Open decisions for the operator

1. **The auto-intake amendment path.** ADR 0007 decision 2 names the shape an
   automatic intake would need — an enrolled `agent` actor under a published
   intake-policy revision — and refuses it until then. Whether that amendment is
   ever wanted, and under which drift conditions, is open; this record keeps
   click-intake.
2. **Who may bless an MCP server under multiple users.** Admission is an
   attributed act in a project store today. Which principal may perform it when
   the installation has more than one human — and whether a tenant admin may
   bless for a project he does not own — is an access-control decision this
   record does not take.

## Required proofs before implementation is accepted

- A connected git source that is scanned leaves the store byte-identical: no
  revision, no lineage, no intake record — drift is a reading, not a write.
- The first intake from a connected source records an actor; no intake record
  exists without one, and no path produces one automatically.
- A plugin whose agent references a skill the package does not contain is
  refused at intake naming both the agent and the missing skill, and nothing of
  that plugin is published.
- An `.mcp.json` whose `env` carries a value is refused at intake; no store row,
  event, log line or API projection afterwards contains that value.
- An MCP revision that was intaken but not admitted is never spawned: a run
  binding it refuses before any process starts, and the refusal names the
  server.
- A rendered plugin in an attempt's scratch root runs under the full containment
  vector: no built-in tool is reachable, no MCP server outside the explicit
  config is reachable, no settings file outside the scratch root is read, and a
  hook declared in the plugin does not execute — proven by observing the process,
  not by reading the flags.
- A run's bytes remain provable after its source repository is force-pushed and
  after it is deleted: the revision still resolves by hash and its intake record
  still names the commit it came from.
- An imported provider agent file with frontmatter keys the atelier does not
  model publishes successfully, reconstructs byte-identically, and reaches its
  provider unchanged — while a file missing a required field is still refused by
  name.
- Casting one role with a Claude agent and with a Codex agent produces two runs
  whose receipts each name their own provider, and no run configuration contains
  a translated model name.

## Out of scope and stop conditions

This record does not decide: the catalog window's layout and interaction (#659,
#9); the #66 Phase C/D contracts themselves — the configuration→definition link
and the launch path — which this record only orders against; how a git source is
read (clone, fetch, cadence — implementation under ADR 0007); the scorecard's
measurements (#8, ADR 0008); the Account and secret-store model (ADR 0017); the
runner cutover (ADR 0009, #540); the board, history and log surfaces, which are
their own items.

Stop implementation on: any translation of a provider agent format into a
neutral composition, including model-name mapping; a write back into a
configured source; an automatic intake without an attributed actor; a
per-attribute column beside the published bytes; an MCP server spawned without
admission or without a runner attesting the binding; a secret value entering the
store from an ingredient declaration; a hook executed as a hook; a reference
resolved outside the intaken package; a startability claim the catalog cannot
back; or a second store beside `published_revisions` for ingredient bytes.

## Supersedes

None. This record **amends ADR 0007** by adding the `mcp_server` kind token
(§5) and otherwise applies decisions 2, 4 and 8 unchanged; ADR 0006's reference
form and ADR 0009's boundary are untouched.

The mid-day idea on #660 — parse both provider agent formats into one neutral
composition contract and translate model names into tier intents — is withdrawn
by the operator's own correction: it bought a portability claim nobody could
honor at the price of a translation the atelier would maintain forever.
