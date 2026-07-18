# tools/check_analytics_regression.py
# -*- coding: utf-8 -*-
# SIMS 분석/KPI 회귀 체크 도구
# 작성자: ChatGPT (2026-05-02)
# VERSION = "check_analytics_regression/2026-05-02-v1"
# 참고: 이 스크립트는SIMS 분석/KPI 회귀 여부를 점검하기 위한 도구입니다.

"""
Analytics/KPI regression checker.

기본 import/helper 확인:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_analytics_regression.py

실제 서비스 DB 조회 smoke test:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_analytics_regression.py --live

NLQ 라우팅까지 확인:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_analytics_regression.py --nlq

전체 확인:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_analytics_regression.py --live --nlq
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import re
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd


# ---------------------------------------------------------------------
# Project root 보정
# ---------------------------------------------------------------------
THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)


# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)

log = logging.getLogger("analytics_regression")


# ---------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class ServiceCase:
    name: str
    function_name: str
    params: dict[str, Any]
    expected_title_contains: str
    expected_meta_key: str | None = None
    expected_analysis_type: str | None = None
    expected_condition_tokens: tuple[str, ...] = ()
    require_seq_column: bool = True
    require_summary_md: bool = True
    require_message: bool = True
    allow_zero_rows: bool = False
    check_code_columns: bool = True


@dataclass
class NlqCase:
    query: str
    expected_action: str
    expected_analysis_type: str | None = None
    expected_meta_key: str | None = None
    expected_params: dict[str, Any] | None = None
    expected_condition_tokens: tuple[str, ...] = ()
    require_summary_md: bool = True
    require_message: bool = True
    allow_empty_meta_counts: bool = False

def _ok(name: str, detail: str = "") -> CheckResult:
    return CheckResult(name=name, ok=True, detail=detail)


def _fail(name: str, detail: str = "") -> CheckResult:
    return CheckResult(name=name, ok=False, detail=detail)


def _print_results(title: str, results: list[CheckResult]) -> int:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)

    failed = 0
    for r in results:
        mark = "OK " if r.ok else "FAIL"
        print(f"[{mark}] {r.name}")
        if r.detail:
            print(f"      {r.detail}")
        if not r.ok:
            failed += 1

    print("-" * 78)
    print(f"총 {len(results)}건 / 성공 {len(results) - failed}건 / 실패 {failed}건")
    return failed


def _safe_len_df(obj: Any) -> int | None:
    try:
        import pandas as pd
        if isinstance(obj, pd.DataFrame):
            return int(len(obj))
    except Exception:
        pass
    return None


def _payload_columns(payload: dict[str, Any]) -> list[str]:
    cols = payload.get("columns")
    if isinstance(cols, list) and cols:
        return [str(c) for c in cols]

    df_display = payload.get("df_display")
    try:
        import pandas as pd
        if isinstance(df_display, pd.DataFrame):
            return [str(c) for c in df_display.columns]
    except Exception:
        pass

    df = payload.get("df")
    try:
        import pandas as pd
        if isinstance(df, pd.DataFrame):
            return [str(c) for c in df.columns]
    except Exception:
        pass

    records = payload.get("records")
    if isinstance(records, list) and records and isinstance(records[0], dict):
        return [str(c) for c in records[0].keys()]

    return []


def _payload_df(payload: dict[str, Any]) -> Any:
    try:
        import pandas as pd
        for key in ("df", "df_display"):
            obj = payload.get(key)
            if isinstance(obj, pd.DataFrame):
                return obj
    except Exception:
        pass
    return None


def _code_column_dtype_problem(payload: dict[str, Any]) -> str:
    try:
        import pandas as pd
    except Exception:
        return ""

    df = _payload_df(payload)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return ""

    code_cols = [
        "제품코드",
        "제조사코드",
        "거래처코드",
        "매입처코드",
        "재고적용처코드",
        "보험코드",
        "표준코드",
        "바코드",
    ]
    bad_cols = [
        col for col in code_cols
        if col in df.columns and pd.api.types.is_numeric_dtype(df[col])
    ]
    if bad_cols:
        return f"코드 컬럼이 numeric dtype으로 변환됨: {bad_cols!r}"
    return ""


def _payload_row_count(payload: dict[str, Any]) -> int:
    meta = payload.get("meta") or {}
    for key in ("row_count_total", "row_count"):
        try:
            v = meta.get(key)
            if v is not None:
                return int(v)
        except Exception:
            pass

    for key in ("df_display", "df"):
        n = _safe_len_df(payload.get(key))
        if n is not None:
            return n

    records = payload.get("records")
    if isinstance(records, list):
        return len(records)

    return 0

def _condition_text_from_payload(payload: dict[str, Any]) -> str:
    """
    조회조건 검증용 문자열.
    분석/KPI는 condition/query_summary/summary_md/message/params 중
    어디에 조건이 들어가도 검증할 수 있게 한곳에 모은다.
    """
    if not isinstance(payload, dict):
        return ""

    meta = payload.get("meta") or {}
    params = payload.get("params") or {}

    parts = [
        meta.get("condition"),
        meta.get("query_summary"),
        meta.get("summary_md"),
        payload.get("message"),
        payload.get("data") if isinstance(payload.get("data"), str) else "",
        str(params or ""),
    ]

    text = " ".join(str(x or "") for x in parts)
    text = text.replace("\n", " ").replace("\r", " ")
    text = " ".join(text.split())

    # params에 20250101 형식으로 들어온 날짜도
    # expected_condition_tokens=("2025-01-01", ...)와 비교 가능하게 한다.
    text = _append_date_variants(text)

    return text

def _missing_condition_tokens(payload: dict[str, Any], tokens: tuple[str, ...]) -> list[str]:
    if not tokens:
        return []

    text = _condition_text_from_payload(payload)
    return [str(token) for token in tokens if str(token) not in text]


def _short_text(value: Any, limit: int = 140) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ")
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit] + "..."
    return text

def _append_date_variants(text: str) -> str:
    """
    condition_text 안의 YYYYMMDD / YYYYMM 값을
    YYYY-MM-DD / YYYY-MM 형태로도 같이 검사할 수 있게 보강한다.

    예:
    - 20250101 → 2025-01-01
    - 20251231 → 2025-12-31
    - 202501   → 2025-01
    """
    import re

    src = str(text or "")
    variants: list[str] = []

    for m in re.findall(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", src):
        y, mo, d = m
        variants.append(f"{y}-{mo}-{d}")

    for m in re.findall(r"(?<!\d)(20\d{2})(\d{2})(?!\d)", src):
        y, mo = m
        variants.append(f"{y}-{mo}")

    if variants:
        src += " " + " ".join(dict.fromkeys(variants))

    return src

# ---------------------------------------------------------------------
# Basic checks
# ---------------------------------------------------------------------
def run_basic_checks() -> list[CheckResult]:
    results: list[CheckResult] = []

    module_name = "app.services.analytics_sales_trend_service"
    required_functions = [
        "get_sales_trend_result",
        "get_sales_trend_summary_result",
        "get_sales_forecast_result",
        "get_stock_shortage_result",
    ]

    try:
        mod = importlib.import_module(module_name)
        results.append(_ok(f"import {module_name}"))
    except Exception as e:
        return [_fail(f"import {module_name}", f"{type(e).__name__}: {e}")]

    for fn_name in required_functions:
        fn = getattr(mod, fn_name, None)
        if callable(fn):
            results.append(_ok(f"{module_name}.{fn_name}"))
        else:
            results.append(_fail(f"{module_name}.{fn_name}", "callable 함수 없음"))

    # NLQ router 쪽 분석 action 해석 함수 확인
    try:
        from app.utils import env_config

        saved_env = {
            k: os.environ.get(k)
            for k in (
                "APP_ENV",
                "CHAT_FILE",
                "UPLOAD_DIR",
                "SSAI_STORAGE_ROOT",
                "LOG_FILE",
                "SIMS_LOG_FILE",
                "SSAI_INSTANCE_ID",
                "SSAI_ENV_FILE",
            )
        }
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "project"
            other = Path(td) / "other"
            chat_dir = root / "chat"
            upload_dir = root / "uploads"
            storage_dir = root / "storage"
            log_file = root / "logs" / "app.log"
            root.mkdir()
            other.mkdir()
            chat_dir.mkdir()
            upload_dir.mkdir()
            storage_dir.mkdir()
            log_file.parent.mkdir()
            project_env = root / ".env"
            project_chat = chat_dir / "root_chat_rooms.json"
            other_chat = other / "wrong_chat_rooms.json"
            project_env.write_text(
                "\n".join(
                    [
                        "APP_ENV=prod",
                        "SSAI_INSTANCE_ID=regression",
                        f"CHAT_FILE={project_chat}",
                        f"UPLOAD_DIR={upload_dir}",
                        f"SSAI_STORAGE_ROOT={storage_dir}",
                        f"LOG_FILE={log_file}",
                    ]
                ),
                encoding="utf-8",
            )
            (other / ".env").write_text(
                "\n".join(
                    [
                        f"CHAT_FILE={other_chat}",
                        f"UPLOAD_DIR={other / 'uploads'}",
                        f"SSAI_STORAGE_ROOT={other / 'storage'}",
                        f"LOG_FILE={other / 'logs' / 'app.log'}",
                    ]
                ),
                encoding="utf-8",
            )

            os.environ["CHAT_FILE"] = str(other_chat)
            os.environ["UPLOAD_DIR"] = str(other / "uploads")
            os.environ["SSAI_STORAGE_ROOT"] = str(other / "storage")
            os.environ["LOG_FILE"] = str(other / "logs" / "app.log")
            os.chdir(other)
            loaded = env_config.load_project_env(override=True, env_path=project_env)
            load_ok = (
                loaded.env_path == project_env
                and loaded.exists
                and os.environ.get("CHAT_FILE") == str(project_chat)
                and os.environ.get("UPLOAD_DIR") == str(upload_dir)
                and os.environ.get("SSAI_STORAGE_ROOT") == str(storage_dir)
                and os.environ.get("LOG_FILE") == str(log_file)
                and os.environ.get("SSAI_ENV_FILE") == str(project_env)
            )
            required_paths = ("CHAT_FILE", "UPLOAD_DIR", "SSAI_STORAGE_ROOT", ("LOG_FILE", "SIMS_LOG_FILE"))
            errors = env_config.validate_startup_env(env_path=project_env, project_root=root, environ=os.environ, required_path_keys=required_paths)
            if load_ok and not errors:
                results.append(_ok("project-root .env overrides cwd and OS env", f"env_file={project_env}"))
            else:
                results.append(_fail("project-root .env overrides cwd and OS env", f"load_ok={load_ok}, errors={errors}"))

            missing_errors = env_config.validate_startup_env(
                env_path=root / "missing.env",
                project_root=root,
                environ={
                    "APP_ENV": "prod",
                    "CHAT_FILE": str(project_chat),
                    "UPLOAD_DIR": str(upload_dir),
                    "SSAI_STORAGE_ROOT": str(storage_dir),
                    "LOG_FILE": str(log_file),
                },
                required_path_keys=required_paths,
            )
            relative_env = root / "relative.env"
            relative_env.write_text(
                "\n".join(
                    [
                        "APP_ENV=prod",
                        "CHAT_FILE=data/chat.json",
                        f"UPLOAD_DIR={upload_dir}",
                        "SSAI_STORAGE_ROOT=data/ssai_storage",
                        "LOG_FILE=logs/app.log",
                    ]
                ),
                encoding="utf-8",
            )
            relative_errors = env_config.validate_startup_env(
                env_path=relative_env,
                project_root=root,
                environ={},
                required_path_keys=required_paths,
            )
            blank_env = root / "blank.env"
            blank_env.write_text(
                "\n".join(
                    [
                        "APP_ENV=prod",
                        "CHAT_FILE=",
                        f"UPLOAD_DIR={upload_dir}",
                        "SSAI_STORAGE_ROOT=",
                        "LOG_FILE=",
                    ]
                ),
                encoding="utf-8",
            )
            blank_errors = env_config.validate_startup_env(
                env_path=blank_env,
                project_root=root,
                environ={},
                required_path_keys=required_paths,
            )
            missing_app_env = root / "missing_app.env"
            missing_app_env.write_text(
                "\n".join(
                    [
                        f"CHAT_FILE={project_chat}",
                        f"UPLOAD_DIR={upload_dir}",
                        f"SSAI_STORAGE_ROOT={storage_dir}",
                        f"LOG_FILE={log_file}",
                    ]
                ),
                encoding="utf-8",
            )
            blank_app_env = root / "blank_app.env"
            blank_app_env.write_text(
                "\n".join(
                    [
                        "APP_ENV=",
                        f"CHAT_FILE={project_chat}",
                        f"UPLOAD_DIR={upload_dir}",
                        f"SSAI_STORAGE_ROOT={storage_dir}",
                        f"LOG_FILE={log_file}",
                    ]
                ),
                encoding="utf-8",
            )
            unknown_app_env = root / "unknown_app.env"
            unknown_app_env.write_text(
                "\n".join(
                    [
                        "APP_ENV=production",
                        f"CHAT_FILE={project_chat}",
                        f"UPLOAD_DIR={upload_dir}",
                        f"SSAI_STORAGE_ROOT={storage_dir}",
                        f"LOG_FILE={log_file}",
                    ]
                ),
                encoding="utf-8",
            )
            app_env_errors = {
                "missing": env_config.validate_startup_env(env_path=missing_app_env, project_root=root, environ={}, required_path_keys=required_paths),
                "blank": env_config.validate_startup_env(env_path=blank_app_env, project_root=root, environ={}, required_path_keys=required_paths),
                "unknown": env_config.validate_startup_env(env_path=unknown_app_env, project_root=root, environ={}, required_path_keys=required_paths),
            }
            app_env_ok = True
            for mode in ("dev", "test", "prod"):
                mode_env = root / f"{mode}.env"
                mode_env.write_text(
                    "\n".join(
                        [
                            f"APP_ENV={mode}",
                            f"CHAT_FILE={project_chat}",
                            f"UPLOAD_DIR={upload_dir}",
                            f"SSAI_STORAGE_ROOT={storage_dir}",
                            f"LOG_FILE={log_file}",
                        ]
                    ),
                    encoding="utf-8",
                )
                if env_config.validate_startup_env(env_path=mode_env, project_root=root, environ={}, required_path_keys=required_paths):
                    app_env_ok = False
            alias_env = root / "alias.env"
            alias_log_file = root / "logs" / "sims-app.log"
            alias_env.write_text(
                "\n".join(
                    [
                        "APP_ENV=prod",
                        f"CHAT_FILE={project_chat}",
                        f"UPLOAD_DIR={upload_dir}",
                        f"SSAI_STORAGE_ROOT={storage_dir}",
                        f"SIMS_LOG_FILE={alias_log_file}",
                    ]
                ),
                encoding="utf-8",
            )
            no_log_env = root / "no_log.env"
            no_log_env.write_text(
                "\n".join(
                    [
                        "APP_ENV=prod",
                        f"CHAT_FILE={project_chat}",
                        f"UPLOAD_DIR={upload_dir}",
                        f"SSAI_STORAGE_ROOT={storage_dir}",
                    ]
                ),
                encoding="utf-8",
            )
            os.environ["LOG_FILE"] = str(other / "logs" / "os-app.log")
            os.environ["SIMS_LOG_FILE"] = str(other / "logs" / "os-sims-app.log")
            alias_errors = env_config.validate_startup_env(env_path=alias_env, project_root=root, environ=os.environ, required_path_keys=required_paths)
            no_log_errors = env_config.validate_startup_env(env_path=no_log_env, project_root=root, environ=os.environ, required_path_keys=required_paths)
            app_env_policy_ok = (
                any("missing required env: APP_ENV" in e for e in app_env_errors["missing"])
                and any("missing required env: APP_ENV" in e for e in app_env_errors["blank"])
                and any("invalid APP_ENV" in e for e in app_env_errors["unknown"])
                and app_env_ok
            )
            if app_env_policy_ok:
                results.append(_ok("APP_ENV required and allow-listed", "dev/test/prod accepted; missing/blank/unknown blocked"))
            else:
                results.append(_fail("APP_ENV required and allow-listed", f"errors={app_env_errors}, valid_modes_ok={app_env_ok}"))
            dev_path = env_config.config_path("CHAT_FILE", project_root=root, environ={"CHAT_FILE": "data/dev_chat.json"})
            storage_path = env_config.config_path("SSAI_STORAGE_ROOT", project_root=root, environ=env_config.read_project_env_file(project_env))
            log_path = env_config.config_path_any(("LOG_FILE", "SIMS_LOG_FILE"), project_root=root, environ=env_config.read_project_env_file(project_env))
            alias_log_path = env_config.config_path_any(("LOG_FILE", "SIMS_LOG_FILE"), project_root=root, environ=env_config.read_project_env_file(alias_env))
            policy_ok = (
                any("missing project env file" in e for e in missing_errors)
                and app_env_policy_ok
                and not alias_errors
                and alias_log_path == alias_log_file.resolve()
                and any("missing required path env: LOG_FILE" in e for e in no_log_errors)
                and any("relative path is not allowed in prod: CHAT_FILE" in e for e in relative_errors)
                and any("relative path is not allowed in prod: SSAI_STORAGE_ROOT" in e for e in relative_errors)
                and any("relative path is not allowed in prod: LOG_FILE" in e for e in relative_errors)
                and any("missing required path env: CHAT_FILE" in e for e in blank_errors)
                and any("missing required path env: SSAI_STORAGE_ROOT" in e for e in blank_errors)
                and any("missing required path env: LOG_FILE" in e for e in blank_errors)
                and dev_path == (root / "data" / "dev_chat.json").resolve()
                and storage_path == storage_dir.resolve()
                and log_path == log_file.resolve()
                and not (root / "data" / "ssai_storage").exists()
                and not (root / "logs" / "app.log").exists()
            )
            if policy_ok:
                results.append(_ok("prod env path validation and dev relative resolution", "storage/log missing/blank/relative prod paths blocked"))
            else:
                results.append(_fail("prod env path validation and dev relative resolution", f"missing={missing_errors}, relative={relative_errors}, blank={blank_errors}, dev_path={dev_path}"))
            os.chdir(old_cwd)
        os.chdir(old_cwd)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    except Exception as e:
        try:
            os.chdir(PROJECT_ROOT)
        except Exception:
            pass
        results.append(_fail("project-root .env policy helpers", f"{type(e).__name__}: {e}"))

    try:
        main_src = Path("app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
        mssql_src = Path("app/db/mssql_client.py").read_text(encoding="utf-8")
        auth_src = Path("app/services/ssai_auth_service.py").read_text(encoding="utf-8")
        company_admin_src = Path("app/services/ssai_company_admin_service.py").read_text(encoding="utf-8")
        storage_src = Path("app/services/ssai_storage_service.py").read_text(encoding="utf-8")
        logging_src = Path("app/utils/logging_setup.py").read_text(encoding="utf-8")
        company_tool_src = Path("tools/ssai_test_company_connection.py").read_text(encoding="utf-8")
        verify_tool_src = Path("tools/ssai_verify_admin_password.py").read_text(encoding="utf-8")
        korean_doc_call_matches = list(re.finditer(r"(?m)^_inject_korean_document_language_once\(\)\s*$", main_src))
        korean_doc_call_pos = korean_doc_call_matches[0].start() if korean_doc_call_matches else -1
        login_check_pos = main_src.find("if not require_login():")
        korean_doc_block_match = re.search(
            r"def _inject_korean_document_language_once\(\) -> None:.*?(?=\ndef _inject_base_css_once)",
            main_src,
            re.S,
        )
        korean_doc_block = korean_doc_block_match.group(0) if korean_doc_block_match else ""
        source_checks = [
            ("main auto dotenv removed", "load_dotenv(override=True)" not in main_src and 'ENV_PATH = APP_DIR / ".env"' not in main_src and "_DEFAULT_ENV_TEXT" not in main_src),
            ("main chat paths require config_path", 'CHAT_FILE         = str(_config_path("CHAT_FILE"))' in main_src and 'UPLOAD_DIR        = str(_config_path("UPLOAD_DIR"))' in main_src),
            ("startup env validation/logs present", "_STARTUP_REQUIRED_PATHS" in main_src and '("LOG_FILE", "SIMS_LOG_FILE")' in main_src and "[app.env.paths]" in main_src and "[app.env.user_paths]" in main_src),
            (
                "browser Korean document language guard",
                "def _inject_korean_document_language_once()" in main_src
                and "document.documentElement" in main_src
                and 'root.lang = "ko"' in main_src
                and 'root.setAttribute("translate", "no")' in main_src
                and 'root.classList.add("notranslate")' in main_src
                and 'meta.setAttribute("name", "google")' in main_src
                and 'meta.setAttribute("content", "notranslate")' in main_src
                and "unsafe_allow_javascript=True" in main_src
                and "__korean_document_language_loaded" in main_src
                and len(korean_doc_call_matches) == 1
                and korean_doc_call_pos >= 0
                and login_check_pos >= 0
                and korean_doc_call_pos < login_check_pos
                and "user_input" not in korean_doc_block
                and "stc.html(" not in korean_doc_block
            ),
            ("db cwd dotenv search removed", "find_dotenv" not in mssql_src),
            ("auth root env parser priority", "p = ENV_PATH" in auth_src and "return env.get(name) or os.environ.get(name) or default" in auth_src),
            ("company admin project env loader", "load_project_env(override=False)" in company_admin_src and "load_dotenv()" not in company_admin_src),
            ("APP_ENV allow-list enforced", "ALLOWED_APP_ENVS" in Path("app/utils/env_config.py").read_text(encoding="utf-8") and "missing required env: APP_ENV" in Path("app/utils/env_config.py").read_text(encoding="utf-8")),
            ("storage root has no data fallback", 'config_path("SSAI_STORAGE_ROOT"' in storage_src and "DEFAULT_STORAGE_ROOT" not in storage_src and "data/ssai_storage" not in storage_src),
            ("logger requires root env log path", "read_project_env_file()" in logging_src and "LOG_FILE is required" in logging_src and "Path(log_dir) / filename" not in logging_src and "C:\\\\" not in logging_src and "D:\\\\" not in logging_src),
            ("connection tool ignores cwd dotenv", "read_project_env_file()" in company_tool_src and "def load_dotenv" not in company_tool_src),
            ("password tool ignores cwd dotenv", "read_project_env_file()" in verify_tool_src and "def load_dotenv" not in verify_tool_src),
        ]
        for name, ok in source_checks:
            results.append(_ok(name) if ok else _fail(name, "source guard failed"))

        try:
            class _FakeLog:
                def __init__(self):
                    self.debug_calls = 0

                def debug(self, *args, **kwargs):
                    self.debug_calls += 1

            class _FakeSt:
                def __init__(self, *, fail: bool = False, loaded: bool = False):
                    self.session_state = {"__korean_document_language_loaded": True} if loaded else {}
                    self.fail = fail
                    self.html_calls = 0

                def html(self, *args, **kwargs):
                    self.html_calls += 1
                    if self.fail:
                        raise RuntimeError("html failed")

            if not korean_doc_block:
                raise AssertionError("helper block not found")

            fake_ok = _FakeSt()
            ns_ok = {"st": fake_ok, "log": _FakeLog()}
            exec(korean_doc_block, ns_ok)
            ns_ok["_inject_korean_document_language_once"]()

            fake_fail = _FakeSt(fail=True)
            ns_fail = {"st": fake_fail, "log": _FakeLog()}
            exec(korean_doc_block, ns_fail)
            ns_fail["_inject_korean_document_language_once"]()

            fake_loaded = _FakeSt(loaded=True)
            ns_loaded = {"st": fake_loaded, "log": _FakeLog()}
            exec(korean_doc_block, ns_loaded)
            ns_loaded["_inject_korean_document_language_once"]()

            behavior_ok = (
                fake_ok.html_calls == 1
                and fake_ok.session_state.get("__korean_document_language_loaded") is True
                and fake_fail.html_calls == 1
                and "__korean_document_language_loaded" not in fake_fail.session_state
                and fake_loaded.html_calls == 0
                and fake_loaded.session_state.get("__korean_document_language_loaded") is True
            )
            if behavior_ok:
                results.append(_ok("browser Korean document language helper behavior", "loaded flag is set only after successful st.html; already-loaded reruns skip injection"))
            else:
                results.append(_fail("browser Korean document language helper behavior", f"ok_calls={fake_ok.html_calls}, fail_state={fake_fail.session_state}, loaded_calls={fake_loaded.html_calls}"))
        except Exception as e:
            results.append(_fail("browser Korean document language helper behavior", f"{type(e).__name__}: {e}"))

        import types

        effective_block = re.search(r"def _effective_chat_file\(\).*?def _chat_storage_mode_enabled", main_src, re.S)
        partition_block = re.search(r"def _partitioned_chat_root\(chat_file: str \| None = None\).*?def _partitioned_rooms_file", main_src, re.S)
        if not effective_block or not partition_block:
            results.append(_fail("user legacy and partition chat path calculation", "function block not found"))
        else:
            ns: dict[str, Any] = {
                "Path": Path,
                "CHAT_FILE": str(PROJECT_ROOT / "tmp_chat" / "chat_rooms.json"),
                "get_current_user": lambda: types.SimpleNamespace(user_id=8),
            }
            exec(effective_block.group(0).rsplit("def _chat_storage_mode_enabled", 1)[0], ns)
            exec(partition_block.group(0).rsplit("def _partitioned_rooms_file", 1)[0], ns)
            effective = Path(ns["_effective_chat_file"]())
            root = ns["_partitioned_chat_root"](str(effective))
            expected_file = PROJECT_ROOT / "tmp_chat" / "user_8_chat_rooms.json"
            expected_root = PROJECT_ROOT / "tmp_chat" / "user_8"
            if effective == expected_file and root == expected_root:
                results.append(_ok("user legacy and partition chat path calculation", f"file={effective.name}, root={root.name}"))
            else:
                results.append(_fail("user legacy and partition chat path calculation", f"file={effective}, root={root}"))
    except Exception as e:
        results.append(_fail("project-root .env source guards", f"{type(e).__name__}: {e}"))

    try:
        from app.services import llm_health
        import time as _time

        class _FakeExc(Exception):
            def __init__(self, status_code: int | None = None, message: str = "fixture error without secrets"):
                super().__init__(message)
                if status_code is not None:
                    self.status_code = status_code

        secret_fixture = "sk-secret-fixture"
        classified = {
            "401": llm_health.classify_llm_exception(_FakeExc(401))["code"],
            "connection": llm_health.classify_llm_exception(type("APIConnectFixture", (Exception,), {})())["code"],
            "404": llm_health.classify_llm_exception(_FakeExc(404))["code"],
            "429": llm_health.classify_llm_exception(_FakeExc(429))["code"],
            "500": llm_health.classify_llm_exception(_FakeExc(500))["code"],
            "timeout": llm_health.classify_llm_exception(type("RequestTimeout", (Exception,), {})())["code"],
        }
        unknown_info = llm_health.classify_llm_exception(_FakeExc(None, f"raw {secret_fixture} http://127.0.0.1/body"))
        preset_unknown = RuntimeError(f"raw preset {secret_fixture}")
        setattr(preset_unknown, "llm_error_code", "not_a_known_code")
        preset_info = llm_health.classify_llm_exception(preset_unknown)

        class _Model:
            def __init__(self, model_id: str):
                self.id = model_id

        class _Models:
            def __init__(self, ids: list[str]):
                self._ids = ids
                self.calls = 0

            def list(self):
                self.calls += 1
                return type("ModelList", (), {"data": [_Model(x) for x in self._ids]})()

        class _Client:
            def __init__(self, ids: list[str]):
                self.models = _Models(ids)
                self.last_options = {}

            def with_options(self, **kwargs):
                self.last_options = dict(kwargs)
                return self

        class _TimeoutModels:
            def __init__(self):
                self.calls = 0

            def list(self):
                self.calls += 1
                raise TimeoutError("fast health timeout fixture")

        class _TimeoutClient:
            def __init__(self):
                self.models = _TimeoutModels()
                self.last_options = {}

            def with_options(self, **kwargs):
                self.last_options = dict(kwargs)
                return self

        ok_client = _Client(["model-a"])
        ok_status = llm_health.check_llm(expected_model="model-a", client=ok_client)
        empty_status = llm_health.check_llm(expected_model="model-a", client=_Client([]))
        mismatch_status = llm_health.check_llm(expected_model="model-a", client=_Client(["model-b"]))
        timeout_client = _TimeoutClient()
        health_t0 = _time.perf_counter()
        timeout_status = llm_health.check_llm(expected_model="model-a", client=timeout_client, health_timeout_s=4)
        health_elapsed = _time.perf_counter() - health_t0
        model_list_options_ok = (
            ok_client.last_options.get("max_retries") == 0
            and timeout_client.last_options.get("timeout") == 4
            and timeout_client.last_options.get("max_retries") == 0
            and timeout_client.models.calls == 1
        )

        class _Usage:
            prompt_tokens = 3
            completion_tokens = 5
            total_tokens = 8

        def _resp(content: str = "", reasoning: str = "", finish: str | None = None):
            msg = type("Msg", (), {"content": content, "reasoning_content": reasoning})()
            choice = type("Choice", (), {"message": msg, "finish_reason": finish})()
            return type("Resp", (), {"choices": [choice], "usage": _Usage()})()

        normal = llm_health.extract_chat_completion_text(_resp("ok", finish="stop"))
        reasoning_only = llm_health.extract_chat_completion_text(_resp("", "hidden reasoning"))
        empty = llm_health.extract_chat_completion_text(_resp(""))
        length = llm_health.extract_chat_completion_text(_resp("cut", finish="length"))
        retry_info = {"code": "timeout", "retryable": True}
        non_retry_info = {"code": "authentication_error", "retryable": False}
        retry_checks = {
            "bounded_none": llm_health.bounded_retry_count(None) == 0,
            "bounded_negative": llm_health.bounded_retry_count(-3) == 0,
            "bounded_high": llm_health.bounded_retry_count(9) == 1,
            "retry_before_content": llm_health.should_retry_llm_error(retry_info, attempt=0, max_retries=1, content_started=False),
            "no_retry_after_max": not llm_health.should_retry_llm_error(retry_info, attempt=1, max_retries=1, content_started=False),
            "no_retry_after_content": not llm_health.should_retry_llm_error(retry_info, attempt=0, max_retries=1, content_started=True),
            "no_retry_auth": not llm_health.should_retry_llm_error(non_retry_info, attempt=0, max_retries=1, content_started=False),
        }
        def _simulated_attempts(err_info: dict, *, max_retries: int, content_started: bool = False) -> int:
            calls = 0
            for attempt in range(llm_health.bounded_retry_count(max_retries) + 1):
                calls += 1
                if not llm_health.should_retry_llm_error(err_info, attempt=attempt, max_retries=max_retries, content_started=content_started):
                    break
            return calls

        retry_call_counts = {
            "non_retry_total_1": _simulated_attempts(non_retry_info, max_retries=1) == 1,
            "retryable_total_2": _simulated_attempts(retry_info, max_retries=1) == 2,
            "partial_total_1": _simulated_attempts(retry_info, max_retries=1, content_started=True) == 1,
        }

        main_src = Path("app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8", errors="replace")
        top_src = "\n".join(main_src.splitlines()[:280])
        model_ready_block = main_src[main_src.find("def _llm_model_ready"):main_src.find("def _fallback_login_greeting")]
        protected_block = main_src[main_src.find("def call_chat_protected"):main_src.find("# =========================================================", main_src.find("def call_chat_protected") + 1)]
        stream_call_start = main_src.find("response_stream = call_chat_protected")
        stream_call_end = main_src.find(")", stream_call_start)
        stream_call_block = main_src[stream_call_start:stream_call_end]
        get_models_start = main_src.find("def _get_models_cached()")
        get_models_end = main_src.find("# =========================", get_models_start + 1)
        get_models_block = main_src[get_models_start:get_models_end]
        source_guards = {
            "top_health_lazy": "LLM_STATUS = {\"ok\": None" in top_src and "LLM_STATUS = check_llm(" not in top_src,
            "model_cache_ttl_short": "@st.cache_data(ttl=5)" in main_src and "def _get_models_cached" in main_src,
            "model_list_timeout": "with_options" in get_models_block and "LLM_HEALTH_TIMEOUT_S" in get_models_block,
            "model_list_sdk_retry_off": "max_retries=0" in get_models_block,
            "client_sdk_retry_off": "CLIENT = OpenAI(" in main_src and "max_retries=0" in main_src,
            "model_ready_no_duplicate_check": "check_llm(" not in model_ready_block,
            "call_protected_no_unused_client": "cli = getattr(CLIENT" not in protected_block,
            "stream_inner_retry_off": "max_retry=0" in stream_call_block,
            "expected_model_fixed_ui": "st.session_state.selected_model = EXPECTED_LM_MODEL" in main_src and "st.expander(" in main_src,
            "stream_no_retry_after_tokens": "if collected:\n                    final_text = \"\".join(collected).strip()" in main_src,
            "reasoning_delta_guard": "reasoning_content" in main_src and "reasoning_seen" in main_src,
            "login_fallback_ready_check": "ready, _ready_message = _llm_model_ready(EXPECTED_LM_MODEL)" in main_src,
            "wait_placeholder": "wait_slot = st.empty()" in main_src and "_clear_wait_notice()" in main_src,
            "no_local_model_runtime_fallback": 'or "local-model"' not in main_src.replace('#                model=model or st.session_state.get("selected_model") or "local-model"', ""),
        }

        mismatches = []
        expected_codes = {
            "401": "authentication_error",
            "connection": "connection_error",
            "404": "model_not_found",
            "429": "rate_or_queue_busy",
            "500": "server_error",
            "timeout": "timeout",
        }
        for key, expected in expected_codes.items():
            if classified.get(key) != expected:
                mismatches.append(f"{key}->{classified.get(key)}")
        if not ok_status.get("ok"):
            mismatches.append("expected model should be ok")
        if empty_status.get("code") != "model_not_loaded":
            mismatches.append(f"empty models code={empty_status.get('code')}")
        if mismatch_status.get("code") != "model_mismatch":
            mismatches.append(f"mismatch code={mismatch_status.get('code')}")
        if timeout_status.get("code") != "timeout" or not model_list_options_ok or health_elapsed > 0.5:
            mismatches.append(f"health timeout/retry guard failed code={timeout_status.get('code')} options={timeout_client.last_options} calls={timeout_client.models.calls} elapsed={health_elapsed:.3f}")
        if secret_fixture in str(unknown_info.get("user_message")) or unknown_info.get("code") != "unknown_error":
            mismatches.append("unknown exception leaked raw user message")
        if secret_fixture in str(preset_info.get("user_message")) or preset_info.get("user_message") != llm_health.SAFE_MESSAGES["unknown_error"]:
            mismatches.append("preset unknown exception leaked raw user message")
        if normal.get("content") != "ok" or normal.get("usage", {}).get("completion_tokens") != 5:
            mismatches.append("normal content/usage extraction failed")
        if reasoning_only.get("code") != "reasoning_only" or reasoning_only.get("content"):
            mismatches.append("reasoning-only guard failed")
        if empty.get("code") != "empty_response":
            mismatches.append("empty response guard failed")
        if length.get("finish_reason") != "length":
            mismatches.append("finish_reason length not preserved")
        failed_retry = [k for k, ok in retry_checks.items() if not ok]
        if failed_retry:
            mismatches.append(f"retry_checks={failed_retry}")
        failed_retry_counts = [k for k, ok in retry_call_counts.items() if not ok]
        if failed_retry_counts:
            mismatches.append(f"retry_call_counts={failed_retry_counts}")
        failed_guards = [k for k, ok in source_guards.items() if not ok]
        if failed_guards:
            mismatches.append(f"source_guards={failed_guards}")

        if mismatches:
            results.append(_fail("LM Studio 0.4.19 stability guards", "; ".join(mismatches)))
        else:
            results.append(_ok("LM Studio 0.4.19 stability guards", "health classification, model mismatch, response guards, and retry source guards verified"))
    except Exception as e:
        results.append(_fail("LM Studio 0.4.19 stability guards", f"{type(e).__name__}: {e}"))

    try:
        router = importlib.import_module("app.sims.nlq.nlq_router")
        resolve = getattr(router, "_resolve_analytics_action", None)
        if callable(resolve):
            tests = [
                ("품목별 매출 추세 2025년 조회", "품목별 매출 추세 분석"),
                ("품목별 매출 추세 요약표 2025년 조회", "품목별 매출 추세 요약표"),
                ("품목별 매출 예상 2025년 조회", "품목별 매출 예상"),
                ("매출처별 매출 예상 2025년 조회", "매출처별 매출 예상"),
                ("영업사원별 매출 예상 2025년 조회", "영업사원별 매출 예상"),
                ("지역별 매출 예상 2025년 조회", "지역별 매출 예상"),
                ("품목별 재고부족현황 2025년 조회", "품목별 재고부족현황"),
                ("매입처별 재고부족 현황 2025년 조회", "매입처별 재고부족 현황"),
            ]
            for q, expected in tests:
                got = resolve(q)
                if got == expected:
                    results.append(_ok(f"analytics action resolver: {q}", got))
                else:
                    results.append(_fail(f"analytics action resolver: {q}", f"expected={expected!r}, got={got!r}"))
        else:
            results.append(_fail("analytics action resolver", "_resolve_analytics_action 없음"))
    except Exception as e:
        results.append(_fail("analytics action resolver", f"{type(e).__name__}: {e}"))

    try:
        old_current_yyyymm = getattr(mod, "_current_yyyymm", None)
        setattr(mod, "_current_yyyymm", lambda: "202607")

        def _row(month: str, amt: int, qty: int = 1, product_code: str = "0001", buy_cd: str = "B1") -> dict[str, Any]:
            return {"기준월": month, "제품코드": product_code, "제품명": "테스트", "규격": "EA", "제조사코드": "M1", "제조사명": "제조사", "제품그룹명": "G", "제품구분명": "D", "제품분류명": "C", "매입처코드": buy_cd, "출고수량": qty, "출고할증수량": 0, "매출공급가액": amt, "매출세액": 0, "매출합계": amt, "집계건수": 1}

        raw_df = pd.DataFrame(
            [
                _row("202601", 100),
                _row("202602", 100),
                _row("202603", 100),
                _row("202604", 200),
                _row("202605", 200),
                _row("202606", 200),
                _row("202607", 300),
                _row("202608", 999),
            ]
        )
        summary_df = mod.get_sales_trend_summary_df(
            {
                "month_from": "202601",
                "month_to": "202608",
                "date_from": "20260101",
                "date_to": "20260831",
                "source_mode": "monthly_book",
            },
            raw_df=raw_df,
        )
        row = summary_df.iloc[0].to_dict()

        expected = {
            "완료월수": 6,
            "완료월총매출": 900,
            "완료월평균매출": 150,
            "월평균매출": 150,
            "최근3개월평균매출": 200,
            "최근6개월평균매출": 150,
            "최근3개월증감률": 33.3333333333,
            "당월 현재매출": 300,
            "당월 예상매출": 230,
            "당월 잔여예상": 0,
            "당월 진척률": 130.4347826087,
        }
        mismatches = []
        for key, exp in expected.items():
            got = float(row.get(key) or 0)
            if abs(got - float(exp)) > 1e-6:
                mismatches.append(f"{key}: expected={exp}, got={got}")

        if mismatches:
            results.append(_fail("sales period policy current/future exclusion", "; ".join(mismatches)))
        else:
            meta = mod._forecast_meta_from_df(summary_df)
            period_meta = mod._period_policy_meta_from_summary_df(summary_df)
            if int(meta.get("month_count") or 0) != 7 or int(period_meta.get("completed_month_count") or 0) != 6:
                results.append(_fail("sales period policy month counters", f"month_count={meta.get('month_count')}, completed={period_meta.get('completed_month_count')}"))
            elif str(row.get("추세판정") or "") != "증가":
                results.append(_fail("sales period policy trend judge", f"expected='증가', got={row.get('추세판정')!r}"))
            else:
                results.append(_ok("sales period policy current/future exclusion", "current=202607 and future=202608 excluded; forecast rate applied"))

        trend_df = mod._add_trend_columns(raw_df)
        period_values = dict(zip(trend_df["기준월"], trend_df["기간구분"]))
        rolling_values = dict(zip(trend_df["기준월"], trend_df["최근3개월평균매출"]))
        rolling_ok = (
            abs(float(rolling_values.get("202604") or 0) - 100) < 1e-6
            and abs(float(rolling_values.get("202607") or 0) - 200) < 1e-6
            and len(set(round(float(v), 6) for v in rolling_values.values())) > 1
        )
        period_ok = (
            period_values.get("202606") == "완료월"
            and period_values.get("202607") == "당월진행"
            and period_values.get("202608") == "미래월"
        )
        if rolling_ok and period_ok:
            results.append(_ok("sales trend detail period labels and rolling averages", "period labels set; row-wise rolling averages preserved"))
        else:
            results.append(_fail("sales trend detail period labels and rolling averages", f"periods={period_values}, rolling={rolling_values}"))

        duplicate_vendor_df = pd.DataFrame(
            [
                _row("202601", 200_000_000, 500, "00439", "B1"),
                _row("202601", 270_721_841, 500, "00439", "B2"),
                _row("202602", 100_000_000, 300, "00439", "B1"),
                _row("202602", 210_956_793, 458, "00439", "B2"),
                _row("202603", 284_213_860, 750, "00439", "B1"),
                _row("202604", 402_633_120, 770, "00439", "B1"),
                _row("202605", 420_000_000, 780, "00439", "B1"),
                _row("202606", 449_938_280, 790, "00439", "B1"),
                _row("202607", 200_886_348, 800, "00439", "B1"),
            ]
        )
        duplicate_trend = mod._add_trend_columns(duplicate_vendor_df)
        feb_rows = duplicate_trend[duplicate_trend["기준월"] == "202602"]
        jun_row = duplicate_trend[duplicate_trend["기준월"] == "202606"].iloc[0]
        jul_row = duplicate_trend[duplicate_trend["기준월"] == "202607"].iloc[0]
        duplicate_checks = [
            ("2026-02 전월대비매출", set(feb_rows["전월대비매출"].round(2).tolist()), {-159_765_048.0}),
            ("2026-02 전월대비수량", set(feb_rows["전월대비수량"].round(2).tolist()), {-242.0}),
            ("2026-02 최근3개월평균매출", set(feb_rows["최근3개월평균매출"].round(2).tolist()), {470_721_841.0}),
        ]
        duplicate_mismatches = []
        for label, got, exp in duplicate_checks:
            if got != exp:
                duplicate_mismatches.append(f"{label}: expected={exp}, got={got}")
        expected_points = {
            "2026-06 최근3개월평균매출": (jun_row.get("최근3개월평균매출"), 368_948_993.33),
            "2026-06 최근6개월평균매출": (jun_row.get("최근6개월평균매출"), 377_705_122.8),
            "2026-07 최근3개월평균매출": (jul_row.get("최근3개월평균매출"), 424_190_466.67),
            "2026-07 최근6개월평균매출": (jul_row.get("최근6개월평균매출"), 389_743_982.33),
        }
        for label, (got, exp) in expected_points.items():
            if abs(float(got or 0) - exp) > 0.01:
                duplicate_mismatches.append(f"{label}: expected={exp}, got={got}")
        if duplicate_mismatches:
            results.append(_fail("sales trend detail monthly aggregate metrics", "; ".join(duplicate_mismatches)))
        else:
            results.append(_ok("sales trend detail monthly aggregate metrics", "duplicate vendor rows share product-month metrics"))

        month_point_mismatches = []
        required_month_cols = ["월시점 증감률", "월시점 추세판정", "월시점 판정결과", "추세판정", "판정결과"]
        for c in required_month_cols:
            if c not in duplicate_trend.columns:
                month_point_mismatches.append(f"missing column {c}")
        if not month_point_mismatches:
            feb_judges = set(feb_rows["월시점 추세판정"].astype(str).tolist())
            feb_compat_judges = set(feb_rows["추세판정"].astype(str).tolist())
            if len(feb_judges) != 1 or feb_judges != feb_compat_judges:
                month_point_mismatches.append(f"202602 duplicate rows judge mismatch: {feb_judges}, compat={feb_compat_judges}")
            feb_expected_values = set(feb_rows["월시점 예상매출"].round(2).tolist())
            if len(feb_expected_values) != 1:
                month_point_mismatches.append(f"202602 duplicate rows expected sales mismatch: {feb_expected_values}")
            if str(jun_row.get("월시점 추세판정") or "") != "안정":
                month_point_mismatches.append(f"202606 expected 안정, got={jun_row.get('월시점 추세판정')!r}")
            if str(jul_row.get("월시점 추세판정") or "") != "안정":
                month_point_mismatches.append(f"202607 expected 안정, got={jul_row.get('월시점 추세판정')!r}")
            if str(jul_row.get("기간구분") or "") != "당월진행":
                month_point_mismatches.append(f"202607 period expected 당월진행, got={jul_row.get('기간구분')!r}")
            judge_seq = duplicate_trend.drop_duplicates(["제품코드", "기준월"])["월시점 추세판정"].astype(str).tolist()
            if len(set(judge_seq)) <= 1:
                month_point_mismatches.append(f"monthly judges appear copied: {judge_seq}")
            suffix_cols = [c for c in duplicate_trend.columns if str(c).endswith(("_x", "_y"))]
            if suffix_cols:
                month_point_mismatches.append(f"unexpected suffix columns: {suffix_cols}")
        if month_point_mismatches:
            results.append(_fail("sales trend detail month-point judge", "; ".join(month_point_mismatches)))
        else:
            results.append(_ok("sales trend detail month-point judge", "monthly point-in-time judges are merged per product-month"))

        product_00439_summary = mod.get_sales_trend_summary_df(
            {
                "month_from": "202601",
                "month_to": "202607",
                "date_from": "20260101",
                "date_to": "20260731",
                "source_mode": "monthly_book",
            },
            raw_df=duplicate_vendor_df,
        )
        row_00439 = product_00439_summary.iloc[0].to_dict()
        meta_00439 = mod._period_policy_meta_from_summary_df(product_00439_summary)
        display_mismatches = []
        expected_00439 = {
            "완료월평균매출": 389_743_982,
            "당월 현재매출": 200_886_348,
            "당월 예상매출": 442_935_939,
            "당월 잔여예상": 242_049_591,
            "당월 진척률": 45.3522419545,
        }
        for key, exp in expected_00439.items():
            got = float(row_00439.get(key) or 0)
            if abs(got - exp) > 0.02:
                display_mismatches.append(f"{key}: expected={exp}, got={got}")
        progress_value = row_00439.get("당월 진척률")
        if int(float(progress_value or 0)) == float(progress_value or 0):
            display_mismatches.append(f"당월 진척률 lost decimal precision: {progress_value}")
        if f"{float(progress_value or 0):.2f}%" != "45.35%":
            display_mismatches.append(f"당월 진척률 display expected 45.35%, got={float(progress_value or 0):.2f}%")
        if any(c.startswith("_당월") for c in product_00439_summary.columns):
            display_mismatches.append("internal _당월 columns exposed in summary")
        if round(float(meta_00439.get("avg_completed_month_sales_amt") or 0)) != 389_743_982:
            display_mismatches.append(f"meta avg_completed_month_sales_amt={meta_00439.get('avg_completed_month_sales_amt')}")
        trend_jul_expected = float(jul_row.get("월시점 예상매출") or 0)
        trend_jul_actual = float(jul_row.get("월시점 실제매출") or 0)
        trend_jul_progress = float(jul_row.get("월시점 달성률") or 0)
        if abs(round(trend_jul_expected) - round(float(row_00439.get("당월 예상매출") or 0))) > 0:
            display_mismatches.append(f"trend/summary expected mismatch: trend={trend_jul_expected}, summary={row_00439.get('당월 예상매출')}")
        if abs(trend_jul_actual - float(row_00439.get("당월 현재매출") or 0)) > 0.02:
            display_mismatches.append(f"trend/summary actual mismatch: trend={trend_jul_actual}, summary={row_00439.get('당월 현재매출')}")
        if abs(trend_jul_progress - float(row_00439.get("당월 진척률") or 0)) > 1e-6:
            display_mismatches.append(f"trend/summary progress mismatch: trend={trend_jul_progress}, summary={row_00439.get('당월 진척률')}")
        old_summary_for_forecast = getattr(mod, "get_sales_trend_summary_df", None)
        try:
            setattr(mod, "get_sales_trend_summary_df", lambda params=None, raw_df=None: product_00439_summary.copy())
            forecast_00439 = mod.get_sales_forecast_df({})
            forecast_row_00439 = forecast_00439.iloc[0].to_dict()
            if abs(float(forecast_row_00439.get("당월 예상매출") or 0) - float(row_00439.get("당월 예상매출") or 0)) > 0.02:
                display_mismatches.append(f"summary/forecast expected mismatch: forecast={forecast_row_00439.get('당월 예상매출')}, summary={row_00439.get('당월 예상매출')}")
            if abs(float(forecast_row_00439.get("당월 현재매출") or 0) - float(row_00439.get("당월 현재매출") or 0)) > 0.02:
                display_mismatches.append(f"summary/forecast actual mismatch: forecast={forecast_row_00439.get('당월 현재매출')}, summary={row_00439.get('당월 현재매출')}")
            if abs(float(forecast_row_00439.get("당월 진척률") or 0) - float(row_00439.get("당월 진척률") or 0)) > 1e-6:
                display_mismatches.append(f"summary/forecast progress mismatch: forecast={forecast_row_00439.get('당월 진척률')}, summary={row_00439.get('당월 진척률')}")
        finally:
            if old_summary_for_forecast is not None:
                setattr(mod, "get_sales_trend_summary_df", old_summary_for_forecast)
        if display_mismatches:
            results.append(_fail("sales forecast current month display summary", "; ".join(display_mismatches)))
        else:
            results.append(_ok("sales forecast current month display summary", "00439 current-month display values and trend/summary/forecast match expected"))

        past_df = pd.DataFrame([
            _row("202601", 100),
            _row("202602", 100),
            _row("202603", 100),
            _row("202604", 200),
            _row("202605", 200),
            _row("202606", 300),
        ])
        past_summary = mod.get_sales_trend_summary_df(
            {"month_from": "202601", "month_to": "202606", "date_from": "20260101", "date_to": "20260630", "source_mode": "monthly_book"},
            raw_df=past_df,
        )
        past_row = past_summary.iloc[0].to_dict()
        past_df_changed = pd.DataFrame([
            _row("202601", 100),
            _row("202602", 100),
            _row("202603", 100),
            _row("202604", 200),
            _row("202605", 200),
            _row("202606", 999),
        ])
        past_summary_changed = mod.get_sales_trend_summary_df(
            {"month_from": "202601", "month_to": "202606", "date_from": "20260101", "date_to": "20260630", "source_mode": "monthly_book"},
            raw_df=past_df_changed,
        )
        past_row_changed = past_summary_changed.iloc[0].to_dict()
        past_expected = float(past_row.get("당월 예상매출") or 0)
        past_expected_changed = float(past_row_changed.get("당월 예상매출") or 0)
        past_progress = float(past_row.get("당월 진척률") or 0)
        if (
            float(past_row.get("완료월수") or 0) == 5
            and float(past_row.get("당월 현재매출") or 0) == 300
            and past_expected > 0
            and past_progress > 0
            and abs(past_expected - past_expected_changed) < 1e-9
        ):
            results.append(_ok("sales period policy past month-end evaluation", "past month-end keeps end month as evaluation and forecast is basis-month only"))
        else:
            results.append(_fail("sales period policy past month-end evaluation", f"row={past_row}, changed={past_row_changed}"))

        source_policy_cases = [
            ("date_to == today", {"month_from": "202601", "date_to": "20260712", "policy_date": "20260712"}, False, "20260712", "current_monthly", "202607", ["202601", "202602", "202603", "202604", "202605", "202606"]),
            ("date_to > today", {"month_from": "202601", "date_to": "20260831", "policy_date": "20260712"}, False, "20260712", "current_monthly", "202607", ["202601", "202602", "202603", "202604", "202605", "202606"]),
            ("past month end", {"month_from": "202601", "date_to": "20260630", "policy_date": "20260712"}, False, "20260630", "historical_month_end", "202606", ["202601", "202602", "202603", "202604", "202605"]),
            ("past mid month", {"month_from": "202601", "date_to": "20260702", "policy_date": "20260712"}, True, "20260702", "historical_midmonth", "202607", ["202601", "202602", "202603", "202604", "202605", "202606"]),
        ]
        source_policy_mismatches = []
        for label, params_case, expected_hybrid, expected_effective, expected_mode, expected_eval_month, expected_basis in source_policy_cases:
            policy = mod._resolve_period_source_policy(params_case)
            if bool(policy.get("use_hybrid")) != expected_hybrid:
                source_policy_mismatches.append(f"{label}: hybrid expected={expected_hybrid}, got={policy.get('use_hybrid')}")
            if bool(policy.get("use_hybrid_detail")) != expected_hybrid:
                source_policy_mismatches.append(f"{label}: hybrid_detail expected={expected_hybrid}, got={policy.get('use_hybrid_detail')}")
            if str(policy.get("effective_date_to") or "") != expected_effective:
                source_policy_mismatches.append(f"{label}: effective expected={expected_effective}, got={policy.get('effective_date_to')}")
            if str(policy.get("evaluation_mode") or "") != expected_mode:
                source_policy_mismatches.append(f"{label}: mode expected={expected_mode}, got={policy.get('evaluation_mode')}")
            if str(policy.get("evaluation_month") or "") != expected_eval_month:
                source_policy_mismatches.append(f"{label}: eval_month expected={expected_eval_month}, got={policy.get('evaluation_month')}")
            if list(policy.get("basis_months") or []) != expected_basis:
                source_policy_mismatches.append(f"{label}: basis expected={expected_basis}, got={policy.get('basis_months')}")
        if source_policy_mismatches:
            results.append(_fail("sales source date policy resolver", "; ".join(source_policy_mismatches)))
        else:
            results.append(_ok("sales source date policy resolver", "today/future/month-end/mid-month branches verified"))

        old_monthly_source = getattr(mod, "get_sales_trend_monthly_df", None)
        old_detail_source = getattr(mod, "get_sales_trend_detail_df", None)
        try:
            source_calls = {"monthly": 0, "detail": 0}
            monthly_params_seen: list[dict[str, Any]] = []
            monthly_source_df = pd.DataFrame(
                [
                    _row("202606", 600, 6, "MIX1", "B1"),
                    _row("202607", 900, 9, "MIX1", "B1"),
                ]
            )
            detail_source_df = pd.DataFrame([_row("202607", 70, 7, "MIX1", "B1")])
            detail_source_df["거래처코드"] = "C1"
            detail_source_df["거래처명"] = "거래처"
            detail_source_df["시도명"] = "서울"
            detail_source_df["시구군명"] = "강남구"
            detail_source_df["법정읍면동명"] = "역삼동"
            detail_source_df["출고건수"] = 1
            detail_source_df["거래처수"] = 1
            month_col = list(monthly_source_df.columns)[0]
            branch_columns: dict[str, list[str]] = {}

            def _fake_monthly_source(params: Optional[dict[str, Any]] = None, source_mode: str = "monthly_book") -> pd.DataFrame:
                source_calls["monthly"] += 1
                monthly_params_seen.append(dict(params or {}))
                return monthly_source_df.copy()

            def _fake_detail_source(params: Optional[dict[str, Any]] = None) -> pd.DataFrame:
                source_calls["detail"] += 1
                return detail_source_df.copy()

            setattr(mod, "get_sales_trend_monthly_df", _fake_monthly_source)
            setattr(mod, "get_sales_trend_detail_df", _fake_detail_source)

            for label, params_case in [
                ("today monthly-only", {"date_to": "20260712", "policy_date": "20260712"}),
                ("future monthly-only", {"date_to": "20260831", "policy_date": "20260712"}),
                ("past month-end monthly-only", {"date_to": "20260630", "policy_date": "20260712"}),
            ]:
                source_calls["detail"] = 0
                monthly_params_seen.clear()
                out_source = mod.get_sales_trend_df(
                    {
                        "month_from": "202606",
                        "month_to": "202608",
                        "source_mode": "monthly_book",
                        **params_case,
                    }
                )
                if source_calls["detail"] != 0:
                    source_policy_mismatches.append(f"{label}: detail calls expected=0, got={source_calls['detail']}")
                if label == "future monthly-only" and str((monthly_params_seen[-1] or {}).get("date_to") or "") != "20260712":
                    source_policy_mismatches.append(f"{label}: monthly date_to not capped, params={monthly_params_seen[-1]}")
                if out_source.empty:
                    source_policy_mismatches.append(f"{label}: empty source result")
                suffix_cols = [c for c in out_source.columns if str(c).endswith(("_x", "_y"))]
                if suffix_cols:
                    source_policy_mismatches.append(f"{label}: unexpected suffix columns={suffix_cols}")
                branch_columns[label] = list(out_source.columns)

            source_calls["detail"] = 0
            out_hybrid = mod.get_sales_trend_df(
                {
                    "month_from": "202606",
                    "month_to": "202607",
                    "date_to": "20260702",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                }
            )
            hybrid_months = out_hybrid[month_col].astype(str).tolist() if month_col in out_hybrid.columns else []
            if source_calls["detail"] != 1:
                source_policy_mismatches.append(f"past mid month hybrid: detail calls expected=1, got={source_calls['detail']}")
            if hybrid_months.count("202607") != 1:
                source_policy_mismatches.append(f"past mid month hybrid: expected one replaced 202607 row, months={hybrid_months}")
            if not bool(getattr(out_hybrid, "attrs", {}).get("mixed_current_month_detail")):
                source_policy_mismatches.append("past mid month hybrid: mixed attrs missing")
            hybrid_suffix_cols = [c for c in out_hybrid.columns if str(c).endswith(("_x", "_y"))]
            if hybrid_suffix_cols:
                source_policy_mismatches.append(f"past mid month hybrid: unexpected suffix columns={hybrid_suffix_cols}")
            branch_columns["past mid month hybrid"] = list(out_hybrid.columns)
            internal_cols = {"거래처코드", "거래처명", "시도명", "시구군명", "법정읍면동명", "출고건수", "거래처수", "평균공급단가"}
            leaked_cols = sorted(c for c in out_hybrid.columns if c in internal_cols)
            if leaked_cols:
                source_policy_mismatches.append(f"past mid month hybrid: leaked internal columns={leaked_cols}")
            if len({tuple(cols) for cols in branch_columns.values()}) != 1:
                source_policy_mismatches.append(f"branch public columns mismatch={branch_columns}")

            current_raw = mod.get_sales_trend_df(
                {
                    "month_from": "202606",
                    "month_to": "202607",
                    "date_to": "20260712",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                }
            )
            current_summary = mod.get_sales_trend_summary_df(
                {
                    "month_from": "202606",
                    "month_to": "202607",
                    "date_to": "20260712",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                },
                raw_df=current_raw,
            )
            hybrid_summary = mod.get_sales_trend_summary_df(
                {
                    "month_from": "202606",
                    "month_to": "202607",
                    "date_to": "20260702",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                },
                raw_df=out_hybrid,
            )
            current_eval = current_summary.iloc[0].to_dict()
            hybrid_eval = hybrid_summary.iloc[0].to_dict()
            if abs(float(current_eval.get("당월 예상매출") or 0) - float(hybrid_eval.get("당월 예상매출") or 0)) > 1e-9:
                source_policy_mismatches.append(f"20260702/20260712 expected mismatch: current={current_eval}, hybrid={hybrid_eval}")
            if abs(float(current_eval.get("당월 현재매출") or 0) - float(hybrid_eval.get("당월 현재매출") or 0)) < 1e-9:
                source_policy_mismatches.append(f"20260702/20260712 actual should differ: current={current_eval}, hybrid={hybrid_eval}")
            if abs(float(current_eval.get("당월 진척률") or 0) - float(hybrid_eval.get("당월 진척률") or 0)) < 1e-9:
                source_policy_mismatches.append(f"20260702/20260712 progress should differ: current={current_eval}, hybrid={hybrid_eval}")

            if source_policy_mismatches:
                results.append(_fail("sales source monthly-only detail skip", "; ".join(source_policy_mismatches)))
            else:
                results.append(_ok("sales source monthly-only detail skip", "monthly-only skipped detail; mid-month hybrid replaced current month"))
        finally:
            if old_monthly_source is not None:
                setattr(mod, "get_sales_trend_monthly_df", old_monthly_source)
            if old_detail_source is not None:
                setattr(mod, "get_sales_trend_detail_df", old_detail_source)

        old_forecast_df = getattr(mod, "get_sales_forecast_df", None)
        old_stock_df = getattr(mod, "_load_product_current_stock", None)
        try:
            stock_base_df = pd.DataFrame(
                [
                    {
                        "제품코드": "STK1",
                        "제품명": "재고테스트1",
                        "규격": "EA",
                        "제조사명": "제조사",
                        "매입처명": "매입처",
                        "2026-01 수량": 10,
                        "2026-02 수량": 20,
                        "2026-03 수량": 30,
                        "2026-04 수량": 40,
                        "2026-05 수량": 50,
                        "2026-06 수량": 60,
                        "2026-07 수량": 25,
                        "2026-08 수량": 999,
                        "총출고수량": 1234,
                    },
                    {
                        "제품코드": "STK2",
                        "제품명": "재고테스트2",
                        "규격": "EA",
                        "제조사명": "제조사",
                        "매입처명": "매입처",
                        "2026-01 수량": 0,
                        "2026-02 수량": 0,
                        "2026-03 수량": 0,
                        "2026-04 수량": 0,
                        "2026-05 수량": 0,
                        "2026-06 수량": 0,
                        "2026-07 수량": 0,
                        "2026-08 수량": 999,
                        "총출고수량": 999,
                    },
                    {
                        "제품코드": "STK3",
                        "제품명": "재고테스트3",
                        "규격": "EA",
                        "제조사명": "제조사",
                        "매입처명": "매입처",
                        "2026-01 수량": 5,
                        "2026-02 수량": 5,
                        "2026-03 수량": 5,
                        "2026-04 수량": 5,
                        "2026-05 수량": 5,
                        "2026-06 수량": 5,
                        "2026-07 수량": 0,
                        "2026-08 수량": 999,
                        "총출고수량": 999,
                    },
                ]
            )
            stock_current_df = pd.DataFrame(
                [
                    {"제품코드": "STK1", "장부재고수량": 20, "실재고수량": 20, "장부재고금액": 20_000, "실재고금액": 40_000, "장부재고평가단가": 1000, "실재고평가단가": 2000, "당월입고수량": 1, "당월출고수량": 2, "당월재고증감수량": -1},
                    {"제품코드": "STK2", "장부재고수량": 10, "실재고수량": 10, "장부재고금액": 20_000, "실재고금액": 30_000, "장부재고평가단가": 2000, "실재고평가단가": 3000, "당월입고수량": 0, "당월출고수량": 0, "당월재고증감수량": 0},
                    {"제품코드": "STK3", "장부재고수량": -2, "실재고수량": -2, "장부재고금액": -200, "실재고금액": -400, "장부재고평가단가": 100, "실재고평가단가": 200, "당월입고수량": 0, "당월출고수량": 0, "당월재고증감수량": 0},
                ]
            )

            setattr(mod, "get_sales_forecast_df", lambda params: stock_base_df.copy())
            setattr(mod, "_load_product_current_stock", lambda *args, **kwargs: stock_current_df.copy())

            stock_result = mod.get_stock_shortage_df(
                {
                    "month_from": "202601",
                    "month_to": "202608",
                    "date_from": "20260101",
                    "date_to": "20260831",
                    "source_mode": "monthly_book",
                    "stock_mode": "book",
                }
            )
            stock_row1 = stock_result[stock_result["제품코드"].astype(str) == "STK1"].iloc[0].to_dict()
            stock_row2 = stock_result[stock_result["제품코드"].astype(str) == "STK2"].iloc[0].to_dict()
            stock_row3 = stock_result[stock_result["제품코드"].astype(str) == "STK3"].iloc[0].to_dict()
            meta_stock = mod._stock_shortage_meta_from_df(stock_result)

            stock_mismatches = []
            expected_stock_1 = {
                "완료월수": 6,
                "완료월총출고수량": 210,
                "완료월평균출고수량": 35,
                "최근3개월평균출고수량": 50,
                "최근6개월평균출고수량": 35,
                "최근3개월수량증감률": 42.8571428571,
                "당월 현재출고수량": 25,
                "당월 예상출고수량": 57.5,
                "당월 잔여예상출고수량": 32.5,
                "당월 출고진척률": 43.4782608696,
                "예상월말재고수량": -12.5,
                "부족예상수량": 12.5,
                "부족예상금액": 12500,
                "당월 재고충족률": 61.5384615385,
            }
            for key, exp in expected_stock_1.items():
                got = float(stock_row1.get(key) or 0)
                if abs(got - exp) > 1e-6:
                    stock_mismatches.append(f"STK1 {key}: expected={exp}, got={got}")
            if float(stock_row1.get("월평균출고수량") or 0) != float(stock_row1.get("완료월평균출고수량") or 0):
                stock_mismatches.append("월평균출고수량 is not aligned to 완료월평균출고수량")
            if str(stock_row1.get("재고부족판정") or "") != "부족":
                stock_mismatches.append(f"STK1 재고부족판정 expected 부족, got={stock_row1.get('재고부족판정')!r}")
            if float(stock_row2.get("당월 재고충족률") or 0) != 100 or str(stock_row2.get("재고부족판정") or "") != "수요없음":
                stock_mismatches.append(f"STK2 expected no-demand fill=100, row={stock_row2}")
            if abs(float(stock_row3.get("부족예상수량") or 0) - 7) > 1e-6:
                stock_mismatches.append(f"STK3 negative stock shortage expected=7, got={stock_row3.get('부족예상수량')}")
            if abs(float(meta_stock.get("overall_stock_fill_rate") or 0) - (30 / 37.5 * 100)) > 1e-6:
                stock_mismatches.append(f"overall fill rate expected=80, got={meta_stock.get('overall_stock_fill_rate')}")
            if abs(float(meta_stock.get("current_month_demand_progress_pct") or 0) - (25 / 62.5 * 100)) > 1e-6:
                stock_mismatches.append(f"weighted demand progress expected=40, got={meta_stock.get('current_month_demand_progress_pct')}")
            if str(stock_result.get("분석자료원", pd.Series([""])).iloc[0]) != "월집계-장부재고(Rddbc220)":
                stock_mismatches.append(f"monthly source label mismatch: {stock_result.get('분석자료원')}")
            internal_cols = [c for c in ["당월입고수량", "당월출고수량", "당월재고증감수량"] if c in stock_result.columns]
            if internal_cols:
                stock_mismatches.append(f"stock shortage internal columns leaked: {internal_cols}")
            if any(str(c).endswith(("_x", "_y")) for c in stock_result.columns):
                stock_mismatches.append("stock shortage result has merge suffix columns")
            if "2026-08 수량" in stock_result.columns and float(stock_row1.get("완료월총출고수량") or 0) >= 999:
                stock_mismatches.append("future month quantity appears included in completed demand")

            stock_hybrid_result = mod.get_stock_shortage_df(
                {
                    "month_from": "202601",
                    "month_to": "202607",
                    "date_from": "20260101",
                    "date_to": "20260702",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                    "stock_mode": "book",
                }
            )
            hybrid_source = str(stock_hybrid_result.get("분석자료원", pd.Series([""])).iloc[0])
            hybrid_stock_source = str(stock_hybrid_result.get("현재고원천", pd.Series([""])).iloc[0])
            if "평가월: 출고상세(Rddbc120)" not in hybrid_source or "현재재고: 전월말+입출고상세" not in hybrid_source:
                stock_mismatches.append(f"hybrid source label mismatch: {hybrid_source}")
            if "입고상세(Rddbc110)" not in hybrid_stock_source or "출고상세(Rddbc120)" not in hybrid_stock_source:
                stock_mismatches.append(f"hybrid stock source label mismatch: {hybrid_stock_source}")
            leaked_hybrid = [
                c for c in ["당월입고수량", "당월출고수량", "당월재고증감수량"]
                if c in stock_hybrid_result.columns
            ]
            if leaked_hybrid:
                stock_mismatches.append(f"hybrid stock internal columns leaked: {leaked_hybrid}")

            stock_past_month_end = mod.get_stock_shortage_df(
                {
                    "month_from": "202601",
                    "month_to": "202606",
                    "date_from": "20260101",
                    "date_to": "20260630",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                    "stock_mode": "book",
                }
            )
            stock_base_df_changed = stock_base_df.copy()
            stock_base_df_changed.loc[stock_base_df_changed["제품코드"].astype(str) == "STK1", "2026-06 수량"] = 999
            setattr(mod, "get_sales_forecast_df", lambda params: stock_base_df_changed.copy())
            stock_past_month_end_changed = mod.get_stock_shortage_df(
                {
                    "month_from": "202601",
                    "month_to": "202606",
                    "date_from": "20260101",
                    "date_to": "20260630",
                    "policy_date": "20260712",
                    "source_mode": "monthly_book",
                    "stock_mode": "book",
                }
            )
            setattr(mod, "get_sales_forecast_df", lambda params: stock_base_df.copy())
            past_stock_row1 = stock_past_month_end[stock_past_month_end["제품코드"].astype(str) == "STK1"].iloc[0].to_dict()
            past_stock_row1_changed = stock_past_month_end_changed[stock_past_month_end_changed["제품코드"].astype(str) == "STK1"].iloc[0].to_dict()
            if abs(float(past_stock_row1.get("당월 예상출고수량") or 0) - 46) > 1e-6:
                stock_mismatches.append(f"past month-end expected demand expected=46, got={past_stock_row1.get('당월 예상출고수량')}")
            if abs(float(past_stock_row1.get("당월 예상출고수량") or 0) - float(past_stock_row1_changed.get("당월 예상출고수량") or 0)) > 1e-9:
                stock_mismatches.append(f"past month-end expected demand changed by actual month: before={past_stock_row1}, after={past_stock_row1_changed}")
            if float(past_stock_row1.get("당월 현재출고수량") or 0) != 60:
                stock_mismatches.append(f"past month-end actual demand expected=60, got={past_stock_row1.get('당월 현재출고수량')}")
            if float(past_stock_row1.get("당월 예상출고수량") or 0) <= 0:
                stock_mismatches.append("past month-end expected demand should not be zero")

            stock_real_result = mod.get_stock_shortage_df(
                {
                    "month_from": "202601",
                    "month_to": "202608",
                    "date_from": "20260101",
                    "date_to": "20260831",
                    "source_mode": "monthly_real",
                    "stock_mode": "real",
                }
            )
            stock_real_row1 = stock_real_result[stock_real_result["제품코드"].astype(str) == "STK1"].iloc[0].to_dict()
            if float(stock_real_row1.get("재고평가단가") or 0) != 2000:
                stock_mismatches.append(f"real stock unit price expected=2000, got={stock_real_row1.get('재고평가단가')}")
            if abs(float(stock_real_row1.get("부족예상금액") or 0) - 25000) > 1e-6:
                stock_mismatches.append(f"real stock shortage amount expected=25000, got={stock_real_row1.get('부족예상금액')}")

            if stock_mismatches:
                results.append(_fail("stock shortage current-month period policy", "; ".join(stock_mismatches)))
            else:
                results.append(_ok("stock shortage current-month period policy", "completed demand, current demand, shortage, fill rate, and unit price selection verified"))
        finally:
            if old_forecast_df is not None:
                setattr(mod, "get_sales_forecast_df", old_forecast_df)
            if old_stock_df is not None:
                setattr(mod, "_load_product_current_stock", old_stock_df)
    except Exception as e:
        results.append(_fail("sales period policy current/future exclusion", f"{type(e).__name__}: {e}"))
    finally:
        try:
            if old_current_yyyymm is not None:
                setattr(mod, "_current_yyyymm", old_current_yyyymm)
        except Exception:
            pass

    try:
        import app.services.analytics_manufacturer_sales_trend_service as manufacturer_mod

        def _manufacturer_raw(date_to: str = "20260712") -> pd.DataFrame:
            july_a = 50 if str(date_to or "") >= "20260712" else 20
            rows = []
            for m, a1, a2, b in [
                ("202601", 40, 60, 10),
                ("202602", 40, 60, 20),
                ("202603", 40, 60, 30),
                ("202604", 40, 60, 40),
                ("202605", 40, 60, 50),
                ("202606", 40, 60, 60),
                ("202607", 10, july_a - 10, 70),
            ]:
                rows.extend([
                    {"기준월": m, "제품코드": "P1", "제품명": "A1", "규격": "", "제조사명": " 제약A ", "매출공급가액": a1, "매출합계": a1},
                    {"기준월": m, "제품코드": "P2", "제품명": "A2", "규격": "", "제조사명": "제약A", "매출공급가액": a2, "매출합계": a2},
                    {"기준월": m, "제품코드": "P3", "제품명": "B1", "규격": "", "제조사명": None, "매출공급가액": b, "매출합계": b},
                ])
            for m, amt in [
                ("202601", 60_000_000),
                ("202602", 50_000_000),
                ("202603", 40_000_000),
                ("202604", 30_000_000),
                ("202605", 20_000_000),
                ("202606", 10_000_000),
                ("202607", 5_000_000),
            ]:
                rows.append({"기준월": m, "제품코드": "P5", "제품명": "D1", "규격": "", "제조사명": "감소D", "매출공급가액": amt, "매출합계": amt})
            rows.append({"기준월": "202607", "제품코드": "P4", "제품명": "C1", "규격": "", "제조사명": "신규C", "매출공급가액": 30, "매출합계": 30})
            df = pd.DataFrame(rows)
            df.attrs["mixed_current_month_detail"] = str(date_to or "") < "20260712" and str(date_to or "")[:6] == "202607"
            df.attrs["source_label_completed"] = "월집계-장부재고(Rddbc220)"
            df.attrs["source_label_current"] = "출고상세(Rddbc120)"
            return df

        old_loader = getattr(manufacturer_mod, "get_sales_trend_df", None)
        captured_manufacturer_params = []

        def _manufacturer_loader(params):
            captured_manufacturer_params.append(dict(params or {}))
            return _manufacturer_raw(str((params or {}).get("date_to") or "20260712"))

        setattr(manufacturer_mod, "get_sales_trend_df", _manufacturer_loader)
        try:
            params_current = {
                "month_from": "202601",
                "month_to": "202607",
                "date_from": "20260101",
                "date_to": "20260712",
                "policy_date": "20260712",
                "source_mode": "monthly_book",
            }
            params_mid = dict(params_current, date_to="20260702")
            params_past_end = dict(params_current, month_to="202606", date_to="20260630")
            detail = manufacturer_mod.get_manufacturer_sales_trend(params_current)
            detail_mid = manufacturer_mod.get_manufacturer_sales_trend(params_mid)
            detail_past = manufacturer_mod.get_manufacturer_sales_trend(params_past_end)
            mismatches = []
            if getattr(detail, "attrs", {}).get("evaluation_mode") != "current_monthly":
                mismatches.append(f"current mode expected current_monthly got={getattr(detail, 'attrs', {}).get('evaluation_mode')}")
            if getattr(detail_mid, "attrs", {}).get("evaluation_mode") != "historical_midmonth":
                mismatches.append(f"mid mode expected historical_midmonth got={getattr(detail_mid, 'attrs', {}).get('evaluation_mode')}")
            if getattr(detail_past, "attrs", {}).get("evaluation_mode") != "historical_month_end":
                mismatches.append(f"past month-end mode expected historical_month_end got={getattr(detail_past, 'attrs', {}).get('evaluation_mode')}")
            address_params = dict(params_current, sido_nm="서울", gugun_nm="강남", road_nm="테헤란로")
            _ = manufacturer_mod.get_manufacturer_sales_trend(address_params)
            last_params = captured_manufacturer_params[-1] if captured_manufacturer_params else {}
            address_mismatches = []
            for k, expected in {"sido_nm": "서울", "gugun_nm": "강남", "road_nm": "테헤란로"}.items():
                if last_params.get(k) != expected:
                    address_mismatches.append(f"manufacturer address param not forwarded {k}={last_params.get(k)!r}")
            query_condition = manufacturer_mod._fmt_analytics_query_summary(address_params, "월집계-장부재고(Rddbc220)")
            for expected in ["시도명 서울", "시구군명 강남", "도로명 테헤란로"]:
                if expected not in query_condition:
                    address_mismatches.append(f"manufacturer address query condition missing {expected}: {query_condition}")
            if address_mismatches:
                results.append(_fail("manufacturer sales trend address filters", "; ".join(address_mismatches)))
            else:
                results.append(_ok("manufacturer sales trend address filters", "sido/gugun/road params and query summary verified"))

            if "제약사명" not in detail.columns:
                mismatches.append("missing 제약사명")
            forbidden_tokens = ["수량", "다음월예상매출", "예상등급"]
            forbidden = [
                c for c in detail.columns
                if any(x in str(c) for x in forbidden_tokens)
                or str(c).endswith(("_x", "_y"))
                or str(c).startswith("_")
                or c in {"제품코드", "제품명", "규격"}
            ]
            if forbidden:
                mismatches.append(f"forbidden public columns={forbidden}")
            required_detail_cols = [
                "기준월",
                "매출공급가액",
                "최근3개월평균매출",
                "월시점 실제매출",
                "월시점 예상매출",
                "월시점 달성률",
                "월시점 예상기준",
                "월시점 적용증감률",
                "월시점 예상대비차이",
                "월시점 잔여예상",
                "월시점 추세판정",
                "월시점 판정결과",
                "판정결과",
                "추세판정",
            ]
            for c in required_detail_cols:
                if c not in detail.columns:
                    mismatches.append(f"missing detail column {c}")
            detail_analysis_block = [
                "월시점 완료월수",
                "월시점 완료월평균매출",
                "월시점 최근3개월평균매출",
                "월시점 최근6개월평균매출",
                "월시점 증감률",
                "월시점 추세판정",
                "월시점 판정결과",
                "월시점 실제매출",
                "월시점 예상기준",
                "월시점 적용증감률",
                "월시점 예상매출",
                "월시점 예상대비차이",
                "월시점 잔여예상",
                "월시점 달성률",
                "추세판정",
                "판정결과",
            ]
            detail_cols = list(detail.columns)
            block_positions = [detail_cols.index(c) for c in detail_analysis_block if c in detail_cols]
            if block_positions != sorted(block_positions) or len(block_positions) != len(detail_analysis_block):
                mismatches.append("detail analysis block order mismatch")
            non_zero_metric_sum = float(
                detail[[c for c in ["월시점 실제매출", "월시점 예상매출", "월시점 달성률"] if c in detail.columns]]
                .apply(pd.to_numeric, errors="coerce")
                .fillna(0)
                .abs()
                .sum()
                .sum()
            )
            if non_zero_metric_sum <= 0:
                mismatches.append("detail month-point actual/expected/progress are all zero")
            zero_check_cols = [
                "매출공급가액",
                "매출세액",
                "매출합계",
                "집계건수",
                "월시점 실제매출",
                "월시점 예상매출",
                "월시점 예상대비차이",
                "월시점 잔여예상",
            ]
            present_zero_cols = [c for c in zero_check_cols if c in detail.columns]
            zero_rows = (
                detail[present_zero_cols].apply(pd.to_numeric, errors="coerce").fillna(0).abs().sum(axis=1).eq(0).sum()
                if present_zero_cols
                else 0
            )
            if int(zero_rows) != 0:
                mismatches.append(f"detail public zero rows should be removed rows={zero_rows}")
            detail_key_counts = detail.groupby(["제약사명", "기준월"]).size()
            if not detail_key_counts.empty and int(detail_key_counts.max()) != 1:
                mismatches.append("detail should have one row per manufacturer-month")

            raw_cur = _manufacturer_raw("20260712")
            raw_sum = float(pd.to_numeric(raw_cur["매출공급가액"], errors="coerce").fillna(0).sum())
            detail_sum = float(pd.to_numeric(detail["매출공급가액"], errors="coerce").fillna(0).sum())
            if abs(raw_sum - detail_sum) > 1e-9:
                mismatches.append(f"detail monthly sum mismatch raw={raw_sum}, detail={detail_sum}")
            a_july = detail[(detail["제약사명"].astype(str) == "제약A") & (detail["기준월"].astype(str) == "202607")].iloc[0]
            a_mid_july = detail_mid[(detail_mid["제약사명"].astype(str) == "제약A") & (detail_mid["기준월"].astype(str) == "202607")].iloc[0]
            if float(a_july["매출공급가액"]) != 50:
                mismatches.append(f"manufacturer detail should aggregate manufacturer-month sales expected=50 got={a_july['매출공급가액']}")
            if float(a_mid_july["매출공급가액"]) == float(a_july["매출공급가액"]):
                mismatches.append("midmonth/current manufacturer actual sales should differ")
            sorted_check = detail.sort_values(["제약사명", "기준월"], ascending=[True, True]).reset_index(drop=True)
            if list(detail[["제약사명", "기준월"]].itertuples(index=False, name=None)) != list(sorted_check[["제약사명", "기준월"]].itertuples(index=False, name=None)):
                mismatches.append("detail final sort should be manufacturer asc + month asc")
            expected_seq = list(range(1, len(detail) + 1))
            if "순번" not in detail.columns or list(pd.to_numeric(detail["순번"], errors="coerce").fillna(0).astype(int)) != expected_seq:
                mismatches.append("detail sequence should be reassigned after final sort")
            if pd.isna(a_july.get("전월대비매출")):
                mismatches.append("detail last month diff sales should use same formula, not NaN")
            if pd.isna(a_july.get("전월대비매출증감률")):
                mismatches.append("detail last month diff pct should use same formula, not NaN")
            bad_period_values = {"당월진행", "current_monthly", "historical_midmonth", "historical_month_end"}
            leaked_period = sorted(set(detail.get("기간구분", pd.Series(dtype=str)).astype(str)) & bad_period_values)
            if leaked_period:
                mismatches.append(f"detail public period leaked forbidden values={leaked_period}")
            mid_period_values = set(detail_mid.get("기간구분", pd.Series(dtype=str)).astype(str))
            if "부분월" not in mid_period_values:
                mismatches.append(f"midmonth detail should expose 부분월 for final month values={sorted(mid_period_values)}")
            blank_july = detail[(detail["제약사명"].astype(str) == "제약사 미지정") & (detail["기준월"].astype(str) == "202607")].iloc[0]
            if float(blank_july["매출공급가액"]) != 70:
                mismatches.append("blank manufacturer group not preserved in detail")
            new_july = detail[(detail["제약사명"].astype(str) == "신규C") & (detail["기준월"].astype(str) == "202607")].iloc[0]
            if str(new_july.get("추세판정") or "") != "자료부족":
                mismatches.append(f"new manufacturer judge expected 자료부족 got={new_july.get('추세판정')}")

            if mismatches:
                results.append(_fail("manufacturer sales trend detail", "; ".join(mismatches)))
            else:
                results.append(_ok("manufacturer sales trend detail", "analysis block, zero-row removal, sort, and monthly sums verified"))

            summary = manufacturer_mod.get_manufacturer_sales_trend_summary(params_current)
            summary_mid = manufacturer_mod.get_manufacturer_sales_trend_summary(params_mid)
            summary_past = manufacturer_mod.get_manufacturer_sales_trend_summary(params_past_end)
            detail_res = manufacturer_mod.get_manufacturer_sales_trend_result(params_current)
            res = manufacturer_mod.get_manufacturer_sales_trend_summary_result(params_current)
            res_past = manufacturer_mod.get_manufacturer_sales_trend_summary_result(params_past_end)
            mismatches = []
            forbidden = [
                c for c in summary.columns
                if any(x in str(c) for x in forbidden_tokens)
                or str(c).endswith(("_x", "_y"))
                or str(c).startswith("_")
                or c in {"제품코드", "제품명", "규격"}
            ]
            if forbidden:
                mismatches.append(f"forbidden summary columns={forbidden}")
            if not any(str(c).endswith(" 매출") and str(c)[:4].isdigit() for c in summary.columns):
                mismatches.append("missing dynamic monthly sales columns")
            required_summary_cols = [
                "순번",
                "제약사명",
                "총매출공급가액",
                "총매출세액",
                "총매출액",
                "완료월총매출",
                "월평균매출",
                "완료월수",
                "완료월평균매출",
                "당월 현재매출",
                "당월 예상매출",
                "당월 잔여예상",
                "당월 진척률",
                "매출발생월수",
                "최근3개월평균매출",
                "최근6개월평균매출",
                "최근3개월증감률",
                "추세판정",
                "제품수",
                "매입처수",
                "총집계건수",
                "분석자료원",
                "기간구분",
            ]
            for c in required_summary_cols:
                if c not in summary.columns:
                    mismatches.append(f"missing summary column {c}")
            summary_cols = list(summary.columns)
            summary_positions = [summary_cols.index(c) for c in required_summary_cols if c in summary_cols]
            if summary_positions != sorted(summary_positions) or len(summary_positions) != len(required_summary_cols):
                mismatches.append("summary core column order mismatch")
            if "평가월 매출" in summary.columns:
                mismatches.append("current monthly summary should expose 당월 현재매출, not 평가월 매출")
            for label, df_check in [("mid", summary_mid), ("past", summary_past)]:
                for c in ["평가월 매출", "평가월 예상매출", "평가월 잔여예상", "평가월 진척률"]:
                    if c not in df_check.columns:
                        mismatches.append(f"{label} historical summary missing {c}")
                if "당월 현재매출" in df_check.columns:
                    mismatches.append(f"{label} historical summary should not expose 당월 현재매출")
                if any(c in df_check.columns for c in ["당월 예상매출", "당월 잔여예상", "당월 진척률"]):
                    mismatches.append(f"{label} historical summary should not expose 당월 expected/progress labels")
            if any(str(c).endswith(" 수량") for c in summary.columns):
                mismatches.append("summary should not expose monthly qty columns")
            summary_sum = float(pd.to_numeric(summary["총매출공급가액"], errors="coerce").fillna(0).sum()) if "총매출공급가액" in summary.columns else -1
            if abs(raw_sum - summary_sum) > 1e-9:
                mismatches.append(f"summary total mismatch raw={raw_sum}, summary={summary_sum}")
            names = set(summary["제약사명"].astype(str).tolist()) if "제약사명" in summary.columns else set()
            if not {"제약A", "제약사 미지정"}.issubset(names):
                mismatches.append(f"manufacturer universe not preserved names={sorted(names)}")
            meta = res.get("meta") or {}
            if meta.get("analysis_type") != "manufacturer_sales_trend_summary" or meta.get("summary_type") != "manufacturer_trend_summary":
                mismatches.append(f"unexpected summary meta={meta}")
            if meta.get("evaluation_mode") != "current_monthly":
                mismatches.append(f"summary meta evaluation_mode expected current_monthly got={meta.get('evaluation_mode')}")
            period_caption = str(meta.get("period_caption") or "")
            if "당월 2026-07" not in period_caption or "current_monthly" in period_caption:
                mismatches.append(f"current summary period caption unexpected={period_caption}")
            mid_caption = str(getattr(summary_mid, "attrs", {}).get("period_caption") or "")
            if "평가월 2026-07(07-02까지)" not in mid_caption or "당월" in mid_caption or "historical_midmonth" in mid_caption:
                mismatches.append(f"mid summary period caption unexpected={mid_caption}")
            past_caption = str(getattr(summary_past, "attrs", {}).get("period_caption") or "")
            if "완료월 2026-01~2026-05" not in past_caption or "평가월 2026-06" not in past_caption or "historical_month_end" in past_caption:
                mismatches.append(f"past summary period caption unexpected={past_caption}")
            if "current_monthly" in set(summary.get("기간구분", pd.Series(dtype=str)).astype(str)):
                mismatches.append("summary public period label leaked internal current_monthly")
            a_summary = summary[summary["제약사명"].astype(str) == "제약A"].iloc[0]
            a_summary_mid = summary_mid[summary_mid["제약사명"].astype(str) == "제약A"].iloc[0]
            for c in ["완료월총매출", "완료월수", "완료월평균매출", "최근3개월평균매출", "최근6개월평균매출", "최근3개월증감률", "추세판정"]:
                if str(c) == "추세판정":
                    if str(a_summary[c]) != str(a_summary_mid[c]):
                        mismatches.append(f"current/mid completed judge differs {a_summary[c]} vs {a_summary_mid[c]}")
                elif abs(float(a_summary[c]) - float(a_summary_mid[c])) > 1e-9:
                    mismatches.append(f"current/mid completed metric differs {c}: {a_summary[c]} vs {a_summary_mid[c]}")
            if float(a_summary["당월 현재매출"]) == float(a_summary_mid["평가월 매출"]):
                mismatches.append("current/mid current month sales should differ in summary")
            if float(a_summary.get("당월 예상매출", 0)) <= 0:
                mismatches.append("current summary expected sales should be populated")
            if float(a_summary.get("당월 진척률", 0)) <= 0:
                mismatches.append("current summary progress should be populated")
            a_past = summary_past[summary_past["제약사명"].astype(str) == "제약A"].iloc[0]
            if int(a_past["완료월수"]) != 5:
                mismatches.append(f"past month-end completed month count expected=5 got={a_past['완료월수']}")
            new_summary = summary[summary["제약사명"].astype(str) == "신규C"].iloc[0]
            if str(new_summary.get("추세판정") or "") != "비교자료 부족":
                mismatches.append(f"new manufacturer summary judge expected 비교자료 부족 got={new_summary.get('추세판정')}")
            for m in ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]:
                col = f"{m} 매출"
                if col in summary.columns:
                    detail_month = m.replace("-", "")
                    dsum = float(pd.to_numeric(detail[detail["기준월"].astype(str) == detail_month]["매출공급가액"], errors="coerce").fillna(0).sum())
                    ssum = float(pd.to_numeric(summary[col], errors="coerce").fillna(0).sum())
                    if abs(dsum - ssum) > 1e-9:
                        mismatches.append(f"detail/summary month sum mismatch {m}: detail={dsum}, summary={ssum}")
            if abs(float(pd.to_numeric(summary["총매출공급가액"], errors="coerce").fillna(0).sum()) - detail_sum) > 1e-9:
                mismatches.append("detail total and summary total differ under identical params")

            try:
                from app.ui.sims_table_display import resolve_sims_table_mode
                old_chat_env = os.environ.get("SIMS_CHAT_FAST_TABLE_CELL_THRESHOLD")
                old_panel_env = os.environ.get("SIMS_FAST_TABLE_CELL_THRESHOLD")
                os.environ["SIMS_CHAT_FAST_TABLE_CELL_THRESHOLD"] = "10"
                os.environ["SIMS_FAST_TABLE_CELL_THRESHOLD"] = "10"
                detail_chat_mode = resolve_sims_table_mode(detail, action="제약사별 매출 추세 분석", render_path="chat")
                detail_panel_mode = resolve_sims_table_mode(detail, action="제약사별 매출 추세 분석", render_path="panel")
                if detail_chat_mode.get("mode") != "fast" or detail_panel_mode.get("mode") != "fast":
                    mismatches.append(f"detail table mode fast expected chat={detail_chat_mode} panel={detail_panel_mode}")
                chat_mode = resolve_sims_table_mode(summary, action="제약사별 매출 추세 분석 요약표", render_path="chat")
                panel_mode = resolve_sims_table_mode(summary, action="제약사별 매출 추세 분석 요약표", render_path="panel")
                if chat_mode.get("mode") != "fast" or panel_mode.get("mode") != "fast":
                    mismatches.append(f"table mode fast expected chat={chat_mode} panel={panel_mode}")
                os.environ["SIMS_CHAT_FAST_TABLE_CELL_THRESHOLD"] = "999999"
                os.environ["SIMS_FAST_TABLE_CELL_THRESHOLD"] = "999999"
                chat_small = resolve_sims_table_mode(summary, action="제약사별 매출 추세 분석 요약표", render_path="chat")
                panel_small = resolve_sims_table_mode(summary, action="제약사별 매출 추세 분석 요약표", render_path="panel")
                if chat_small.get("mode") != "small" or panel_small.get("mode") != "small":
                    mismatches.append(f"table mode small expected chat={chat_small} panel={panel_small}")
            finally:
                if 'old_chat_env' in locals():
                    if old_chat_env is None:
                        os.environ.pop("SIMS_CHAT_FAST_TABLE_CELL_THRESHOLD", None)
                    else:
                        os.environ["SIMS_CHAT_FAST_TABLE_CELL_THRESHOLD"] = old_chat_env
                if 'old_panel_env' in locals():
                    if old_panel_env is None:
                        os.environ.pop("SIMS_FAST_TABLE_CELL_THRESHOLD", None)
                    else:
                        os.environ["SIMS_FAST_TABLE_CELL_THRESHOLD"] = old_panel_env

            def _manufacturer_bucket(value):
                text = str(value or "").strip()
                if text in {"증가", "신규/증가"}:
                    return "증가"
                if text == "감소":
                    return "감소"
                if text == "안정":
                    return "안정"
                return "자료부족"

            def _expected_counts(frame):
                counts = {"증가": 0, "감소": 0, "안정": 0, "자료부족": 0}
                if frame is None or frame.empty:
                    return counts
                if "기준월" in frame.columns:
                    work_counts = (
                        frame.assign(_기준월_sort=frame["기준월"].astype(str))
                        .sort_values(["제약사명", "_기준월_sort"])
                        .drop_duplicates("제약사명", keep="last")
                    )
                else:
                    work_counts = frame.drop_duplicates("제약사명", keep="last")
                for value in work_counts.get("추세판정", pd.Series(dtype=object)).tolist():
                    counts[_manufacturer_bucket(value)] += 1
                return counts

            header_mismatches = []
            detail_meta = detail_res.get("meta") or {}
            summary_meta = res.get("meta") or {}
            past_meta = res_past.get("meta") or {}
            expected_detail_counts = _expected_counts(detail)
            expected_summary_counts = _expected_counts(summary)
            for label, frame, meta_check, expected_counts in [
                ("detail", detail, detail_meta, expected_detail_counts),
                ("summary", summary, summary_meta, expected_summary_counts),
            ]:
                counts = meta_check.get("trend_judge_counts") or {}
                four_total = sum(int(counts.get(k, 0) or 0) for k in ["증가", "감소", "안정", "자료부족"])
                manufacturer_count = int(meta_check.get("manufacturer_count") or 0)
                if four_total != manufacturer_count:
                    header_mismatches.append(f"{label} judge count total {four_total} != manufacturer_count {manufacturer_count}")
                for k in ["증가", "감소", "안정", "자료부족"]:
                    if int(counts.get(k, 0) or 0) != int(expected_counts.get(k, 0) or 0):
                        header_mismatches.append(f"{label} judge {k} expected={expected_counts.get(k)} got={counts.get(k)}")
                if not isinstance(counts.get("자료부족", 0), int):
                    header_mismatches.append(f"{label} 자료부족 count should be integer")

            if detail_meta.get("current_progress_title") != "당월 진행 요약":
                header_mismatches.append(f"detail current progress title unexpected={detail_meta.get('current_progress_title')}")
            if summary_meta.get("current_progress_title") != "당월 진행 요약":
                header_mismatches.append(f"summary current progress title unexpected={summary_meta.get('current_progress_title')}")
            if past_meta.get("current_progress_title") != "평가월 진행 요약":
                header_mismatches.append(f"past progress title unexpected={past_meta.get('current_progress_title')}")
            for meta_label, meta_check in [("detail", detail_meta), ("summary", summary_meta), ("past", past_meta)]:
                for key in [
                    "completed_month_count",
                    "avg_completed_month_sales_amt",
                    "sum_current_month_sales_amt",
                    "sum_current_month_expected_amt",
                    "sum_current_month_remaining_expected_amt",
                    "current_month_progress_pct",
                ]:
                    if key not in meta_check:
                        header_mismatches.append(f"{meta_label} missing header meta {key}")

            eval_month = str(detail_meta.get("evaluation_month") or "")
            eval_rows = detail[detail["기준월"].astype(str) == eval_month] if eval_month and "기준월" in detail.columns else detail
            actual_total = float(pd.to_numeric(eval_rows.get("월시점 실제매출", 0), errors="coerce").fillna(0).sum())
            expected_total = float(pd.to_numeric(eval_rows.get("월시점 예상매출", 0), errors="coerce").fillna(0).sum())
            expected_progress = (actual_total / expected_total * 100) if abs(expected_total) >= 1e-12 else 0
            if abs(float(detail_meta.get("current_month_progress_pct") or 0) - expected_progress) > 1e-9:
                header_mismatches.append("detail progress should use summed actual / summed expected")
            summary_actual = float(pd.to_numeric(summary.get("당월 현재매출", 0), errors="coerce").fillna(0).sum())
            summary_expected = float(pd.to_numeric(summary.get("당월 예상매출", 0), errors="coerce").fillna(0).sum())
            summary_progress = (summary_actual / summary_expected * 100) if abs(summary_expected) >= 1e-12 else 0
            if abs(float(summary_meta.get("current_month_progress_pct") or 0) - summary_progress) > 1e-9:
                header_mismatches.append("summary progress should use summed actual / summed expected")

            if header_mismatches:
                results.append(_fail("manufacturer sales trend header summaries", "; ".join(header_mismatches)))
            else:
                results.append(_ok("manufacturer sales trend header summaries", "summary cards, progress totals, judge buckets, and table modes verified"))

            try:
                from app.ui.current_table_followups.action_dispatcher import handle_current_table_followup_by_action

                pushed_tables = []
                pushed_notices = []

                def _test_find_col(frame, *, exact=(), include_any=(), exclude_any=()):
                    cols = [str(c) for c in frame.columns]
                    for name in exact:
                        if name in cols:
                            return name
                    for col in cols:
                        if include_any and not any(w in col for w in include_any):
                            continue
                        if exclude_any and any(w in col for w in exclude_any):
                            continue
                        return col
                    return ""

                def _test_to_num(sr):
                    return pd.to_numeric(
                        sr.fillna("").astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False),
                        errors="coerce",
                    ).fillna(0)

                def _test_push_table(**kwargs):
                    pushed_tables.append(kwargs)
                    return True

                def _test_push_notice(**kwargs):
                    pushed_notices.append(kwargs)
                    return True

                class _NoopLog:
                    def info(self, *args, **kwargs):
                        return None

                    def exception(self, *args, **kwargs):
                        return None

                helpers = {
                    "find_col": _test_find_col,
                    "to_num": _test_to_num,
                    "push_table": _test_push_table,
                    "push_notice": _test_push_notice,
                }

                followup_mismatches = []
                source_action = "제약사별 매출 추세 분석 요약표"
                source_key = "test_manufacturer_summary"

                pushed_tables.clear()
                pushed_notices.clear()
                handled = handle_current_table_followup_by_action(
                    df=summary,
                    query="현재표 추세판정 집계",
                    top_n=20,
                    table_key=source_key,
                    source_action=source_action,
                    helpers=helpers,
                    log=_NoopLog(),
                )
                if not handled or not pushed_tables:
                    followup_mismatches.append("trend judge group should return table")
                else:
                    group_payload = pushed_tables[-1]
                    group_df = group_payload.get("df")
                    extra_meta = group_payload.get("extra_meta") or {}
                    if extra_meta.get("group_column") != "추세판정":
                        followup_mismatches.append(f"group column expected 추세판정 got={extra_meta.get('group_column')}")
                    if not isinstance(group_df, pd.DataFrame) or group_df.empty:
                        followup_mismatches.append("trend judge group dataframe empty")
                    else:
                        if "제약사수" not in group_df.columns:
                            followup_mismatches.append("trend judge group missing 제약사수")
                        elif int(pd.to_numeric(group_df["제약사수"], errors="coerce").fillna(0).sum()) != int(summary["제약사명"].nunique()):
                            followup_mismatches.append("trend judge group manufacturer count sum mismatch")
                        if "총매출액" in group_df.columns:
                            grouped_total = float(pd.to_numeric(group_df["총매출액"], errors="coerce").fillna(0).sum())
                            original_total = float(pd.to_numeric(summary["총매출액"], errors="coerce").fillna(0).sum())
                            if abs(grouped_total - original_total) > 1e-9:
                                followup_mismatches.append("trend judge group total sales sum mismatch")
                        if "현재표 분석/KPI 후속분석 불가" in str(group_payload.get("title") or ""):
                            followup_mismatches.append("trend judge group fell through to analytics kpi unsupported notice")

                for query, expected_col in [
                    ("현재표 추세판정 감소만 보여줘", "추세판정"),
                    ("현재표 제약사명 제약A 상세", "제약사명"),
                    ("현재표 당월 진척률 100 이상", "당월 진척률"),
                    ("현재표 총매출액 1억 이상", "총매출액"),
                ]:
                    pushed_tables.clear()
                    pushed_notices.clear()
                    handled = handle_current_table_followup_by_action(
                        df=summary,
                        query=query,
                        top_n=20,
                        table_key=source_key,
                        source_action=source_action,
                        helpers=helpers,
                        log=_NoopLog(),
                    )
                    if not handled or not pushed_tables:
                        followup_mismatches.append(f"followup should return table query={query}")
                        continue
                    out_df = pushed_tables[-1].get("df")
                    meta_extra = pushed_tables[-1].get("extra_meta") or {}
                    if not isinstance(out_df, pd.DataFrame):
                        followup_mismatches.append(f"followup output not dataframe query={query}")
                        continue
                    if expected_col not in out_df.columns:
                        followup_mismatches.append(f"followup output missing original column {expected_col} query={query}")
                    if "순번" not in out_df.columns:
                        followup_mismatches.append(f"followup should regenerate sequence query={query}")
                    if expected_col == "추세판정" and not out_df["추세판정"].astype(str).str.contains("감소").all():
                        followup_mismatches.append("trend judge text filter contains non 감소 rows")
                    if expected_col == "제약사명" and not out_df["제약사명"].astype(str).str.contains("제약A").all():
                        followup_mismatches.append("manufacturer text filter contains non 제약A rows")
                    if expected_col in {"당월 진척률", "총매출액"} and meta_extra.get("filter_column") != expected_col:
                        followup_mismatches.append(f"numeric filter meta column mismatch query={query} meta={meta_extra}")

                if followup_mismatches:
                    results.append(_fail("current table generic group/filter followups", "; ".join(followup_mismatches)))
                else:
                    results.append(_ok("current table generic group/filter followups", "generic group, text filter, numeric filter, and analytics fallback blocking verified"))
            except Exception as e:
                results.append(_fail("current table generic group/filter followups", f"{type(e).__name__}: {e}"))

            if mismatches:
                results.append(_fail("manufacturer sales trend summary", "; ".join(mismatches)))
            else:
                results.append(_ok("manufacturer sales trend summary", "summary schema, monthly pivot, totals, universe, and meta verified"))
        finally:
            if old_loader is not None:
                setattr(manufacturer_mod, "get_sales_trend_df", old_loader)
    except Exception as e:
        results.append(_fail("manufacturer sales trend", f"{type(e).__name__}: {e}"))


    try:
        supp_mod = importlib.import_module("app.services.analytics_supplier_stock_shortage_service")
        product_base = pd.DataFrame([
            {
                "제품코드": "P001",
                "제품명": "테스트제품",
                "규격": "EA",
                "제조사명": "제조사A",
                "제품그룹명": "G",
                "제품구분명": "D",
                "제품분류명": "C",
                "재고기준": "장부",
                "현재재고수량": 100,
                "현재재고금액": 2000,
                "재고평가단가": 20,
                "당월 예상출고수량": 100,
                "당월 잔여예상출고수량": 150,
                "당월 출고진척률": 50,
                "부족예상수량": 50,
                "부족예상금액": 1000,
                "1개월부족수량": 10,
                "2개월부족수량": 20,
                "3개월부족수량": 30,
                "재고커버월수": 0.67,
                "당월 재고충족률": 66.67,
                "재고부족판정": "부족",
                "부족등급": "부족",
            },
            {
                "제품코드": "P002",
                "제품명": "테스트제품2",
                "규격": "EA",
                "제조사명": "제조사A",
                "제품그룹명": "G",
                "제품구분명": "D",
                "제품분류명": "C",
                "재고기준": "장부",
                "현재재고수량": 13,
                "현재재고금액": 143,
                "재고평가단가": 11,
                "당월 예상출고수량": 0,
                "당월 잔여예상출고수량": 0,
                "당월 출고진척률": 0,
                "부족예상수량": 0,
                "부족예상금액": 0,
                "1개월부족수량": 0,
                "2개월부족수량": 0,
                "3개월부족수량": 0,
                "재고커버월수": 0,
                "당월 재고충족률": 100,
                "재고부족판정": "수요없음",
                "부족등급": "정상",
            }
        ])
        supplier_stock = pd.DataFrame([
            {
                "제품코드": "P001",
                "매입처코드": "A",
                "매입처명": "매입처A",
                "매입처원본재고수량": 120,
                "매입처원본재고금액": 1200,
                "양수재고수량": 120,
                "양수재고금액": 700,
                "최근6완료월매입금액": 70,
                "전체완료월매입금액": 70,
                "매입처입고누계수량": 70,
            },
            {
                "제품코드": "P001",
                "매입처코드": "B",
                "매입처명": "매입처B",
                "매입처원본재고수량": -20,
                "매입처원본재고금액": -200,
                "양수재고수량": 80,
                "양수재고금액": 300,
                "최근6완료월매입금액": 30,
                "전체완료월매입금액": 30,
                "매입처입고누계수량": 30,
            },
            {
                "제품코드": "P002",
                "매입처코드": "C",
                "매입처명": "매입처C",
                "매입처원본재고수량": 7,
                "매입처원본재고금액": 999999,
                "양수재고수량": 7,
                "양수재고금액": 77,
                "최근6완료월매입금액": 0,
                "전체완료월매입금액": 0,
                "매입처입고누계수량": 7,
            },
            {
                "제품코드": "P002",
                "매입처코드": "D",
                "매입처명": "매입처D",
                "매입처원본재고수량": 6,
                "매입처원본재고금액": 888888,
                "양수재고수량": 6,
                "양수재고금액": 66,
                "최근6완료월매입금액": 0,
                "전체완료월매입금액": 0,
                "매입처입고누계수량": 6,
            },
        ])
        detail = supp_mod.build_supplier_allocation_detail(product_base, supplier_stock)
        summary_all = supp_mod.build_supplier_shortage_summary(detail, {})
        summary_b = supp_mod.build_supplier_shortage_summary(detail, {"buy_nm": "매입처B"})

        a_amt = float(summary_all.loc[summary_all["매입처코드"] == "A", "배정부족예상금액"].iloc[0])
        b_amt = float(summary_all.loc[summary_all["매입처코드"] == "B", "배정부족예상금액"].iloc[0])
        total_qty = float(detail["배정부족예상수량"].sum())
        stock_sum = float(detail.loc[detail["제품코드"] == "P001", "매입처원본재고수량"].sum())
        p001_stock_amt = float(detail.loc[detail["제품코드"] == "P001", "매입처원본재고금액"].sum())
        p002_stock_amt = float(detail.loc[detail["제품코드"] == "P002", "매입처원본재고금액"].sum())
        p002_qty_7_amt = float(detail.loc[detail["매입처코드"] == "C", "매입처원본재고금액"].iloc[0])
        p002_qty_6_amt = float(detail.loc[detail["매입처코드"] == "D", "매입처원본재고금액"].iloc[0])
        neg_b = float(detail.loc[detail["매입처코드"] == "B", "매입처원본재고수량"].iloc[0])
        neg_b_amt = float(detail.loc[detail["매입처코드"] == "B", "매입처원본재고금액"].iloc[0])
        filtered_b_amt = float(summary_b["배정부족예상금액"].sum())

        mismatches = []
        if abs(a_amt - 700) > 1e-6 or abs(b_amt - 300) > 1e-6:
            mismatches.append(f"70/30 allocation expected 700/300 got {a_amt}/{b_amt}")
        if abs(total_qty - 50) > 1e-6:
            mismatches.append(f"shortage qty should remain product-level 50 got {total_qty}")
        if abs(stock_sum - 100) > 1e-6 or abs(neg_b + 20) > 1e-6:
            mismatches.append(f"negative supplier stock not preserved stock_sum={stock_sum} neg_b={neg_b}")
        if abs(p001_stock_amt - 2000) > 1e-6 or abs(neg_b_amt + 400) > 1e-6:
            mismatches.append(f"stock amount must use product unit price p001_sum={p001_stock_amt} neg_b_amt={neg_b_amt}")
        if abs(p002_stock_amt - 143) > 1e-6 or abs(p002_qty_7_amt - 77) > 1e-6 or abs(p002_qty_6_amt - 66) > 1e-6:
            mismatches.append(f"7/6 stock amount expected 77/66 sum 143 got {p002_qty_7_amt}/{p002_qty_6_amt}/{p002_stock_amt}")
        if not set(detail["재고정합성"].dropna().astype(str).unique()).issubset({"일치"}):
            mismatches.append(f"stock consistency should match public basis got={detail['재고정합성'].dropna().astype(str).unique().tolist()}")
        if abs(filtered_b_amt - 300) > 1e-6 or len(summary_b) != 1:
            mismatches.append(f"supplier filter must apply after allocation got rows={len(summary_b)} amount={filtered_b_amt}")

        if mismatches:
            results.append(_fail("supplier stock shortage allocation fixture", "; ".join(mismatches)))
        else:
            results.append(_ok("supplier stock shortage allocation fixture", "unit-price stock amount, negative stock, 70/30 allocation, and post-filter OK"))

        old_product_loader = getattr(supp_mod, "load_product_shortage_base")
        old_supplier_loader = getattr(supp_mod, "load_supplier_product_stock")
        try:
            setattr(supp_mod, "load_product_shortage_base", lambda params: product_base.copy())
            setattr(supp_mod, "load_supplier_product_stock", lambda product_codes, params: supplier_stock.copy())
            payload = supp_mod.get_supplier_stock_shortage_result({"stock_mode": "book"})
            result_df = payload.get("df") if isinstance(payload.get("df"), pd.DataFrame) else payload.get("df_display")
            meta = payload.get("meta") or {}
            import io
            import json

            unsafe_types = (pd.DataFrame, pd.Series, bytes, bytearray)
            unsafe_meta = [k for k, v in meta.items() if isinstance(v, unsafe_types)]
            unsafe_attrs = [
                k
                for k, v in getattr(result_df, "attrs", {}).items()
                if isinstance(v, unsafe_types)
            ] if isinstance(result_df, pd.DataFrame) else []
            room = {
                "id": "fixture",
                "messages": [
                    {
                        "role": "assistant",
                        "type": payload.get("type"),
                        "action": payload.get("action"),
                        "title": payload.get("title"),
                        "meta": meta,
                    }
                ],
            }
            json.dumps(room, ensure_ascii=False)

            from app.ui.chat_middleware import _make_table_downloads
            from openpyxl import load_workbook

            _, xlsx_buf = _make_table_downloads(result_df)
            sheet_names = load_workbook(io.BytesIO(xlsx_buf.getvalue()), read_only=True).sheetnames
            follow_df = result_df.head(1).copy()
            follow_df.attrs.clear()
            _, follow_xlsx_buf = _make_table_downloads(follow_df)
            follow_sheet_names = load_workbook(io.BytesIO(follow_xlsx_buf.getvalue()), read_only=True).sheetnames
            if unsafe_meta or unsafe_attrs:
                results.append(_fail("supplier stock shortage json-safe payload", f"unsafe_meta={unsafe_meta} unsafe_attrs={unsafe_attrs}"))
            elif sheet_names != ["매입처별요약", "제품매입처상세"]:
                results.append(_fail("supplier stock shortage json-safe payload", f"unexpected excel sheets={sheet_names}"))
            elif follow_sheet_names != ["SIMS"]:
                results.append(_fail("supplier stock shortage json-safe payload", f"current-table result should be one sheet got={follow_sheet_names}"))
            else:
                results.append(_ok("supplier stock shortage json-safe payload", "json.dumps room OK; original Excel 2 sheets; current-table Excel one sheet"))
        finally:
            setattr(supp_mod, "load_product_shortage_base", old_product_loader)
            setattr(supp_mod, "load_supplier_product_stock", old_supplier_loader)

        try:
            generic_mod = importlib.import_module("app.ui.current_table_followups.generic")
            captured: dict[str, object] = {}

            def _push_table(**kwargs):
                captured.update(kwargs)
                return True

            top_df = pd.DataFrame({
                "매입처코드": [f"B{i:02d}" for i in range(25)],
                "배정부족예상금액": list(range(25)),
                "매입처원본재고금액": list(range(100, 125)),
            })
            top_df.attrs["supplier_detail_key"] = "detail-key-should-not-inherit"
            ok_top = generic_mod.handle_common_column_filter_followup(
                df=top_df,
                query="현재표 배정부족예상금액 TOP 20",
                top_n=20,
                table_key="supplier-table",
                source_action="매입처별 재고부족 현황",
                helpers={"push_table": _push_table},
                log=log,
            )
            out_top = captured.get("df")
            if not ok_top or not isinstance(out_top, pd.DataFrame):
                results.append(_fail("supplier current-table top amount", "TOP 20 route did not push table"))
            elif len(out_top) != 20 or float(out_top["배정부족예상금액"].iloc[0]) != 24 or float(out_top["배정부족예상금액"].iloc[-1]) != 5:
                results.append(_fail("supplier current-table top amount", f"unexpected TOP rows={len(out_top)} head/tail={out_top['배정부족예상금액'].head(1).tolist()}/{out_top['배정부족예상금액'].tail(1).tolist()}"))
            elif getattr(out_top, "attrs", {}).get("supplier_detail_key"):
                results.append(_fail("supplier current-table top amount", "supplier_detail_key leaked into current-table TOP result"))
            else:
                results.append(_ok("supplier current-table top amount", "배정부족예상금액 TOP 20 sorted desc; detail attrs cleared"))
        except Exception as e:
            results.append(_fail("supplier current-table top amount", f"{type(e).__name__}: {e}"))

        try:
            generic_mod = importlib.import_module("app.ui.current_table_followups.generic")
            captured_filter: dict[str, object] = {}

            def _push_filter_table(**kwargs):
                captured_filter.clear()
                captured_filter.update(kwargs)
                return True

            shortage_df = pd.DataFrame(
                [
                    {"제품코드": "P001", "제품명": "가나다정", "규격": "10T", "제조사명": "A제약", "부족예상수량": 5, "부족예상금액": 1000},
                    {"제품코드": "P002", "제품명": "라마바정", "규격": "20T", "제조사명": "B제약", "부족예상수량": 12, "부족예상금액": 2000},
                    {"제품코드": "P003", "제품명": "사아자정", "규격": "30T", "제조사명": "C제약", "부족예상수량": 30, "부족예상금액": 3000},
                ]
            )
            ok_filter = generic_mod.handle_common_column_filter_followup(
                df=shortage_df,
                query="현재표 부족제품수 10 이상 목록",
                top_n=20,
                table_key="stock-shortage",
                source_action="품목별 재고부족현황",
                helpers={"push_table": _push_filter_table},
                log=log,
            )
            out_filter = captured_filter.get("df")
            action_filter = str(captured_filter.get("action") or "")
            if not ok_filter or not isinstance(out_filter, pd.DataFrame):
                results.append(_fail("current-table shortage qty alias", "부족제품수 numeric filter did not push table"))
            elif "부족예상수량" not in action_filter or "제품명" in action_filter:
                results.append(_fail("current-table shortage qty alias", f"unexpected action={action_filter}"))
            elif list(out_filter["제품명"]) != ["라마바정", "사아자정"]:
                results.append(_fail("current-table shortage qty alias", f"product names shifted={list(out_filter['제품명'])}"))
            elif "부족예상수량" not in list(out_filter.columns):
                results.append(_fail("current-table shortage qty alias", f"missing 부족예상수량 columns={list(out_filter.columns)}"))
            else:
                results.append(_ok("current-table shortage qty alias", "부족제품수 10 이상 -> 부족예상수량 numeric filter; 제품명 values preserved"))
        except Exception as e:
            results.append(_fail("current-table shortage qty alias", f"{type(e).__name__}: {e}"))

        try:
            chat_mod = importlib.import_module("app.ui.chat_middleware")
            amount_df = pd.DataFrame({
                "기준월": ["202607"],
                "매입처코드": ["B001"],
                "완료월수": [6],
                "배정부족예상금액": [1234],
                "배정1개월부족금액": [100],
                "매입처원본재고금액": [200],
            })
            profile = chat_mod._build_sims_sales_time_profile(
                amount_df,
                chat_mod._sims_business_terms("매입처별 재고부족 현황"),
            )
            amount_col = str((profile or {}).get("amount_col") or "")
            amount_label = str((profile or {}).get("amount_label") or "")
            if amount_col != "배정부족예상금액":
                results.append(_fail("supplier amount column priority", f"expected 배정부족예상금액 got={amount_col}"))
            elif amount_label != "배정부족예상금액":
                results.append(_fail("supplier amount column priority", f"expected amount_label=배정부족예상금액 got={amount_label}"))
            else:
                current_profile = chat_mod._build_sims_sales_time_profile(
                    amount_df,
                    chat_mod._sims_business_terms("현재표 배정부족예상금액 TOP 20"),
                )
                current_amount_col = str((current_profile or {}).get("amount_col") or "")
                current_amount_label = str((current_profile or {}).get("amount_label") or "")
                inherited_ctx = chat_mod._build_sims_analysis_context_from_df(
                    amount_df,
                    result={},
                    action_name="현재표 부족제품수 10 이상 목록",
                    params={},
                    meta={
                        "current_table_followup": True,
                        "flow": "매입처별 재고부족",
                        "amount_label": "배정부족예상금액",
                        "amount_priority": (
                            "배정부족예상금액",
                            "배정1개월부족금액",
                            "매입처원본재고금액",
                        ),
                    },
                )
                inherited_amount_col = str(((inherited_ctx or {}).get("sales_time_profile") or {}).get("amount_col") or "")
                if current_amount_col != "배정부족예상금액" or current_amount_label != "배정부족예상금액":
                    results.append(_fail("supplier amount column priority", f"current-table amount mismatch col={current_amount_col} label={current_amount_label}"))
                elif inherited_amount_col != "배정부족예상금액":
                    results.append(_fail("supplier amount column priority", f"inherited current-table amount mismatch col={inherited_amount_col}"))
                else:
                    results.append(_ok("supplier amount column priority", "amount_col=배정부족예상금액 for source, followup, and inherited profile"))
        except Exception as e:
            results.append(_fail("supplier amount column priority", f"{type(e).__name__}: {e}"))

        try:
            chat_mod = importlib.import_module("app.ui.chat_middleware")
            panel_mod = importlib.import_module("app.ui.sims_panel")

            class _FakeExpander:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            class _FakeStHeader:
                def __init__(self):
                    self.captions: list[str] = []
                    self.markdowns: list[str] = []
                    self.expanders: list[tuple[str, bool]] = []

                def caption(self, text, *args, **kwargs):
                    self.captions.append(str(text))

                def markdown(self, text, *args, **kwargs):
                    self.markdowns.append(str(text))

                def expander(self, label, expanded=False, *args, **kwargs):
                    self.expanders.append((str(label), bool(expanded)))
                    return _FakeExpander()

            long_tail = "끝부분-원문보존"
            long_query = "조회조건 " + ("가나다라마바사 " * 80) + long_tail
            long_nlq = "NLQ 원문 " + ("사용자 질문 내용 " * 80) + long_tail
            sample_df = pd.DataFrame({"제품코드": ["P001", "P002"], "부족예상수량": [1, 2]})
            sample_item = {
                "type": "table",
                "title": "품목별 재고부족현황",
                "action": "품목별 재고부족현황",
                "params": {"date_from": "2026-01-01", "date_to": "2026-07-17"},
            }
            sample_meta = {
                "table_key": "sims_fixture",
                "source_key": "source_fixture",
                "source_action": "품목별 재고부족현황",
                "query_summary": long_query,
                "download_row_count": 11713,
                "display_row_count": 3000,
                "created_at": "2026-07-17T20:01:31",
                "nlq_query": long_nlq,
            }
            view = chat_mod._build_sims_result_header_view(sample_item, sample_meta, sample_df, title="품목별 재고부족현황")
            fake_chat_st = _FakeStHeader()
            old_chat_st = getattr(chat_mod, "st", None)
            setattr(chat_mod, "st", fake_chat_st)
            try:
                chat_mod._render_sims_result_header_view(view)
            finally:
                if old_chat_st is not None:
                    setattr(chat_mod, "st", old_chat_st)

            expired_view = chat_mod._build_sims_result_header_view(sample_item, sample_meta, title="품목별 재고부족현황", expired=True)
            panel_payload = {
                "title": "품목별 재고부족현황",
                "action": "품목별 재고부족현황",
                "df": pd.DataFrame({"제품코드": range(5)}),
                "meta": dict(sample_meta),
            }
            fake_panel_st = _FakeStHeader()
            old_panel_st = getattr(panel_mod, "st", None)
            setattr(panel_mod, "st", fake_panel_st)
            try:
                panel_mod._render_panel_result_compact_header(panel_payload, "품목별 재고부족현황", "품목별 재고부족현황", sample_df)
            finally:
                if old_panel_st is not None:
                    setattr(panel_mod, "st", old_panel_st)
            panel_payload_no_key = {
                "title": "품목별 재고부족현황",
                "action": "품목별 재고부족현황",
                "df": pd.DataFrame({"제품코드": range(5)}),
                "meta": {k: v for k, v in sample_meta.items() if k != "table_key"},
            }
            fake_panel_no_key_st = _FakeStHeader()
            setattr(panel_mod, "st", fake_panel_no_key_st)
            try:
                panel_mod._render_panel_result_compact_header(panel_payload_no_key, "품목별 재고부족현황", "품목별 재고부족현황", sample_df)
            finally:
                if old_panel_st is not None:
                    setattr(panel_mod, "st", old_panel_st)

            header_mismatches = []
            if len(fake_chat_st.captions) != 2:
                header_mismatches.append(f"chat captions expected 2 got={fake_chat_st.captions}")
            if any("table_key" in c or "NLQ" in c for c in fake_chat_st.captions):
                header_mismatches.append(f"chat captions leaked diagnostics={fake_chat_st.captions}")
            detail_text = "\n".join(fake_chat_st.markdowns)
            if "table_key" not in detail_text or "sims_fixture" not in detail_text or "NLQ 원문" not in detail_text:
                header_mismatches.append(f"chat details missing diagnostics={detail_text}")
            if long_tail not in detail_text:
                header_mismatches.append("chat details truncated long query/NLQ tail")
            if long_tail in "\n".join(fake_chat_st.captions):
                header_mismatches.append(f"chat captions must compact long query={fake_chat_st.captions}")
            if fake_chat_st.expanders != [("상세 조회정보", False)]:
                header_mismatches.append(f"chat expander unexpected={fake_chat_st.expanders}")
            if "전체 11,713건" not in str(view.get("line1")) or "표 데이터 3,000건" not in str(view.get("line1")):
                header_mismatches.append(f"row summary unexpected={view.get('line1')}")
            if "품목별 재고부족현황" in str(view.get("line1")):
                header_mismatches.append("title duplicated inside row summary")
            if expired_view.get("followup_available"):
                header_mismatches.append("expired fallback must not advertise followup availability")
            if "만료" not in str(expired_view.get("line1")):
                header_mismatches.append(f"expired line missing status={expired_view.get('line1')}")
            if len(fake_panel_st.captions) != 2:
                header_mismatches.append(f"panel captions expected 2 got={fake_panel_st.captions}")
            if any("table_key" in c for c in fake_panel_st.captions):
                header_mismatches.append(f"panel captions leaked table_key={fake_panel_st.captions}")
            if "table_key" not in "\n".join(fake_panel_st.markdowns):
                header_mismatches.append("panel details missing table_key")
            if "현재표 후속질문 가능" not in " ".join(fake_panel_st.captions):
                header_mismatches.append(f"panel caption missing followup availability={fake_panel_st.captions}")
            if "현재표 후속질문 가능" in " ".join(fake_panel_no_key_st.captions):
                header_mismatches.append(f"panel without table_key advertised followup={fake_panel_no_key_st.captions}")
            if long_tail not in "\n".join(fake_panel_st.markdowns):
                header_mismatches.append("panel details truncated long query tail")

            if header_mismatches:
                results.append(_fail("sims result compact header render", "; ".join(header_mismatches)))
            else:
                results.append(_ok("sims result compact header render", "chat/panel headers use two captions; detail expander preserves long originals; expired/no-key paths hide followup"))
        except Exception as e:
            results.append(_fail("sims result compact header render", f"{type(e).__name__}: {e}"))

        try:
            chat_mod = importlib.import_module("app.ui.chat_middleware")
            st_obj = getattr(chat_mod, "st", None)
            session_state = getattr(st_obj, "session_state", None)
            if session_state is None:
                raise RuntimeError("streamlit session_state unavailable")

            old_cache = session_state.get("__sims_analysis_ctx_by_table_key")
            old_latest = session_state.get("__sims_analysis_ctx")
            try:
                product_ctx = {
                    "kind": "SIMS_ANALYSIS_CONTEXT_V1",
                    "table_key": "sims_product_codes",
                    "source_table_key": "",
                    "action": "제품코드 목록",
                    "row_count": 2,
                    "column_count": 2,
                    "analysis_text": "product context",
                }
                vendor_ctx = {
                    "kind": "SIMS_ANALYSIS_CONTEXT_V1",
                    "table_key": "sims_vendors",
                    "source_table_key": "",
                    "action": "거래처 목록",
                    "row_count": 3,
                    "column_count": 2,
                    "analysis_text": "vendor context",
                }
                session_state["__sims_analysis_ctx_by_table_key"] = {}
                chat_mod._cache_sims_analysis_ctx_by_table_key(product_ctx)
                chat_mod._cache_sims_analysis_ctx_by_table_key(vendor_ctx)
                session_state["__sims_analysis_ctx"] = vendor_ctx

                selected_ctx, selected_source = chat_mod._select_sims_analysis_ctx_for_table(
                    table_key="sims_product_codes",
                    action="제품코드 목록",
                    meta={"table_key": "sims_product_codes", "action": "제품코드 목록"},
                    download_df=pd.DataFrame({"제품코드": ["A", "B"], "제품명": ["a", "b"]}),
                )
                mismatch_same = chat_mod._sims_clicked_llm_context_mismatch(
                    selected_ctx,
                    "sims_product_codes",
                    "제품코드 목록",
                )
                mismatch_wrong = chat_mod._sims_clicked_llm_context_mismatch(
                    vendor_ctx,
                    "sims_product_codes",
                    "제품코드 목록",
                )
                rebuilt_ctx, rebuilt_source = chat_mod._select_sims_analysis_ctx_for_table(
                    table_key="sims_rebuilt",
                    action="제품코드 목록",
                    meta={"table_key": "sims_rebuilt", "action": "제품코드 목록"},
                    download_df=pd.DataFrame({"제품코드": ["C"], "제품명": ["c"]}),
                )
                mismatch_spaced = chat_mod._sims_clicked_llm_context_mismatch(
                    {"kind": "SIMS_ANALYSIS_CONTEXT_V1", "table_key": "sims_product_codes", "action": "Product Codes"},
                    "sims_product_codes",
                    "  Product   Codes  ",
                )

                class _DummyColumn:
                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return False

                calls = []
                warnings = []

                def _fake_runner(prompt, **kwargs):
                    calls.append({"prompt": prompt, "kwargs": kwargs})

                old_runner = session_state.get("__sims_llm_analysis_runner")
                old_button = getattr(chat_mod.st, "button")
                old_columns = getattr(chat_mod.st, "columns")
                old_download_button = getattr(chat_mod.st, "download_button")
                old_caption = getattr(chat_mod.st, "caption")
                old_warning = getattr(chat_mod.st, "warning")
                try:
                    session_state["__sims_llm_analysis_runner"] = _fake_runner
                    chat_mod.st.columns = lambda *args, **kwargs: [_DummyColumn(), _DummyColumn(), _DummyColumn()]
                    chat_mod.st.download_button = lambda *args, **kwargs: None
                    chat_mod.st.caption = lambda *args, **kwargs: None
                    chat_mod.st.warning = lambda msg, *args, **kwargs: warnings.append(str(msg))

                    chat_mod.st.button = lambda *args, **kwargs: True
                    chat_mod._render_sims_result_actions_plain(
                        key_suffix="plain_product",
                        csv_bytes=b"a,b\n1,2\n",
                        csv_name="plain.csv",
                        excel_bytes=b"xlsx",
                        xlsx_name="plain.xlsx",
                        prompt="analyze product",
                        table_key="sims_product_codes",
                        clicked_action="",
                        clicked_message_id="msg_product",
                        clicked_meta={"table_key": "sims_product_codes", "action": ""},
                        download_df=pd.DataFrame({"code": ["A"], "name": ["a"]}),
                    )

                    chat_mod.st.button = lambda *args, **kwargs: "prepare" not in str(kwargs.get("key") or "")
                    chat_mod._render_sims_result_actions_lazy(
                        key_suffix="lazy_product",
                        table_key="sims_product_codes",
                        download_df=pd.DataFrame({"code": ["A"], "name": ["a"]}),
                        csv_name="lazy.csv",
                        xlsx_name="lazy.xlsx",
                        prompt="analyze lazy product",
                        expected_rows=10000,
                        display_rows=1,
                        clicked_message_id="msg_product_lazy",
                        clicked_action="",
                        clicked_meta={"table_key": "sims_product_codes", "action": ""},
                    )

                    before_missing = len(calls)
                    chat_mod._run_clicked_sims_llm_analysis(
                        prompt="missing override",
                        key_suffix="missing_product",
                        table_key="sims_missing",
                        action="Product Codes",
                        message_id="msg_missing",
                        analysis_ctx=None,
                        context_source="missing",
                    )
                    missing_blocked = len(calls) == before_missing

                    before_mismatch = len(calls)
                    chat_mod._run_clicked_sims_llm_analysis(
                        prompt="wrong override",
                        key_suffix="wrong_product",
                        table_key="sims_product_codes",
                        action="?쒗뭹肄붾뱶 紐⑸줉",
                        message_id="msg_wrong",
                        analysis_ctx=vendor_ctx,
                        context_source="cache",
                    )
                    mismatch_blocked = len(calls) == before_mismatch
                finally:
                    if old_runner is None:
                        session_state.pop("__sims_llm_analysis_runner", None)
                    else:
                        session_state["__sims_llm_analysis_runner"] = old_runner
                    chat_mod.st.button = old_button
                    chat_mod.st.columns = old_columns
                    chat_mod.st.download_button = old_download_button
                    chat_mod.st.caption = old_caption
                    chat_mod.st.warning = old_warning

                source = Path("app/ui/chat_middleware.py").read_text(encoding="utf-8")
                main_source = Path("app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
                mismatches = []
                if selected_ctx.get("table_key") != "sims_product_codes" or selected_ctx.get("action") != "제품코드 목록":
                    mismatches.append(f"selected wrong ctx={selected_ctx}")
                if selected_source != "cache":
                    mismatches.append(f"expected cache source got={selected_source}")
                if mismatch_same:
                    mismatches.append(f"same table/action mismatch={mismatch_same}")
                if mismatch_wrong != "table_key_mismatch":
                    mismatches.append(f"wrong table mismatch={mismatch_wrong}")
                if mismatch_spaced:
                    mismatches.append(f"spaced action should match, got={mismatch_spaced}")
                if rebuilt_source != "rebuilt" or rebuilt_ctx.get("table_key") != "sims_rebuilt":
                    mismatches.append(f"rebuilt ctx/source unexpected={rebuilt_ctx}, {rebuilt_source}")
                if len(calls) != 2:
                    mismatches.append(f"expected plain/lazy runner calls=2 got={len(calls)} calls={calls}")
                else:
                    plain_ctx = calls[0]["kwargs"].get("analysis_ctx_override")
                    lazy_ctx = calls[1]["kwargs"].get("analysis_ctx_override")
                    if not isinstance(plain_ctx, dict) or plain_ctx.get("table_key") != "sims_product_codes":
                        mismatches.append(f"plain runner used wrong ctx={plain_ctx}")
                    if not isinstance(lazy_ctx, dict) or lazy_ctx.get("table_key") != "sims_product_codes":
                        mismatches.append(f"lazy runner used wrong ctx={lazy_ctx}")
                if not missing_blocked:
                    mismatches.append("missing table-scoped context did not block runner")
                if not mismatch_blocked:
                    mismatches.append("mismatched table-scoped context did not block runner")
                if "__sims_old_table_download_enabled" in source or "sims_old_table_prepare_excel" in source:
                    mismatches.append("old-table early Excel-only return block remains")
                if "analysis_ctx_override" not in main_source or "selected_context_table_key" not in main_source:
                    mismatches.append("main runner does not accept/log table-scoped analysis context")
                if "table_scoped_request and not valid_override" not in main_source:
                    mismatches.append("main runner does not fail closed when clicked table context is missing")
                if "_run_clicked_sims_llm_analysis" not in source or "clicked_table_key" not in source:
                    mismatches.append("chat action buttons do not use table-scoped LLM helper")

                if mismatches:
                    results.append(_fail("table-scoped SIMS LLM analysis context", "; ".join(mismatches)))
                else:
                    results.append(_ok("table-scoped SIMS LLM analysis context", "old table buttons use clicked table_key/action context; latest global context is not used as fallback; mismatch blocks LLM"))
            finally:
                if old_cache is None:
                    session_state.pop("__sims_analysis_ctx_by_table_key", None)
                else:
                    session_state["__sims_analysis_ctx_by_table_key"] = old_cache
                if old_latest is None:
                    session_state.pop("__sims_analysis_ctx", None)
                else:
                    session_state["__sims_analysis_ctx"] = old_latest
        except Exception as e:
            results.append(_fail("table-scoped SIMS LLM analysis context", f"{type(e).__name__}: {e}"))

        try:
            chat_mod = importlib.import_module("app.ui.chat_middleware")

            master_cases = [
                {
                    "analysis_type": "users_master",
                    "llm_summary_kind": "users_master_summary",
                    "llm_summary_md": "사용자마스터 전체 집계 요약\n- 전체 조회건수: 227건",
                    "users_master_summary": {"top": ["부서"]},
                    "table_key": "sims_users",
                },
                {
                    "analysis_type": "goods_master",
                    "llm_summary_kind": "goods_master_summary",
                    "llm_summary_md": "제품마스터 전체 집계 요약\n- 전체 조회건수: 120건",
                    "goods_master_summary": {"top": ["제품"]},
                    "table_key": "sims_goods",
                },
                {
                    "analysis_type": "vendor_master",
                    "llm_summary_kind": "vendor_master_summary",
                    "llm_summary_md": "거래처마스터 자동 집계 요약\n- 전체 조회건수: 340건",
                    "vendor_master_summary": {"top": ["거래처"]},
                    "master_nlq": True,
                    "domain": "vendors",
                    "source_key": "sims_vendors",
                },
                {
                    "analysis_type": "codes_master",
                    "llm_summary_kind": "codes_master_summary",
                    "llm_summary_md": "코드마스터 전체 집계 요약\n- 전체 조회건수: 42건",
                    "codes_master_summary": {"top": ["코드"]},
                    "table_key": "sims_codes",
                },
            ]
            visible_cases = [
                {"summary_md": "조회 결과가 없습니다. 조건을 확인해 주세요.", "analysis_type": "no_data"},
                {"summary_md": "오류가 발생했습니다. 다시 시도해 주세요.", "analysis_type": "error"},
                {"summary_md": "LLM 분석 답변입니다.", "analysis_type": "llm_answer"},
                {"summary_md": "현재표 후속질문 결과 요약", "current_table_followup": True},
            ]
            summary_sample = (
                "조회조건: 기간 2026-01-01 ~ 2026-07-17\n"
                "조회결과: 227건\n"
                "조회결과: 11,713건\n"
                "전체 조회건수: 11,713건\n"
                "화면 표시건수: 3,000건\n"
                "조회 완료: 29,716건\n"
                "조회 결과: 11,713건 (표시 3,000건)\n"
                "조회결과: 조회 결과가 없습니다.\n"
                "조회 결과: 조건에 맞는 자료가 없습니다.\n"
                "조회 완료: 일부 자료를 불러오지 못했습니다.\n"
                "오류가 발생했습니다.\n"
                "권한이 없습니다.\n"
                "조건을 확인해 주세요.\n"
                "일부 데이터만 조회됐다는 경고\n"
                "KPI 수치: 123건\n"
                "업무 요약 본문"
            )
            cleaned = chat_mod._clean_chat_summary_text_v2(summary_sample, "기간 2026-01-01 ~ 2026-07-17")
            inbound_summary = (
                "조회조건: 기간 2026-07-01 ~ 2026-07-18 / 재고위치 전체\n"
                "조회결과: 전체 12,131건 / 화면 표시 12,131건\n"
                "입고수량: 9,999개\n"
                "업무 요약 본문"
            )
            inbound_cleaned = chat_mod._clean_chat_summary_text_v2(
                inbound_summary,
                "기간 2026-07-01 ~ 2026-07-18 / 재고위치 전체",
            )
            inbound_item_for_header = {
                "type": "table",
                "action": "입고명세 조회",
                "meta": {
                    "query_summary": "기간 2026-07-01 ~ 2026-07-18 / 재고위치 전체",
                    "table_key": "sims_dfca5279",
                },
            }
            inbound_summary_cond_text = chat_mod._summary_condition_text_for_cleanup(
                inbound_item_for_header,
                inbound_item_for_header["meta"],
                is_sims_table_or_text=True,
                caption_cond_text="",
            )
            inbound_cleaned_from_render_path = chat_mod._clean_chat_summary_text_v2(
                inbound_summary,
                inbound_summary_cond_text,
            )
            product_flow_summary = (
                "제품정보: 제품코드 12345 / 제품명 테스트제품 / 제조사명 테스트제약\n"
                "이월재고: 10개\n"
                "입고수량: 20개\n"
                "출고수량: 5개\n"
                "재고수량: 25개\n"
                "집계 요약 펼쳐보기"
            )
            product_flow_cleaned = chat_mod._clean_chat_summary_text_v2(
                product_flow_summary,
                "제품코드 12345 / 제품명 테스트제품 / 제조사명 테스트제약",
            )
            inventory_summary = (
                "조회조건: 기간 2026-01-01 ~ 2026-07-18 / 재고기준 장부재고\n"
                "조회 결과: 14,308건 (표시 3,000건)\n"
                "현재재고수량: 100개\n"
                "경고: 일부 데이터만 조회됐다는 경고"
            )
            inventory_cleaned = chat_mod._clean_chat_summary_text_v2(
                inventory_summary,
                "기간 2026-01-01 ~ 2026-07-18 / 재고기준 장부재고",
            )
            inventory_cleaned_drop_product = chat_mod._clean_chat_summary_text_v2(
                "제품정보: 제품코드 12345 / 제품명 테스트제제\n현재재고수량: 100개",
                "기간 2026-01-01 ~ 2026-07-18 / 재고기준 장부재고",
                drop_product_info=True,
            )
            inventory_cleaned_keep_product = chat_mod._clean_chat_summary_text_v2(
                "제품정보: 제품코드 12345 / 제품명 테스트제제\n현재재고수량: 100개",
                "제품코드 12345 / 제품명 테스트제제",
                drop_product_info=False,
            )
            explicit_product_meta = {"params": {"제품코드": "12345"}}
            maker_only_meta = {"params": {"제조사명": "테스트제약"}}
            product_group_only_meta = {"params": {"제품그룹명": "전문의약품"}}
            product_name_only_meta = {"params": {"제품명": "정"}}
            blank_product_code_meta = {"params": {"제품코드": ""}}
            full_inventory_df = pd.DataFrame({"제품코드": ["A", "B"], "제품명": ["a", "b"]})
            single_inventory_df = pd.DataFrame({"제품코드": ["A", "A"], "제품명": ["a", "a"]})
            src = Path("app/ui/chat_middleware.py").read_text(encoding="utf-8")
            mismatches = []
            if not all(chat_mod._is_internal_master_summary(meta) for meta in master_cases):
                mismatches.append("master summary meta not hidden")
            if any(chat_mod._is_internal_master_summary(meta) for meta in visible_cases):
                mismatches.append("visible user-facing summary misclassified as internal master")
            if "업무 요약 본문" not in cleaned:
                mismatches.append(f"cleaned summary lost body={cleaned!r}")
            removed_lines = (
                "조회결과: 227건",
                "조회결과: 11,713건",
                "전체 조회건수: 11,713건",
                "화면 표시건수: 3,000건",
                "조회 완료: 29,716건",
                "조회 결과: 11,713건 (표시 3,000건)",
            )
            preserved_lines = (
                "조회결과: 조회 결과가 없습니다.",
                "조회 결과: 조건에 맞는 자료가 없습니다.",
                "조회 완료: 일부 자료를 불러오지 못했습니다.",
                "오류가 발생했습니다.",
                "권한이 없습니다.",
                "조건을 확인해 주세요.",
                "일부 데이터만 조회됐다는 경고",
                "KPI 수치: 123건",
            )
            if any(line in cleaned for line in removed_lines):
                mismatches.append(f"cleaned summary kept duplicate row-count line={cleaned!r}")
            if any(line not in cleaned for line in preserved_lines):
                mismatches.append(f"cleaned summary removed business notice={cleaned!r}")
            if "조회조건:" in inbound_cleaned or "조회결과:" in inbound_cleaned:
                mismatches.append(f"inbound duplicate condition/row-count kept={inbound_cleaned!r}")
            if "조회조건:" in inbound_cleaned_from_render_path or "조회결과:" in inbound_cleaned_from_render_path:
                mismatches.append(f"inbound render-path duplicate condition/row-count kept={inbound_cleaned_from_render_path!r}")
            if inbound_summary_cond_text != "기간 2026-07-01 ~ 2026-07-18 / 재고위치 전체":
                mismatches.append(f"inbound render-path summary condition unexpected={inbound_summary_cond_text!r}")
            if "입고수량: 9,999개" not in inbound_cleaned or "업무 요약 본문" not in inbound_cleaned:
                mismatches.append(f"inbound business summary lost={inbound_cleaned!r}")
            if "입고수량: 9,999개" not in inbound_cleaned_from_render_path or "업무 요약 본문" not in inbound_cleaned_from_render_path:
                mismatches.append(f"inbound render-path business summary lost={inbound_cleaned_from_render_path!r}")
            if "제품정보:" not in product_flow_cleaned:
                mismatches.append(f"product flow product info removed={product_flow_cleaned!r}")
            for token in ("이월재고: 10개", "입고수량: 20개", "출고수량: 5개", "재고수량: 25개", "집계 요약 펼쳐보기"):
                if token not in product_flow_cleaned:
                    mismatches.append(f"product flow KPI lost token={token!r} cleaned={product_flow_cleaned!r}")
            if "조회조건:" in inventory_cleaned or "조회 결과:" in inventory_cleaned:
                mismatches.append(f"inventory duplicate condition/row-count kept={inventory_cleaned!r}")
            if "현재재고수량: 100개" not in inventory_cleaned or "경고: 일부 데이터만 조회됐다는 경고" not in inventory_cleaned:
                mismatches.append(f"inventory business/warning summary lost={inventory_cleaned!r}")
            if "제품정보:" in inventory_cleaned_drop_product or "현재재고수량: 100개" not in inventory_cleaned_drop_product:
                mismatches.append(f"inventory full-list product info cleanup failed={inventory_cleaned_drop_product!r}")
            if "제품정보:" not in inventory_cleaned_keep_product:
                mismatches.append(f"inventory explicit product info removed={inventory_cleaned_keep_product!r}")
            if not chat_mod._should_show_product_inventory_info(explicit_product_meta, full_inventory_df):
                mismatches.append("explicit product filter did not show product info")
            if chat_mod._should_show_product_inventory_info(maker_only_meta, full_inventory_df):
                mismatches.append("maker-only filter showed product info for multi-product inventory")
            if chat_mod._should_show_product_inventory_info(product_group_only_meta, full_inventory_df):
                mismatches.append("product-group-only filter showed product info for multi-product inventory")
            if chat_mod._should_show_product_inventory_info(product_name_only_meta, full_inventory_df):
                mismatches.append("product-name-only filter showed product info for multi-product inventory")
            if chat_mod._should_show_product_inventory_info(blank_product_code_meta, full_inventory_df):
                mismatches.append("blank product-code filter showed product info for multi-product inventory")
            if chat_mod._should_show_product_inventory_info({}, full_inventory_df):
                mismatches.append("multi-product inventory showed first-row product info")
            if not chat_mod._should_show_product_inventory_info({}, single_inventory_df):
                mismatches.append("single-product inventory did not show product info")
            fake_item_for_force = {"id": "msg-force"}
            fake_meta_for_force = {"table_key": "sims_force_table"}
            chat_mod._set_old_sims_table_force_rendered(fake_item_for_force, fake_meta_for_force, "force_uid", True)
            if not chat_mod._is_old_sims_table_force_rendered(fake_item_for_force, fake_meta_for_force, "force_uid"):
                mismatches.append("old table force render helper did not enable")
            chat_mod._set_old_sims_table_force_rendered(fake_item_for_force, fake_meta_for_force, "force_uid", False)
            if chat_mod._is_old_sims_table_force_rendered(fake_item_for_force, fake_meta_for_force, "force_uid"):
                mismatches.append("old table force render helper did not clear one table")
            old_user_keys = {
                key: session_state.get(key)
                for key in ("user", "current_user", "auth_user", "login_user", "__ssai_user", "ssai_user")
                if key in session_state
            }
            try:
                for key in ("user", "current_user", "auth_user", "login_user", "__ssai_user", "ssai_user"):
                    session_state.pop(key, None)
                session_state["user"] = {"user_type": "SALES"}
                if chat_mod._is_internal_admin_for_raw_meta():
                    mismatches.append("raw meta visible to non-internal user")
                session_state["user"] = {"user_type": "SSART_ADMIN"}
                if not chat_mod._is_internal_admin_for_raw_meta():
                    mismatches.append("raw meta hidden from internal admin")
            finally:
                for key in ("user", "current_user", "auth_user", "login_user", "__ssai_user", "ssai_user"):
                    session_state.pop(key, None)
                session_state.update(old_user_keys)
            if not all("llm_summary_md" in meta and (meta.get("table_key") or meta.get("source_key")) for meta in master_cases[:3]):
                mismatches.append("master metadata keys not preserved in fixture")
            if '"?? ?? ????"' in src or "master_summary_actions = {" in src:
                mismatches.append("broken master expander/action-list branch remains")
            if "and not _is_internal_master_summary(meta)" not in src:
                mismatches.append("debug meta expander is not guarded for internal master summaries")

            if mismatches:
                results.append(_fail("master llm summary hidden from screen", "; ".join(mismatches)))
            else:
                results.append(_ok("master llm summary hidden from screen", "users/goods/vendors/codes master summaries hidden from default screen; LLM metadata preserved; visible summaries unaffected"))
        except Exception as e:
            results.append(_fail("master llm summary hidden from screen", f"{type(e).__name__}: {e}"))

        try:
            chat_mw_src = Path("app/ui/chat_middleware.py").read_text(encoding="utf-8")
            checks = {
                "download_prepare_light": '"download_prepare"' in chat_mw_src and '"__ui_rerun_reason"] = "download_prepare"' in chat_mw_src,
                "download_prepare_table_key": "__sims_download_prepare_table_key" in chat_mw_src,
                "current_followup_compact": "def _render_current_followup_compact_header" in chat_mw_src,
                "current_followup_header_branch": "if is_current_followup:" in chat_mw_src and "_render_current_followup_compact_header" in chat_mw_src,
                "compact_header_helper": "def _build_sims_result_header_view" in chat_mw_src and "상세 조회정보" in chat_mw_src,
                "no_default_table_key_caption": 'st.caption(f"table_key:' not in chat_mw_src and "NLQ 질문:" not in chat_mw_src,
                "excel_cell_lazy": "SIMS_EXCEL_EAGER_MAX_CELLS" in chat_mw_src and "lazy_basis_cells" in chat_mw_src,
            }
            failed = [name for name, ok in checks.items() if not ok]
            if failed:
                results.append(_fail("current-table compact render policy", f"failed={failed}"))
            else:
                results.append(_ok("current-table compact render policy", "followup header compact; hides table_key/meta captions; download_prepare uses light history path; Excel lazy uses cell threshold"))
        except Exception as e:
            results.append(_fail("current-table compact render policy", f"{type(e).__name__}: {e}"))

        try:
            from app.ui import sims_table_display as display_mod

            quantity_kind = {
                "완료월평균출고수량": display_mod._numeric_display_kind("완료월평균출고수량"),
                "최근3개월평균수요수량": display_mod._numeric_display_kind("최근3개월평균수요수량"),
                "부족예상수량": display_mod._numeric_display_kind("부족예상수량"),
                "수요증감률": display_mod._numeric_display_kind("수요증감률"),
            }
            if quantity_kind != {
                "완료월평균출고수량": "int",
                "최근3개월평균수요수량": "int",
                "부족예상수량": "int",
                "수요증감률": "percent2",
            }:
                results.append(_fail("stock shortage quantity display format", f"unexpected={quantity_kind}"))
            else:
                results.append(_ok("stock shortage quantity display format", "shortage quantity columns render as integer; percent columns keep percent2"))
        except Exception as e:
            results.append(_fail("stock shortage quantity display format", f"{type(e).__name__}: {e}"))

        try:
            from app.ui import sims_table_display as display_mod

            src_df = pd.DataFrame(
                [
                    {"제품코드": "0001", "재고기준": "장부재고", "수요예상기준": "비교자료부족", "분석자료원": "월집계-장부재고(Rddbc220)", "현재고원천": "장부재고월집계(Rddbc220) 누계", "부족예상수량": 0, "flag": False},
                    {"제품코드": "0002", "재고기준": "장부재고", "수요예상기준": "최근3개월평균수요수량", "분석자료원": "월집계-장부재고(Rddbc220)", "현재고원천": "장부재고월집계(Rddbc220) 누계", "부족예상수량": 10, "flag": True},
                    {"제품코드": "0003", "재고기준": "None", "수요예상기준": "NULL", "분석자료원": None, "현재고원천": pd.NA, "부족예상수량": 0, "flag": False},
                ]
            )
            original_nulls = {
                "재고기준": int(src_df["재고기준"].isna().sum()),
                "수요예상기준": int(src_df["수요예상기준"].isna().sum()),
                "분석자료원": int(src_df["분석자료원"].isna().sum()),
                "현재고원천": int(src_df["현재고원천"].isna().sum()),
            }
            display_df = display_mod.normalize_display_df_for_streamlit(src_df)
            display_mod.log_sims_display_fields(src_df, display_df, action="품목별 재고부족현황", render_path="chat", mode="fast")
            ok_display = (
                original_nulls == {"재고기준": 0, "수요예상기준": 0, "분석자료원": 1, "현재고원천": 1}
                and display_df.loc[0, "재고기준"] == "장부재고"
                and display_df.loc[0, "수요예상기준"] == "비교자료부족"
                and display_df.loc[0, "분석자료원"] == "월집계-장부재고(Rddbc220)"
                and display_df.loc[0, "현재고원천"] == "장부재고월집계(Rddbc220) 누계"
                and display_df.loc[1, "수요예상기준"] == "최근3개월평균수요수량"
                and display_df.loc[1, "현재고원천"] == "장부재고월집계(Rddbc220) 누계"
                and display_df.loc[2, "재고기준"] == "None"
                and display_df.loc[2, "수요예상기준"] == "NULL"
                and display_df.loc[2, "분석자료원"] == ""
                and display_df.loc[2, "현재고원천"] == ""
                and display_df.loc[0, "부족예상수량"] == 0
                and bool(display_df.loc[0, "flag"]) is False
            )
            if ok_display:
                results.append(_ok("stock shortage display null cleanup", "display copy preserves business strings and blanks only actual nulls while numeric zero/False stay unchanged"))
            else:
                results.append(_fail("stock shortage display null cleanup", f"display={display_df.to_dict(orient='records')} source={src_df.to_dict(orient='records')}"))
        except Exception as e:
            results.append(_fail("stock shortage display null cleanup", f"{type(e).__name__}: {e}"))

        try:
            from app.ui import chat_middleware as chat_mod
            from app.ui import sims_table_display as display_mod

            text_cols = [
                "\uc7ac\uace0\uae30\uc900",
                "\uc7ac\uace0\uae30\uc900\ud310\uc815",
                "\ubd80\uc871\ub4f1\uae09",
                "\uc218\uc694\uc608\uc0c1\ub4f1\uae09",
                "\uc218\uc694\uc608\uc0c1\uae30\uc900",
                "\ubd84\uc11d\uc790\ub8cc\uc6d0",
                "\ud604\uc7ac\uace0\uc6d0\ucc9c",
            ]
            row0 = {
                "\uc7ac\uace0\uae30\uc900": "\uc7a5\ubd80\uc7ac\uace0",
                "\uc7ac\uace0\uae30\uc900\ud310\uc815": "\uc801\uc815",
                "\ubd80\uc871\ub4f1\uae09": "\uc815\uc0c1",
                "\uc218\uc694\uc608\uc0c1\ub4f1\uae09": "\ube44\uad50\uc790\ub8cc\ubd80\uc871",
                "\uc218\uc694\uc608\uc0c1\uae30\uc900": "\ucd5c\uadfc3\uac1c\uc6d4\ud3c9\uade0\uc218\uc694\uc218\ub7c9",
                "\ubd84\uc11d\uc790\ub8cc\uc6d0": "\uc6d4\uc9d1\uacc4-\uc7a5\ubd80\uc7ac\uace0(Rddbc220)",
                "\ud604\uc7ac\uace0\uc6d0\ucc9c": "\uc7a5\ubd80\uc7ac\uace0\uc6d4\uc9d1\uacc4(Rddbc220) \ub204\uacc4",
                "\ubd80\uc871\uc608\uc0c1\uc218\ub7c9": 0,
                "\ud3c9\uac00\uc6d4 \uc218\uc694\uc9c4\ucc99\ub960": 61.67,
                "flag": False,
            }
            row1 = dict(row0)
            row1.update({
                "\uc7ac\uace0\uae30\uc900": None,
                "\uc218\uc694\uc608\uc0c1\uae30\uc900": "None",
                "\ud604\uc7ac\uace0\uc6d0\ucc9c": pd.NA,
                "\ubd80\uc871\uc608\uc0c1\uc218\ub7c9": 10,
                "\ud3c9\uac00\uc6d4 \uc218\uc694\uc9c4\ucc99\ub960": 0,
            })
            source_df = pd.DataFrame([row0, row1])
            source_snapshot = source_df.copy(deep=True)

            def _common_renderer_view(frame: pd.DataFrame) -> pd.DataFrame:
                fast = chat_mod._chat_fast_display_df(frame)
                normalized = display_mod.normalize_display_df_for_streamlit(fast)
                view, _cfg, _width, _height = display_mod.build_sims_table_display_config(
                    normalized,
                    action_name="\ud488\ubaa9\ubcc4 \uc7ac\uace0\ubd80\uc871\ud604\ud669",
                    add_row_no=False,
                    row_no_name="\uc21c\ubc88",
                    enable_pinning=True,
                    max_pinned_cols=5,
                )
                return view

            route_views = {
                "chat_fast": _common_renderer_view(source_df),
                "chat_small": display_mod.build_sims_table_display_config(
                    display_mod.normalize_display_df_for_streamlit(source_df),
                    action_name="\ud488\ubaa9\ubcc4 \uc7ac\uace0\ubd80\uc871\ud604\ud669",
                    add_row_no=False,
                )[0],
                "panel_fast": _common_renderer_view(source_df),
                "panel_small": display_mod.build_sims_table_display_config(
                    display_mod.normalize_display_df_for_streamlit(source_df),
                    action_name="\ud488\ubaa9\ubcc4 \uc7ac\uace0\ubd80\uc871\ud604\ud669",
                    add_row_no=False,
                )[0],
                "history_reopen": _common_renderer_view(source_df),
                "current_followup": _common_renderer_view(source_df.loc[[0]].copy()),
            }

            route_failures: list[str] = []
            for route, view in route_views.items():
                for col in text_cols:
                    if col not in view.columns:
                        route_failures.append(f"{route}:{col}:missing")
                        continue
                    if not (pd.api.types.is_object_dtype(view[col]) or pd.api.types.is_string_dtype(view[col])):
                        route_failures.append(f"{route}:{col}:dtype={view[col].dtype}")
                    expected_non_null = int(source_df.loc[view.index, col].notna().sum()) if len(view.index) else 0
                    actual_non_null = int(view[col].replace("", pd.NA).notna().sum())
                    if actual_non_null != expected_non_null:
                        route_failures.append(f"{route}:{col}:non_null={actual_non_null}/{expected_non_null}")
                    if col in row0 and view.iloc[0][col] != row0[col]:
                        route_failures.append(f"{route}:{col}:value={view.iloc[0][col]!r}")
                if "current_followup" != route:
                    if view.loc[1, "\uc7ac\uace0\uae30\uc900"] != "":
                        route_failures.append(f"{route}:actual_none_not_blank")
                    if view.loc[1, "\uc218\uc694\uc608\uc0c1\uae30\uc900"] != "None":
                        route_failures.append(f"{route}:literal_none_not_preserved")
                    if view.loc[0, "\ubd80\uc871\uc608\uc0c1\uc218\ub7c9"] != 0:
                        route_failures.append(f"{route}:numeric_zero_changed")
                    if bool(view.loc[0, "flag"]) is not False:
                        route_failures.append(f"{route}:false_changed")

            source_unchanged = source_df.equals(source_snapshot)
            numeric_preserved = all(pd.api.types.is_numeric_dtype(v["\ubd80\uc871\uc608\uc0c1\uc218\ub7c9"]) for v in route_views.values())
            if route_failures or not source_unchanged or not numeric_preserved:
                results.append(_fail("stock shortage renderer text field preservation", f"failures={route_failures} source_unchanged={source_unchanged} numeric_preserved={numeric_preserved}"))
            else:
                results.append(_ok("stock shortage renderer text field preservation", "chat fast/small, panel fast/small, history reopen, and current-table followup preserve stock shortage business text fields while numeric columns stay numeric"))
        except Exception as e:
            results.append(_fail("stock shortage renderer text field preservation", f"{type(e).__name__}: {e}"))

        try:
            from app.ui import chat_middleware as chat_mod
            from app.ui import sims_table_display as display_mod

            full_df = pd.DataFrame(
                [
                    {"제품코드": "0001", "재고기준": "장부재고", "수요예상기준": "비교자료부족", "분석자료원": "월집계-장부재고(Rddbc220)", "현재고원천": "장부재고월집계(Rddbc220) 누계", "부족예상수량": 0},
                    {"제품코드": "0002", "재고기준": "장부재고", "수요예상기준": "최근3개월평균수요수량", "분석자료원": "월집계-장부재고(Rddbc220)", "현재고원천": "장부재고월집계(Rddbc220) 누계", "부족예상수량": 10},
                    {"제품코드": "0003", "재고기준": None, "수요예상기준": None, "분석자료원": None, "현재고원천": None, "부족예상수량": 0},
                ],
                index=[10, 20, 30],
            )
            display_df = full_df[["제품코드", "재고기준", "수요예상기준", "분석자료원", "현재고원천", "부족예상수량"]].copy()
            display_df.loc[10, ["재고기준", "수요예상기준", "분석자료원", "현재고원천"]] = [None, None, "None", pd.NA]
            display_df.loc[20, ["재고기준", "수요예상기준", "분석자료원", "현재고원천"]] = ["None", "NULL", None, "nan"]

            restored = chat_mod._restore_display_fields_from_full_df(
                display_df,
                full_df,
                action="품목별 재고부족현황",
                stage="regression",
            )
            fast_view = display_mod.normalize_display_df_for_streamlit(restored)
            small_view = display_mod.normalize_display_df_for_streamlit(restored.copy())
            filtered_view = display_mod.normalize_display_df_for_streamlit(restored.loc[[20]].copy())
            unchanged_full = (
                full_df.loc[10, "수요예상기준"] == "비교자료부족"
                and full_df.loc[20, "수요예상기준"] == "최근3개월평균수요수량"
            )
            ok_restore = (
                fast_view.loc[10, "재고기준"] == "장부재고"
                and fast_view.loc[10, "수요예상기준"] == "비교자료부족"
                and fast_view.loc[10, "분석자료원"] == "월집계-장부재고(Rddbc220)"
                and fast_view.loc[10, "현재고원천"] == "장부재고월집계(Rddbc220) 누계"
                and small_view.loc[20, "수요예상기준"] == "최근3개월평균수요수량"
                and filtered_view.loc[20, "분석자료원"] == "월집계-장부재고(Rddbc220)"
                and filtered_view.loc[20, "현재고원천"] == "장부재고월집계(Rddbc220) 누계"
                and fast_view.loc[30, "재고기준"] == ""
                and fast_view.loc[30, "수요예상기준"] == ""
                and fast_view.loc[10, "부족예상수량"] == 0
                and unchanged_full
            )
            if ok_restore:
                results.append(_ok("stock shortage display field restore", "full/display/fast/small/filter paths preserve Excel business values by original index"))
            else:
                results.append(_fail("stock shortage display field restore", f"fast={fast_view.to_dict(orient='records')} full={full_df.to_dict(orient='records')}"))
        except Exception as e:
            results.append(_fail("stock shortage display field restore", f"{type(e).__name__}: {e}"))

        try:
            import io
            import json
            from openpyxl import load_workbook

            from app.services import product_flow_service as product_flow_mod
            from app.sims.views import rddbc_io_shared as io_shared_mod
            from app.ui import chat_middleware as chat_mod
            from app.ui import sims_table_display as display_mod

            seq_col = "\uba85\uc138\uc11c\ubc88\ud638"
            product_no_col = "\uc81c\uc870\ubc88\ud638"
            validation_col = "\uac80\uc218\ud655\uc778"
            in_qty_col = "\uc785\uace0\uc218\ub7c9"
            out_qty_col = "\ucd9c\uace0\uc218\ub7c9"
            supply_col = "\uacf5\uae09\uac00\uc561"
            total_col = "\ud569\uacc4\uae08\uc561"
            product_code_col = "\uc81c\ud488\ucf54\ub4dc"
            biz_no_col = "\uc0ac\uc5c5\uc790\ubc88\ud638"
            phone_col = "\uc804\ud654\ubc88\ud638"
            zip_col = "\uc6b0\ud3b8\ubc88\ud638"

            source_df = pd.DataFrame(
                {
                    seq_col: [None, 268, 628, 893, None, None, None],
                    product_no_col: ["114625021", "000123", "001-A", "", None, pd.NA, "NULL"],
                    validation_col: ["1", "0", "Y", "", float("nan"), "<NA>", "nan"],
                    in_qty_col: [0, "10", "20", "", "30", "40", "50"],
                    out_qty_col: [0, "1", "2", "", "3", "4", "5"],
                    supply_col: [0, "1000", "2000", "", "3000", "4000", "5000"],
                    total_col: [0, "1100", "2200", "", "3300", "4400", "5500"],
                    product_code_col: ["00001", "00002", "00003", "00004", "00005", "00006", "00007"],
                    biz_no_col: ["012-34-56789", "1112233333", "", "999-88-77777", "0000011111", "2222233333", "4444455555"],
                    phone_col: ["02-1234-5678", "01000000000", "", "031-123-4567", "01011112222", "01033334444", "01055556666"],
                    zip_col: ["01234", "12345", "", "67890", "00001", "00002", "00003"],
                }
            )

            finalized = product_flow_mod._finalize_display_df_250(source_df)
            prepared = io_shared_mod._prepare_io_display_df(finalized, add_row_no=False)
            view_df, column_config, _table_width, _table_height = display_mod.build_sims_table_display_config(
                prepared.copy(),
                action_name="\uc81c\ud488\uc218\ubd88\ud604\ud669 \uc870\ud68c",
                add_row_no=False,
            )

            failures: list[str] = []

            if str(finalized[seq_col].dtype) != "Int64":
                failures.append(f"service_seq_dtype={finalized[seq_col].dtype}")
            if finalized[seq_col].isna().iloc[0] is not True and not pd.isna(finalized[seq_col].iloc[0]):
                failures.append(f"service_seq_blank={finalized[seq_col].iloc[0]!r}")
            if finalized[seq_col].dropna().astype(int).tolist() != [268, 628, 893]:
                failures.append(f"service_seq_values={finalized[seq_col].tolist()!r}")

            for col, expected in [
                (product_no_col, ["114625021", "000123", "001-A", "", "", "", "NULL"]),
                (validation_col, ["1", "0", "Y", "", "", "<NA>", "nan"]),
            ]:
                if not (pd.api.types.is_object_dtype(finalized[col]) or pd.api.types.is_string_dtype(finalized[col])):
                    failures.append(f"service_{col}_dtype={finalized[col].dtype}")
                if finalized[col].tolist() != expected:
                    failures.append(f"service_{col}_values={finalized[col].tolist()!r}")

            if str(prepared[seq_col].dtype) != "Int64":
                failures.append(f"display_seq_dtype={prepared[seq_col].dtype}")
            for col, expected in [
                (product_no_col, ["114625021", "000123", "001-A", "", "", "", "NULL"]),
                (validation_col, ["1", "0", "Y", "", "", "<NA>", "nan"]),
                (product_code_col, ["00001", "00002", "00003", "00004", "00005", "00006", "00007"]),
                (biz_no_col, ["012-34-56789", "1112233333", "", "999-88-77777", "0000011111", "2222233333", "4444455555"]),
                (phone_col, ["02-1234-5678", "01000000000", "", "031-123-4567", "01011112222", "01033334444", "01055556666"]),
                (zip_col, ["01234", "12345", "", "67890", "00001", "00002", "00003"]),
            ]:
                if prepared[col].tolist() != expected:
                    failures.append(f"display_{col}_values={prepared[col].tolist()!r}")
                if not (pd.api.types.is_object_dtype(prepared[col]) or pd.api.types.is_string_dtype(prepared[col])):
                    failures.append(f"display_{col}_dtype={prepared[col].dtype}")

            for col in [in_qty_col, out_qty_col, supply_col, total_col]:
                if not pd.api.types.is_numeric_dtype(prepared[col]):
                    failures.append(f"numeric_{col}_dtype={prepared[col].dtype}")

            if not display_mod._is_numeric_display_col(view_df, seq_col):
                failures.append("seq_not_number_column")
            for col in [product_no_col, validation_col, product_code_col, biz_no_col, phone_col, zip_col]:
                if display_mod._is_numeric_display_col(view_df, col):
                    failures.append(f"{col}_unexpected_number_column")
            type_expectations = {
                seq_col: ("number", 1),
                product_no_col: ("text", None),
                validation_col: ("text", None),
                product_code_col: ("text", None),
                biz_no_col: ("text", None),
                phone_col: ("text", None),
                zip_col: ("text", None),
            }
            for col, (expected_type, expected_step) in type_expectations.items():
                cfg = column_config.get(col)
                actual_type = (cfg or {}).get("type_config", {}).get("type")
                actual_step = (cfg or {}).get("type_config", {}).get("step")
                if actual_type != expected_type:
                    failures.append(f"column_config_{col}_type={actual_type}")
                if expected_step is not None and actual_step != expected_step:
                    failures.append(f"column_config_{col}_step={actual_step}")

            json.dumps(prepared.to_dict(orient="records"), ensure_ascii=False)

            bio = io.BytesIO()
            excel_df = chat_mod._sanitize_dataframe_for_excel(finalized)
            excel_df.to_excel(bio, index=False, sheet_name="SIMS")
            bio.seek(0)
            wb = load_workbook(bio, read_only=True, data_only=True)
            ws = wb["SIMS"]
            header = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]

            def _cell(row: int, col_name: str):
                return ws.cell(row=row, column=header.index(col_name) + 1)

            if _cell(2, seq_col).value is not None:
                failures.append(f"excel_seq_blank={_cell(2, seq_col).value!r}")
            for row in [3, 4, 5]:
                if _cell(row, seq_col).data_type != "n":
                    failures.append(f"excel_seq_type_row{row}={_cell(row, seq_col).data_type}")
            for row in [2, 3, 4]:
                if _cell(row, product_no_col).data_type not in {"s", "inlineStr"}:
                    failures.append(f"excel_product_no_type_row{row}={_cell(row, product_no_col).data_type}")
                if _cell(row, validation_col).data_type not in {"s", "inlineStr"}:
                    failures.append(f"excel_validation_type_row{row}={_cell(row, validation_col).data_type}")
            if _cell(3, product_no_col).value != "000123":
                failures.append(f"excel_leading_zero={_cell(3, product_no_col).value!r}")
            if _cell(8, product_no_col).value != "NULL":
                failures.append(f"excel_literal_NULL={_cell(8, product_no_col).value!r}")
            if _cell(7, validation_col).value != "<NA>":
                failures.append(f"excel_literal_pdna={_cell(7, validation_col).value!r}")
            if _cell(8, validation_col).value != "nan":
                failures.append(f"excel_literal_nan={_cell(8, validation_col).value!r}")
            wb.close()

            if failures:
                results.append(_fail("product flow column type policy", "; ".join(failures)))
            else:
                results.append(_ok("product flow column type policy", "statement seq is nullable integer/numeric Excel cell; product no and validation remain text in service/display/Excel; numeric and identifier columns preserved"))
        except Exception as e:
            results.append(_fail("product flow column type policy", f"{type(e).__name__}: {e}"))

        try:
            main_src = Path("app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
            no_source_checks = {
                "simple_notice": "현재표가 없습니다. 먼저 SIMS 조회를 실행한 뒤 다시 질문해 주세요." in main_src,
                "source_probe": "def _has_current_table_source_df" in main_src,
                "immediate_notice": "no current source; notice pushed immediately" in main_src,
                "no_deferred": "st.session_state.pop(\"__deferred_current_table_followup\", None)" in main_src,
                "title_limit": "def _room_title_text_from_message(content: str, *, limit: int = 20)" in main_src,
            }
            failed = [name for name, ok in no_source_checks.items() if not ok]
            if failed:
                results.append(_fail("current-table missing source immediate notice", f"failed={failed}"))
            else:
                results.append(_ok("current-table missing source immediate notice", "no-source current-table questions push one notice immediately and do not defer to panel"))
        except Exception as e:
            results.append(_fail("current-table missing source immediate notice", f"{type(e).__name__}: {e}"))

        try:
            small_df = pd.DataFrame({"배정부족예상금액": range(20), "부족제품수": range(20)})
            eight_df = pd.DataFrame({"배정부족예상금액": range(8), "부족제품수": range(8)})
            mid_259_df = pd.DataFrame({
                "배정부족예상금액": range(259),
                "부족제품수": range(259),
                "매입처명": [f"B{i}" for i in range(259)],
            })
            mid_470_df = pd.DataFrame({
                "배정부족예상금액": range(470),
                "음수재고제품수": range(470),
                "매입처명": [f"B{i}" for i in range(470)],
            })
            group_df = pd.DataFrame({"주요배분기준": ["A", "B", "C", "D"], "배정부족예상금액": [1, 2, 3, 4]})
            meta_followup = {"current_table_followup": True}
            checks = {
                "20": chat_mod._chat_is_current_followup_fast_table(small_df, meta_followup),
                "8": chat_mod._chat_is_current_followup_fast_table(eight_df, meta_followup),
                "259": chat_mod._chat_is_current_followup_fast_table(mid_259_df, meta_followup),
                "470": chat_mod._chat_is_current_followup_fast_table(mid_470_df, meta_followup),
                "4": chat_mod._chat_is_current_followup_fast_table(group_df, meta_followup),
            }
            if checks != {"20": False, "8": False, "259": True, "470": True, "4": False}:
                results.append(_fail("supplier current-table fast render policy", f"unexpected modes={checks}"))
            else:
                results.append(_ok("supplier current-table fast render policy", "20/8/4 small; 259/470 fast"))
        except Exception as e:
            results.append(_fail("supplier current-table fast render policy", f"{type(e).__name__}: {e}"))

        try:
            import streamlit as st

            old_reason = st.session_state.get("__ui_rerun_reason")
            old_reason_current = st.session_state.get("__ui_rerun_reason_current")
            old_path = st.session_state.get("__sims_table_render_path")
            old_last_key = st.session_state.get("__sims_last_table_key")
            old_latest_followup = st.session_state.get("__sims_latest_followup_table_key")
            try:
                st.session_state["__sims_table_render_path"] = "history"
                st.session_state["__sims_last_table_key"] = "new-table"
                st.session_state["__sims_latest_followup_table_key"] = "new-table"
                old_item = {"type": "table", "action": "매입처별 재고부족 현황", "table_key": "old-table"}
                old_meta = {"table_key": "old-table", "row_count": 975}
                new_item = {"type": "table", "action": "현재표 배정부족예상금액 TOP 20", "table_key": "new-table"}
                new_meta = {"table_key": "new-table", "row_count": 20}

                st.session_state["__ui_rerun_reason_current"] = "sims_panel_open"
                panel_open_old = chat_mod._should_full_render_sims_table(old_item, old_meta, "old")
                panel_open_new = chat_mod._should_full_render_sims_table(new_item, new_meta, "new")
                st.session_state["__ui_rerun_reason_current"] = "chat_room_change"
                room_change_old = chat_mod._should_full_render_sims_table(old_item, old_meta, "old")
                st.session_state["__ui_rerun_reason"] = "sims_action_change"
                st.session_state["__ui_rerun_reason_current"] = "current_table_followup"
                st.session_state[chat_mod._old_sims_table_force_key(old_item, old_meta, "old")] = True
                followup_old = chat_mod._should_full_render_sims_table(old_item, old_meta, "old")
                followup_new = chat_mod._should_full_render_sims_table(new_item, new_meta, "new")

                if panel_open_old or panel_open_new or room_change_old:
                    results.append(_fail("supplier history lightweight rerun policy", f"light rerun should skip history tables panel_old={panel_open_old} panel_new={panel_open_new} room_old={room_change_old}"))
                elif followup_old or not followup_new:
                    results.append(_fail("supplier history lightweight rerun policy", f"followup should skip old/render latest old={followup_old} new={followup_new}"))
                elif chat_mod._ui_rerun_reason() != "current_table_followup":
                    results.append(_fail("supplier history lightweight rerun policy", f"stale reason priority failed got={chat_mod._ui_rerun_reason()}"))
                else:
                    results.append(_ok("supplier history lightweight rerun policy", "panel/action rerun skips old tables; followup renders latest only; stale action reason ignored"))
            finally:
                st.session_state.pop(chat_mod._old_sims_table_force_key(old_item, old_meta, "old"), None)
                if old_reason is None:
                    st.session_state.pop("__ui_rerun_reason", None)
                else:
                    st.session_state["__ui_rerun_reason"] = old_reason
                if old_reason_current is None:
                    st.session_state.pop("__ui_rerun_reason_current", None)
                else:
                    st.session_state["__ui_rerun_reason_current"] = old_reason_current
                if old_path is None:
                    st.session_state.pop("__sims_table_render_path", None)
                else:
                    st.session_state["__sims_table_render_path"] = old_path
                if old_last_key is None:
                    st.session_state.pop("__sims_last_table_key", None)
                else:
                    st.session_state["__sims_last_table_key"] = old_last_key
                if old_latest_followup is None:
                    st.session_state.pop("__sims_latest_followup_table_key", None)
                else:
                    st.session_state["__sims_latest_followup_table_key"] = old_latest_followup
        except Exception as e:
            results.append(_fail("supplier history lightweight rerun policy", f"{type(e).__name__}: {e}"))

        try:
            main_src = Path("app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
            required_close_keys = [
                '"__sims_open"',
                '"__sims_open_ui"',
                '"__sims_panel_active"',
                '"__sims_force_open"',
                '"__sims_run_flag"',
                '"__sims_inner_submit"',
                '"__sims_selected_snapshot"',
            ]
            has_close_helper = "def _close_sims_panel_for_room_change" in main_src
            has_close_guard = '"__sims_close_for_chat_room_change"' in main_src
            has_guard_consume = "def _consume_sims_close_for_chat_room_change" in main_src
            has_panel_close_log = "[chat.room.panel_close]" in main_src
            has_after_success_log = '_log_sims_panel_room_close_state("after success=True")' in main_src
            has_no_open_assignment = '"__sims_open",\n            "__sims_open_ui"' not in main_src and 'ss["__sims_open"] = False' not in main_src
            room_switch_block = main_src[main_src.find("if picked and picked != ss.current_room:"):main_src.find("cur_name = id_to_name.get", main_src.find("if picked and picked != ss.current_room:"))]
            has_no_direct_close_in_room_switch = "_close_sims_panel_for_room_change()" not in room_switch_block
            has_render_block = 'if st.session_state.get("__ui_rerun_reason_current") == "chat_room_change":' in main_src and "should_render = False" in main_src
            has_room_reason = '"chat_room_change"' in main_src
            has_switch_total = 'switch_total = float(stats.get("event_to_main_elapsed") or 0.0) + float(stats.get("history_elapsed") or 0.0)' in main_src
            has_switch_event_id = '"__chat_room_switch_event_id"' in main_src and "event_id=%s" in room_switch_block
            has_event_id_perf = '"__ui_event_id"' in main_src and "[ui.event_to_rerun] event_id=%s" in main_src
            has_startup_pending = '"__auth_login_perf_pending"' in main_src and '"__auth_startup_perf_emitted_sig"' in main_src
            has_no_unconditional_startup_log = "[auth.startup.perf] company_select=" not in main_src
            has_save_detail = '"__chat_room_switch_save_detail"' in main_src and "json_serialize=%.3fs" in main_src
            has_sims_open_perf = "[sims.panel_open.perf]" in main_src and '"__sims_panel_open_fragment_elapsed"' in main_src
            has_authenticate_perf = "__auth_login_authenticate_elapsed" in main_src and "authenticate=%.3fs" in main_src
            has_script_path_perf = (
                "[ui.script_path.perf]" in main_src
                and "room_selector=%.3fs" in main_src
                and "sims_fragment=%.3fs" in main_src
                and "unattributed=%.3fs" in main_src
                and 'st.session_state["__ui_script_perf_durations"] = {}' in main_src
            )
            has_save_skip = (
                "[chat.save.skip]" in main_src
                and "reason=unchanged" in main_src
                and "unchanged_or_selection_only" in main_src
                and "compare_mode=%s" in main_src
                and "_record_chat_save_skip" in main_src
            )
            has_selection_only_save_skip = (
                "removed_empty_pending = _drop_empty_auto_rooms(keep_room_id=picked)" in room_switch_block
                and "dirty_reason=\"selection_only\"" in room_switch_block
                and "save_chat_rooms()" in room_switch_block
            )
            has_chat_save_diff = "[chat.save.diff]" in main_src and "changed_fields=%s" in main_src
            has_latest_message_anchor = (
                '"__chat_scroll_to_bottom_once"' not in main_src
                and '"__chat_scroll_event_id"' not in main_src
                and "[chat.room.autoscroll]" not in main_src
                and "setTimeout(run" not in main_src
                and "focus({ preventScroll: true })" not in main_src
                and "ssai-chat-bottom-anchor" in main_src
                and "ssai-latest-message-link" in main_src
                and 'href="#ssai-chat-bottom-anchor"' in main_src
                and '[data-testid="stChatInput"]' in main_src
                and "position: sticky" in main_src
            )
            has_chronological_render_merge = (
                "def _build_room_render_messages" in main_src
                and "for channel in (\"messages\", \"history\", \"sims_messages\", \"gen_messages\")" in main_src
                and "merged_msgs = _build_room_render_messages(current_room)" in main_src
                and "def _message_time_key" in main_src
                and "def _message_dedupe_key" in main_src
                and "content_sig" in main_src
            )
            has_stale_event_clear = (
                'current_ui_event_id = str(st.session_state.pop("__ui_event_id", "") or "").strip()' in main_src
                and 'if current_ui_rerun_reason != "chat_room_change":' in main_src
                and 'st.session_state.pop("__chat_room_switch_event_id", None)' in main_src
            )
            has_partitioned_storage = (
                "def _partitioned_chat_root" in main_src
                and "def _load_partitioned_chat_rooms" in main_src
                and "def _save_partitioned_chat_rooms" in main_src
                and "def _try_migrate_legacy_to_partitioned" in main_src
                and "def _ensure_partitioned_room_loaded" in main_src
                and "def _load_partitioned_room_messages" in main_src
                and '"messages.jsonl"' in main_src
                and '"rooms.json"' in main_src
                and "[chat.storage.migration]" in main_src
                and "[chat.storage.save]" in main_src
                and "[chat.storage.load]" in main_src
                and "_partition_message_key" in main_src
                and "_partition_message_payload" in main_src
                and "append_count" in main_src
                and "skipped_bad_lines" in main_src
                and "fallback=True" in main_src
                and "legacy_chat_file" in main_src
                and "storage_mode=partitioned" in main_src
                and '"__messages_loaded"' in main_src
                and "_ensure_partitioned_room_loaded(picked)" in main_src
            )
            has_partitioned_allowlist = (
                "_CHAT_PARTITION_MESSAGE_ALLOW_KEYS" in main_src
                and "_CHAT_PARTITION_META_ALLOW_KEYS" in main_src
                and "def _partition_record_line" in main_src
                and "[chat.storage.record_guard]" in main_src
                and '"channels"' in main_src
                and "def _partition_collect_records" in main_src
                and "_partition_logical_message_key" in main_src
            )
            has_room_index = (
                "def _write_rooms_index_csv" in main_src
                and '"rooms_index.csv"' in main_src
                and '"utf-8-sig"' in main_src
                and "messages_file_bytes" in main_src
            )
            has_compact_room_context = (
                "def _build_current_room_compact_context" in main_src
                and "[CURRENT_ROOM_COMPACT_CONTEXT]" in main_src
                and "[chat.room.compact_context]" in main_src
                and "Do not invent hidden table rows" in main_src
            )
            has_partitioned_perf_detail = (
                '"render_list"' in main_src
                and '"chat_context"' in main_src
                and '"session_compact"' in main_src
                and "render_list=%.3fs" in main_src
                and "chat_context=%.3fs" in main_src
                and "session_compact=%.3fs" in main_src
                and "[chat.storage.append_record]" in main_src
            )
            has_compact_common_drop_keys = (
                "CHAT_PERSISTENCE_DROP_KEYS = {" in main_src
                and "supplier_detail_df" in main_src
                and "product_shortage_df" in main_src
                and "for k in DROP_KEYS" not in main_src
                and " in DROP_KEYS" not in main_src
                and "CHAT_PERSISTENCE_DROP_KEYS" in main_src
                and "has_large_nested = any(k in msg for k in CHAT_PERSISTENCE_DROP_KEYS)" in main_src
            )
            has_room_projection = (
                "def _room_persistence_projection" in main_src
                and "_room_persistence_projection(cur_room)" in main_src
                and "json.dumps(cur_room, ensure_ascii=False, indent=2).encode" not in main_src
                and "str(k) not in CHAT_PERSISTENCE_DROP_KEYS" in main_src
            )
            has_pending_login_policy = (
                'selected_room_id = str(st.session_state.get("current_room") or "").strip()' in main_src
                and 'selected_room_id = ""' in main_src
                and "meta_obj.get(\"current_room\")" not in main_src
                and "_select_pending_new_room()" in main_src
                and "new pending room selected" in main_src
                and "was_pending = room.get(\"auto_created\") is True and room.get(\"title_initialized\") is not True" in main_src
                and "room[\"title_initialized\"] = True" in main_src
            )
            has_readable_folder_policy = (
                "def _readable_room_dirname" in main_src
                and "def _split_room_name_datetime_prefix" in main_src
                and "def _ensure_room_relative_path" in main_src
                and "relative_path" in main_src
                and "_partitioned_messages_file_for_room(root, room)" in main_src
                and "_partitioned_room_dir_for_room(root, room)" in main_src
            )
            chat_mw_src = Path("app/ui/chat_middleware.py").read_text(encoding="utf-8")
            has_sims_df_detection = (
                'elif isinstance(payload.get("data"), pd.DataFrame):' in chat_mw_src
                and 'pd.DataFrame.from_records(payload["records"])' in chat_mw_src
            )
            compact_runtime_ok = False
            compact_runtime_detail = ""
            projection_runtime_ok = False
            projection_runtime_detail = ""
            readable_tool_runtime_ok = False
            readable_tool_runtime_detail = ""
            try:
                start = main_src.index("_CHAT_PARTITION_MESSAGE_ALLOW_KEYS = {")
                end = main_src.index("\ndef _partition_seen_index", start)
                compact_src = main_src[start:end]

                class _FakeState(dict):
                    pass

                class _FakeSt:
                    session_state = _FakeState({"__chat_storage_mode": "partitioned"})

                class _FakeLog:
                    def warning(self, *args, **kwargs):
                        return None

                    def exception(self, *args, **kwargs):
                        return None

                ns = {
                    "os": __import__("os"),
                    "time": __import__("time"),
                    "json": __import__("json"),
                    "hashlib": __import__("hashlib"),
                    "Any": object,
                    "_CHAT_PARTITION_CHANNELS": ("messages", "history", "sims_messages", "gen_messages"),
                    "st": _FakeSt,
                    "log": _FakeLog(),
                    "_json_sanitize": lambda obj: obj,
                    "_script_perf_add": lambda name, elapsed: None,
                    "_partition_message_key": lambda msg: "id:" + str((msg or {}).get("id") or (msg or {}).get("table_key") or id(msg)),
                }
                exec(compact_src, ns)
                ns["_room_meta_only"] = lambda room: {
                    "id": str((room or {}).get("id") or ""),
                    "name": str((room or {}).get("name") or ""),
                    "message_count": len(ns["_partition_collect_records"](room)),
                }
                room = {
                    "id": "fixture-room",
                    "name": "fixture",
                    "messages": [
                        {"role": "user", "content": "small", "meta": {"action": "일반"}},
                        {
                            "role": "assistant",
                            "type": "table",
                            "action": "매입처별 재고부족 현황",
                            "table_key": "tbl-1",
                            "meta": {
                                "action": "매입처별 재고부족 현황",
                                "summary_md": "요약",
                                "table_key": "tbl-1",
                                "supplier_detail_df": {"records": [{"a": 1}]},
                                "product_shortage_df": {"records": [{"b": 2}]},
                            },
                        },
                        {
                            "role": "assistant",
                            "type": "table",
                            "action": "SIMS DF",
                            "table_key": "tbl-df",
                            "df": pd.DataFrame({"제품코드": ["001"], "배정부족예상금액": [10]}),
                            "df_display": pd.DataFrame({"제품코드": ["001"], "배정부족예상금액": [10]}),
                            "data": pd.DataFrame({"제품코드": ["001"], "배정부족예상금액": [10]}),
                            "records": [{"제품코드": "001", "배정부족예상금액": 10}],
                            "columns": ["제품코드", "배정부족예상금액"],
                            "meta": {
                                "action": "SIMS DF",
                                "summary_md": "summary",
                                "table_key": "tbl-df",
                                "supplier_detail_df": {"records": [{"a": 1}]},
                            },
                        },
                    ],
                    "history": [],
                    "sims_messages": [],
                    "gen_messages": [],
                }
                stats = ns["_compact_partition_room_in_memory"](room)
                meta = room["messages"][1].get("meta") or {}
                runtime_df_msg = room["messages"][2]
                projection = ns["_room_persistence_projection"](room)
                projection_json = json.dumps(projection, ensure_ascii=False)
                projected_msg = next(
                    (
                        m.get("message")
                        for m in projection.get("messages", [])
                        if (m.get("message") or {}).get("table_key") == "tbl-df"
                    ),
                    {},
                )
                compact_runtime_ok = (
                    stats.get("changed") == 1
                    and "supplier_detail_df" not in meta
                    and "product_shortage_df" not in meta
                    and meta.get("action") == "매입처별 재고부족 현황"
                    and meta.get("summary_md") == "요약"
                    and meta.get("table_key") == "tbl-1"
                )
                compact_runtime_detail = f"stats={stats} meta_keys={sorted(meta.keys())}"
                projection_runtime_ok = (
                    isinstance(runtime_df_msg.get("df"), pd.DataFrame)
                    and isinstance(runtime_df_msg.get("df_display"), pd.DataFrame)
                    and isinstance(runtime_df_msg.get("data"), pd.DataFrame)
                    and "supplier_detail_df" not in projection_json
                    and '"df"' not in projection_json
                    and '"df_display"' not in projection_json
                    and '"data"' not in projection_json
                    and '"records"' not in projection_json
                    and isinstance(projected_msg, dict)
                    and (projected_msg.get("meta") or {}).get("table_key") == "tbl-df"
                    and (projected_msg.get("meta") or {}).get("summary_md") == "summary"
                )
                projection_runtime_detail = f"projection_messages={len(projection.get('messages') or [])} bytes={len(projection_json.encode('utf-8'))}"
            except Exception as e:
                compact_runtime_detail = f"{type(e).__name__}: {e}"
                projection_runtime_detail = f"{type(e).__name__}: {e}"
            remigrate_src = Path("tools/remigrate_partitioned_chat.py").read_text(encoding="utf-8")
            has_remigrate_tool = (
                "def run(args: argparse.Namespace)" in remigrate_src
                and "parser.add_argument(\"--apply\"" in remigrate_src
                and "dry-run" in remigrate_src
                and "PARTITION_NEW_MESSAGES_AFTER_DEDUPE" in remigrate_src
                and "REMOVED_LARGE_KEY_PATH_COUNTS" in remigrate_src
                and "ROLLBACK_COMMAND" in remigrate_src
                and "messages.jsonl" in remigrate_src
                and "rooms_index.csv" in remigrate_src
                and "def messages_file_from_meta" in remigrate_src
                and "relative_path" in remigrate_src
                and "legacy file not found" in remigrate_src
                and "os.replace(str(partition_root), str(backup_root))" in remigrate_src
                and "os.replace(str(tmp_root), str(partition_root))" in remigrate_src
                and "logical_key(" in remigrate_src
                and "compact_message(" in remigrate_src
            )
            try:
                rename_path = Path("tools/rename_partitioned_room_folders.py")
                spec = importlib.util.spec_from_file_location("rename_partitioned_room_folders", rename_path)
                rename_mod = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(rename_mod)
                with tempfile.TemporaryDirectory() as td:
                    chat_root = Path(td)
                    root = chat_root / "user_8"
                    rooms_dir = root / "rooms"
                    rooms_dir.mkdir(parents=True)
                    rooms = [
                        {
                            "id": "5266b472-4869-4b52-8a41-afe7d2f79a75",
                            "name": "2026-07-16 13:08 오늘 이 채팅방에서 정리한 내용을 간단히 정리 해줘",
                            "created_at": "2026-07-14T10:19:00",
                            "updated_at": "2026-07-14T10:20:00",
                            "message_count": 2,
                        },
                        {
                            "id": "cdc0d332-0000-0000-0000-000000000000",
                            "name": "2026-07-16 13:06 오늘 이 채팅방에서 정리한 내용을 간단히 정리 해줘",
                            "created_at": "2026-07-12T15:43:00",
                            "updated_at": "2026-07-12T15:44:00",
                            "message_count": 2,
                        },
                        {
                            "id": "e22dd0d9-0000-0000-0000-000000000000",
                            "name": "2026-07-16 13:13 매입처별 재고부족 현황",
                            "created_at": "2026-07-16T13:13:00",
                            "updated_at": "2026-07-16T13:14:00",
                            "message_count": 2,
                        },
                        {
                            "id": "abcdef12-0000-0000-0000-000000000001",
                            "name": '동일:제목/테스트*긴 제목 ' + "가" * 80,
                            "created_at": "2026-07-14T10:19:10",
                            "updated_at": "2026-07-14T10:20:00",
                            "message_count": 1,
                        },
                        {
                            "id": "abcdef12-0000-0000-0000-000000000002",
                            "name": '동일:제목/테스트*긴 제목 ' + "가" * 80,
                            "created_at": "2026-07-14T10:19:20",
                            "updated_at": "2026-07-14T10:20:00",
                            "message_count": 1,
                        },
                    ]
                    legacy_rooms = [
                        {
                            "id": "5266b472-4869-4b52-8a41-afe7d2f79a75",
                            "name": "2026-07-14 10:19 품목별 재고부족현황",
                        },
                        {
                            "id": "cdc0d332-0000-0000-0000-000000000000",
                            "name": "2026-07-12 15:43 품목별 재고부족현황",
                        },
                    ]
                    (chat_root / "user_8_chat_rooms.json").write_text(
                        json.dumps({"rooms": legacy_rooms}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    for room in rooms:
                        d = rooms_dir / rename_mod.safe_uuid_dirname(room["id"])
                        d.mkdir()
                        (d / "messages.jsonl").write_text('{"message":{"role":"user","content":"x"}}\n', encoding="utf-8")
                        (d / "room.json").write_text(json.dumps(room, ensure_ascii=False), encoding="utf-8")
                    (root / "rooms.json").write_text(json.dumps({"version": 1, "rooms": rooms}, ensure_ascii=False, indent=2), encoding="utf-8")
                    originals = rename_mod.load_original_room_names(chat_root, "8")
                    plan = rename_mod.build_plan(root, originals)
                    target_by_id = {item["room_id"]: item for item in plan}
                    restore_ok = (
                        target_by_id["5266b472-4869-4b52-8a41-afe7d2f79a75"]["needs_title_restore"]
                        and target_by_id["5266b472-4869-4b52-8a41-afe7d2f79a75"]["proposed_name"] == "2026-07-14 10:19 품목별 재고부족현황"
                        and target_by_id["cdc0d332-0000-0000-0000-000000000000"]["proposed_name"] == "2026-07-12 15:43 품목별 재고부족현황"
                        and target_by_id["e22dd0d9-0000-0000-0000-000000000000"]["proposed_name"] == "2026-07-16 13:13 매입처별 재고부족 현황"
                    )
                    expected_date_parse = rename_mod.readable_dirname_for_name(
                        {
                            "id": "5266b472-4869-4b52-8a41-afe7d2f79a75",
                            "created_at": "2026-07-16T13:08:00",
                        },
                        "2026-07-14 10:19 품목별 재고부족현황",
                    )
                    no_dup_date = not any(item.get("duplicated_date") for item in plan)
                    dry_run_ok = (
                        len(plan) == 5
                        and all(item["needs_rename"] for item in plan)
                        and restore_ok
                        and no_dup_date
                        and expected_date_parse == "2026-07-14_10-19_품목별_재고부족현황__5266b472"
                        and not any(item["exists_conflict"] or item["too_long"] for item in plan)
                    )
                    rename_mod.apply_plan(root, plan)
                    doc = json.loads((root / "rooms.json").read_text(encoding="utf-8"))
                    rels = [str(r.get("relative_path") or "") for r in doc.get("rooms", [])]
                    names_after = {str(r.get("id") or ""): str(r.get("name") or "") for r in doc.get("rooms", [])}
                    applied_ok = (
                        len(set(rels)) == 5
                        and all("messages.jsonl" in rel for rel in rels)
                        and all((root / rel).exists() for rel in rels)
                        and (root / "rooms_index.csv").exists()
                        and any("2026-07-14_10-19_품목별_재고부족현황__5266b472" in rel for rel in rels)
                        and any("2026-07-12_15-43_품목별_재고부족현황__cdc0d332" in rel for rel in rels)
                        and names_after["5266b472-4869-4b52-8a41-afe7d2f79a75"] == "2026-07-14 10:19 품목별 재고부족현황"
                        and names_after["e22dd0d9-0000-0000-0000-000000000000"] == "2026-07-16 13:13 매입처별 재고부족 현황"
                    )
                    # Failure path: conflict should be detected before apply.
                    root_conflict = Path(td) / "user_9"
                    (root_conflict / "rooms").mkdir(parents=True)
                    conflict_room = {
                        "id": "11111111-2222-3333-4444-555555555555",
                        "name": "충돌 테스트",
                        "created_at": "2026-07-14T10:19:00",
                        "message_count": 1,
                    }
                    old_conflict = root_conflict / "rooms" / rename_mod.safe_uuid_dirname(conflict_room["id"])
                    old_conflict.mkdir()
                    (old_conflict / "messages.jsonl").write_text('{"message":{"role":"user","content":"x"}}\n', encoding="utf-8")
                    target_conflict = root_conflict / "rooms" / rename_mod.readable_dirname(conflict_room)
                    target_conflict.mkdir()
                    (root_conflict / "rooms.json").write_text(json.dumps({"version": 1, "rooms": [conflict_room]}, ensure_ascii=False), encoding="utf-8")
                    conflict_plan = rename_mod.build_plan(root_conflict)
                    failure_detected = any(item["exists_conflict"] for item in conflict_plan)
                    readable_tool_runtime_ok = dry_run_ok and applied_ok and failure_detected
                    readable_tool_runtime_detail = f"dry_run={dry_run_ok} restore={restore_ok} no_dup_date={no_dup_date} applied={applied_ok} conflict={failure_detected} rels={rels[:2]}"
            except Exception as e:
                readable_tool_runtime_detail = f"{type(e).__name__}: {e}"
            missing = [k for k in required_close_keys if k not in main_src]
            if not has_close_helper or missing:
                results.append(_fail("chat room switch panel close policy", f"helper={has_close_helper} missing={missing}"))
            elif not has_room_reason or not has_switch_total or not has_close_guard or not has_guard_consume or not has_panel_close_log or not has_after_success_log or not has_no_open_assignment or not has_no_direct_close_in_room_switch or not has_render_block or not has_switch_event_id or not has_event_id_perf or not has_startup_pending or not has_no_unconditional_startup_log or not has_save_detail or not has_sims_open_perf or not has_authenticate_perf or not has_script_path_perf or not has_save_skip or not has_selection_only_save_skip or not has_chat_save_diff or not has_latest_message_anchor or not has_chronological_render_merge or not has_stale_event_clear or not has_partitioned_storage or not has_partitioned_allowlist or not has_room_index or not has_compact_room_context or not has_partitioned_perf_detail or not has_compact_common_drop_keys or not has_room_projection or not has_pending_login_policy or not has_readable_folder_policy or not has_sims_df_detection or not compact_runtime_ok or not projection_runtime_ok or not has_remigrate_tool or not readable_tool_runtime_ok:
                results.append(_fail("chat room switch panel close policy", f"reason={has_room_reason} switch_total={has_switch_total} guard={has_close_guard} consume={has_guard_consume} log={has_panel_close_log} after_success={has_after_success_log} no_open_assign={has_no_open_assignment} no_direct_close={has_no_direct_close_in_room_switch} render_block={has_render_block} switch_event_id={has_switch_event_id} event_perf={has_event_id_perf} startup_pending={has_startup_pending} no_unconditional_startup={has_no_unconditional_startup_log} save_detail={has_save_detail} sims_open_perf={has_sims_open_perf} authenticate_perf={has_authenticate_perf} script_path_perf={has_script_path_perf} save_skip={has_save_skip} selection_only_skip={has_selection_only_save_skip} save_diff={has_chat_save_diff} latest_anchor={has_latest_message_anchor} chronological_merge={has_chronological_render_merge} stale_event_clear={has_stale_event_clear} partitioned_storage={has_partitioned_storage} partitioned_allowlist={has_partitioned_allowlist} room_index={has_room_index} compact_context={has_compact_room_context} partitioned_perf={has_partitioned_perf_detail} common_drop_keys={has_compact_common_drop_keys} room_projection={has_room_projection} pending_login={has_pending_login_policy} readable_folder={has_readable_folder_policy} readable_tool={readable_tool_runtime_ok} readable_detail={readable_tool_runtime_detail} sims_df_detection={has_sims_df_detection} compact_runtime={compact_runtime_ok} compact_detail={compact_runtime_detail} projection_runtime={projection_runtime_ok} projection_detail={projection_runtime_detail} remigrate_tool={has_remigrate_tool}"))
            else:
                results.append(_ok("chat room switch panel close policy", "room change consumes close guard, blocks SIMS render, logs event-scoped perf, startup perf is one-shot, skips unchanged saves, restores login pending-room flow, uses readable room folders for new rooms, uses manual latest-message anchor, renders normal/SIMS history chronologically, uses allow-listed partitioned append-only storage, writes room index, keeps runtime DataFrames out of persistence projection, adds compact room context, and provides safe remigration tooling"))
        except Exception as e:
            results.append(_fail("chat room switch panel close policy", f"{type(e).__name__}: {e}"))

        try:
            main_src = Path("app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
            helper_start = main_src.index("def _room_has_any_messages")
            start = main_src.index("def _compute_room_sidebar_state")
            end = main_src.index("\ndef _ensure_sims_panel_room_title", start)
            sidebar_src = main_src[helper_start:end]
            ns: dict[str, Any] = {"Any": Any, "Iterable": Iterable}
            exec(sidebar_src, ns)
            compute = ns["_compute_room_sidebar_state"]
            resolve_pick = ns["_resolve_room_pick"]
            rooms = [{"id": f"room-{i:02d}", "name": f"room {i:02d}"} for i in range(22)]
            initial = compute(rooms, current_room_id="room-00", filter_text="", page=0, previous_filter="", page_size=10, initial_to_last=True)
            page0 = compute(rooms, current_room_id="room-00", filter_text="", page=0, previous_filter="", page_size=10)
            page1 = compute(rooms, current_room_id="room-00", filter_text="", page=1, previous_filter="", page_size=10)
            page2 = compute(rooms, current_room_id="room-00", filter_text="", page=2, previous_filter="", page_size=10)
            stale = compute(rooms, current_room_id="missing-room", filter_text="", page=1, previous_filter="", page_size=10)
            filter_changed = compute(rooms, current_room_id="room-00", filter_text="room", page=0, previous_filter="", page_size=10)
            filter_two = compute(rooms, current_room_id="room-00", filter_text="room 2", page=1, previous_filter="", page_size=10)
            filter_cleared = compute(rooms, current_room_id="room-00", filter_text="", page=1, previous_filter="room 2", page_size=10)
            same_filter = compute(rooms, current_room_id="room-00", filter_text="room", page=1, previous_filter="room", page_size=10)
            pending_rooms = rooms + [{"id": "pending", "name": "새 대화", "auto_created": True, "messages": []}]
            initial = compute(pending_rooms, current_room_id="pending", filter_text="", page=0, previous_filter="", page_size=10, initial_to_last=True)
            pending = compute(pending_rooms, current_room_id="pending", filter_text="", page=0, previous_filter="", page_size=10, initial_to_last=True)
            has_save_current_room_guard = (
                "def _valid_current_room_id_for_rooms" in main_src
                and "current_room_for_meta = _valid_current_room_id_for_rooms(rooms_to_save)" in main_src
                and "current_room_for_meta = _valid_current_room_id_for_rooms(rooms)" in main_src
                and '"current_room": current_room_for_meta' in main_src
            )
            has_room_list_log = (
                "[chat.room_list]" in main_src
                and "persisted_rooms=%s pending_visible=%s filtered_persisted_rooms=%s" in main_src
                and "current_kind=%s current_room_id=%s current_in_persisted=%s page_reset_reason=%s" in main_src
            )
            has_visible_pager = (
                'st.button(\n                "이전"' in main_src
                and 'st.button(\n                "다음"' in main_src
                and "페이지 {room_list_state['page_label']}" in main_src
            )
            has_visible_pager = (
                'key="__room_prev"' in main_src
                and 'key="__room_next"' in main_src
                and "room_list_state['page_label']" in main_src
            )
            has_no_page_callback_rerun = (
                "on_click=_room_prev_page" in main_src
                and "on_click=_room_next_page" in main_src
                and "explicit_rerun_called=%s" in main_src
                and "on_click=lambda: ss.update(__room_page" not in main_src
            )
            login_no_auto_pick = resolve_pick(
                current_room_id="pending",
                picked_pending="pending",
                picked_persisted=None,
                pending_ids=["pending"],
                persisted_ids=[r["id"] for r in page2["view"]],
            )
            page2_pick = resolve_pick(
                current_room_id="pending",
                picked_pending="pending",
                picked_persisted="room-21",
                pending_ids=["pending"],
                persisted_ids=[r["id"] for r in page2["view"]],
            )
            page0_pick = resolve_pick(
                current_room_id="pending",
                picked_pending="pending",
                picked_persisted="room-00",
                pending_ids=["pending"],
                persisted_ids=[r["id"] for r in page0["view"]],
            )
            page1_pick = resolve_pick(
                current_room_id="pending",
                picked_pending="pending",
                picked_persisted="room-10",
                pending_ids=["pending"],
                persisted_ids=[r["id"] for r in page1["view"]],
            )
            pending_pick = resolve_pick(
                current_room_id="room-21",
                picked_pending="pending",
                picked_persisted="room-21",
                pending_ids=["pending"],
                persisted_ids=[r["id"] for r in page2["view"]],
            )
            has_selection_resolver = (
                "def _resolve_room_pick" in main_src
                and "picked = _resolve_room_pick(" in main_src
                and "picked = picked_persisted or picked_pending" not in main_src
            )
            has_room_select_log = "[chat.room_select] phase=request" in main_src and "[chat.room_select] phase=render_ready" in main_src
            ok = (
                len(page0["view"]) == 20
                and page0["caption"] == "1-20 / 저장된 채팅방 총 22개"
                and page0["page"] == 0
                and page0["next_disabled"] is False
                and len(page1["view"]) == 2
                and page1["caption"] == "21-22 / 저장된 채팅방 총 22개"
                and page1["page"] == 1
                and page1["prev_disabled"] is False
                and page1["next_disabled"] is True
                and stale["page"] == 2
                and stale["page_reset_reason"] == "stale_current_room"
                and stale["current_kind"] == "stale"
                and stale["current_in_persisted"] is False
                and filter_changed["page"] == 0
                and filter_changed["page_reset_reason"] == "filter_changed"
                and filter_changed["caption"] == "검색 결과 2개 / 전체 22개"
                and filter_cleared["page"] == 0
                and filter_cleared["page_reset_reason"] == "filter_changed"
                and same_filter["page"] == 1
                and same_filter["page_reset_reason"] == ""
                and pending["persisted_room_count"] == 22
                and pending["filtered_persisted_rooms"] == 22
                and pending["pending_visible"] is True
                and pending["current_kind"] == "pending"
                and len(pending["view"]) == 20
                and has_save_current_room_guard
                and has_room_list_log
                and has_visible_pager
            )
            ok = (
                len(initial["view"]) == 2
                and initial["page"] == 2
                and initial["current_kind"] == "pending"
                and initial["page_label"] == "3 / 3"
                and initial["prev_disabled"] is False
                and initial["next_disabled"] is True
                and len(page0["view"]) == 10
                and page0["page"] == 0
                and page0["next_disabled"] is False
                and len(page1["view"]) == 10
                and page1["page"] == 1
                and page1["prev_disabled"] is False
                and page1["next_disabled"] is False
                and len(page2["view"]) == 2
                and page2["page"] == 2
                and page2["prev_disabled"] is False
                and page2["next_disabled"] is True
                and stale["page"] == 2
                and stale["page_reset_reason"] == "stale_current_room"
                and stale["current_kind"] == "stale"
                and stale["current_in_persisted"] is False
                and filter_changed["page"] == 2
                and filter_changed["page_reset_reason"] == "filter_changed"
                and filter_two["page"] == 0
                and filter_two["filtered_persisted_rooms"] == 2
                and filter_cleared["page"] == 2
                and filter_cleared["page_reset_reason"] == "filter_changed"
                and same_filter["page"] == 1
                and same_filter["page_reset_reason"] == ""
                and pending["persisted_room_count"] == 22
                and pending["filtered_persisted_rooms"] == 22
                and pending["pending_visible"] is True
                and pending["current_kind"] == "pending"
                and len(pending["view"]) == 2
                and pending["page"] == 2
                and has_save_current_room_guard
                and has_room_list_log
                and has_visible_pager
                and has_no_page_callback_rerun
                and has_selection_resolver
                and login_no_auto_pick is None
                and page2_pick == "room-21"
                and page1_pick == "room-10"
                and page0_pick == "room-00"
                and pending_pick == "pending"
                and has_room_select_log
            )
            fixture_messages = {r["id"]: f"history-message-{r['id']}" for r in rooms}
            loaded_page2_message = fixture_messages.get(page2_pick or "")
            loaded_page1_message = fixture_messages.get(page1_pick or "")
            loaded_page0_message = fixture_messages.get(page0_pick or "")
            checks = {
                "login_pending_page2": len(initial["view"]) == 2 and initial["page"] == 2 and initial["current_kind"] == "pending" and initial["page_label"] == "3 / 3",
                "page0_ten_rooms": len(page0["view"]) == 10 and page0["page"] == 0 and page0["next_disabled"] is False,
                "page1_ten_rooms": len(page1["view"]) == 10 and page1["page"] == 1 and page1["prev_disabled"] is False and page1["next_disabled"] is False,
                "page2_two_rooms": len(page2["view"]) == 2 and page2["page"] == 2 and page2["prev_disabled"] is False and page2["next_disabled"] is True,
                "stale_current_room_to_page2": stale["page"] == 2 and stale["page_reset_reason"] == "stale_current_room" and stale["current_kind"] == "stale" and stale["current_in_persisted"] is False,
                "filter_change_to_last_page": filter_changed["page"] == 2 and filter_changed["page_reset_reason"] == "filter_changed",
                "filtered_two_rooms": filter_two["page"] == 0 and filter_two["filtered_persisted_rooms"] == 2,
                "filter_clear_to_last_page": filter_cleared["page"] == 2 and filter_cleared["page_reset_reason"] == "filter_changed",
                "same_filter_preserves_page": same_filter["page"] == 1 and same_filter["page_reset_reason"] == "",
                "pending_not_in_persisted_page": pending["persisted_room_count"] == 22 and pending["filtered_persisted_rooms"] == 22 and pending["pending_visible"] is True and pending["current_kind"] == "pending" and len(pending["view"]) == 2 and pending["page"] == 2,
                "save_current_room_guard": has_save_current_room_guard,
                "room_list_log": has_room_list_log,
                "visible_pager": has_visible_pager,
                "page_callback_no_explicit_rerun": has_no_page_callback_rerun,
                "selection_resolver_used": has_selection_resolver,
                "login_no_auto_pick": login_no_auto_pick is None,
                "page2_request": page2_pick == "room-21",
                "page1_request": page1_pick == "room-10",
                "page0_request": page0_pick == "room-00",
                "pending_return_request": pending_pick == "pending",
                "room_select_phase_logs": has_room_select_log,
                "selected_page2_history_message": loaded_page2_message == "history-message-room-21",
                "selected_page1_history_message": loaded_page1_message == "history-message-room-10",
                "selected_page0_history_message": loaded_page0_message == "history-message-room-00",
            }
            failed_checks = [name for name, passed in checks.items() if not passed]
            ok = not failed_checks
            if ok:
                results.append(_ok("chat room sidebar pagination policy", "focused integration harness executed pagination, delta-based room selection, request/consume/load/render-ready logging, selected-room history mapping, stale guard, and callback no-rerun policy"))
            else:
                results.append(_fail("chat room sidebar pagination policy", f"failed_checks={failed_checks} initial={initial} page0={len(page0['view'])}/{page0['caption']} page1={len(page1['view'])}/{page1['caption']} page2={len(page2['view'])}/{page2['caption']} stale={stale} filter_changed={filter_changed} filter_cleared={filter_cleared} same_filter={same_filter} pending={pending} picks={[login_no_auto_pick, page0_pick, page1_pick, page2_pick, pending_pick]} loaded={[loaded_page0_message, loaded_page1_message, loaded_page2_message]} save_guard={has_save_current_room_guard} log={has_room_list_log} pager={has_visible_pager} resolver={has_selection_resolver}"))
        except Exception as e:
            results.append(_fail("chat room sidebar pagination policy", f"{type(e).__name__}: {e}"))

        try:
            login_src = Path("app/ui/ssai_login.py").read_text(encoding="utf-8")
            render_defs = len(re.findall(r"^def\s+render_company_selector\s*\(", login_src, flags=re.M))
            checks = {
                "single_render_company_selector": render_defs == 1,
                "empty_login_default": 'st.text_input("로그인 ID", value="", placeholder="아이디를 입력하세요")' in login_src,
                "enter_to_submit": "enter_to_submit=True" in login_src,
                "password_key_defined": 'password_key = "__ssai_company_change_sims_password"' in login_src,
                "clear_password_key_defined": 'clear_password_key = "__ssai_clear_company_change_sims_password"' in login_src,
                "logout_clears_password": '"__ssai_company_change_sims_password"' in login_src and '"__ssai_clear_company_change_sims_password"' in login_src,
                "ssart_admin_fallback_only_when_missing": 'if _is_ssart_user(user) and not sims_user_id_for_change:' in login_src,
                "normal_user_sims_id": 'sims_user_id_for_change = str(user.sims_user_id or "").strip()' in login_src,
                "sims_password_verify": "verify_sims_plain_password(" in login_src,
            }
            failed = [k for k, ok in checks.items() if not ok]
            if failed:
                results.append(_fail("ssai login company selector policy", f"failed={failed} render_defs={render_defs}"))
            else:
                results.append(_ok("ssai login company selector policy", "selector/password keys/login defaults/logout cleanup verified"))
        except Exception as e:
            results.append(_fail("ssai login company selector policy", f"{type(e).__name__}: {e}"))
    except Exception as e:
        results.append(_fail("supplier stock shortage allocation fixture", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"))

    results.extend(_run_customer_sales_forecast_basic_checks())
    return results


def _run_customer_sales_forecast_basic_checks() -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        customer_mod = importlib.import_module("app.services.analytics_customer_sales_forecast_service")
        chat_mod = importlib.import_module("app.ui.chat_middleware")
        views_mod = importlib.import_module("app.sims.views.analytics_views")
    except Exception as e:
        return [_fail("customer sales forecast import", f"{type(e).__name__}: {e}")]

    calls = {"r130": 0, "master": 0, "date_ranges": []}

    monthly_rows = pd.DataFrame(
        [
            {"기준월": "202601", "매출처코드": "50001", "매출공급가액": 1000, "매출세액": 100, "매출합계": 1100, "집계건수": 1},
            {"기준월": "202602", "매출처코드": "50001", "매출공급가액": 1200, "매출세액": 120, "매출합계": 1320, "집계건수": 1},
            {"기준월": "202603", "매출처코드": "50001", "매출공급가액": 1300, "매출세액": 130, "매출합계": 1430, "집계건수": 1},
            {"기준월": "202604", "매출처코드": "50001", "매출공급가액": 1400, "매출세액": 140, "매출합계": 1540, "집계건수": 1},
            {"기준월": "202605", "매출처코드": "50001", "매출공급가액": 1500, "매출세액": 150, "매출합계": 1650, "집계건수": 1},
            {"기준월": "202606", "매출처코드": "50001", "매출공급가액": 1600, "매출세액": 160, "매출합계": 1760, "집계건수": 1},
            {"기준월": "202607", "매출처코드": "50001", "매출공급가액": 1700, "매출세액": 170, "매출합계": 1870, "집계건수": 1},
            {"기준월": "202601", "매출처코드": "50002", "매출공급가액": 0, "매출세액": 0, "매출합계": 0, "집계건수": 1},
            {"기준월": "202607", "매출처코드": "50002", "매출공급가액": 900, "매출세액": 90, "매출합계": 990, "집계건수": 1},
        ]
    )
    master_rows = pd.DataFrame(
        [
            {"매출처코드": "50001", "매출처명": "한미거래처", "영업사원코드": "S1", "담당영업사원명": "김영업", "시도명": "서울", "시구군명": "강남구", "도로명": "테헤란로"},
            {"매출처코드": "50002", "매출처명": "종근당거래처", "영업사원코드": "S2", "담당영업사원명": "박영업", "시도명": "부산", "시구군명": "해운대구", "도로명": "센텀로"},
        ]
    )

    old_130 = getattr(customer_mod, "_load_rddbc130_monthly", None)
    old_master = getattr(customer_mod, "_load_customer_master", None)

    def fake_130(params, policy):
        calls["r130"] += 1
        date_from = str(params.get("date_from") or "")
        date_to = str(policy.get("effective_date_to") or policy.get("requested_date_to") or params.get("date_to") or "")
        calls["date_ranges"].append((date_from, date_to, policy.get("evaluation_mode")))
        out = monthly_rows.copy()
        if date_to == "20260702":
            out.loc[(out["매출처코드"].astype(str) == "50001") & (out["기준월"].astype(str) == "202607"), ["매출공급가액", "매출세액", "매출합계"]] = [300, 30, 330]
            out.loc[(out["매출처코드"].astype(str) == "50002") & (out["기준월"].astype(str) == "202607"), ["매출공급가액", "매출세액", "매출합계"]] = [200, 20, 220]
        out.attrs.update(
            {
                "source_table": "Rddbc130",
                "source_mode": "transaction_statement",
                "trans_di": "3",
                "date_from": date_from,
                "date_to": date_to,
                "raw_rows": int(out["집계건수"].sum()),
                "monthly_rows": int(len(out)),
                "total_supply": float(out["매출공급가액"].sum()),
                "total_tax": float(out["매출세액"].sum()),
                "total_amount": float(out["매출합계"].sum()),
            }
        )
        return out

    def fake_master(params):
        calls["master"] += 1
        out = master_rows.copy()
        if params.get("sido_nm"):
            out = out[out["시도명"].str.contains(str(params.get("sido_nm")), na=False)].copy()
        if params.get("ven_nm"):
            out = out[out["매출처명"].str.contains(str(params.get("ven_nm")), na=False)].copy()
        return out

    try:
        setattr(customer_mod, "_load_rddbc130_monthly", fake_130)
        setattr(customer_mod, "_load_customer_master", fake_master)

        params_current = {
            "month_from": "202601",
            "month_to": "202607",
            "date_from": "20260101",
            "date_to": "20260712",
            "policy_date": "20260712",
            "top": 0,
        }
        current = customer_mod.get_customer_sales_forecast_df(params_current)
        current_res = customer_mod.get_customer_sales_forecast_result(params_current)
        params_mid = {**params_current, "date_to": "20260702"}
        mid = customer_mod.get_customer_sales_forecast_df(params_mid)
        current_salesperson = customer_mod.get_salesperson_sales_forecast_df(params_current)
        current_region = customer_mod.get_region_sales_forecast_df(params_current)
        current_salesperson_res = customer_mod.get_salesperson_sales_forecast_result(params_current)
        current_region_res = customer_mod.get_region_sales_forecast_result(params_current)
        mid_salesperson = customer_mod.get_salesperson_sales_forecast_df(params_mid)
        mid_region = customer_mod.get_region_sales_forecast_df(params_mid)
        mismatches: list[str] = []
        required_cols = [
            "순번",
            "매출처코드",
            "매출처명",
            "총매출공급가액",
            "총매출세액",
            "총매출액",
            "완료월총매출",
            "완료월평균매출",
            "당월 현재매출",
            "당월 예상매출",
            "당월 잔여예상",
            "당월 진척률",
            "다음월예상매출",
            "예상등급",
            "2026-07 매출",
        ]
        for c in required_cols:
            if c not in current.columns:
                mismatches.append(f"missing current column {c}")
        salesperson_required_cols = [
            "순번",
            "영업사원코드",
            "담당영업사원명",
            "매출처수",
            "총매출액",
            "당월 현재매출",
            "당월 예상매출",
            "예상등급",
            "2026-07 매출",
        ]
        for c in salesperson_required_cols:
            if c not in current_salesperson.columns:
                mismatches.append(f"missing salesperson forecast column {c}")
        region_required_cols = [
            "순번",
            "시도명",
            "시구군명",
            "매출처수",
            "영업사원수",
            "총매출액",
            "당월 현재매출",
            "당월 예상매출",
            "예상등급",
            "2026-07 매출",
        ]
        for c in region_required_cols:
            if c not in current_region.columns:
                mismatches.append(f"missing region forecast column {c}")
        if any(str(c).endswith(" 수량") or str(c) in {"제품코드", "제품명"} for c in current.columns):
            mismatches.append("customer forecast exposed product/qty columns")
        if any(str(c).endswith(" 수량") or str(c) in {"제품코드", "제품명", "매출처코드", "매출처명"} for c in current_salesperson.columns):
            mismatches.append("salesperson forecast exposed customer/product/qty columns")
        if any(str(c).endswith(" 수량") or str(c) in {"제품코드", "제품명", "매출처코드", "매출처명"} for c in current_region.columns):
            mismatches.append("region forecast exposed customer/product/qty columns")
        if calls["r130"] < 3:
            mismatches.append("Rddbc130 transaction statement loader should be used for every period")
        if any(start != "20260101" for start, _end, _mode in calls["date_ranges"]):
            mismatches.append(f"Rddbc130 loader did not preserve date_from ranges={calls['date_ranges']}")
        if not any(end == "20260702" for _start, end, _mode in calls["date_ranges"]):
            mismatches.append(f"Rddbc130 loader did not receive exact historical date_to ranges={calls['date_ranges']}")
        row_50001 = current[current["매출처코드"].astype(str) == "50001"].iloc[0]
        if int(row_50001["완료월수"]) != 6:
            mismatches.append(f"current completed month count expected=6 got={row_50001['완료월수']}")
        if abs(float(row_50001["당월 현재매출"]) - 1870) > 1e-9:
            mismatches.append(f"current month sales expected Rddbc130 total 1870 got={row_50001['당월 현재매출']}")
        mid_row = mid[mid["매출처코드"].astype(str) == "50001"].iloc[0]
        if "평가월 매출" not in mid.columns or "당월 현재매출" in mid.columns:
            mismatches.append("historical midmonth label map failed")
        if abs(float(mid_row["평가월 매출"]) - 330) > 1e-9:
            mismatches.append(f"midmonth evaluation sales expected Rddbc130 total 330 got={mid_row['평가월 매출']}")
        if abs(float(mid_row["평가월 예상매출"]) - float(row_50001["당월 예상매출"])) > 1e-9:
            mismatches.append("current and midmonth expected sales should match from completed months")
        if "평가월 매출" not in mid_salesperson.columns or "당월 현재매출" in mid_salesperson.columns:
            mismatches.append("salesperson historical midmonth label map failed")
        if "평가월 매출" not in mid_region.columns or "당월 현재매출" in mid_region.columns:
            mismatches.append("region historical midmonth label map failed")
        total_customer = float(current["총매출액"].sum())
        if abs(float(current_salesperson["총매출액"].sum()) - total_customer) > 1e-9:
            mismatches.append("salesperson forecast total should match customer forecast total")
        if abs(float(current_region["총매출액"].sum()) - total_customer) > 1e-9:
            mismatches.append("region forecast total should match customer forecast total")
        current_sales_customer = float(current["당월 현재매출"].sum())
        if abs(float(current_salesperson["당월 현재매출"].sum()) - current_sales_customer) > 1e-9:
            mismatches.append("salesperson current sales should match customer current sales")
        if abs(float(current_region["당월 현재매출"].sum()) - current_sales_customer) > 1e-9:
            mismatches.append("region current sales should match customer current sales")
        if current_salesperson[["영업사원코드", "담당영업사원명"]].duplicated().any():
            mismatches.append("salesperson forecast should keep one final row per salesperson")
        if current_region[["시도명", "시구군명"]].duplicated().any():
            mismatches.append("region forecast should keep one final row per region")
        meta = current_res.get("meta") or {}
        if meta.get("analysis_type") != "customer_sales_forecast" or meta.get("summary_type") != "customer_forecast":
            mismatches.append(f"unexpected result meta={meta}")
        if meta.get("source_table") != "Rddbc130" or meta.get("source_mode") != "transaction_statement" or str(meta.get("trans_di")) != "3":
            mismatches.append(f"unexpected source meta={meta}")
        if meta.get("raw_rows", 0) <= 0 or meta.get("monthly_rows", 0) <= 0:
            mismatches.append(f"source row meta missing={meta}")
        if not isinstance(meta.get("salesperson_count"), int):
            mismatches.append(f"salesperson_count should be int got={type(meta.get('salesperson_count')).__name__}")
        if isinstance(meta.get("salesperson_count"), dict):
            mismatches.append("salesperson_count must not contain distribution dict")
        if not isinstance(meta.get("region_count"), int):
            mismatches.append(f"region_count should be int got={type(meta.get('region_count')).__name__}")
        if isinstance(meta.get("region_count"), dict):
            mismatches.append("region_count must not contain distribution dict")
        if not isinstance(meta.get("salesperson_distribution"), dict) or not meta.get("salesperson_distribution"):
            mismatches.append("salesperson_distribution should be separate non-empty dict")
        if not isinstance(meta.get("province_distribution"), dict) or not meta.get("province_distribution"):
            mismatches.append("province_distribution should be separate non-empty dict")
        if not isinstance(meta.get("region_distribution"), dict) or not meta.get("region_distribution"):
            mismatches.append("region_distribution should be separate non-empty dict")
        if not isinstance(meta.get("forecast_grade_counts"), dict) or not meta.get("forecast_grade_counts"):
            mismatches.append("forecast_grade_counts missing")
        if current["매출처코드"].duplicated().any():
            mismatches.append("customer forecast should keep one final row per customer")
        sp_meta = current_salesperson_res.get("meta") or {}
        if sp_meta.get("analysis_type") != "salesperson_sales_forecast" or sp_meta.get("summary_type") != "salesperson_forecast":
            mismatches.append(f"unexpected salesperson meta={sp_meta}")
        if not isinstance(sp_meta.get("salesperson_count"), int) or isinstance(sp_meta.get("salesperson_count"), dict):
            mismatches.append(f"salesperson forecast salesperson_count should be int got={type(sp_meta.get('salesperson_count')).__name__}")
        rg_meta = current_region_res.get("meta") or {}
        if rg_meta.get("analysis_type") != "region_sales_forecast" or rg_meta.get("summary_type") != "region_forecast":
            mismatches.append(f"unexpected region meta={rg_meta}")
        if not isinstance(rg_meta.get("region_count"), int) or isinstance(rg_meta.get("region_count"), dict):
            mismatches.append(f"region forecast region_count should be int got={type(rg_meta.get('region_count')).__name__}")
        llm_df = current.copy()
        llm_df.insert(1, "거래일자", "20260712")
        amount_profile = chat_mod._build_sims_sales_time_profile(  # noqa: SLF001
            llm_df,
            chat_mod._sims_business_terms("매출처별 매출 예상"),  # noqa: SLF001
        )
        if amount_profile.get("amount_col") == "매출처코드":
            mismatches.append("customer forecast LLM amount_col must not be 매출처코드")
        if amount_profile.get("amount_col") != "총매출액":
            mismatches.append(f"customer forecast LLM amount_col expected 총매출액 got={amount_profile.get('amount_col')}")
        sp_llm = current_salesperson.copy()
        sp_llm.insert(1, "거래일자", "20260712")
        sp_amount_profile = chat_mod._build_sims_sales_time_profile(  # noqa: SLF001
            sp_llm,
            chat_mod._sims_business_terms("영업사원별 매출 예상"),  # noqa: SLF001
        )
        if sp_amount_profile.get("amount_col") != "총매출액":
            mismatches.append(f"salesperson forecast LLM amount_col expected 총매출액 got={sp_amount_profile.get('amount_col')}")
        rg_llm = current_region.copy()
        rg_llm.insert(1, "거래일자", "20260712")
        rg_amount_profile = chat_mod._build_sims_sales_time_profile(  # noqa: SLF001
            rg_llm,
            chat_mod._sims_business_terms("지역별 매출 예상"),  # noqa: SLF001
        )
        if rg_amount_profile.get("amount_col") != "총매출액":
            mismatches.append(f"region forecast LLM amount_col expected 총매출액 got={rg_amount_profile.get('amount_col')}")

        old_form = getattr(views_mod, "_render_customer_sales_forecast_form", None)
        old_st = getattr(views_mod, "st", None)

        class _FakeSt:
            @staticmethod
            def subheader(*_args, **_kwargs):
                return None

            @staticmethod
            def caption(*_args, **_kwargs):
                return None

        try:
            setattr(views_mod, "st", _FakeSt())
            setattr(views_mod, "_render_customer_sales_forecast_form", lambda _action_key: (False, {}))
            for fn_name, title in [
                ("render_customer_sales_forecast_analysis", "매출처별 매출 예상"),
                ("render_salesperson_sales_forecast_analysis", "영업사원별 매출 예상"),
                ("render_region_sales_forecast_analysis", "지역별 매출 예상"),
            ]:
                payload = getattr(views_mod, fn_name)()
                if not isinstance(payload, dict) or payload.get("final") is not False or payload.get("title") != title:
                    mismatches.append(f"{fn_name} initial render contract failed payload={payload}")
        finally:
            if old_form is not None:
                setattr(views_mod, "_render_customer_sales_forecast_form", old_form)
            if old_st is not None:
                setattr(views_mod, "st", old_st)

        filtered = customer_mod.get_customer_sales_forecast_df({**params_current, "sido_nm": "서울"})
        if set(filtered["매출처코드"].astype(str).tolist()) != {"50001"}:
            mismatches.append(f"master address filter failed codes={filtered['매출처코드'].astype(str).tolist()}")

        if mismatches:
            results.append(_fail("customer sales forecast", "; ".join(mismatches)))
        else:
            results.append(_ok("customer sales forecast", "Rddbc130 transaction statement source, labels, filters, schema, and meta verified"))
    except Exception as e:
        results.append(_fail("customer sales forecast", f"{type(e).__name__}: {e}"))
    finally:
        if old_130 is not None:
            setattr(customer_mod, "_load_rddbc130_monthly", old_130)
        if old_master is not None:
            setattr(customer_mod, "_load_customer_master", old_master)

    return results


# ---------------------------------------------------------------------
# Service live checks
# ---------------------------------------------------------------------
def _service_cases() -> list[ServiceCase]:
    common_params = {
        "month_from": "202501",
        "month_to": "202512",
        "date_from": "20250101",
        "date_to": "20251231",
        "source_mode": "monthly_book",
        "top": 2000,
    }

    common_condition_tokens = ("2025-01-01", "2025-12-31", "장부재고")

    return [
        ServiceCase(
            name="품목별 매출 추세 분석",
            function_name="get_sales_trend_result",
            params=dict(common_params),
            expected_title_contains="품목별 매출 추세",
            expected_meta_key="trend_judge_counts",
            expected_analysis_type="sales_trend",
            expected_condition_tokens=common_condition_tokens,
            require_seq_column=True,
        ),
        ServiceCase(
            name="품목별 매출 추세 요약표",
            function_name="get_sales_trend_summary_result",
            params=dict(common_params),
            expected_title_contains="품목별 매출 추세 요약표",
            expected_meta_key="trend_judge_counts",
            expected_analysis_type="sales_trend",
            expected_condition_tokens=common_condition_tokens,
            require_seq_column=True,
        ),
        ServiceCase(
            name="품목별 매출 예상",
            function_name="get_sales_forecast_result",
            params=dict(common_params),
            expected_title_contains="품목별 매출 예상",
            expected_meta_key="forecast_grade_counts",
            expected_analysis_type="sales_forecast",
            expected_condition_tokens=common_condition_tokens,
            require_seq_column=True,
        ),
        ServiceCase(
            name="품목별 재고부족현황",
            function_name="get_stock_shortage_result",
            params={
                **common_params,
                "stock_mode": "book",
            },
            expected_title_contains="품목별 재고부족현황",
            expected_meta_key="shortage_grade_counts",
            expected_analysis_type="stock_shortage",
            expected_condition_tokens=common_condition_tokens,
            require_seq_column=True,
        ),
        ServiceCase(
            name="품목별 매출 추세 분석 - 추세판정 필터",
            function_name="get_sales_trend_result",
            params={
                **common_params,
                "trend_judge": "감소",
            },
            expected_title_contains="품목별 매출 추세",
            expected_meta_key="trend_judge_counts",
            expected_analysis_type="sales_trend",
            expected_condition_tokens=common_condition_tokens + ("추세판정", "감소"),
            allow_zero_rows=True,
        ),
        ServiceCase(
            name="품목별 매출 추세 요약표 - 추세판정 필터",
            function_name="get_sales_trend_summary_result",
            params={
                **common_params,
                "trend_judge": "증가",
            },
            expected_title_contains="품목별 매출 추세 요약표",
            expected_meta_key="trend_judge_counts",
            expected_analysis_type="sales_trend",
            expected_condition_tokens=common_condition_tokens + ("추세판정", "증가"),
            allow_zero_rows=True,
        ),
        ServiceCase(
            name="품목별 매출 예상 - 추세판정 필터",
            function_name="get_sales_forecast_result",
            params={
                **common_params,
                "trend_judge": "반품주의",
            },
            expected_title_contains="품목별 매출 예상",
            expected_meta_key="forecast_grade_counts",
            expected_analysis_type="sales_forecast",
            expected_condition_tokens=common_condition_tokens + ("추세판정", "반품주의"),
            allow_zero_rows=True,
        ),
        ServiceCase(
            name="품목별 재고부족현황 - 부족등급 필터",
            function_name="get_stock_shortage_result",
            params={
                **common_params,
                "stock_mode": "book",
                "shortage_grade": "정상",
            },
            expected_title_contains="품목별 재고부족현황",
            expected_meta_key="shortage_grade_counts",
            expected_analysis_type="stock_shortage",
            expected_condition_tokens=common_condition_tokens + ("부족등급", "정상"),
            allow_zero_rows=True,
        ),
    ]

def _evaluate_service_payload(case: ServiceCase, payload: Any) -> CheckResult:
    name = f"service: {case.name}"

    if not isinstance(payload, dict):
        return _fail(name, f"payload가 dict가 아님: {type(payload).__name__}")

    title = str(payload.get("title") or payload.get("action") or "").strip()
    action = str(payload.get("action") or "").strip()
    meta = payload.get("meta") or {}
    ptype = str(payload.get("type") or "").strip()

    if case.expected_title_contains not in title and case.expected_title_contains not in action:
        return _fail(
            name,
            f"title/action mismatch: expected contains {case.expected_title_contains!r}, title={title!r}, action={action!r}",
        )

    row_count = _payload_row_count(payload)
    if row_count <= 0 and not case.allow_zero_rows:
        return _fail(name, f"row_count가 0 이하: rows={row_count}, title={title!r}, type={ptype!r}")

    cols = _payload_columns(payload)

    if row_count > 0 and case.require_seq_column and "순번" not in cols:
        return _fail(name, f"'순번' 컬럼 없음. columns={cols[:20]}")

    if row_count > 0 and case.expected_meta_key:
        val = meta.get(case.expected_meta_key)
        if not isinstance(val, dict) or not val:
            return _fail(name, f"meta[{case.expected_meta_key!r}] 없음 또는 빈값: {val!r}")
        if case.expected_meta_key == "shortage_grade_counts":
            try:
                if sum(int(v or 0) for v in val.values()) != row_count:
                    return _fail(
                        name,
                        f"shortage_grade_counts 합계 불일치: counts={val!r}, rows={row_count}",
                    )
            except Exception:
                return _fail(name, f"shortage_grade_counts 값 변환 실패: {val!r}")

    if case.expected_analysis_type:
        got_analysis_type = str(meta.get("analysis_type") or "").strip()
        if got_analysis_type != case.expected_analysis_type:
            return _fail(
                name,
                (
                    f"analysis_type mismatch: "
                    f"expected={case.expected_analysis_type!r}, got={got_analysis_type!r}, "
                    f"meta_keys={list(meta.keys())}"
                ),
            )

    missing_tokens = _missing_condition_tokens(payload, case.expected_condition_tokens)
    if missing_tokens:
        return _fail(
            name,
            (
                f"조회조건 토큰 누락: missing={missing_tokens!r}, "
                f"condition_text={_condition_text_from_payload(payload)!r}"
            ),
        )

    if row_count > 0 and case.check_code_columns:
        code_problem = _code_column_dtype_problem(payload)
        if code_problem:
            return _fail(name, code_problem)

    summary_md = str(meta.get("summary_md") or "").strip()
    message = str(payload.get("message") or "").strip()

    if case.require_summary_md and not summary_md:
        return _fail(name, f"summary_md 누락: meta_keys={list(meta.keys())}")

    if case.require_message and not message:
        return _fail(name, f"message 누락: payload_keys={list(payload.keys())}")

    condition_preview = _short_text(_condition_text_from_payload(payload), 120)

    detail = (
        f"title={title!r}, action={action!r}, type={ptype!r}, "
        f"rows={row_count}, cols={len(cols)}, "
        f"analysis_type={meta.get('analysis_type')!r}, "
        f"{case.expected_meta_key}={meta.get(case.expected_meta_key) if case.expected_meta_key else None}, "
        f"summary_md={'Y' if summary_md else 'N'}, message={'Y' if message else 'N'}, "
        f"condition={condition_preview!r}"
    )
    return _ok(name, detail)

def run_service_live_checks() -> list[CheckResult]:
    results: list[CheckResult] = []

    try:
        mod = importlib.import_module("app.services.analytics_sales_trend_service")
    except Exception as e:
        return [_fail("import analytics service", f"{type(e).__name__}: {e}")]

    for case in _service_cases():
        fn = getattr(mod, case.function_name, None)
        if not callable(fn):
            results.append(_fail(f"service: {case.name}", f"{case.function_name} callable 없음"))
            continue

        try:
            payload = fn(case.params)
            results.append(_evaluate_service_payload(case, payload))
        except Exception as e:
            detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"
            results.append(_fail(f"service: {case.name}", detail))

    return results


# ---------------------------------------------------------------------
# NLQ live checks
# ---------------------------------------------------------------------
class PayloadCapture:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def fake_push(self, payload=None, action=None, *args, **kwargs):
        if payload is None and args:
            payload = args[0]
        if action is None and len(args) >= 2:
            action = args[1]

        if isinstance(payload, dict):
            p = dict(payload)
            if action and not p.get("action"):
                p["action"] = action
            self.payloads.append(p)
        else:
            self.payloads.append(
                {
                    "final": True,
                    "type": "unknown",
                    "action": action,
                    "data": payload,
                    "meta": {},
                }
            )
        return True

    def pop_last(self) -> dict[str, Any] | None:
        if not self.payloads:
            return None
        return self.payloads[-1]


def _patch_push_function(capture: PayloadCapture) -> None:
    module_names = [
        "app.ui.chat_middleware",
        "app.sims.nlq.nlq_router",
    ]

    for module_name in module_names:
        try:
            mod = importlib.import_module(module_name)
            if hasattr(mod, "push_sims_result_to_chat"):
                setattr(mod, "push_sims_result_to_chat", capture.fake_push)
        except Exception:
            pass


def _make_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _next_seq_factory() -> Callable[[], int]:
    seq = {"v": 0}

    def _next_seq() -> int:
        seq["v"] += 1
        return seq["v"]

    return _next_seq


def _nlq_cases() -> list[NlqCase]:
    base_tokens = ("2025-01-01", "2025-12-31")

    return [
        NlqCase(
            "품목별 매출 추세 2025년 조회",
            "품목별 매출 추세 분석",
            expected_analysis_type="sales_trend",
            expected_meta_key="trend_judge_counts",
            expected_condition_tokens=base_tokens,
        ),
        NlqCase(
            "품목별 매출 추세 요약표 2025년 조회",
            "품목별 매출 추세 요약표",
            expected_analysis_type="sales_trend",
            expected_meta_key="trend_judge_counts",
            expected_condition_tokens=base_tokens,
        ),
        NlqCase(
            "품목별 매출 예상 2025년 조회",
            "품목별 매출 예상",
            expected_analysis_type="sales_forecast",
            expected_meta_key="forecast_grade_counts",
            expected_condition_tokens=base_tokens,
        ),
        NlqCase(
            "품목별 재고부족현황 2025년 장부재고 기준 조회",
            "품목별 재고부족현황",
            expected_analysis_type="stock_shortage",
            expected_meta_key="shortage_grade_counts",
            expected_params={"stock_mode": "book"},
            expected_condition_tokens=base_tokens + ("장부재고",),
        ),
        NlqCase(
            "품목별 재고부족현황 2025년 장부재고 기준 부족등급 정상 조회",
            "품목별 재고부족현황",
            expected_analysis_type="stock_shortage",
            expected_meta_key="shortage_grade_counts",
            expected_params={"stock_mode": "book", "shortage_grade": "정상"},
            expected_condition_tokens=base_tokens + ("장부재고", "부족등급", "정상"),
            allow_empty_meta_counts=True,
        ),
        NlqCase(
            "품목별 매출 추세 2025년 감소 조회",
            "품목별 매출 추세 분석",
            expected_analysis_type="sales_trend",
            expected_meta_key="trend_judge_counts",
            expected_params={"trend_judge": "감소"},
            expected_condition_tokens=base_tokens + ("추세판정", "감소"),
        ),
        NlqCase(
            "품목별 매출 추세 요약표 2025년 증가 조회",
            "품목별 매출 추세 요약표",
            expected_analysis_type="sales_trend",
            expected_meta_key="trend_judge_counts",
            expected_params={"trend_judge": "증가"},
            expected_condition_tokens=base_tokens + ("추세판정", "증가"),
        ),
        NlqCase(
            "품목별 매출 예상 2025년 반품주의 조회",
            "품목별 매출 예상",
            expected_analysis_type="sales_forecast",
            expected_meta_key="forecast_grade_counts",
            expected_params={"trend_judge": "반품주의"},
            expected_condition_tokens=base_tokens + ("추세판정", "반품주의"),
        ),
    ]

def _evaluate_nlq_case(case: NlqCase, handled: bool, payload: dict[str, Any] | None) -> CheckResult:
    name = f"nlq: {case.query}"

    if not handled:
        return _fail(name, "try_handle_nlq()가 False 반환")

    if not isinstance(payload, dict):
        return _fail(name, "payload 없음")

    action = str(payload.get("action") or payload.get("title") or "").strip()
    meta = payload.get("meta") or {}

    params = payload.get("params") or {}
    if case.expected_params:
        for k, expected_v in case.expected_params.items():
            got_v = params.get(k)
            if got_v != expected_v:
                return _fail(
                    name,
                    f"params mismatch {k!r}: expected={expected_v!r}, got={got_v!r}, params={params!r}",
                )

    if case.expected_action not in action:
        return _fail(name, f"action mismatch expected contains {case.expected_action!r}, got {action!r}")

    if not bool(meta.get("analysis_nlq")):
        return _fail(name, f"meta.analysis_nlq 누락: meta keys={list(meta.keys())}")

    if case.expected_meta_key:
        val = meta.get(case.expected_meta_key)
        if not isinstance(val, dict) or (not val and not case.allow_empty_meta_counts):
            return _fail(name, f"meta[{case.expected_meta_key!r}] 없음 또는 빈값: {val!r}")

    if case.expected_analysis_type:
        got_analysis_type = str(meta.get("analysis_type") or "").strip()
        if got_analysis_type != case.expected_analysis_type:
            return _fail(
                name,
                (
                    f"analysis_type mismatch: "
                    f"expected={case.expected_analysis_type!r}, got={got_analysis_type!r}, "
                    f"meta_keys={list(meta.keys())}"
                ),
            )

    missing_tokens = _missing_condition_tokens(payload, case.expected_condition_tokens)
    if missing_tokens:
        return _fail(
            name,
            (
                f"조회조건 토큰 누락: missing={missing_tokens!r}, "
                f"condition_text={_condition_text_from_payload(payload)!r}"
            ),
        )

    summary_md = str(meta.get("summary_md") or "").strip()
    message = str(payload.get("message") or "").strip()

    if case.require_summary_md and not summary_md:
        return _fail(name, f"summary_md 누락: meta_keys={list(meta.keys())}")

    if case.require_message and not message:
        return _fail(name, f"message 누락: payload_keys={list(payload.keys())}")

    row_count = _payload_row_count(payload)
    cols = _payload_columns(payload)
    condition_preview = _short_text(_condition_text_from_payload(payload), 120)

    detail = (
        f"action={action!r}, rows={row_count}, cols={len(cols)}, "
        f"type={payload.get('type')!r}, "
        f"analysis_type={meta.get('analysis_type')!r}, "
        f"analysis_nlq={meta.get('analysis_nlq')!r}, "
        f"summary_md={'Y' if summary_md else 'N'}, message={'Y' if message else 'N'}, "
        f"condition={condition_preview!r}"
    )
    return _ok(name, detail)

def run_nlq_live_checks() -> list[CheckResult]:
    results: list[CheckResult] = []

    try:
        router = importlib.import_module("app.sims.nlq.nlq_router")
        try_handle_nlq = getattr(router, "try_handle_nlq")
    except Exception as e:
        return [_fail("import router.try_handle_nlq", f"{type(e).__name__}: {e}")]

    capture = PayloadCapture()
    _patch_push_function(capture)

    for case in _nlq_cases():
        room: dict[str, Any] = {"messages": []}
        session_state: dict[str, Any] = {
            "__sims_selected": {},
            "__io_pending_product_pick": {},
        }
        next_seq = _next_seq_factory()
        before_count = len(capture.payloads)

        try:
            handled = bool(
                try_handle_nlq(
                    case.query,
                    room=room,
                    session_state=session_state,
                    make_ts=_make_ts,
                    next_seq=next_seq,
                    logger=log,
                )
            )

            payload = capture.pop_last() if len(capture.payloads) > before_count else None
            results.append(_evaluate_nlq_case(case, handled, payload))

        except Exception as e:
            detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"
            results.append(_fail(f"nlq: {case.query}", detail))

    return results


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="SIMS analytics/KPI regression checker")
    parser.add_argument(
        "--live",
        action="store_true",
        help="실제 analytics service DB 조회 smoke test",
    )
    parser.add_argument(
        "--nlq",
        action="store_true",
        help="try_handle_nlq 분석/KPI 라우팅까지 확인",
    )
    args = parser.parse_args()

    print(f"Project root: {PROJECT_ROOT}")

    failed = 0

    basic_results = run_basic_checks()
    failed += _print_results("BASIC IMPORT / HELPER CHECKS", basic_results)

    if args.live:
        service_results = run_service_live_checks()
        failed += _print_results("SERVICE LIVE CHECKS", service_results)

    if args.nlq:
        nlq_results = run_nlq_live_checks()
        failed += _print_results("NLQ LIVE ROUTING CHECKS", nlq_results)

    print()
    if failed:
        print(f"RESULT: FAIL ({failed} failed)")
        return 1

    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
