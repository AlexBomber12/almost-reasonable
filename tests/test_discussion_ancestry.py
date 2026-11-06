"""Parent-chain source union cannot drop a withheld or missing own ancestor."""

from dataclasses import replace

import pytest

from app.core.public_experience import PublicExperienceRecord
from app.integration.discussion_ancestry import discussion_dependencies


def record(identity, parent, *, kind="external_assertion", conversation="root"):
    return PublicExperienceRecord(
        kind,
        "2026-11-05T12:00:00+00:00",
        "Синтетическое обсуждение.",
        identity,
        parent,
        conversation,
    )


def indexed(records):
    by_message, sources = {}, {}
    for index, value in enumerate(records):
        key, binding = "source:" + str(index), "binding:" + str(index)
        by_message.setdefault(value.message_id, []).append((key, value, binding))
        sources[key] = {"binding": binding, "dependencies": {}}
    return by_message, sources


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "source_missing",
        "withheld_own_alias",
        "incoming_own_alias",
        "cycle",
        "conflict",
        "wrong_root",
        "root_not_owned",
        "root_is_reply",
        "no_parent",
        "binding",
        "overflow",
    ],
)
def test_unavailable_or_inconsistent_parent_chain_cannot_be_imported(case):
    root = record("root", None, kind="confirmed_publication", conversation=None)
    own = record("own", "root", kind="confirmed_publication")
    child = record("child", "own")
    by_message, sources = indexed([root, own])
    owned = {"root", "own"}
    if case == "missing":
        by_message.pop("own")
    elif case == "source_missing":
        sources.pop("source:1")
    elif case == "withheld_own_alias":
        # A verified external-stream alias cannot launder a withdrawn own reply.
        by_message["own"] = [
            ("source:1", replace(own, kind="external_assertion"), "binding:1")
        ]
    elif case == "incoming_own_alias":
        child = replace(child, message_id="own", parent_message_id="root")
    elif case == "cycle":
        by_message["own"] = [
            ("source:1", replace(own, parent_message_id="own"), "binding:1")
        ]
    elif case == "conflict":
        by_message["own"].append(
            ("source:1", replace(own, text="Иной ответ."), "binding:1")
        )
    elif case == "wrong_root":
        by_message["own"] = [
            ("source:1", replace(own, conversation_id="foreign"), "binding:1")
        ]
    elif case == "root_not_owned":
        owned.remove("root")
    elif case == "root_is_reply":
        by_message["root"] = [
            ("source:0", replace(root, parent_message_id="own"), "binding:0")
        ]
    elif case == "no_parent":
        child = replace(child, parent_message_id=None)
    elif case == "binding":
        sources["source:1"]["binding"] = "changed"
    else:
        sources["source:1"]["dependencies"] = {
            f"ancestor:{i}": "binding" for i in range(100)
        }
    assert (
        discussion_dependencies(child, by_message, sources, owned, {"child": "binding"})
        is None
    )


@pytest.mark.parametrize("length,admitted", [(1, True), (98, True), (99, False)])
def test_chain_limits_count_complete_sources_and_preserve_every_admitted_parent(
    length, admitted
):
    records = [record("root", None, kind="confirmed_publication", conversation=None)]
    for index in range(length):
        records.append(
            record(
                "parent-" + str(index),
                "root" if index == 0 else "parent-" + str(index - 1),
            )
        )
    by_message, sources = indexed(records)
    result = discussion_dependencies(
        record("child", "parent-" + str(length - 1)),
        by_message,
        sources,
        {"root"},
        {"child": "binding"},
    )
    if admitted:
        assert result == {
            "child": "binding",
            **{key: source["binding"] for key, source in sources.items()},
        }
    else:
        assert result is None


def test_identical_cross_stream_parent_records_keep_both_source_bindings():
    parent = record("parent", "root")
    by_message, sources = indexed(
        [
            record("root", None, kind="confirmed_publication", conversation=None),
            parent,
            parent,
        ]
    )
    result = discussion_dependencies(
        record("child", "parent"), by_message, sources, {"root"}, {}
    )
    assert result == {key: source["binding"] for key, source in sources.items()}
