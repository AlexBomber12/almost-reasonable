"""Fixed public output diagnostics; no rejected text or provider exception strings."""

PUBLIC_PROPOSAL_REJECTIONS = frozenset(
    {
        "public_usage_invalid",
        "public_response_incomplete",
        "public_response_refused",
        "public_output_invalid",
        "public_json_invalid",
        "public_proposal_invalid",
        "public_mission_invalid",
        "public_research_invalid",
    }
)


class PublicOutputRejected(ValueError):
    def __init__(self, code, message):
        if type(code) is not str or code not in PUBLIC_PROPOSAL_REJECTIONS:
            raise ValueError("unsupported public rejection code")
        self.code = code
        super().__init__(message)
