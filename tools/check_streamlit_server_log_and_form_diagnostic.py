"""Focused regression for server-log rotation and login form lifecycle diagnostics."""

from __future__ import annotations

import ast
import io
import logging
import re
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ssai_auth_service import AuthUser  # noqa: E402
from app.ui import ssai_login as login  # noqa: E402
from app.utils.streamlit_server_log import (  # noqa: E402
    STREAMLIT_SERVER_LOG_BACKUP_COUNT,
    STREAMLIT_SERVER_LOG_MAX_BYTES,
    StreamlitServerLogSink,
    streamlit_server_log_path,
)
from tools import run_streamlit_server as runner  # noqa: E402


def _fixture_user() -> AuthUser:
    return AuthUser(
        user_id=41,
        login_id="sensitive-login-id",
        user_name="fixture",
        nickname=None,
        phone=None,
        user_type="WHOLESALE_ADMIN",
        user_grade="MANAGER",
        default_company_id=6,
        sims_user_id="fixture",
        approval_status="APPROVED",
        is_active=True,
    )


def test_rotation() -> None:
    assert STREAMLIT_SERVER_LOG_MAX_BYTES == 5_000_000
    assert STREAMLIT_SERVER_LOG_BACKUP_COUNT == 5
    with tempfile.TemporaryDirectory() as temp_dir:
        environment = {"SSAI_LOG_ROOT": temp_dir, "SSAI_INSTANCE_ID": "HO1"}
        assert streamlit_server_log_path(environment) == Path(temp_dir) / "streamlit_server_1ho.log"
        environment["SSAI_INSTANCE_ID"] = "HO2"
        assert streamlit_server_log_path(environment) == Path(temp_dir) / "streamlit_server_2ho.log"
        path = Path(temp_dir) / "streamlit.log"
        sink = StreamlitServerLogSink(path, max_bytes=180, backup_count=2)
        try:
            for index in range(12):
                sink.write_line(f"server-line-{index} " + ("가" * 25))
        finally:
            sink.close()
        files = [path, path.with_name("streamlit.log.1"), path.with_name("streamlit.log.2")]
        assert all(item.exists() for item in files)
        assert not path.with_name("streamlit.log.3").exists()
        assert "server-line-11" in path.read_text(encoding="utf-8")


def test_single_timestamp_per_server_line() -> None:
    timestamp_pattern = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "streamlit.log"
        sink = StreamlitServerLogSink(path)
        try:
            sink.write_line("[2026-08-19 09:58:18] INFO [ssai] already timestamped")
            sink.write_line("INFO:     Uvicorn startup without timestamp")
            sink.write_line("2026-08-19 11:09:56.577 Uvicorn server started on 0.0.0.0:8501")
        finally:
            sink.close()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert all(len(timestamp_pattern.findall(line)) == 1 for line in lines)
        assert lines[0].endswith("INFO [ssai] already timestamped")
        assert lines[1].endswith("INFO:     Uvicorn startup without timestamp")
        assert lines[2].endswith("Uvicorn server started on 0.0.0.0:8501")


def test_log_environment_fail_closed() -> None:
    for environment in (
        {},
        {"SSAI_LOG_ROOT": "C:\\fixture"},
        {"SSAI_INSTANCE_ID": "HO1"},
        {"SSAI_LOG_ROOT": "C:\\fixture", "SSAI_INSTANCE_ID": "invalid"},
    ):
        try:
            streamlit_server_log_path(environment)
        except RuntimeError:
            continue
        raise AssertionError(f"environment should fail closed: {environment!r}")


def test_runner_contract() -> None:
    source = (ROOT / "tools" / "run_streamlit_server.py").read_text(encoding="utf-8")
    assert "stderr=subprocess.STDOUT" in source
    assert 'instance_id == "HO1"' in source
    assert "text=True" not in source
    assert 'raw_line.decode("utf-8", errors="strict")' in source
    assert runner.normalized_streamlit_args(["--", "--server.port", "8501"]) == ["--server.port", "8501"]
    assert runner.normalized_streamlit_args(["--server.port", "8501"]) == ["--server.port", "8501"]
    original_args = ["--server.port", "8501", "--server.address", "0.0.0.0"]
    assert runner.streamlit_args_for_instance(original_args, "HO1") == [*original_args, "--server.headless=false"]
    assert runner.streamlit_args_for_instance(original_args, "HO2") == [*original_args, "--server.headless=true"]
    assert runner.streamlit_args_for_instance(["--server.headless=true", "--server.port", "8501"], "HO1") == ["--server.headless=true", "--server.port", "8501"]
    assert runner.streamlit_args_for_instance(["--server.headless", "false", "--server.port", "8501"], "HO2") == ["--server.headless", "false", "--server.port", "8501"]
    for invalid_instance_id in ("", "invalid"):
        try:
            runner.streamlit_args_for_instance([], invalid_instance_id)
        except RuntimeError:
            continue
        raise AssertionError(f"instance should fail closed: {invalid_instance_id!r}")
    child_env = runner.streamlit_subprocess_environment()
    assert child_env["PYTHONIOENCODING"] == "utf-8"
    assert child_env["PYTHONUTF8"] == "1"


