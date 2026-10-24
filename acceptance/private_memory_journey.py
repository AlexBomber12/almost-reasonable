"""Synthetic B03D conversation assertions shared by offline and opt-in live runs."""

from collections.abc import Callable
import json


FIRST = "My fictional robot project is Amber. I prefer quiet motors."
REVISION = "I renamed my fictional robot project to Cobalt and now prefer silent gears."
QUESTION = (
    "Как называется мой текущий вымышленный проект робота и какое у меня "
    "предпочтение в конструкции? Используй сохранённые названия без перевода."
)
FILLERS = tuple(
    f"Synthetic unrelated garden inventory {index}: " + "clover " * 1050
    for index in range(5)
)
MAX_OPERATIONS = 3 * (len(FILLERS) + 4)


def _complete(send, message_id, content):
    result = send(message_id, content)
    if result.get("model_state") != "completed":
        raise AssertionError("synthetic conversation was not completed")
    return result


def _reply_projection(inspect_request):
    request = inspect_request()
    if request.get("task") != "private_conversation":
        raise AssertionError("actual provider request is not the reply projection")
    return request


def _remembered(request):
    memories = request.get("memories")
    if not isinstance(memories, list):
        raise AssertionError("memory projection is missing")
    return json.dumps(memories, ensure_ascii=False)


def run_robot_journey(
    send: Callable[[str, str], dict],
    restart_app: Callable[[], None],
    inspect_request: Callable[[], dict],
    delete_revision: Callable[[], dict],
    record_step: Callable[[str], None] | None = None,
) -> dict:
    """Prove persistent retrieval, bounded dialogue eviction and erasure."""
    _complete(send, "robot-original", FIRST)
    _complete(send, "robot-revision", REVISION)
    for index, filler in enumerate(FILLERS):
        _complete(send, f"robot-filler-{index}", filler)
    if record_step is not None:
        record_step("created_and_revised")
    restart_app()
    recalled = _complete(send, "robot-recall", QUESTION)
    request = _reply_projection(inspect_request)
    conversation = json.dumps(request.get("conversation", []), ensure_ascii=False)
    remembered = _remembered(request)
    if not {"candidate_useful_memory", "durable_interest"} <= {
        item.get("kind") for item in request["memories"] if isinstance(item, dict)
    }:
        raise AssertionError("both private memory families are not in the reply")
    if (
        "Amber" in conversation
        or "Cobalt" in conversation
        or "silent gears" in conversation
    ):
        raise AssertionError("bounded recent dialogue still contains the answer")
    if "Cobalt" not in remembered or "silent gears" not in remembered:
        raise AssertionError("persistent memories did not supply the answer")
    answer = recalled.get("response")
    if (
        not isinstance(answer, str)
        or "Cobalt" not in answer
        or "silent gears" not in answer
    ):
        raise AssertionError("current robot project was not recalled")
    if record_step is not None:
        record_step("recalled_after_restart")
    if delete_revision().get("status") != "applied":
        raise AssertionError("revision source was not deleted")
    _complete(send, "robot-after-delete", QUESTION)
    after = _reply_projection(inspect_request)
    if "Cobalt" in _remembered(after) or "silent gears" in _remembered(after):
        raise AssertionError("deleted revision still influences persistent memory")
    if record_step is not None:
        record_step("deleted_source_excluded")
    return {
        "steps": [
            "created_and_revised",
            "recalled_after_restart",
            "deleted_source_excluded",
        ]
    }
