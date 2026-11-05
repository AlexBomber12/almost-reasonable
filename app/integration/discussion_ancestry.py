"""Complete, bounded parent ancestry for private use of public discussion."""

from app.core.intellectual_state import MAX_DEPENDENCIES


def discussion_dependencies(experience, by_message, sources, owned_ids, dependencies):
    root = experience.conversation_id
    if root not in owned_ids:
        return None
    result = dict(dependencies)
    seen = {experience.message_id}
    parent = experience.parent_message_id
    # Each step adds a distinct verified source. Cycles and the dependency limit
    # bound traversal; a missing parent never becomes an unrestricted assertion.
    while parent is not None:
        if parent in seen:
            return None
        seen.add(parent)
        matches = [
            (key, record, binding)
            for key, record, binding in by_message.get(parent, ())
            if key in sources
            and sources[key]["binding"] == binding
            and (parent not in owned_ids or record.kind == "confirmed_publication")
        ]
        if not matches:
            return None
        identities = {
            (record.kind, record.text, record.parent_message_id, record.conversation_id)
            for _, record, _ in matches
        }
        if len(identities) != 1:
            return None
        for key, _record, binding in matches:
            result.update(sources[key]["dependencies"])
            result[key] = binding
        if len(result) > MAX_DEPENDENCIES:
            return None
        record = matches[0][1]
        if parent == root:
            return result if record.parent_message_id is None else None
        if record.conversation_id != root:
            return None
        parent = record.parent_message_id
    return None
