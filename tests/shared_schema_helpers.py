"""Remove only additive v6 objects when constructing genuine historical fixtures."""


def remove_v6(db):
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