def test_utf8_subprocess_exact_equality() -> None:
    expected = "stdout 한글 English\nstderr 한글 English\n"
    command = [
        sys.executable,
        "-u",
        "-c",
        "import sys; print('stdout 한글 English'); print('stderr 한글 English', file=sys.stderr)",
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "streamlit.log"
        sink = StreamlitServerLogSink(path)
        console = io.StringIO()
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env=runner.streamlit_subprocess_environment(),
            )
            assert runner._forward_process_output(
                process,
                sink=sink,
                tee_console=True,
                console=console,
            ) == 0
        finally:
            sink.close()
        logged_lines = [line.split("] ", 1)[1] for line in path.read_text(encoding="utf-8").splitlines()]
        assert "\n".join(logged_lines) + "\n" == expected
        assert console.getvalue().replace("\r\n", "\n") == expected


def test_form_diagnostic() -> None:
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    state = {
        login.SESSION_AUTH_USER: _fixture_user(),
        login.SESSION_COMPANY: {"company_id": 6},
    }
    handler = Capture()
    previous_level = login.log.level
    login.log.setLevel(logging.INFO)
    login.log.addHandler(handler)
    try:
        with patch.object(login.st, "session_state", state):
            for phase in ("form_enter", "submit_registered", "form_exit"):
                login._log_form_lifecycle("ssai_login_form", phase)
    finally:
        login.log.removeHandler(handler)
        login.log.setLevel(previous_level)
    assert [f"phase={phase}" for phase in ("form_enter", "submit_registered", "form_exit")] == [
        next(token for token in record.split() if token.startswith("phase=")) for record in records
    ]
    assert all("form_key=ssai_login_form" in record for record in records)
    assert all("user_id=41" in record and "company_id=6" in record for record in records)
    assert all("sensitive-login-id" not in record for record in records)


def test_login_form_instrumentation() -> None:
    source = (ROOT / "app" / "ui" / "ssai_login.py").read_text(encoding="utf-8")
    for form_key in (
        "ssai_login_form",
        "ssai_sims_password_form",
        "ssai_signup_form",
        "__ssai_company_change_form",
    ):
        for phase in ("form_enter", "submit_registered", "form_exit"):
            assert f'_log_form_lifecycle("{form_key}", "{phase}"' in source


def _is_st_form(context_expr: ast.expr) -> bool:
    return (
        isinstance(context_expr, ast.Call)
        and isinstance(context_expr.func, ast.Attribute)
        and isinstance(context_expr.func.value, ast.Name)
        and context_expr.func.value.id == "st"
        and context_expr.func.attr == "form"
    )


def _is_submit_button(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "st"
        and call.func.attr == "form_submit_button"
    )


def _has_lifecycle_interrupt(node: ast.With) -> bool:
    for child in ast.walk(node):
        if isinstance(child, (ast.Return, ast.Raise)):
            return True
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == "st"
            and child.func.attr in {"rerun", "stop"}
        ):
            return True
    return False


def test_all_streamlit_forms_static_contract() -> None:
    direct_forms: list[str] = []
    delegated_forms: list[str] = []
    interrupted_forms: list[str] = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        with tokenize.open(str(path)) as source_file:
            tree = ast.parse(source_file.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.With) or not any(_is_st_form(item.context_expr) for item in node.items):
                continue
            label = f"{path.relative_to(ROOT)}:{node.lineno}"
            submits = [child for child in ast.walk(node) if isinstance(child, ast.Call) and _is_submit_button(child)]
            if submits:
                direct_forms.append(label)
            else:
                delegated_forms.append(label)
            if _has_lifecycle_interrupt(node):
                interrupted_forms.append(label)
    assert len(direct_forms) == 32, direct_forms
    assert len(delegated_forms) == 1, delegated_forms
    assert delegated_forms[0].replace("\\", "/").startswith("app/sims/views/dashboard_lite.py:"), delegated_forms
    assert not interrupted_forms, interrupted_forms

    dashboard_source = (ROOT / "app" / "sims" / "views" / "dashboard_lite.py").read_text(encoding="utf-8")
    for phase in ("form_enter", "submit_registered", "form_exit"):
        assert f"form_key=dashboard_lite_scope_form phase={phase}" in dashboard_source


def main() -> None:
    test_rotation()
    print("PASS server log rotation")
    test_single_timestamp_per_server_line()
    print("PASS server log single timestamp normalization")
    test_log_environment_fail_closed()
    print("PASS server log env path and fail-closed contract")
    test_runner_contract()
    print("PASS server stdout/stderr and console tee contract")
    test_utf8_subprocess_exact_equality()
    print("PASS UTF-8 subprocess log and console exact equality")
    test_form_diagnostic()
    print("PASS login form lifecycle diagnostic")
    test_login_form_instrumentation()
    print("PASS login form instrumentation coverage")
    test_all_streamlit_forms_static_contract()
    print("PASS all Streamlit form static lifecycle survey")
    print("RESULT OK tests=8")


if __name__ == "__main__":
    main()
