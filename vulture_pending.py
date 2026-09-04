"""Names that wait for a decision an open item already owns.

Read as data by `scripts/check_dead_code.py`; never imported at runtime. Every
group carries the day it expires, and the gate turns red once that day arrives:
a parked decision is allowed to be slow, not permanent. A name that turns out to
be built ahead of a caller rather than awaiting a decision moves to
`vulture_frozen.py`; a name the decision retires is deleted with its code.
"""

WAITING_FOR_A_DECISION = (
    {
        "names": (
            "V9_SCHEMA_HANDOFF",
            "V10_SCHEMA_HANDOFF",
            "V11_SCHEMA_HANDOFF",
            "V12_SCHEMA_HANDOFF",
            "V13_SCHEMA_HANDOFF",
            "V14_SCHEMA_HANDOFF",
            "V15_SCHEMA_HANDOFF",
            "V16_SCHEMA_HANDOFF",
            "V17_SCHEMA_HANDOFF",
            "V18_SCHEMA_HANDOFF",
            "V19_SCHEMA_HANDOFF",
            "V20_SCHEMA_HANDOFF",
            "V21_SCHEMA_HANDOFF",
            "V22_SCHEMA_HANDOFF",
            "V23_SCHEMA_HANDOFF",
            "V24_SCHEMA_HANDOFF",
            "V25_SCHEMA_HANDOFF",
            "V26_SCHEMA_HANDOFF",
            "V27_SCHEMA_HANDOFF",
            "V28_SCHEMA_HANDOFF",
            "V29_SCHEMA_HANDOFF",
            "V30_SCHEMA_HANDOFF",
            "V31_SCHEMA_HANDOFF",
            "V32_SCHEMA_HANDOFF",
            "V33_SCHEMA_HANDOFF",
            "V34_SCHEMA_HANDOFF",
            "V35_SCHEMA_HANDOFF",
            "V36_SCHEMA_HANDOFF",
            "V37_SCHEMA_HANDOFF",
            "V38_SCHEMA_HANDOFF",
            "V39_SCHEMA_HANDOFF",
            "V40_SCHEMA_HANDOFF",
            "V41_SCHEMA_HANDOFF",
            "V42_SCHEMA_HANDOFF",
            "V43_SCHEMA_HANDOFF",
            "V44_SCHEMA_HANDOFF",
            "V45_SCHEMA_HANDOFF",
            "V46_SCHEMA_HANDOFF",
            "V47_SCHEMA_HANDOFF",
            "V48_SCHEMA_HANDOFF",
            "V49_SCHEMA_HANDOFF",
            "PRODUCT_SCHEMA_HANDOFF",
        ),
        "why": (
            "#1168 finding 8: the V9..V49 schema-handoff ledger is alive only in "
            "tests/integration/test_store_migration.py -- production migrates one "
            "hop and refuses every other version. #1168 finding 2a decides whether "
            "the migration ladder below the live store's version is deleted, and "
            "the ledger goes with it."
        ),
        "expires_on": "2026-10-04",
    },
    {
        "names": (
            "CONDUCTOR_MESSAGE_SCHEMA",
            "CONDUCTOR_REPORT_SCHEMA",
            "conductor_workflow_document",
        ),
        "why": (
            "#1168 finding 9: host/serving.py imports only the conductor's door "
            "name and tools; the loop document and both schemas are built by tests "
            "and tests/e2e/serve_cockpit.py. #1078 decides at its dispatch whether "
            "the conductor loop is parked for a slice or abandoned."
        ),
        "expires_on": "2026-10-04",
    },
)
