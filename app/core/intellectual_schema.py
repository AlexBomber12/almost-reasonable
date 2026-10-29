"""Additive agent-owned intellectual state schema for existing stores."""

SCHEMA_V14 = (
    "CREATE TABLE IF NOT EXISTS agent_intellectual_schema(version TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agent_intellectual_operations(\n        audience TEXT NOT NULL CHECK(audience IN ('private','public')), owner TEXT NOT NULL, operation TEXT NOT NULL,\n        context_binding TEXT NOT NULL, controls TEXT NOT NULL,\n        snapshot TEXT CHECK(snapshot IS NULL OR length(CAST(snapshot AS BLOB))<=65536), proposal TEXT CHECK(proposal IS NULL OR length(CAST(proposal AS BLOB))<=16384), metadata TEXT CHECK(metadata IS NULL OR length(CAST(metadata AS BLOB))<=2048), status TEXT NOT NULL CHECK(status IN ('reserved','received','accepted','rejected','unavailable','no_change')),\n        PRIMARY KEY(audience,owner,operation))",
    "CREATE TABLE IF NOT EXISTS agent_intellectual_revisions(\n        audience TEXT NOT NULL CHECK(audience IN ('private','public')), owner TEXT NOT NULL, item TEXT NOT NULL,\n        revision INTEGER NOT NULL CHECK(revision>=1), operation TEXT NOT NULL,\n        payload TEXT CHECK(payload IS NULL OR length(CAST(payload AS BLOB))<=32768), dependencies TEXT NOT NULL CHECK(length(CAST(dependencies AS BLOB))<=32768),\n        PRIMARY KEY(audience,owner,item,revision), UNIQUE(audience,owner,operation))",
)
