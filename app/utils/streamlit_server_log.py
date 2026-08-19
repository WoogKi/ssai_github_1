"""Shared stdout/stderr rotation for Streamlit server processes."""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Mapping

from app.utils.env_config import config_path


STREAMLIT_SERVER_LOG_MAX_BYTES = 5_000_000
STREAMLIT_SERVER_LOG_BACKUP_COUNT = 5
INSTANCE_LOG_FILENAMES = {
    "HO1": "streamlit_server_1ho.log",
    "HO2": "streamlit_server_2ho.log",
}
_LEADING_TIMESTAMP = re.compile(r"^(?:\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+")


def streamlit_server_log_path(environ: Mapping[str, str] | None = None) -> Path:
    source = environ if environ is not None else os.environ
    instance_id = str(source.get("SSAI_INSTANCE_ID") or "").strip().upper()
    try:
        filename = INSTANCE_LOG_FILENAMES[instance_id]
    except KeyError as exc:
        raise RuntimeError("required env SSAI_INSTANCE_ID must be HO1 or HO2") from exc
    return config_path("SSAI_LOG_ROOT", environ=source) / filename


class StreamlitServerLogSink:
    """Write one combined server stream to UTF-8 size-rotated files."""

    def __init__(
        self,
        log_path: str | Path,
        *,
        max_bytes: int = STREAMLIT_SERVER_LOG_MAX_BYTES,
        backup_count: int = STREAMLIT_SERVER_LOG_BACKUP_COUNT,
    ) -> None:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(f"ssai.streamlit_server.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._handler = RotatingFileHandler(
            path,
            maxBytes=int(max_bytes),
            backupCount=int(backup_count),
            encoding="utf-8",
        )
        self._handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        self._logger.addHandler(self._handler)

    def write_line(self, line: str) -> None:
        text = _LEADING_TIMESTAMP.sub("", str(line or "").rstrip("\r\n"), count=1)
        if text:
            self._logger.info(text)

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self._handler.close()
