"""Run Streamlit with common rotated stdout/stderr capture for either instance."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.utils.streamlit_server_log import StreamlitServerLogSink, streamlit_server_log_path
from app.utils.env_config import load_project_env


INSTANCE_HEADLESS_DEFAULTS = {
    "HO1": "false",
    "HO2": "true",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Streamlit with UTF-8 rotating server logs")
    parser.add_argument("--tee-console", action="store_true", help="also write server output to this console")
    parser.add_argument(
        "streamlit_args",
        nargs=argparse.REMAINDER,
        help="arguments after -- are forwarded to streamlit",
    )
    return parser.parse_args(argv)


def normalized_streamlit_args(streamlit_args: list[str]) -> list[str]:
    args = list(streamlit_args or ())
    return args[1:] if args[:1] == ["--"] else args


def _has_explicit_headless_option(streamlit_args: list[str]) -> bool:
    return any(
        arg == "--server.headless" or arg.startswith("--server.headless=")
        for arg in streamlit_args
    )


def streamlit_args_for_instance(streamlit_args: list[str], instance_id: str) -> list[str]:
    args = list(streamlit_args or ())
    normalized_instance_id = str(instance_id or "").strip().upper()
    try:
        default_headless = INSTANCE_HEADLESS_DEFAULTS[normalized_instance_id]
    except KeyError as exc:
        raise RuntimeError("required env SSAI_INSTANCE_ID must be HO1 or HO2") from exc
    if _has_explicit_headless_option(args):
        return args
    return [*args, f"--server.headless={default_headless}"]


def streamlit_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment


def _forward_process_output(
    process: subprocess.Popen[bytes],
    *,
    sink: StreamlitServerLogSink,
    tee_console: bool,
    console: object = sys.stdout,
) -> int:
    """Decode the child pipe exactly once before writing either destination."""
    assert process.stdout is not None
    try:
        for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="strict")
            sink.write_line(line)
            if tee_console:
                console.write(line)  # type: ignore[attr-defined]
                console.flush()  # type: ignore[attr-defined]
        return process.wait()
    except UnicodeDecodeError as exc:
        process.terminate()
        process.wait()
        raise RuntimeError("Streamlit server output was not valid UTF-8") from exc


def run_server(*, log_path: Path, streamlit_args: list[str], tee_console: bool) -> int:
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "app/Lmstudio_SSAI_chat_main.py",
        *streamlit_args,
    ]
    sink = StreamlitServerLogSink(log_path)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=streamlit_subprocess_environment(),
    )
    try:
        return _forward_process_output(process, sink=sink, tee_console=tee_console)
    except KeyboardInterrupt:
        process.terminate()
        return process.wait()
    finally:
        sink.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_project_env(override=True)
    log_path = streamlit_server_log_path()
    instance_id = str(os.environ.get("SSAI_INSTANCE_ID") or "").strip().upper()
    return run_server(
        log_path=log_path,
        streamlit_args=streamlit_args_for_instance(
            normalized_streamlit_args(list(args.streamlit_args or ())),
            instance_id,
        ),
        tee_console=bool(args.tee_console or instance_id == "HO1"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
