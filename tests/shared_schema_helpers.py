"""Remove later schema objects when constructing genuine historical fixtures."""

from app.core.store import _SCHEMA_V5


def remove_v8(db):
    db.execute("DROP TABLE private_extraction_receipts")
    db.execute("DROP TABLE private_extraction_audit")
    db.execute("DROP TABLE agent_reply_resolutions")
    db.execute(_SCHEMA_V5[0])


def remove_v7(db):
    remove_v8(db)
    db.execute("DROP TABLE protected_reply_generic")
    for index in (
        "generic_memory_sources_event",
        "generic_memory_parents_parent",
        "generic_memory_context_source",
        "generic_reply_count",
    ):
        db.execute(f"DROP INDEX {index}")
    for table in (
        "generic_memory_context",
        "generic_memory_parents",
        "generic_memory_sources",
        "generic_memory_payloads",
        "generic_memory_operations",
        "generic_memory_items",
    ):
        db.execute(f"DROP TABLE {table}")
    db.execute("ALTER TABLE agent_reply_operations DROP COLUMN generic_count")


def remove_v6(db):
    remove_v7(db)
    for table in (
        "protected_mission_shared",
        "shared_mission_document",
        "protected_reply_shared",
        "protected_shared_audiences",
        "protected_shared_sources",
        "shared_payloads",
        "protected_shared_manifests",
        "shared_operations",
    ):
        db.execute(f"DROP TABLE {table}")
    db.execute("ALTER TABLE agent_reply_operations DROP COLUMN shared_count")
