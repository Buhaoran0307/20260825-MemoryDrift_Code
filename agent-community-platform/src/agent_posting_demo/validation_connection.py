"""Stage-two connection overrides keep the selected model and memory unchanged."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


def normalize_backend_connection(value: Any) -> dict[str, str]:
    if value is None:
        return {"mode": "inherit"}
    if not isinstance(value, dict):
        raise ValueError("validation.backend_connection must be an object")
    mode = value.get("mode", "inherit")
    if mode not in ("inherit", "environment", "custom"):
        raise ValueError("validation.backend_connection.mode must be inherit, environment, or custom")
    allowed = {"mode"}
    if mode != "inherit":
        allowed.update({"api_key_env", "api_base_env" if mode == "environment" else "api_base"})
    if set(value) - allowed:
        raise ValueError("validation.backend_connection contains unsupported fields")
    result = {"mode": mode}
    if mode == "inherit":
        return result
    for key, default in (("api_key_env", "AGENT_COMMUNITY_API_KEY"),
                         ("api_base_env", "AGENT_COMMUNITY_API_BASE")):
        if key == "api_base_env" and mode != "environment":
            continue
        name = value.get(key, default)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"validation.backend_connection.{key} must be an environment variable name")
        result[key] = name
    if mode == "custom":
        url = value.get("api_base")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("validation.backend_connection.api_base is required")
        url = url.strip().rstrip("/")
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                     and parsed.port != 0 and parsed.username is None
                     and parsed.password is None and not parsed.query and not parsed.fragment
                     and not any(char.isspace() for char in url))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("validation.backend_connection.api_base must be an HTTP(S) URL without credentials, query, or fragment")
        result["api_base"] = url
    return result


def apply_backend_connection(backend: dict[str, Any], value: Any) -> dict[str, Any]:
    connection = normalize_backend_connection(value)
    result = dict(backend)
    if connection["mode"] == "inherit":
        return result
    result.pop("api_base", None)
    result.pop("api_base_env", None)
    result.update({key: value for key, value in connection.items() if key != "mode"})
    return result
