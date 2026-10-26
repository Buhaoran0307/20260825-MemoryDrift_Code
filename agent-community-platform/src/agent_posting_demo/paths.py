from __future__ import annotations

from .config import PROJECT_ROOT, load_app_config


RUNTIME_CONFIG = load_app_config()
OUTPUT_DIR = RUNTIME_CONFIG.output_dir
TMP_DIR = RUNTIME_CONFIG.tmp_dir
DEFAULT_DB_PATH = RUNTIME_CONFIG.db_path
ROUND_OUTPUT_DIR = OUTPUT_DIR / "rounds"
CALL_OUTPUT_DIR = OUTPUT_DIR / "calls"


def ensure_workspace_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    ROUND_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CALL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
