"""EventStore v6: protected Stage 0 shared graph, separate from private state."""

SCHEMA_V6 = (
    """ALTER TABLE agent_reply_operations ADD COLUMN shared_count INTEGER NOT NULL
        DEFAULT 0 CHECK(shared_count BETWEEN 0 AND 16)""",
    """CREATE TABLE shared_operations (
        operation_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        expected_revision INTEGER NOT NULL CHECK(expected_revision BETWEEN 0 AND 31),
        revision INTEGER NOT NULL CHECK(revision = expected_revision + 1),
        parent_operation TEXT REFERENCES shared_operations(operation_id),
        author_principal TEXT NOT NULL,
        purpose TEXT NOT NULL CHECK(purpose = 'conversation'),
        mission_evidence INTEGER NOT NULL CHECK(mission_evidence IN (0,1)),
        kind TEXT NOT NULL CHECK(kind IN ('durable_interest','belief_or_hypothesis',
            'goal_or_intention','unresolved_question')),
        status TEXT NOT NULL CHECK(status IN ('quarantined','active','invalidated')),
        reason TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK(schema_version = 1),
        screen_version TEXT,
        screen_result TEXT NOT NULL,
        source_count INTEGER NOT NULL CHECK(source_count BETWEEN 1 AND 50),
        audience_count INTEGER NOT NULL CHECK(audience_count BETWEEN 1 AND 16),
        decided_at TEXT NOT NULL,
        UNIQUE(item_id, revision, operation_id)
    )""",
    """CREATE UNIQUE INDEX shared_active_revision ON shared_operations(item_id, revision)
        WHERE status = 'active'""",
    """CREATE TABLE shared_payloads (
        operation_id TEXT PRIMARY KEY REFERENCES shared_operations(operation_id),
        value TEXT NOT NULL CHECK(typeof(value)='text' AND
            length(CAST(value AS BLOB)) BETWEEN 1 AND 4096),
        confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
        verification TEXT NOT NULL CHECK(verification='unverified')
    )""",
    """CREATE TABLE protected_shared_manifests (
        operation_id TEXT PRIMARY KEY REFERENCES shared_operations(operation_id),
        binding TEXT NOT NULL CHECK(json_valid(binding) AND length(CAST(binding AS BLOB)) BETWEEN 1 AND 32768)
    )""",
    """CREATE TABLE protected_shared_sources (
        operation_id TEXT NOT NULL REFERENCES shared_operations(operation_id),
        event_id TEXT NOT NULL REFERENCES event_envelopes(event_id),
        source_principal TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('supporting','counter','inherited')),
        classification TEXT NOT NULL CHECK(classification IN ('supporting','counter')),
        graph_version INTEGER NOT NULL CHECK(graph_version=1),
        PRIMARY KEY(operation_id, event_id),
        CHECK(role='inherited' OR role=classification)
    )""",
    """CREATE TABLE protected_shared_audiences (
        operation_id TEXT NOT NULL REFERENCES shared_operations(operation_id),
        principal_id TEXT NOT NULL,
        purpose TEXT NOT NULL CHECK(purpose='conversation'),
        PRIMARY KEY(operation_id, principal_id)
    )""",
    """CREATE TABLE protected_reply_shared (
        operation_id TEXT NOT NULL REFERENCES agent_reply_operations(operation_id),
        shared_operation TEXT NOT NULL REFERENCES shared_operations(operation_id),
        graph_version INTEGER NOT NULL CHECK(graph_version=1),
        PRIMARY KEY(operation_id, shared_operation)
    )""",
    """CREATE TABLE shared_mission_document (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        status TEXT NOT NULL CHECK(status IN ('ready','reassessment')),
        current_version TEXT NOT NULL,
        document TEXT CHECK(document IS NULL OR (typeof(document)='text' AND
            length(CAST(document AS BLOB)) BETWEEN 1 AND 67108864)),
        CHECK((status='ready') = (document IS NOT NULL))
    )""",
    """CREATE TABLE protected_mission_shared (
        proposal_id TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('proposal','confirmation')),
        shared_operation TEXT NOT NULL REFERENCES shared_operations(operation_id),
        graph_version INTEGER NOT NULL CHECK(graph_version=1),
        PRIMARY KEY(proposal_id, phase)
    )""",
)
