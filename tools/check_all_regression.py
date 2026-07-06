# tools/check_all_regression.py
# -*- coding: utf-8 -*-
# VERSION = "check_all_regression/2026-05-02-v1"
# 작성자: ChatGPT (OpenAI)  
# 정의 : SIMS 전체 회귀 테스트 실행 스크립트
"""
SIMS 전체 회귀 테스트 실행 스크립트.

빠른 확인:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_all_regression.py --quick

전체 확인:
    & "C:\\Program Files\\Python313\\python.exe" tools\\check_all_regression.py

개별 스크립트:
    tools/check_master_nlq_regression.py
    tools/check_analytics_regression.py
    tools/check_io_nlq_regression.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[1]


@dataclass
class RunSpec:
    name: str
    args: list[str]
    required: bool = True


@dataclass
class RunResult:
    name: str
    ok: bool
    returncode: int
    seconds: float
    command: str


def _script_path(name: str) -> str:
    return str(PROJECT_ROOT / "tools" / name)


def _run_spec(spec: RunSpec) -> RunResult:
    cmd = [sys.executable, *spec.args]
    command_text = " ".join(f'"{x}"' if " " in x else x for x in cmd)

    print()
    print("=" * 90)
    print(f"RUN: {spec.name}")
    print("=" * 90)
    print(command_text)
    print("-" * 90)

    started = time.time()

    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    seconds = time.time() - started
    ok = proc.returncode == 0

    print("-" * 90)
    print(f"{'OK' if ok else 'FAIL'}: {spec.name} ({seconds:.1f}s, returncode={proc.returncode})")

    return RunResult(
        name=spec.name,
        ok=ok,
        returncode=proc.returncode,
        seconds=seconds,
        command=command_text,
    )


def _build_specs(*, quick: bool) -> list[RunSpec]:
    if quick:
        return [
            RunSpec(
                name="Master NLQ live smoke",
                args=[_script_path("check_master_nlq_regression.py"), "--live"],
            ),
            RunSpec(
                name="Analytics/KPI live + NLQ",
                args=[_script_path("check_analytics_regression.py"), "--live", "--nlq"],
            ),
            RunSpec(
                name="IO NLQ live smoke",
                args=[_script_path("check_io_nlq_regression.py"), "--live"],
            ),
        ]

    return [
        RunSpec(
            name="Master NLQ live smoke",
            args=[_script_path("check_master_nlq_regression.py"), "--live"],
        ),
        RunSpec(
            name="Analytics/KPI live + NLQ",
            args=[_script_path("check_analytics_regression.py"), "--live", "--nlq"],
        ),
        RunSpec(
            name="IO NLQ live all",
            args=[_script_path("check_io_nlq_regression.py"), "--live-all"],
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="SIMS 전체 회귀 테스트 실행")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="IO는 --live만 실행해서 빠르게 확인한다. 기본은 IO --live-all까지 실행.",
    )
    args = parser.parse_args()

    print(f"Project root: {PROJECT_ROOT}")
    print(f"Python: {sys.executable}")
    print(f"Mode: {'QUICK' if args.quick else 'FULL'}")

    specs = _build_specs(quick=bool(args.quick))
    results: list[RunResult] = []

    started_all = time.time()

    for spec in specs:
        result = _run_spec(spec)
        results.append(result)

        if spec.required and not result.ok:
            print()
            print("중단: 필수 회귀 테스트가 실패했습니다.")
            break

    total_seconds = time.time() - started_all

    print()
    print("=" * 90)
    print("SUMMARY")
    print("=" * 90)

    failed = 0
    for r in results:
        mark = "OK " if r.ok else "FAIL"
        print(f"[{mark}] {r.name} ({r.seconds:.1f}s)")
        if not r.ok:
            failed += 1

    print("-" * 90)
    print(f"총 {len(results)}개 / 성공 {len(results) - failed}개 / 실패 {failed}개 / 전체 {total_seconds:.1f}s")

    if failed:
        print()
        print("RESULT: FAIL")
        return 1

    print()
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())