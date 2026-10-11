"""Explicit injected, offline extraction fixture; no provider implementation."""

from collections.abc import Callable

from .contracts import ExtractionRequest, ExtractorIdentity


class SyntheticMemoryExtractor:
    """Record requests and produce controlled proposals from a local fixture."""

    def __init__(
        self,
        proposal: Callable[[ExtractionRequest], object],
        identity: ExtractorIdentity,
    ):
        self.identity = identity
        self._proposal = proposal
        self.requests: list[ExtractionRequest] = []

    def extract(self, request: ExtractionRequest) -> object:
        self.requests.append(request)
        return self._proposal(request)
