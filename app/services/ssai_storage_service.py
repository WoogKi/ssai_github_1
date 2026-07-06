# app/services/ssai_storage_service.py
#
# SS AI Phase 3
# 사용자별/회사별 파일 저장소 서비스
# create 2026/06/24

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.services.ssai_auth_service import load_dotenv, pick_env


DEFAULT_STORAGE_ROOT = "data/ssai_storage"

STORAGE_AREAS = {
    "uploads",
    "downloads",
    "reports",
    "temp",
    "logs",
}


def _project_root() -> Path:
    """
    현재 파일 기준 프로젝트 루트 경로.
    app/services/ssai_storage_service.py -> 프로젝트 루트는 parents[2]
    """
    return Path(__file__).resolve().parents[2]


def get_storage_root() -> Path:
    """
    SS AI 파일 저장소 루트.

    .env 우선순위:
    - SSAI_STORAGE_ROOT

    없으면:
    - data/ssai_storage
    """
    env = load_dotenv(".env")
    root_value = pick_env(env, ["SSAI_STORAGE_ROOT"], DEFAULT_STORAGE_ROOT)

    root = Path(str(root_value or DEFAULT_STORAGE_ROOT))

    if not root.is_absolute():
        root = _project_root() / root

    return root.resolve()


def _safe_int_id(value: int | str, *, name: str) -> int:
    try:
        number = int(value)
    except Exception as e:
        raise ValueError(f"{name}는 숫자여야 합니다. value={value}") from e

    if number <= 0:
        raise ValueError(f"{name}는 1 이상이어야 합니다. value={value}")

    return number


def make_safe_filename(filename: str, *, default: str = "file") -> str:
    """
    사용자 입력 파일명을 안전한 파일명으로 변환한다.

    - 경로 구분자 제거
    - 윈도우 금지 문자 제거
    - 너무 긴 파일명 제한
    """
    text = str(filename or "").strip()

    if not text:
        text = default

    text = text.replace("\\", "_").replace("/", "_")
    text = re.sub(r'[:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(". ")

    if not text:
        text = default

    if len(text) > 180:
        stem = Path(text).stem[:120]
        suffix = Path(text).suffix[:20]
        text = f"{stem}{suffix}" if suffix else stem

    return text


def get_company_storage_dir(
    *,
    company_id: int | str,
    create: bool = True,
) -> Path:
    company_id_int = _safe_int_id(company_id, name="company_id")
    path = get_storage_root() / f"company_{company_id_int}"

    if create:
        path.mkdir(parents=True, exist_ok=True)

    return path


def get_user_storage_dir(
    *,
    company_id: int | str,
    user_id: int | str,
    create: bool = True,
) -> Path:
    company_id_int = _safe_int_id(company_id, name="company_id")
    user_id_int = _safe_int_id(user_id, name="user_id")

    path = get_storage_root() / f"company_{company_id_int}" / f"user_{user_id_int}"

    if create:
        path.mkdir(parents=True, exist_ok=True)

    return path


def get_user_area_dir(
    *,
    company_id: int | str,
    user_id: int | str,
    area: str,
    create: bool = True,
) -> Path:
    area = str(area or "").strip().lower()

    if area not in STORAGE_AREAS:
        raise ValueError(
            f"허용되지 않은 저장 영역입니다. area={area}, allowed={sorted(STORAGE_AREAS)}"
        )

    path = get_user_storage_dir(
        company_id=company_id,
        user_id=user_id,
        create=create,
    ) / area

    if create:
        path.mkdir(parents=True, exist_ok=True)

    return path


def get_user_file_path(
    *,
    company_id: int | str,
    user_id: int | str,
    area: str,
    filename: str,
    create_parent: bool = True,
) -> Path:
    area_dir = get_user_area_dir(
        company_id=company_id,
        user_id=user_id,
        area=area,
        create=create_parent,
    )

    safe_name = make_safe_filename(filename)
    return area_dir / safe_name


def ensure_user_storage_dirs(
    *,
    company_id: int | str,
    user_id: int | str,
) -> dict[str, Any]:
    """
    사용자별 기본 저장 폴더를 모두 생성한다.

    구조:
    data/ssai_storage/
      company_1/
        user_3/
          uploads/
          downloads/
          reports/
          temp/
          logs/
    """
    root = get_storage_root()
    company_dir = get_company_storage_dir(company_id=company_id, create=True)
    user_dir = get_user_storage_dir(
        company_id=company_id,
        user_id=user_id,
        create=True,
    )

    area_dirs: dict[str, str] = {}

    for area in sorted(STORAGE_AREAS):
        path = get_user_area_dir(
            company_id=company_id,
            user_id=user_id,
            area=area,
            create=True,
        )
        area_dirs[area] = str(path)

    return {
        "ok": True,
        "storage_root": str(root),
        "company_dir": str(company_dir),
        "user_dir": str(user_dir),
        "areas": area_dirs,
    }


def describe_user_storage(
    *,
    company_id: int | str,
    user_id: int | str,
) -> dict[str, Any]:
    """
    생성하지 않고 현재 폴더 존재 여부를 확인한다.
    """
    root = get_storage_root()
    company_dir = get_company_storage_dir(company_id=company_id, create=False)
    user_dir = get_user_storage_dir(
        company_id=company_id,
        user_id=user_id,
        create=False,
    )

    areas: dict[str, dict[str, Any]] = {}

    for area in sorted(STORAGE_AREAS):
        path = get_user_area_dir(
            company_id=company_id,
            user_id=user_id,
            area=area,
            create=False,
        )
        areas[area] = {
            "path": str(path),
            "exists": path.exists(),
        }

    return {
        "storage_root": str(root),
        "storage_root_exists": root.exists(),
        "company_dir": str(company_dir),
        "company_dir_exists": company_dir.exists(),
        "user_dir": str(user_dir),
        "user_dir_exists": user_dir.exists(),
        "areas": areas,
    }