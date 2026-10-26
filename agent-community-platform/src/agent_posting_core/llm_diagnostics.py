"""Bounded, credential-redacted diagnostics for model HTTP failures."""
from __future__ import annotations

import copy
import http.client
import json
import re
from urllib.parse import quote, urlsplit, urlunsplit


ERROR_BODY_LIMIT = 16_384
_HEADER_NAMES = {
    "content-type", "date", "server", "retry-after", "request-id", "x-request-id",
    "x-correlation-id", "x-amzn-requestid", "cf-ray", "traceparent",
    "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests", "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens",
}
_PRIVATE_FIELDS = re.compile(
    r"^(?:authorization|proxy.authorization|api.?key|access.?token|refresh.?token|"
    r"token|password|secret|cookie|set.cookie|messages|prompt|input|request|"
    r"request_body|headers|tools)$", re.I,
)


class LLMHTTPError(RuntimeError):
    """Transport failure with safe diagnostics independent of exception chaining."""

    def __init__(self, message: str, diagnostics: dict) -> None:
        super().__init__(message)
        self.diagnostics = copy.deepcopy(diagnostics)


def redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        if not secret:
            continue
        for variant in {secret, json.dumps(secret, ensure_ascii=True)[1:-1],
                        quote(secret, safe="")}:
            text = text.replace(variant, "[REDACTED]")
            # A bounded response read may end in the middle of an echoed secret.
            for size in range(min(len(variant) - 1, len(text)), 3, -1):
                if text.endswith(variant[:size]):
                    text = text[:-size] + "[REDACTED]"
                    break
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[^\s\"'<>]+", r"\1 [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    return re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|"
        r"authorization|cookie)\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,;<>]+",
        r"\1[REDACTED]", text,
    )


def safe_endpoint(url: str, secrets: tuple[str, ...]) -> str:
    parts = urlsplit(url)
    return redact(urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1],
                             parts.path, "", "")), secrets)


def http_failure_detail(error, *, secrets: tuple[str, ...]) -> dict:
    """Read once, always close, and never retain request headers or raw bytes."""
    read_error = None
    try:
        raw = error.read(ERROR_BODY_LIMIT + 1)
    except http.client.IncompleteRead as exc:
        raw = exc.partial[:ERROR_BODY_LIMIT + 1]
        read_error = type(exc).__name__
    except OSError as exc:
        raw = b""
        read_error = type(exc).__name__
    finally:
        error.close()
    truncated = len(raw) > ERROR_BODY_LIMIT or read_error == "IncompleteRead"
    body = raw.decode("utf-8", errors="replace")

    def clean(value):
        if isinstance(value, dict):
            return {key: "[REDACTED]" if _PRIVATE_FIELDS.fullmatch(key) else clean(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    try:
        body = json.dumps(clean(json.loads(body)), ensure_ascii=False)
    except (ValueError, RecursionError):
        # Truncated JSON may echo a partial prompt that cannot match the full input.
        body = re.sub(
            r'''(?is)(["'](?:messages|prompt|input|request|request_body|headers|tools)["']\s*:).*''',
            r'\1 "[REDACTED]"', body,
        )
    body = redact(body, secrets)
    truncated = truncated or len(body) > ERROR_BODY_LIMIT
    headers = {
        name.lower(): redact(str(value), secrets)[:1024]
        for name, value in (error.headers.items() if error.headers else [])
        if name.lower() in _HEADER_NAMES
    }
    return {
        "status_code": error.code,
        "response_body": body[:ERROR_BODY_LIMIT],
        "response_body_truncated": truncated,
        "response_body_read_error": read_error,
        "response_headers": headers,
        "provider_request_id": next((headers[name] for name in (
            "x-request-id", "request-id", "x-amzn-requestid", "x-correlation-id",
        ) if name in headers), None),
    }
