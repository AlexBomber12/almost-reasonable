"""Read a closed provider envelope while retaining duplicate-key evidence."""

import json


class ProposalDiagnostic(ValueError):
    """Fixed, text-free rejection category for untrusted provider data."""

    def __init__(self, code: str):
        self.code = code
        super().__init__("invalid provider proposal")


def provider_proposal(raw: str, family: str, max_bytes: int) -> dict:
    try:
        if type(raw) is not str or len(raw.encode("utf-8")) > max_bytes:
            raise ProposalDiagnostic(f"{family}_invalid_size")

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ProposalDiagnostic(f"{family}_duplicate_key")
                value[key] = item
            return value

        data = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except ProposalDiagnostic:
        raise
    except UnicodeError:
        raise ProposalDiagnostic(f"{family}_invalid_size") from None
    except (TypeError, ValueError, OverflowError):
        raise ProposalDiagnostic(f"{family}_invalid_json") from None
    if type(data) is not dict or set(data) != {"proposal"}:
        raise ProposalDiagnostic(f"{family}_invalid_envelope")
    if type(data["proposal"]) is not dict:
        raise ProposalDiagnostic(f"{family}_invalid_shape")
    return data["proposal"]
