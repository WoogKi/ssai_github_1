# tools/ssai_phase3_storage_audit_smoke.py
#
# SS AI Phase 3
# 사용자별 폴더 / 감사 로그 smoke test

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.ssai_audit_service import (  # noqa: E402
    get_audit_log,
    list_audit_logs,
    log_audit_event,
)
from app.services.ssai_storage_service import (  # noqa: E402
    describe_user_storage,
    ensure_user_storage_dirs,
    get_user_file_path,
)


def main() -> None:
    company_id = 1
    user_id = 1

    storage_result = ensure_user_storage_dirs(
        company_id=company_id,
        user_id=user_id,
    )

    sample_file_path = get_user_file_path(
        company_id=company_id,
        user_id=user_id,
        area="temp",
        filename="감사로그_폴더_테스트.txt",
    )

    sample_file_path.write_text(
        "SS AI Phase 3 storage/audit smoke test\n",
        encoding="utf-8",
    )

    audit_id = log_audit_event(
        event_type="SMOKE_TEST",
        action_result="SUCCESS",
        actor_user_id=user_id,
        actor_login_id="smoke_test",
        company_id=company_id,
        target_user_id=user_id,
        target_company_id=company_id,
        message="사용자별 폴더/감사 로그 smoke test",
        details={
            "storage_result": storage_result,
            "sample_file_path": str(sample_file_path),
        },
    )

    result = {
        "ok": True,
        "storage": describe_user_storage(
            company_id=company_id,
            user_id=user_id,
        ),
        "sample_file_path": str(sample_file_path),
        "audit_id": audit_id,
        "audit_log": get_audit_log(audit_id),
        "recent_logs": list_audit_logs(top=5),
    }

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()