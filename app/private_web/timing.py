"""Request-local fixed-name durations; no payloads, identifiers or stored traces."""

from contextvars import ContextVar
from functools import wraps
from time import perf_counter


_ACTIVE = ContextVar("private_request_timing", default=None)


def timed_stage(name):
    if name not in {"context", "model_stage"}:
        raise ValueError("unknown private timing stage")

    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            stages = _ACTIVE.get()
            if stages is None:
                return function(*args, **kwargs)
            started = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                stages[name] = stages.get(name, 0) + (perf_counter() - started) * 1000

        return call

    return decorate


def timed_request(function):
    @wraps(function)
    def call(self, environ, start_response):
        stages = {}
        token = _ACTIVE.set(stages)
        started = perf_counter()

        def respond(status, headers):
            if status[:3] in {"200", "201"} and environ.get("PATH_INFO") in {
                "/api/messages",
                "/api/history",
            }:
                values = {"app": (perf_counter() - started) * 1000, **stages}
                headers = [
                    *headers,
                    (
                        "Server-Timing",
                        ", ".join(
                            f"{key};dur={value:.3f}" for key, value in values.items()
                        ),
                    ),
                ]
            return start_response(status, headers)

        try:
            return function(self, environ, respond)
        finally:
            _ACTIVE.reset(token)

    return call
