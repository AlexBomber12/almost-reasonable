"""One synthetic conversation journey shared by offline and approved live runs."""

import re
from collections.abc import Callable


FIRST = (
    "Придумай однословное русское имя кораблю робота Маяк, летящего к Юпитеру. "
    "Ответь только этим именем, одним словом."
)
_FORMAT = (
    "Ответь одной строкой ровно в формате «Корабль: <имя>; Назначение: <планета>»."
)
ASK = "Как называется корабль и к какой планете он летит? " + _FORMAT
MARS = "Сохрани имя корабля, но измени пункт назначения на Марс. " + _FORMAT
_NAME = re.compile(r"[А-ЯЁа-яё]+\Z")


def _destination_is(response, name, planet):
    return (
        isinstance(response, str)
        and re.fullmatch(
            rf"Корабль: {re.escape(name)}; Назначение: {planet}", response.strip()
        )
        is not None
    )


def run_ship_journey(
    send: Callable[[str, str], dict],
    history: Callable[[], list[dict]],
    restart_app: Callable[[], None],
    inspect_request: Callable[[], dict] | None = None,
    record_step: Callable[[str], None] | None = None,
    record_name: Callable[[str], None] | None = None,
) -> dict:
    """Require delivered answers and the actual post-restart provider transcript."""

    first = send("ship-name", FIRST)
    name = first.get("response")
    if first.get("model_state") != "completed" or not isinstance(name, str):
        raise AssertionError("ship naming was not delivered")
    name = name.strip()
    if _NAME.fullmatch(name) is None or name in FIRST:
        raise AssertionError("ship name must be a new Russian word")
    if record_name is not None:
        record_name(name)
    if record_step is not None:
        record_step("name")

    second = send("ship-check", ASK)
    if second.get("model_state") != "completed" or not _destination_is(
        second.get("response"), name, "Юпитер"
    ):
        raise AssertionError("ship name and Jupiter were not recalled")
    if record_step is not None:
        record_step("recall")

    restart_app()
    third = send("ship-mars", MARS)
    if third.get("model_state") != "completed" or not _destination_is(
        third.get("response"), name, "Марс"
    ):
        raise AssertionError("ship name and Mars were not delivered after restart")
    if record_step is not None:
        record_step("restart_mars")

    if inspect_request is not None:
        conversation = inspect_request()["conversation"]
        if [(part["role"], part["trust"]) for part in conversation] != [
            ("participant", "untrusted_conversational_data"),
            ("agent", "untrusted_conversational_data"),
            ("participant", "untrusted_conversational_data"),
            ("agent", "untrusted_conversational_data"),
            ("participant", "untrusted_conversational_data"),
        ]:
            raise AssertionError("provider transcript roles or trust labels differ")
        if [part["content"] for part in conversation] != [
            FIRST,
            first["response"],
            ASK,
            second["response"],
            MARS,
        ]:
            raise AssertionError("provider transcript content differs")
    persisted = history()
    if [part["role"] for part in persisted] != [
        "participant",
        "agent",
        "participant",
        "agent",
        "participant",
        "agent",
    ]:
        raise AssertionError("persisted message roles differ")
    if [part["content"] for part in persisted] != [
        FIRST,
        first["response"],
        ASK,
        second["response"],
        MARS,
        third["response"],
    ]:
        raise AssertionError("persisted message content differs")
    if record_step is not None:
        record_step("history")
    return {"ship_name": name, "steps": ["name", "recall", "restart_mars", "history"]}
