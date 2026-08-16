# Requirement 0004: Execution happens anywhere, and one trust boundary is what makes that safe

```text
Status:         AGREED
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/21
Source-Threads: #21, #1
Distilled-From: 5300858953, 5300894378, 5302132060, 5302584358, 5302587068,
                5302590978, 5302602114, 5302961156, 5302967786, 5302447161,
                5307632389
                #21 body, sha256 over the exact served bytes — 787 bytes, last
                byte `.`, nothing appended —
                5c03ceb1d5f1b85f81ec3acc1f6dea1c72d89817929a772432b9b02fbb74a56b
Approved-By:    5307632389
```

`AGREED` as a reading (operator comment 5307632389, 16.08.2026, „ja passt"): the
operator approved that this document reads its thread faithfully. That
approval settles no direction beyond what the thread itself settles — the
paragraph below keeps saying why that caution matters here. And on this subject the status carries more weight than usual, for two
reasons.

The first is the thread's voice. #21 is an **ADR mandate**, not a wish: its body
lists what a decision record must own, and almost every comment under it is the
desk writing engineering direction. **No rule below is graded `OPERATOR`.** Every
object in this thread that looks like operator authority is one of the four
things the convention names as `DESK`: a *question* („es gibt doch GitLab-Runner
— entwickeln wir ein Konzept, das wir nicht brauchen?", „ist das der richtige
Weg? sei absolut ehrlich", and the *Operator-Nachfrage* whose standard rule 15
answers), a *rejection* („das reicht ja nicht — ich kenne die Umgebung nicht"), a
rendering the thread itself marks *wörtlich sinngemäß*, or an attribution the
desk wrote around its own prose (`ERGÄNZUNG (Operator…)`). So
what follows is the desk's answer to his questions, and its binding force is
negative — make no decision now that blocks it. Not one sentence below binds him
until he rules it.

The second is that the decision record this thread commissioned **landed while
this document was being reviewed**. [ADR
0009](../decisions/0009-runner-trust.md) reached `main` as PR #78's merge
`1a88dbdd`, at head `0f6d3aca`. Every `§` reference below now names a record in
the tree rather than a draft on a branch, and the convention's precedence rule —
a requirement document never outranks a landed decision record — applies for
real: where a rule below and that record disagree, the record wins and this
document is corrected.

Its own `Status` is still `PROPOSED`, decision-only, nothing implemented
(5300894378), and that is a statement about code and not about authority. What
it does mean is that no rule below may be read as built.

`Source-Threads` names `#1` for one object only: the house rule 5302447161,
which the #21 thread invokes by id at rule 4. This document does not distil #1
and takes no position on its content beyond quoting the sentence the thread
leans on.

**The #21 body digest is over the exact served bytes**, reproduced with

```console
$ gh api repos/FlexOr2/atelier-2/issues/21 --template '{{.body}}' | sha256sum
```

which writes the field and appends nothing. The recipe this directory's README
currently prescribes, `--jq '.body' | sha256sum`, digests the body plus the
newline `gh`'s raw-string output adds and yields
`81cf4b1f4703ad6f7000836037beccf848fea9617c633ccd0b8a32b17dca47cf` for the same
787 bytes — an identity over a byte the object does not contain. The exact form
is used here for one reason that is not a preference: the now-landed ADR 0009
binds #21 as its decision authority by `5c03ceb1…`, and one object cited under
two digests in two documents is exactly the ambiguity a digest exists to remove.
The rule making that form canonical is itself in flight — ADR 0010 decision 5,
PR #81 at head `2ced1ec4`, not landed — and the README's own correction is owed
and routed to [#93](https://github.com/FlexOr2/atelier-2/issues/93). Named here
so a reader who reproduces the README recipe and gets the other value knows
which byte differs, and neither digest is presented as settled convention.

## Intent

What the operator wants of remote execution, as 5302584358 records it — and that
comment marks its own quotation **wörtlich sinngemäß**, a rendering rather than
a transcript, so it is repeated with that qualifier and never promoted:

> Das Atelier ist Server und Koordinator; WO ausgeführt wird, ist beliebig —
> lokaler PC, CI, Docker-Sandbox, beliebiger Server. Beispiel: Codex-Runner
> lokal beim Operator, Claude-Reviewer in einer Cloud. Flexibel und sicher;
> Provider-Credentials (API-Key im Env o. ä.) liegen auf der jeweiligen
> Maschine.

Under that sit three fragments the thread attributes to him directly. **None of
them states a rule.** Two are questions and one is a rejection; they are recorded
here because they are what forced the rules below into existence, and the answers
are the desk's:

- „professionell und zugleich absolut sicher und funktionsfähig" (5302587068) —
  the standard the desk was challenged to meet. The thread frames it as an
  *Operator-Nachfrage*, and the words are not a sentence: no subject, no verb,
  nothing that can independently state a `must`. The rule holding this standard
  is the desk's reading of it; rule 15.
- „es gibt doch GitLab-Runner — entwickeln wir ein Konzept, das wir nicht
  brauchen?" (5302590978) — the question that produced the build-versus-consume
  boundary. The boundary is the desk's; rule 4.
- „das reicht ja nicht — ich kenne die Umgebung nicht; wo gebe ich Credentials
  an? ist das durchdacht?" (5302967786) — the rejection that produced the first
  open question. It says what is not enough without saying what to build, so
  everything answering it is `DESK`, and the part still missing stayed open
  rather than being invented.

The subject itself is stated by the mandate: **one boundary separates the
coordinating service from every runner, and it is the same boundary whether the
runner is this machine, a CI job, or a server across the world** (#21 body @
5c03ceb1).

## Rules

**Where this document defers rather than repeats.** Requirement
[0002](0002-teams-und-zugang.md) (#82) already reads the neighbouring thread,
and these rules do not restate it:

- **Operator authentication.** #21's body mandates it for the remote cockpit and
  API; the rules that answer it are 0002 rules 4 and 5 (loopback needs no login,
  a non-loopback bind makes it mandatory, a human is admitted by login plus a
  granted role).
- **Enrolment as the admission of a machine, and the owner scope on a runner
  holding a personal subscription credential** — 0002 rules 6 and 10. What #21
  adds is below at rules 5, 6 and 14.
- **Credentials stay references and are never transported** — 0002 rule 14. #21
  adds where the two halves live and what happens when one is missing: rules 12
  and 13.
- **Remote machines are first class for subscription credentials** — 0002 rule
  11 rules the principle. Rule 14 below carries only the mechanism.
- **Money and estimates.** 0002 rule 15 owns it, and it is the one `OPERATOR`
  rule over there. Nothing in this document computes, displays, or gates on a
  cost figure, and nothing here restates that rule.

### The boundary

1. `DESK` — **A runner delivers evidence; it never writes truth.** The
   coordinating service owns the durable record; a runner owns provider
   invocation and reports what it observed. This is the sentence every other
   rule here is an instance of. (5302584358 §3, naming it as invariant 1 of the
   record that became ADR 0009; corroborated by 5302602114 §2, where it is what killed
   the cheaper alternative.)

2. `DESK` — **Store-sharing was examined and refused, and it stays refused.**
   Running a remote machine as a second durable worker against the same database
   needs no protocol of our own at all, and was rejected anyway: the runner
   could then write truth instead of delivering evidence, which erases rule 1.
   Named verdict: the largest trust surface for the smallest saving. (5302602114
   §2.)

3. `DESK` — **Runners pull; the atelier never pushes.** Runners connect outbound
   to the atelier, fetch bound attempts, and stream observations — so no remote
   machine opens an inbound port. Transport is outgoing HTTPS against the
   existing API, not a new protocol. A CI runner is the one-shot case of the
   same thing: fetch one → execute → report → exit. The pattern is consumed from
   GitHub-Actions and GitLab runners, not invented. (5302584358 §1, 5302590978.)

4. `DESK` — **We build only the thin evidence protocol; everything else is
   consumed.** This is the answer to the operator's question „es gibt doch
   GitLab-Runner — entwickeln wir ein Konzept, das wir nicht brauchen?"
   (5302590978), and the question is quoted rather than graded because it says
   what he doubts, not what to build.

   Four structural reasons a CI runner alone cannot carry this contract — stated
   as structure, not taste: CI is at-least-once by design (an infrastructure
   fault restarts the job) while our core is at-most-once on paid calls with a
   durable witness; logs and artefacts are not a receipt and attestation chain;
   CI offers no live observation and no intervention mid-run; and interactive
   subscription credentials do not fit the CI secret model.

   So we build one small runner that speaks the evidence side — fetch attempt →
   witness → stream observations → receipt evidence — and consume the rest:
   fleet management, provisioning and scaling from CI platforms or plain systemd
   and Docker; identity from standard mTLS; secrets as references into local
   sources. Our runner is expected to run *inside* a GitLab or GitHub runner in
   one-shot mode, and the first remote proof is exactly that — our runner in a
   GitHub-Actions job, with no infrastructure of our own. The thread invokes the
   house rule 5302447161 here, whose operator sentence reads „Ich will nichts
   machen, was der Provider (Claude/Codex) mitliefert und besser kann — ich will
   das Drumherum verbessern." That sentence is about provider-shipped
   capability; applying it to CI platforms and PKI is the desk's generalisation,
   which is why this rule is `DESK` and not `OPERATOR`.

### What a remote machine is

5. `DESK` — **A remote machine needs exactly four things, and no fifth.** The
   runner binary or container; enrolment (a one-time token exchanged for a
   per-runner credential, mutual TLS); the provider credential **locally**, by
   reference; and an attested sandbox capability. Anything a fifth requirement
   would add is the open question at the end of this document, not a silent
   addition here. (5302584358 §2.)

6. `DESK` — **Enrolment is per runner, and the register is what answers "which
   runners are mine".** 0002 rule 10 owns the register itself; what this subject
   adds is that the enrolment record is the only place where a runner's
   identity, its owner, and its allowed projects become *facts the atelier can
   match on* (rule 7) rather than claims a connecting process makes about
   itself. A runner that is not enrolled is not a runner; a revoked enrolment
   hits exactly one machine. (5302584358 §2; 5302961156 §1 for
   `owner`+`allowed-projects` as matched facts; 0002 rule 10 for the register.)

### Placement

7. `DESK` — **"Tags" are attested facts, never typed labels.** Runner selection
   is matching a binding's requirements against **proven** capabilities:
   resolvable credential references, sandbox level, provider versions, `owner`
   and `allowed-projects`. The ergonomics may look like GitLab tags; the
   substance is attestation, because a typed label is a claim and a claim is
   what this boundary exists to refuse. Manual runner-class constraints per role
   are additionally allowed, and they never replace the matching — they narrow
   it. (5302961156 §1.)

8. `DESK` — **Requirements are declared at the work, capabilities are attested
   by the runner, and the atelier only matches.** The two-sided principle, with
   the declaring side named: the agent definition declares tool need, the
   executor revision declares runtime need, the project declares runner classes
   and placement rules. The atelier itself declares nothing and attests nothing;
   it matches. (5302967786, which states this half as decided.)

9. `DESK` — **Placement is per attempt, never per run, and a running attempt
   never migrates.** At-most-once and the evidence chain both die the moment a
   live attempt moves. A lost runner yields `POSSIBLY_RAN`, loudly, and never a
   re-placement. (5302961156 §2; consistent with ADR 0009 §10.)

10. `DESK` — **Change of machine happens at honest boundaries, and a replacement
    attempt is first class.** Retry, resume, and a deliberately replaced attempt
    are each **re-placed** — automatically onto any attesting-fit runner, or
    pinned by the operator. The thread's own normal case: the runner ran out of
    tokens, so the next attempt runs on another machine, as a **new receipted
    attempt** rather than a continuation of the old one. Replacement attempts
    are built as first class, not modelled as a failure mode. (5302961156 §2.)

11. `DESK` — **Placement fails closed.** Work waits visibly rather than running
    unsafely; a binding that no connected, enrolled runner attests does not get
    a runner that nearly fits, and no mode is silently downgraded. Until the
    open core at the end of this document is decided, **remote bindings stay
    refused** altogether. (5302587068, "fail-closed-Platzierung (Arbeit wartet
    sichtbar statt unsicher zu laufen)"; 5302584358 §4.) What "waits visibly"
    means in the machine is no longer this document's to guess: ADR 0009 §7,
    landed, rules that an unplaceable run is refused at run start and never
    queued, and the open question below records that ruling rather than the
    disagreement it replaced.

### Credentials

12. `DESK` — **Values live on the machine; names live in the configuration.**
    Both places are right, and they are different kinds of thing: the credential
    **value** sits on the host — environment, file, keychain — put there at
    enrolment; the **name or reference** sits in configuration, where a project
    says it uses reference `github-work`. Who can resolve that reference is an
    attested fact (rule 7), never a transport. The atelier is at no point a
    secret-distribution channel. (5302967786; 0002 rule 14 owns the
    never-transported half.)

13. `DESK` — **A missing credential fails closed at three layers, and no layer
    downgrades.** (a) *Configuration* carries only a reference, so a missing
    value cannot be smuggled in as a value. (b) *Placement* refuses a binding no
    connected, enrolled runner attests it can resolve — the reference's
    resolvability is one of the matched facts of rule 7, so an unresolvable
    binding never reaches a machine. (c) *Run start on the runner* refuses when
    the bound reference does not resolve on that host, with no fallback to
    another auth mode. Each layer refuses; none substitutes a weaker credential
    or a different mode. (Layers b and c are 5302961156 §1 and 5302584358 §2/§3,
    and ADR 0009 §6/§7 gives them the refusal names
    `auth-profile-unresolvable` and `no-runner-attests-binding`, which bind now
    that the record has landed.)

14. `DESK` — **Remote subscription runners are first class in practice, not only
    in principle.** 0002 rule 11 states the principle, itself as `DESK`: a
    long-lived remote machine with one interactive login carries a subscription
    executor fully, and only *ephemeral* environments structurally cannot. What
    this subject owns is what makes that true on a real machine — rules 5, 6, 12
    and 13 — and the consequence that a remote subscription runner is an
    ordinary placement target, not an exception carved into the matcher. This
    thread's own picture of the end state is exactly that shape: a Codex runner
    locally at the operator, a Claude reviewer in a cloud, with the provider
    credentials on their respective machines. (5302584358, whose quotation the
    thread marks *wörtlich sinngemäß*; 5302961156 §1 for the resolvable
    credential reference as a matched fact.)

### The standard to reach

15. `DESK` — **The end state is professional and absolutely secure and
    functional at the same time; security is not bought with unusability.** The
    standard the desk is answering, as 5302587068 frames it:

    > „professionell und zugleich absolut sicher und funktionsfähig"

    An earlier revision graded this rule `OPERATOR`. That was wrong, and the
    reason it was wrong is worth keeping at the rule. The convention's grade
    requires the cited object to reproduce the operator's own **sentence**, and
    that sentence to say what the rule says. This fails both halves. The comment
    frames the words as an *Operator-Nachfrage* — a question, which the
    convention grades `DESK` outright — and it does so inside an `ERGÄNZUNG
    (Operator…)` attribution the desk wrote, which the convention also grades
    `DESK`. The words themselves are a fragment: no subject and no verb, so they
    cannot independently state the `must` this rule states. Reading them as
    authoritative because they contain no question mark would make the voice test
    turn on punctuation inside a desk-authored framing, which is the
    self-certification the convention exists to refuse. None of the ten objects
    this document distils contains a complete operator sentence stating this
    requirement; if one is later posted, this rule is regraded against it.

    The fragment is kept because it is the standard that motivated the rule —
    three terms held together by *zugleich* — and the rest of the comment is desk
    elaboration binding nothing either: the four-stage maturity ladder, the choice
    of SPIFFE/SPIRE as the end form, and the one-command onboarding of rule 16.
    The framing „Long-term, nicht Prio 1" likewise sits outside the quotation
    marks and is the desk's, which is why rule 23 carries it as the desk's
    sequencing rather than as his instruction.

16. `DESK` — **Onboarding is one command, and everything after it is invisibly
    correct.** `atelier runner join <token>` and nothing else, because security
    with friction is worked around and invisible security is lived. This is the
    desk's operational reading of rule 15's *funktionsfähig*. (5302587068.)

17. `DESK` — **No PKI or identity framework of our own.** Standard mTLS tooling
    is consumed now, workload identity after SPIFFE/SPIRE is the named end form
    (auto-rotating short-lived identities instead of long-lived secrets), and
    the convergence is deliberate: the in-toto agent predicate from the #104
    research uses SPIFFE IDs, so dossier and runner identity end up speaking one
    language. Humans authenticate by OIDC, which is 0002's subject, not this
    one. (5302587068.)

18. `DESK` — **Three principles hold across every stage of the ladder:** zero
    trust — identity plus attestation, never network position; short-lived
    rather than revocable; and fail-closed placement, which rule 11 states in
    full. The ladder they hold across: stage 1 local loopback-only (done), stage
    2 OS process separation on one host (#15, in progress — the miniature of the
    remote case with the same invariants and no network), stage 3 pull runners
    with mTLS enrolment, lease/heartbeat/fencing and an attested sandbox, stage
    4 workload identity. The ladder is direction, not a schedule. (5302587068,
    with 5302590978 for stage 2 being the runner protocol without the wire.)

### Actor and channel, from the mandate

19. `DESK` — **Every command carries a typed, authenticated actor**, and until
    it does, nothing may be called attribution. The mandate names the actor and
    attribution model for commands as an ADR obligation (#21 body @ 5c03ceb1,
    feeding #7); the honest half is that today's reconcile actor is a
    self-asserted label, so no surface may present it as attribution. (#21 body
    @ 5c03ceb1; 5300894378 for the self-asserted-label finding.)

20. `DESK` — **The terminal channel is a separately gated, default-off
    capability**, with a per-attach step-up, short-lived tokens, and an attach
    audit. It is the one channel that carries a human's keystrokes into a
    credential-bearing process, so execution capability never implies it. (#21
    body @ 5c03ceb1; 5300894378. The audit trail's general shape is 0002 rule
    3.)

### The durable half, in code

21. `DESK` — **"Decision and enqueue are one transaction" must become
    enforceable instead of hand-written.** The ADR 0001 invariant is
    hand-written at every call site — the finding's headline says five and the
    list under it names seven, across five modules — each with its own pre- and
    post-conditions, so one more site that enqueues outside the transaction
    violates it without any gate or test noticing. That is precisely the state
    this boundary must exclude: a runner receiving an attempt that no committed
    decision covers. Named smallest fix: one owner taking the decision write and
    the enqueue in a single call, so "decision without enqueue" becomes a type
    error rather than a review question. (5302132060, finding H4, HIGH, against
    main `b9c7796e`.)

22. `DESK` — **"Which executor modes can this runner enforce" needs exactly one
    author.** Today the answer has two: a second composition root builds the
    executor registry from factory lists in parallel to the application composer
    (M8), and the registry itself carries resolution behaviour inside the
    runtime-pure ports package, where an optional argument silently builds an
    empty registry — a fail-open default that exists only to make tests cheaper
    (M7). Both are the same defect for this subject: a trust boundary whose
    central question has two authors cannot be attested. (5302132060, findings
    M7 and M8, MED.)

### Sequence

23. `DESK` — **The gate for building remote is value, not calendar.** For
    single-operator use the value of remote stays modest for a long time, so it
    is built at the first real second-machine need — the named candidate being
    the "from work" scenario of #79 — and "not priority one" is part of being
    right rather than an excuse. (5302602114 §1.)

24. `DESK` — **Managed provider sandboxes are a watch point, not a competitor to
    build against.** If execution inside a provider's cloud under operator
    policies matures, our runner shrinks to an adapter for those cases and the
    placement seam — attested capability — survives unchanged; self-hosted
    remains the sovereignty path. The instruction is to observe and to keep the
    seam, not to pre-build either side. (5302602114 §3.)

## Open questions

- **The environment-requirements vocabulary is missing, and this epic owes it
  before any remote release.** The operator's rejection is the source: „das
  reicht ja nicht — ich kenne die Umgebung nicht; wo gebe ich Credentials an?
  ist das durchdacht?" (5302967786). What is decided is the *structure* —
  declare, attest, match (rule 8) and the two credential places (rule 12). What
  is missing is the *content*: the concrete requirement vocabulary beyond
  credentials, sandbox level, and provider version — tools (`docker`,
  `node@20`), resources (RAM, disk, GPU), network reachability — **each with its
  form of proof**, since the honest question is how a runner attests "has
  Docker" without merely claiming it (a version probe, not an assertion). The
  thinking model is to be consumed rather than invented: Kubernetes selectors
  and taints, GitHub Actions `runs-on`. The extension seam exists — ADR 0006's
  capability vocabulary is versioned and extensible and ADR 0009 §7's
  attestation takes new entries — so what is undecided is the content, not
  the structure. This is an open building block, deliberately not written as a
  rule, because a document that resolved it would be inventing the requirement
  it is supposed to be reading. Owner: this epic, before remote is released.

- **The named open core before remote release** (5302584358 §4): the lease,
  heartbeat, and fencing contract; the transport; and the runner's packaging and
  update channel. Until these are decided, rule 11 keeps remote bindings
  refused. Owner: the remote ADR, #9 part 3.

- **Does an unplaceable binding wait or refuse? — closed by citation.** The
  question was whether the thread's „Arbeit wartet sichtbar statt unsicher zu
  laufen" (5302587068) and the ADR's refusal at run start were the same answer.
  [ADR 0009](../decisions/0009-runner-trust.md) §7, landed, rules it: an
  unplaceable run is **refused** — before any durable run, binding, attempt, or
  provider process — and "never queued in the hope a runner appears, because
  queueing it turns fail-closed into a hang". The refusal
  `no-runner-attests-binding` names the node, the binding and the missing
  attestation, so what „wartet sichtbar" wanted — the operator seeing it rather
  than a silent stall — is carried by the refusal and not by a waiting item.
  Nothing is left for this document to choose.

- **What the ADR mandate deliberately did not decide** (5300858953, 5300894378):
  transport and protocol details; the remote epic's scope and its
  attempt-ownership contract (#9 part 3); multi-project isolation (#23); and the
  sandbox mechanism (#60).

- **The secrets-channel decision is shared with #24 and must be taken once.**
  The audit's recommended order is the graph interpreter's move into the core
  (#86) first, then rules 21 and 22, then the secrets-channel decision that #21
  and #24 both need — decided once, not twice. (5302132060, closing.)

## Acceptance

No story has declared an acceptance sentence for this requirement, so what
follows is a set of candidates and not a set of declared sentences; none of
them has an identifier to name. The rules above already state in testable form, notably:

- a process that reaches the service without an enrolment receives no attempt;
- a binding that no connected, enrolled runner attests starts nothing — no
  durable run, no attempt, no provider process;
- a runner whose bound credential reference does not resolve on its host refuses
  rather than falling back to another auth mode;
- a running attempt never changes machine, and a lost runner yields
  `POSSIBLY_RAN` rather than a second placement;
- a resumed or replaced attempt appears as a new receipted attempt, on a runner
  chosen by matching and not by a typed label;
- a runner attestation that differs from the enrolled one is a visible diff
  requiring a fresh operator act, never a silent widening;
- no credential value appears in any record, projection, or transmission in
  either direction;
- attach is off unless separately enabled, and every attach is audited.

The environment-requirements gap above is deliberately absent from this list:
until that vocabulary exists there is no honest sentence to write for it, and an
acceptance sentence invented ahead of its requirement is the failure this
directory's convention exists to prevent.
