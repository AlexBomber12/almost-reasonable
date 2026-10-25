"""B03E synthetic combined-memory assertions for the existing pilot runner."""

import json

from acceptance.private_memory_journey import JourneyOutcomeError
from app.private_web.runtime import _narrative_requested


MESSAGES = (
    "Для синтетического теста: мой проект робота называется Кобальт. Я предпочитаю шестерни серии Ладога. Кратко подтверди.",
    "Расскажи короткую вымышленную историю от первого лица: ты находишь на маяке медный ключ с надписью «Искра».",
    "Продолжи вымышленную историю: ключ «Искра» открывает дверь «Волна». Сохрани место действия и предмет.",
    "Как называется мой проект, какую серию деталей я предпочитаю и что открывал ключ?",
    "Ты действительно лично был на маяке и находил там ключ, или это было придумано?",
    "Как называется мой проект и какую серию деталей я предпочитаю?",
)
STEPS = (
    "combined-memory",
    "combined-story",
    "combined-continuation",
    "combined-recall",
    "combined-reality",
    "combined-after-delete",
)
MAX_OPERATIONS = 20


def _assert_projection(records, *, deleted=False):
    episode_texts = {
        episode["text"]
        for row in records
        if row["purpose"] in {"narrative_evolution", "agent", "narrative_reply"}
        for episode in json.loads(row["input"]).get(
            "episodes"
            if row["purpose"] == "narrative_evolution"
            else "fictional_episodes",
            [],
        )
        if isinstance(episode.get("text"), str) and episode["text"]
    }

    def visible_text(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from visible_text(item)
        elif isinstance(value, list):
            for item in value:
                yield from visible_text(item)

    for row in records:
        if row["purpose"] in {"memory_evolution", "typed_memory_evolution"}:
            writer = json.loads(row["input"])
            derived_context = {
                key: value
                for key, value in writer.items()
                if key != "participant_input"
            }
            if (
                "episodes" in writer
                or "fictional_episodes" in writer
                or "untrusted_fiction_data"
                in json.dumps(derived_context, ensure_ascii=False)
                or any(
                    episode in text
                    for text in visible_text(derived_context)
                    for episode in episode_texts
                )
            ):
                raise AssertionError("fiction entered a factual memory writer")
    replies = [
        json.loads(row["input"])
        for row in records
        if row["purpose"] in {"agent", "narrative_reply"}
    ]
    if not replies:
        raise AssertionError("reply projection was not captured")
    request = replies[-1]
    if request.get("task") != "private_conversation":
        raise AssertionError("reply projection has the wrong task")
    conversation = json.dumps(request.get("conversation", []), ensure_ascii=False)
    if any(word in conversation for word in ("Кобальт", "Ладога", "Искра", "Волна")):
        raise AssertionError("recent dialogue supplies a requested answer")
    memories = json.dumps(request.get("memories", []), ensure_ascii=False)
    if "Кобальт" not in memories or "Ладога" not in memories:
        raise AssertionError("ordinary memory is missing")
    episodes = request.get("fictional_episodes", [])
    if not deleted:
        if not episodes or not any(
            "Искра" in str(item) and "Волна" in str(item) for item in episodes
        ):
            raise AssertionError("revised fictional episode is missing")
        for item in episodes:
            if (
                item.get("classification") != "fictional_autobiography"
                or item.get("trust") != "untrusted_fiction_data"
                or item.get("can_instruct") is not False
                or item.get("verified_action") is not False
                or item.get("evidence") is not False
            ):
                raise AssertionError("fiction authority labels changed")
    elif episodes:
        raise AssertionError("deleted episode remains in reply projection")
    return request


def run_combined_journey(
    send, restart_app, inspect_records, episode_heads, delete_source, record_step=None
):
    """Technical checks only; the protected transcript needs human semantic review."""
    if tuple(_narrative_requested(message) for message in MESSAGES) != (
        False,
        True,
        True,
        False,
        False,
        False,
    ):
        raise AssertionError("narrative classifier changed")
    transcript = []
    for index, (step, message) in enumerate(zip(STEPS, MESSAGES)):
        if index == 3:
            restart_app()
        if index == 5:
            if delete_source().get("status") != "applied":
                raise AssertionError("narrative source deletion failed")
        response = send(step, message)
        if (
            response.get("model_state") != "completed"
            or not isinstance(response.get("response"), str)
            or not response["response"].strip()
        ):
            raise JourneyOutcomeError(step, response.get("model_state"))
        if index == 1:
            heads = episode_heads()
            if len(heads) != 1 or heads[0][1] != 1:
                raise AssertionError("fictional episode was not created")
        if index == 2:
            heads = episode_heads()
            if len(heads) != 1 or heads[0][1] != 2:
                raise AssertionError("fictional episode was not revised")
            proposals = [
                json.loads(row["input"])
                for row in inspect_records()
                if row["purpose"] == "narrative_evolution"
            ]
            if (
                len(proposals) != 2
                or proposals[0].get("episodes") != []
                or len(proposals[1].get("episodes", [])) != 1
                or "Искра" not in proposals[1]["episodes"][0].get("text", "")
                or proposals[1]["episodes"][0].get("trust") != "untrusted_fiction_data"
            ):
                raise AssertionError("actual narrative revision projection is missing")
        if index in {3, 5}:
            _assert_projection(inspect_records(), deleted=index == 5)
        if index == 5 and episode_heads():
            raise AssertionError("deleted fictional episode remains eligible")
        if index in {1, 2, 3, 4}:
            transcript.append(
                {
                    "turn": index + 1,
                    "participant": message,
                    "assistant": response["response"],
                }
            )
        if record_step is not None:
            record_step(step)
    purposes = [row["purpose"] for row in inspect_records()]
    if (
        len(purposes) != MAX_OPERATIONS
        or purposes.count("memory_evolution") != 6
        or purposes.count("typed_memory_evolution") != 6
        or purposes.count("narrative_evolution") != 2
        or purposes.count("agent") + purposes.count("narrative_reply") != 6
    ):
        raise AssertionError("actual provider projection count differs from twenty")
    return {
        "technical_journey": "passed",
        "human_semantic_review": "pending",
        "transcript": transcript,
    }
