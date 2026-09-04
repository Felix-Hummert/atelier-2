"""Names built ahead of their caller and kept on purpose.

Operator ruling 04.09.2026: code built before a caller exists is frozen, not
thrown away -- we would only build it again. Frozen means no hardening and no
new tests, not silent rot: `scripts/check_dead_code.py` reports every entry on
every run without failing, and each group names the open item that owns the
caller it waits for. When that item lands the caller, the entry is deleted;
when it decides against the caller, the code is deleted with it.

Read as data by the gate; never imported at runtime.
"""

WAITING_FOR_A_CALLER = (
    {
        "names": (
            "attest_runner_inspect",
            "encode_free_runner_job",
            "MAXIMUM_RUNNER_SESSION_WIRE_FRAME_BYTES",
            "refused_exchange",
        ),
        "why": (
            "The Agent Runner cluster (runner/, adapters/free_runner_executor.py, "
            "adapters/runner_child.py, adapters/docker_carrier.py): the candidate "
            "issuer, its job encoder and the session codec's bounds are exercised "
            "by tests/witness and the codec tests, and wait for the placement of "
            "the Runner in the serving host."
        ),
        "item": "#1177 (Runner-Platzierung offen)",
    },
    {
        "names": ("confirm_execution", "authorize_retry", "commit_action_completed"),
        "why": (
            "The effect-reconciliation half of an Action node: an authorization "
            "confirms an execution and a retry is authorized against the same "
            "intent, but no route or workflow calls either yet -- the run store's "
            "completion writer waits with them."
        ),
        "item": "#1168 Befund 7 (test-only-lebendig, Owner beim Dispatch)",
    },
    {
        "names": ("latest_model_registry_revisions", "publish_project_root_revision"),
        "why": (
            "Host-configuration reads and writes declared on the port and "
            "implemented in the DBOS adapter, with no route asking for them yet."
        ),
        "item": "#1168 Befund 7 (test-only-lebendig, Owner beim Dispatch)",
    },
    {
        "names": ("peak_active_queries", "abandoned_queries", "effect_adapter"),
        "why": (
            "Instrumentation the SSE runner and the DBOS runtime expose for a "
            "reader that does not exist yet; today only their tests observe them."
        ),
        "item": "#1168 Befund 7 (test-only-lebendig, Owner beim Dispatch)",
    },
    {
        "names": ("recorded_pull_requests", "recorded_documentation_pushes"),
        "why": (
            "Recorders on the GitHub effect fake that lives in the production "
            "adapter module; their callers are acceptance tests, and moving the "
            "fake out of src is a cut that item owns."
        ),
        "item": "#1168 Befund 7 (test-only-lebendig, Owner beim Dispatch)",
    },
    {
        "names": (
            "resolve_declared_reference",
            "agent_configuration_revision_for",
            "join_of",
            "AnyBoundRun",
            "MAXIMUM_KIND_TOKEN_CHARACTERS",
            "derived",
        ),
        "why": (
            "Contract helpers a caller was planned for and has not arrived at: the "
            "scheduler that applies a join, the reader that reports which lineage "
            "id was derived, the bound-run alias and the kind token bound. Each "
            "is proven by a domain test and named by an ADR."
        ),
        "item": "#1168 Befund 7 (test-only-lebendig, Owner beim Dispatch)",
    },
    {
        "names": ("PLATFORM_CONNECTION_UNKNOWN",),
        "why": (
            "ADR 0010 names `platform-connection-unknown` as the refusal for an "
            "operation naming a project with no connection record, but the served "
            "route answers `project-source-not-connected` and the application "
            "answers the typed `PlatformConnectionUnknown`. The word has no "
            "speaker yet; which of the two the product keeps is the open question."
        ),
        "item": "#1168 (Verteiler, Befund 10)",
    },
    {
        "names": (
            "WORKSPACE_READ",
            "COMMAND",
            "NETWORK",
            "SECRET_READ",
            "PATH_PREFIX",
            "COMMAND_NAME",
            "HOST",
            "for_call",
            "granted",
            "policy_revision_hash",
        ),
        "why": (
            "The asking half of the permission boundary "
            "(contracts/agent_permissions.py): the effect and scope vocabulary a "
            "provider question is expressed in, the correlation id minted for one "
            "call of one attempt, and the two fields of a decision only a reader "
            "of the answer needs. Production binds the policy and hands the "
            "decider to every session today; nothing asks yet, so the grant "
            "branch and the words a question is spelled in wait for the first "
            "provider channel that can put one (ADR 0020 step 2)."
        ),
        "item": "#1177 Schritt 2 (erster fragender Provider-Kanal)",
    },
    {
        "names": ("METHOD_INITIALIZED", "MCP_TOOL_HTTP_DOORS"),
        "why": (
            "The MCP door table and the initialized notification: the server "
            "answers the methods it serves today, and the table that maps every "
            "tool to its HTTP door waits for the router that reads it."
        ),
        "item": "#1168 Befund 7 (test-only-lebendig, Owner beim Dispatch)",
    },
)
