from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agent_posting_core.llm import DEFAULT_LLM_TIMEOUT_SECONDS


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_API_BASE = "https://api.example.local/v1"
DEFAULT_MODEL = "demo-fallback-model"
DEFAULT_API_KEY = "demo-fallback-api-key"
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_TMP_DIR = "tmp"
DEFAULT_DB_PATH = "output/demo_forum.sqlite"
DEFAULT_LLM_CONCURRENCY = 2
DEFAULT_LLM_TEMPERATURE = 0.2
DEFAULT_LLM_MAX_TOKENS = 800


@dataclass(frozen=True)
class AppConfig:
    api_base: str
    model: str
    api_key: str
    output_dir: Path
    tmp_dir: Path
    db_path: Path
    llm_concurrency: int
    llm_timeout_seconds: float
    llm_temperature: float
    llm_max_tokens: int

    def public_dict(self) -> dict[str, str | bool | int | float]:
        return {
            "api_base": self.api_base,
            "model": self.model,
            "api_key_configured": bool(self.api_key),
            "output_dir": str(self.output_dir),
            "tmp_dir": str(self.tmp_dir),
            "db_path": str(self.db_path),
            "llm_concurrency": self.llm_concurrency,
            "llm_timeout_seconds": self.llm_timeout_seconds,
            "llm_temperature": self.llm_temperature,
            "llm_max_tokens": self.llm_max_tokens,
        }


def load_app_config(env_path: Path = ENV_FILE) -> AppConfig:
    values = read_env_file(env_path)

    def pick(name: str, fallback: str) -> str:
        return os.environ.get(name) or values.get(name) or fallback

    output_dir = resolve_project_path(pick("AGENT_COMMUNITY_OUTPUT_DIR", DEFAULT_OUTPUT_DIR))
    tmp_dir = resolve_project_path(pick("AGENT_COMMUNITY_TMP_DIR", DEFAULT_TMP_DIR))
    db_path = resolve_project_path(pick("AGENT_COMMUNITY_DB_PATH", DEFAULT_DB_PATH))
    llm_concurrency = _positive_int(
        "AGENT_COMMUNITY_LLM_CONCURRENCY",
        pick("AGENT_COMMUNITY_LLM_CONCURRENCY", str(DEFAULT_LLM_CONCURRENCY)),
    )
    llm_timeout_seconds = _positive_float(
        "AGENT_COMMUNITY_LLM_TIMEOUT_SECONDS",
        pick(
            "AGENT_COMMUNITY_LLM_TIMEOUT_SECONDS",
            str(DEFAULT_LLM_TIMEOUT_SECONDS),
        ),
    )
    llm_max_tokens = _positive_int(
        "AGENT_COMMUNITY_LLM_MAX_TOKENS",
        pick("AGENT_COMMUNITY_LLM_MAX_TOKENS", str(DEFAULT_LLM_MAX_TOKENS)),
    )
    try:
        llm_temperature = float(
            pick("AGENT_COMMUNITY_LLM_TEMPERATURE", str(DEFAULT_LLM_TEMPERATURE))
        )
    except ValueError as exc:
        raise ValueError("AGENT_COMMUNITY_LLM_TEMPERATURE must be a number") from exc

    return AppConfig(
        api_base=pick("AGENT_COMMUNITY_API_BASE", DEFAULT_API_BASE),
        model=pick("AGENT_COMMUNITY_MODEL", DEFAULT_MODEL),
        api_key=pick("AGENT_COMMUNITY_API_KEY", DEFAULT_API_KEY),
        output_dir=output_dir,
        tmp_dir=tmp_dir,
        db_path=db_path,
        llm_concurrency=llm_concurrency,
        llm_timeout_seconds=llm_timeout_seconds,
        llm_temperature=llm_temperature,
        llm_max_tokens=llm_max_tokens,
    )


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = strip_optional_quotes(value.strip())
        if key:
            values[key] = value
    return values


def strip_optional_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def resolve_project_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _positive_int(name: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be positive")
    return parsed


def _positive_float(name: str, value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed
