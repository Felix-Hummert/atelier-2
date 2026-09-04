"""Names a production site does reach, at a site `vulture` cannot see.

Read as data by `scripts/check_dead_code.py`; never imported at runtime. Every
group names the site that reaches it, because an entry without a site is an
excuse rather than a fact. An entry the gate no longer needs is deleted, not
kept "just in case" -- the gate refuses a name it no longer reports.
"""

REACHED_BY_A_SITE_VULTURE_CANNOT_SEE = (
    {
        "names": ("install_landlock_guard",),
        "why": (
            "adapters/runner_child.py builds the child's `-c` program as text and "
            "imports this name inside that text, so the only caller is a string."
        ),
    },
    {
        "names": ("run_free_runner_job",),
        "why": (
            "adapters/free_runner_executor.py builds the fixed candidate program "
            "as text and calls this name inside that text."
        ),
    },
    {
        "names": ("WORKSPACE_WRITE",),
        "why": (
            "A CodexSandboxMode member; the adapter selects a sandbox by the "
            "value codex-cli documents, never by attribute."
        ),
    },
    {
        "names": ("STANDARD_OUTPUT", "STANDARD_ERROR"),
        "why": (
            "RunnerOutputStream members; the terminal-evidence codec reads a "
            "stream back by its persisted value."
        ),
    },
    {
        "names": ("INTERACTIVE",),
        "why": (
            "An AgentExecutionCapability member; a manifest declares capabilities "
            "by value, and the vocabulary must stay whole to refuse the rest."
        ),
    },
    {
        "names": ("DETERMINISTIC", "WAIT", "SUBWORKFLOW", "ACTION"),
        "why": (
            "NodeKindV3 members; a workflow document names a node kind by value "
            "and the wire vocabulary must carry all five."
        ),
    },
    {
        "names": ("BLOCKED",),
        "why": (
            "A member of both NodeStateName and PersistedReceiptDisposition; a "
            "stored state is read back by value."
        ),
    },
    {
        "names": ("SCORECARD_POLICY", "SELECTION_POLICY", "ADMISSION_POLICY"),
        "why": (
            "RevisionKind members; a published revision names its kind by value, "
            "and the catalog refuses a kind this vocabulary does not carry."
        ),
    },
    {
        "names": ("REVISE",),
        "why": (
            "A Verdict member; a review answer is decoded from its value, and "
            "ACCEPTED alone would not be a verdict."
        ),
    },
    {
        "names": (
            "attempt_deadline_seconds",
            "reported_input_token_threshold",
            "reported_output_token_threshold",
        ),
        "why": (
            "Budget document fields read through BudgetField, whose members carry "
            "these exact names as their values (contracts/budgets_v3.py)."
        ),
    },
    {
        "names": ("from_output",),
        "why": (
            "A handover field pydantic binds from the workflow document; the "
            "document names the key, no Python reader names the attribute."
        ),
    },
    {
        "names": ("content_hash", "definition_hash"),
        "why": (
            "`field(init=False)` digests their own frozen dataclass writes with "
            '`object.__setattr__(self, "content_hash", ...)` in __post_init__.'
        ),
    },
    {
        "names": ("canonical_database_path",),
        "why": (
            "Read by DbosRuntimeBinding's generated `__eq__`: adapters/dbos/"
            "runtime.py refuses a second, incompatible binding by comparing the "
            "whole record."
        ),
    },
    {
        "names": ("create_sql",),
        "why": (
            "Read by `asdict()` in adapters/dbos/schema.py's product-schema "
            "fingerprint, whose sha256 is what refuses a malformed store."
        ),
    },
    {
        "names": (
            "tool_capability",
            "project_commit",
            "project_tree",
            "output_schema_document",
        ),
        "why": (
            "TypedDict keys adapters/dbos/node_binding_codec.py reads as string "
            "subscripts of the encoded binding, never as attributes."
        ),
    },
    {
        "names": ("agent_receipts",),
        "why": (
            "A sa.Table that registers itself in adapters/dbos/schema.py's shared "
            "MetaData on construction; every table in that module keeps a name."
        ),
    },
    {
        "names": ("buffer_size",),
        "why": (
            "The positional-only parameter of the `recv` contract runner/session.py "
            "and adapters/runner_core_transport.py state; an SSLSocket fills it."
        ),
    },
    {
        "names": ("checker",),
        "why": (
            "The TypeChecker jsonschema passes to a registered type check "
            "(contracts/schemas_v3.py); the library owns the signature."
        ),
    },
    {
        "names": ("openapi",),
        "why": "FastAPI reads `app.openapi` when it serves the document.",
    },
    {
        "names": ("disabled",),
        "why": "The stdlib `logging.Logger.disabled` flag host/logging.py sets.",
    },
)
