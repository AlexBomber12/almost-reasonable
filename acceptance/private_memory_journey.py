"""Synthetic B03D conversation assertions shared by offline and opt-in live runs."""

from collections.abc import Callable
import json


FIRST = "Мой вымышленный проект робота называется Янтарь. Я предпочитаю моторы вымышленной серии Шелест."
REVISION = "Я переименовал вымышленный проект робота в Кобальт и теперь предпочитаю шестерни вымышленной серии Ладога."
QUESTION = (
    "Как называется мой текущий вымышленный проект робота и какую вымышленную "
    "серию деталей я предпочитаю? Назови оба названия по-русски."
)
FILLERS = tuple(
    f"Вымышленная опись сада {index}: "
    + "клевер " * 80
    + " Если нужен ответ, кратко подтверди получение по-русски."
    for index in range(5)
)
MAX_OPERATIONS = 3 * (len(FILLERS) + 4)
_FIXED_STEPS = frozenset(
    {
        "robot-original",
        "robot-revision",
        "robot-recall",
        "robot-after-delete",
        "ship-name",
        "ship-check",
        "ship-mars",
        "combined-memory",
        "combined-story",
        "combined-continuation",
        "combined-recall",
        "combined-reality",
        "combined-after-delete",
    }
    | {f"robot-filler-{index}" for index in range(len(FILLERS))}
)


class JourneyOutcomeError(AssertionError):
    """A bounded, text-free diagnosis of a failed journey turn."""

    def __init__(self, step, state, *, operation_family=None, diagnostic_code=None):
        self.step = (
            step if isinstance(step, str) and step in _FIXED_STEPS else "<invalid>"
        )
        self.operation_family = (
            operation_family
            if operation_family
            in {"evolution", "typed", "narrative", "reply", "unknown"}
            else "unknown"
            if operation_family is not None
            else None
        )
        if diagnostic_code is None:
            self.diagnostic_code = None
        else:
            from app.cognition.worker_typed import TYPED_DIAGNOSTIC_CODES
            from app.cognition.worker_narrative import NARRATIVE_DIAGNOSTIC_CODES
            from app.cognition.worker_evolution import EVOLUTION_DIAGNOSTIC_CODES
            from app.cognition.worker_extraction import EXTRACTION_DIAGNOSTIC_CODES
            from app.cognition.worker_shared import SHARED_DIAGNOSTIC_CODES

            self.diagnostic_code = (
                diagnostic_code
                if diagnostic_code
                in (
                    TYPED_DIAGNOSTIC_CODES
                    | NARRATIVE_DIAGNOSTIC_CODES
                    | EVOLUTION_DIAGNOSTIC_CODES
                    | EXTRACTION_DIAGNOSTIC_CODES
                    | SHARED_DIAGNOSTIC_CODES
                )
                else "unknown"
            )
        self.model_state = (
            state
            if isinstance(state, str)
            and state.isascii()
            and state.replace("_", "").isalnum()
            and len(state) <= 32
            else "<invalid>"
        )
        super().__init__(f"{self.step}: invalid model_state={self.model_state}")


def _complete(send, message_id, content):
    result = send(message_id, content)
    if (
        not isinstance(result, dict)
        or result.get("model_state") != "completed"
        or not isinstance(result.get("response"), str)
        or not result["response"].strip()
    ):
        raise JourneyOutcomeError(
            message_id, result.get("model_state") if isinstance(result, dict) else None
        )
    return result


def _filler(send, message_id, content):
    result = send(message_id, content)
    if not isinstance(result, dict):
        raise JourneyOutcomeError(message_id, None)
    state = result.get("model_state")
    response = result.get("response")
    if (state == "completed" and isinstance(response, str) and response.strip()) or (
        state == "no_action" and response is None
    ):
        return
    raise JourneyOutcomeError(message_id, state)


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
        _filler(send, f"robot-filler-{index}", filler)
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
        "Янтарь" in conversation
        or "Кобальт" in conversation
        or "Шелест" in conversation
        or "Ладога" in conversation
    ):
        raise AssertionError("bounded recent dialogue still contains the answer")
    if "Кобальт" not in remembered or "Ладога" not in remembered:
        raise AssertionError("persistent memories did not supply the answer")
    answer = recalled.get("response")
    if not isinstance(answer, str) or "Кобальт" not in answer or "Ладога" not in answer:
        raise AssertionError("current robot project was not recalled")
    if record_step is not None:
        record_step("recalled_after_restart")
    if delete_revision().get("status") != "applied":
        raise AssertionError("revision source was not deleted")
    _complete(send, "robot-after-delete", QUESTION)
    after = _reply_projection(inspect_request)
    if "Кобальт" in _remembered(after) or "Ладога" in _remembered(after):
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
