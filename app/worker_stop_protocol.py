"""Fixed, versioned wire tokens for the single worker Stop assertion."""

REQUEST = b"ASSERT-STOP/1\n"
ACK = b"STOP-CONFIRMED/1\n"
STOP_VERSIONS = frozenset(
    {
        "private-pilot-emergency-stop-v1",
        "public-model-stop-v1",
        "public-research-stop-v1",
    }
)
