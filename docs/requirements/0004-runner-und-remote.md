# Requirement 0004: Execution keeps product truth and provider secrets with their owners

## Intent

Atelier coordinates durable work while Agent Runner execution may occur outside
Core without transferring product truth or provider credential values into Core.

## Rules

### REQ-REMOTE-01: An Agent Runner returns evidence and never writes durable product truth.
Quelle: DESK — #21 body @ 3c1f663cd51a1c7aedbeffc39c3f38ee2ed6174d16103ab68d9d811014352ed0

### REQ-REMOTE-11: Work without a bound, mutually authenticated and authorized Runner is refused before an Attempt starts.
Quelle: DESK — #21 body @ 3c1f663cd51a1c7aedbeffc39c3f38ee2ed6174d16103ab68d9d811014352ed0

### REQ-REMOTE-32: Provider credential values never enter Atelier Core.
Quelle: DESK — #21 body @ 3c1f663cd51a1c7aedbeffc39c3f38ee2ed6174d16103ab68d9d811014352ed0

### REQ-REMOTE-29: Atelier Core is the sole writer of durable product truth.
Quelle: DESK — #21 body @ 3c1f663cd51a1c7aedbeffc39c3f38ee2ed6174d16103ab68d9d811014352ed0

### REQ-REMOTE-30: Each local AgentAttempt executes in a boundary isolated from other Attempts.
Quelle: DESK — #21 body @ 3c1f663cd51a1c7aedbeffc39c3f38ee2ed6174d16103ab68d9d811014352ed0

### REQ-REMOTE-31: Remote and CI Agent Runner execution remains closed until its carrier boundary is separately decided and proven.
Quelle: DESK — #21 body @ 3c1f663cd51a1c7aedbeffc39c3f38ee2ed6174d16103ab68d9d811014352ed0

## Non-goals

This requirement does not choose a carrier, transport, certificate authority,
container hardening or rollout sequence; ADR 0009 and #21 own those decisions.
