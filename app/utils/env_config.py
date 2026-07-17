from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

try:
    from dotenv import load_dotenv as _python_load_dotenv
except Exception:  # pragma: no cover - optional dependency guard
    _python_load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
ENV_FILE_ENV_KEY = "SSAI_ENV_FILE"


@dataclass(frozen=True)
class EnvLoadResult:
    env_path: Path
    exists: bool
    loaded: bool


def load_project_env(*, override: bool = True, env_path: Path = ENV_PATH) -> EnvLoadResult:
    """Load only the project-root .env file.

    This intentionally does not use find_dotenv() or cwd-based discovery.
    """
    loaded = False
    if _python_load_dotenv is not None and env_path.exists():
        loaded = bool(_python_load_dotenv(dotenv_path=env_path, override=override))
    os.environ[ENV_FILE_ENV_KEY] = str(env_path)
    return EnvLoadResult(env_path=env_path, exists=env_path.exists(), loaded=loaded)


def read_project_env_file(env_path: Path = ENV_PATH) -> dict[str, str]:
    """Parse key/value pairs from the project-root .env without changing os.environ."""
    env: dict[str, str] = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def app_env(environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    return str(env.get("APP_ENV") or "dev").strip().lower()


def env_value(name: str, environ: Mapping[str, str] | None = None, default: str | None = None) -> str | None:
    env = environ if environ is not None else os.environ
    value = env.get(name)
    if value not in (None, ""):
        return str(value)
    return default


def resolve_path_value(value: str, *, project_root: Path = PROJECT_ROOT) -> Path:
    path = Path(str(value).strip())
    if path.is_absolute():
        return path
    return (project_root / path).resolve()


def validate_startup_env(
    *,
    env_path: Path = ENV_PATH,
    project_root: Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
    required_path_keys: tuple[str, ...] = ("CHAT_FILE", "UPLOAD_DIR"),
) -> list[str]:
    """Return startup env errors without exposing values.

    In prod, the project-root .env must exist and required storage paths must be
    present and absolute. In non-prod, required paths must still be present; if
    relative, callers may resolve them against project_root.
    """
    env = environ if environ is not None else os.environ
    mode = app_env(env)
    errors: list[str] = []

    if mode == "prod" and not env_path.exists():
        errors.append(f"missing project env file: {env_path}")

    for key in required_path_keys:
        raw = str(env.get(key) or "").strip()
        if not raw:
            errors.append(f"missing required path env: {key}")
            continue
        if mode == "prod" and not Path(raw).is_absolute():
            errors.append(f"relative path is not allowed in prod: {key}")

    return errors


def config_path(
    key: str,
    *,
    project_root: Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
) -> Path:
    raw = env_value(key, environ)
    if not raw:
        raise RuntimeError(f"required path env is missing: {key}")
    return resolve_path_value(raw, project_root=project_root)
