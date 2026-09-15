"""SELECT-only discovery of safe account fields for manual Knowledge smoke."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.ssai_auth_service import connect_ssai_db, get_user_permissions

ROLES = {"SYSTEM_ADMIN", "SSART_MANAGER", "SSART_STAFF", "WHOLESALE_MANAGER", "WHOLESALE_STAFF"}
CODES = ("RAG_USE", "KNOWLEDGE_ERP_DB_READ", "KNOWLEDGE_PROJECT_SOURCE_READ")


class SelectConnection:
    def __init__(self, connection):
        self.connection = connection
        self.select_count = 0

    def cursor(self):
        return SelectCursor(self, self.connection.cursor())


class SelectCursor:
    def __init__(self, owner, cursor):
        self.owner, self.raw = owner, cursor

    def execute(self, sql, *params):
        if not sql.lstrip().upper().startswith("SELECT"):
            raise RuntimeError("SELECT only")
        self.owner.select_count += 1
        self.raw.execute(sql, *params)
        return self

    def fetchall(self):
        return self.raw.fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to(ROOT):
        parser.error("Output must be within repository")
    try:
        raw = connect_ssai_db()
        try:
            conn = SelectConnection(raw)
            rows = conn.cursor().execute("""
                SELECT u.user_id, u.login_id, u.user_name, u.user_type,
                       u.default_company_id, u.approval_status, r.role_code, ur.company_id
                FROM dbo.SSAI_USERS u
                JOIN dbo.SSAI_USER_ROLES ur ON ur.user_id = u.user_id AND ur.is_active = 1
                JOIN dbo.SSAI_ROLES r ON r.role_id = ur.role_id AND r.is_active = 1
                WHERE u.is_active = 1
                ORDER BY u.user_id, r.role_code
            """).fetchall()
            companies = [int(row[0]) for row in conn.cursor().execute(
                "SELECT company_id FROM dbo.SSAI_COMPANIES WHERE is_active = 1 ORDER BY company_id"
            ).fetchall()]
            memberships = conn.cursor().execute("""
                SELECT uc.user_id, uc.company_id
                FROM dbo.SSAI_USER_COMPANIES uc
                JOIN dbo.SSAI_COMPANIES c ON c.company_id = uc.company_id AND c.is_active = 1
                WHERE uc.is_active = 1
            """).fetchall()
            users = {}
            for row in rows:
                if str(row[6]) not in ROLES:
                    continue
                user = users.setdefault(int(row[0]), dict(user_id=int(row[0]), login_id=str(row[1]),
                    user_name=str(row[2]), user_type=str(row[3]), default_company_id=row[4],
                    approval_status=str(row[5]), role_assignments=[]))
                user["role_assignments"].append(dict(role=str(row[6]), company_id=row[7]))
            for user in users.values():
                default_permissions = set(get_user_permissions(conn, user_id=user["user_id"],
                                                               company_id=user["default_company_id"]))
                all_access = "SIMS_DB_ACCESS_ALL" in default_permissions or user["user_type"] == "SSART_ADMIN"
                allowed = companies if all_access else sorted({int(row[1]) for row in memberships
                                                               if int(row[0]) == user["user_id"]})
                user["accessible_companies"] = allowed
                user["company_permissions"] = []
                for company in allowed:
                    permissions = set(get_user_permissions(conn, user_id=user["user_id"], company_id=company))
                    user["company_permissions"].append(dict(company_id=company,
                        **{code: code in permissions for code in CODES},
                        technical_erp=all(code in permissions for code in ("RAG_USE", "KNOWLEDGE_ERP_DB_READ")),
                        technical_project=all(code in permissions for code in ("RAG_USE", "KNOWLEDGE_PROJECT_SOURCE_READ"))))
            result = dict(status="PASS", users=list(users.values()), select_count=conn.select_count,
                db_write_count=0, retry_count=0, password_fields_selected=False,
                company_access_authority="get_active_companies_for_user policy with safe company-id projection",
                permission_authority="ssai_auth_service.get_user_permissions")
        finally:
            raw.close()
    except Exception as exc:
        print(json.dumps(dict(status="ERROR", error_type=type(exc).__name__, retry_count=0,
                              db_write_count=0), ensure_ascii=False))
        return 1
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(status=result["status"], user_count=len(result["users"]),
        select_count=result["select_count"], db_write_count=0, retry_count=0,
        output=str(args.output)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
