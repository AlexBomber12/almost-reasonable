"""Worker-only protected-file factory for the OpenAI SDK transport."""

from collections.abc import Callable
import os
from pathlib import Path
import re
import stat

from app.core.provider_contract import require_identifier
from app.trusted.openai_transport import (
    OPENAI_API_BASE_URL,
    OpenAIResponsesTransport,
    TrustedTransportConfigurationError,
)


OPENAI_CREDENTIAL_PATH = Path("/run/secrets/openai-api-key")
MAX_CREDENTIAL_BYTES = 512
PROTECTED_FILE_MODE = 0o400
_API_KEY = re.compile(rb"sk-[A-Za-z0-9_-]{16,508}\Z")


def read_protected_worker_file(path: Path, *, maximum_bytes: int) -> bytes:
    """Read one exact regular file without following links or leaking its content."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise TrustedTransportConfigurationError("protected file path is invalid")
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise TrustedTransportConfigurationError("protected file bound is invalid")
    try:
        parent = path.parent
        parent_stat = parent.lstat()
        if (
            parent.resolve(strict=True) != parent
            or not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or stat.S_IMODE(parent_stat.st_mode) != 0o700
        ):
            raise TrustedTransportConfigurationError("protected file parent is unsafe")
        if path.resolve(strict=True) != path:
            raise TrustedTransportConfigurationError("protected file is unsafe")
        before = path.lstat()
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except TrustedTransportConfigurationError:
        raise
    except OSError:
        raise TrustedTransportConfigurationError(
            "protected file is unavailable"
        ) from None
    try:
        current = os.fstat(descriptor)
        safe = (
            stat.S_ISREG(current.st_mode)
            and current.st_uid == os.geteuid()
            and stat.S_IMODE(current.st_mode) == PROTECTED_FILE_MODE
            and current.st_nlink == 1
            and (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino)
            and 0 < current.st_size <= maximum_bytes
        )
        if not safe:
            raise TrustedTransportConfigurationError("protected file is unsafe")
        content = os.read(descriptor, maximum_bytes + 1)
        if len(content) != current.st_size or len(content) > maximum_bytes:
            raise TrustedTransportConfigurationError("protected file is unsafe")
        return content
    except OSError:
        raise TrustedTransportConfigurationError(
            "protected file is unavailable"
        ) from None
    finally:
        os.close(descriptor)


def build_worker_openai_transport(
    *,
    project_id: str,
    organization_id: str,
    client_factory: Callable[..., object] | None = None,
) -> OpenAIResponsesTransport:
    """Construct the fixed-endpoint, zero-retry transport from the exact secret path."""

    try:
        require_identifier("project id", project_id)
        require_identifier("organization id", organization_id)
    except ValueError:
        raise TrustedTransportConfigurationError(
            "OpenAI billing scope is invalid"
        ) from None
    if not project_id.startswith("proj_") or not organization_id.startswith("org-"):
        raise TrustedTransportConfigurationError("OpenAI billing scope is invalid")
    credential = read_protected_worker_file(
        OPENAI_CREDENTIAL_PATH, maximum_bytes=MAX_CREDENTIAL_BYTES
    )
    if _API_KEY.fullmatch(credential) is None:
        raise TrustedTransportConfigurationError("OpenAI credential is malformed")
    if client_factory is None:
        try:
            from openai import OpenAI
        except ImportError:
            raise TrustedTransportConfigurationError(
                "OpenAI SDK is unavailable"
            ) from None
        client_factory = OpenAI
    try:
        client = client_factory(
            api_key=credential.decode("ascii"),
            base_url=OPENAI_API_BASE_URL,
            max_retries=0,
            organization=organization_id,
            project=project_id,
        )
    except Exception:
        raise TrustedTransportConfigurationError(
            "OpenAI SDK client construction failed"
        ) from None
    return OpenAIResponsesTransport(client)
