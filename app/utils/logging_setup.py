# app/utils/logging_setup.py
# ---------------------------------------------------------
# 파일+콘솔 로깅 설정
# - project-root .env LOG_FILE 우선 사용
# - 상대 fallback 경로 생성 금지
# - 반복 호출 시 중복 핸들러 제거
# ---------------------------------------------------------

from __future__ import annotations

import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

from app.utils.env_config import app_env, config_path_any, read_project_env_file


class OnlyNamespace(logging.Filter):
    """
    주어진 네임스페이스로 시작하는 로거 기록만 통과.
    예: namespace='ssai' 이면 ssai, ssai.xxx 로그만 통과.
    """

    def __init__(self, namespace: str):
        super().__init__()
        self.ns = (namespace or "").strip()

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.ns:
            return True
        return record.name.startswith(self.ns)


def _resolve_log_file(
    *,
    log_file: str | Path | None,
    log_dir: str | Path,
    filename: str,
) -> Path:
    """
    로그 파일 경로 결정.

    우선순위:
    1. 함수 인자 log_file
    2. 환경변수 LOG_FILE
    3. 환경변수 SIMS_LOG_FILE
    4. fallback 없음
    """
    if log_file not in (None, ""):
        value = str(log_file).strip()
    else:
        project_env = read_project_env_file()
        return config_path_any(("LOG_FILE", "SIMS_LOG_FILE"), environ=project_env)

    if not value:
        raise RuntimeError("LOG_FILE is required for rotating logger setup")
    if app_env(read_project_env_file()) == "prod" and not Path(value).is_absolute():
        raise RuntimeError("relative LOG_FILE is not allowed in prod")

    return Path(value)


def setup_rotating_logger(
    name: str = "ssai",
    level: int = logging.INFO,
    *,
    # 신규: .env LOG_FILE 또는 직접 파일 경로
    log_file: str | Path | None = None,

    # 회전 방식
    by_size: bool = True,
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
    when: str = "midnight",
    interval: int = 1,

    # 기존 호환 경로/형식
    log_dir: str | Path = "logs",
    filename: str = "app.log",
    fmt: str = "[%(asctime)s] %(levelname)s [%(name)s] %(message)s",
    datefmt: str = "%Y-%m-%d %H:%M:%S",

    # 루트/필터
    root_level: int = logging.WARNING,
    filter_namespace: str = "ssai",
) -> logging.Logger:
    """
    파일+콘솔 핸들러를 붙인 logger 반환.

    LOG_FILE 사용 예:

    기존 사용도 계속 가능:
        setup_rotating_logger(name="ssai", level=level)
    """

    # 0) 루트 레벨 설정
    root = logging.getLogger()
    root.setLevel(root_level)

    # 1) 대상 로거 준비
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    # 2) 기존 핸들러 제거
    if logger.handlers:
        for h in list(logger.handlers):
            try:
                logger.removeHandler(h)
                h.close()
            except Exception:
                pass

    formatter = logging.Formatter(fmt=fmt, datefmt=datefmt)

    # 3) 콘솔 핸들러
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(formatter)
    if filter_namespace:
        ch.addFilter(OnlyNamespace(filter_namespace))
    logger.addHandler(ch)

    # 4) 파일 핸들러
    logfile = _resolve_log_file(
        log_file=log_file,
        log_dir=log_dir,
        filename=filename,
    )

    logfile.parent.mkdir(parents=True, exist_ok=True)

    if by_size:
        fh = RotatingFileHandler(
            str(logfile),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    else:
        fh = TimedRotatingFileHandler(
            str(logfile),
            when=when,
            interval=interval,
            backupCount=backup_count,
            encoding="utf-8",
            utc=False,
        )

    fh.setLevel(level)
    fh.setFormatter(formatter)
    if filter_namespace:
        fh.addFilter(OnlyNamespace(filter_namespace))
    logger.addHandler(fh)

    # 5) 실제 로그 파일 경로 확인용
    logger.debug("logger initialized: name=%s level=%s logfile=%s", name, level, logfile)

    return logger


def quiet_noisy_loggers(level: int = logging.WARNING) -> None:
    """
    Streamlit/Watcher/네트워크/그래픽 등 noisy 로거를 낮춘다.
    """
    noisy_names = (
        "streamlit",
        "watchdog",
        "PIL",
        "urllib3",
        "tornado",
        "asyncio",
        "numexpr",
        "matplotlib",
        "sqlalchemy.engine",
        "sqlalchemy.pool",
        "sqlalchemy.dialects",
    )

    for n in noisy_names:
        try:
            logging.getLogger(n).setLevel(level)
        except Exception:
            pass
