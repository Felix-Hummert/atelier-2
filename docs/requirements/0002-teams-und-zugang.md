# Requirement 0002: Access is an invitation, and the installation is the team's workshop

```text
Status:         AGREED
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/82
Source-Threads: #82
Distilled-From: 5302604615, 5302806812, 5302820772, 5302849696, 5302855908
```

`AGREED` here covers the *principles* below — they were ruled in the thread. The
protocol mechanics that carry them out are explicitly still open for their own
decision record: the thread heads its engineering direction "zu prüfen im
späteren ADR, nicht vorentschieden". Until that ADR lands, the binding force of
this document is negative as much as positive: make no decision that blocks it.

## Intent

The operator's wish, verbatim (#82 body):

> „Wir brauchen ein echtes Login und Sicherheit wie im Songmaker oder besser —
> Entra-ID-Login und OAuth ermöglichen, so dass es auch von professionellen
> Firmen verwendet werden kann."

Songmaker is the bar the operator is measuring against. Behind the wish sit two
sentences that decide the shape of the whole subject:

- **Installation is not access. Access is an invitation by the operator — for
  humans and for machines alike.** (5302604615, answering the operator's
  question "jeder der den Client installiert kann dann das Atelier verwenden?".)
- **The installation is the workshop of a team, not of a person.** Today's
  single-person operation is the special case of team size 1. (5302806812 §1.)

## Binding rules

1. **OIDC is the one protocol, and the atelier never owns passwords.** Identity
   providers are configuration — Entra ID, Google, GitHub, Keycloak — so
   "Entra support" falls out of the architecture instead of being a special
   case. Storing passwords is refused as a design, with the same reference
   discipline already applied to provider tokens. (#82 body.)

2. **Authorisation is roles per project.** Viewer / Operator / Admin, granted
   per project (#23 coupling). Agents and runners stay their own typed actors —
   the actor typing of ADR 0009 §9 is the seam that is already laid. (#82 body.)

3. **The audit trail answers who started, parked, or intervened.** It is the
   attach audit of ADR 0009 §8, generalised. (#82 body.)

4. **Local stays simple, and the boundary is the one that already exists.**
   Single-operator loopback mode needs no login (ADR 0009 §3); login becomes
   mandatory exactly when a non-loopback bind happens. The lower bound stays
   fail-closed, with no intermediate state. (#82 body, 5302604615.)

5. **A human is admitted by login plus a granted role.** OIDC login against the
   configured IdP *and* an operator-granted role per project; without both, any
   installed CLI or browser session is only knocking. Sessions are short-lived,
   actions audited. Paid starts are additionally subject to the budget rules,
   independent of which actor triggered them. (5302604615.)

6. **A machine is admitted by enrolment.** A one-time join token is exchanged
   for the runner's own short-lived certificate and is consumed in the process
   (ADR 0009 §4); revocation then hits exactly one runner. (5302604615.)

7. **The wire is TLS, thought in both directions, from the standard stack.** The
   client verifies it is talking to the real atelier via the server certificate;
   a runner additionally uses mTLS. Public deployments use Let's Encrypt,
   private ones a minimal own CA. No self-built crypto. (5302604615.)

8. **The project is the sharing unit.** Roles are per project — who sees, who
   starts, who administers. (5302806812 §2.)

9. **Sharing libraries and workflows means sharing git sources.** The ADR 0007
   model is the only sharing channel: sources are registered globally and
   selected per project. There is no second sharing channel. (5302806812 §3.)

10. **Runners are installation-bound and carry an owner scope.** They are
    registered at the team's atelier through an enrolment register — the GitLab
    runner model, deliberately consumed rather than reinvented. A runner holding
    a *personal subscription credential* carries `owner` and `allowed-projects`,
    so no colleague spends someone else's quota. API-key runners may be
    team-wide. (5302806812 §4.)

11. **Placement follows attestation, not preference.** A subscription needs a
    long-lived machine with one interactive login — **including remote machines,
    which are first class**: a long-lived remote machine with a single
    interactive login carries a subscription executor fully, and the credential
    directory stays local, so the reference principle is untouched. Only
    *ephemeral* environments (throwaway CI) structurally cannot hold a
    subscription and therefore use API keys. (5302806812 §5, sharpened by
    5302820772 §1.)

12. **Consumption is tracked per mode, and modes are never mixed.** Both modes
    measure attempts, duration, and tokens. Money is exact **only** in key mode;
    a subscription is an honestly labelled quota share, never mixed with money
    and never extrapolated. (5302806812 §5, confirming ADR 0008 and #8.)

13. **Team-wide API-key consumption is attributed, not merely measured — per
    project and per trigger (actor or workflow), never only per runner.** This
    makes "which project or team member spent what" structurally answerable
    without mixing personal quotas. The `owner` + `allowed-projects` fences stay
    the protection instrument for *personal subscription* quotas; the asymmetry
    is justified because an API key is team billing by construction, so there is
    no personal quota there to fence. (5302855908, the one finding that survived
    the machine review of 5302849696.)

14. **Credentials stay references and are never transported through the
    atelier** (ADR 0009 §6). Central distribution would be a secret manager's
    job, not this product's. (5302806812 §6.)

15. **Estimates may be displayed and may never be proof.** Subscription
    consumption may carry a clearly labelled "≈ estimated" money line in the UI,
    provider-neutral, computed from **configurable** price tables — never
    hardcoded. Receipts carry only measured values and no gate ever computes
    with an estimate (ADR 0008 claim 3 untouched). The dividing line: the
    display layer may guess, the proof layer never. (5302820772 §2.)

## Open questions

- **The protocol mechanics are not pre-decided.** The OIDC direction above is
  explicitly to be examined in a later ADR (#82 body). Until then the binding
  obligation on everything built now is negative: session and API auth
  assumptions in frontend and API stay exchangeable, so nothing blocks OIDC
  later.
- **SCIM, provisioning, and multi-tenancy are named, not designed.** (#82 body.)
- **Sequence.** After the function chain and the start of the remote epic. This
  concept makes #82, #23, and #21 team-consistent; it builds nothing ahead of
  time. (#82 body, 5302806812 closing.)

## Acceptance

No story has declared an acceptance sentence for this requirement yet; the
subject is deliberately seam-now, build-later. The sentences a story would have
to declare are the ones the rules above already state in testable form —
notably: an installed client without an invitation reaches nothing; a
non-loopback bind without a configured authenticator is refused rather than
served; a one-time join token cannot be used twice; a personal subscription
runner refuses a project outside its `allowed-projects`; a receipt never carries
an estimated money value.

## Provenance note

Rule 13 exists because the concept was read back by the product itself: a
second-opinion workflow revision ran one paid `claude-haiku-4-5` call against a
1,021-character condensation of the team concept, receipt
`1435f55a15ecccdbf297c912a323ea9d55ad5fd9b641788d3ee591bb70ac749e`
(5302849696).

Of that review, exactly one finding survived — the attribution line now standing
as rule 13. Its headline charge ("no way to attribute consumption") was false
against the full concept and hit only the condensation, which had dropped the
per-mode consumption tracking to fit the field limit. Its main recommendation —
abolish team-wide API-key runners — contradicted the attestation rule it praised
in the same breath, since ephemeral CI structurally cannot hold a subscription.
The concept therefore stands and gained a clarifying line; it was not corrected.
