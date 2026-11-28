"""Compare registered query expressions with the independently approved service contracts."""

import argparse
import csv
from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from app.services.erp_table_nlq import resolve_registered_erp_table_nlq

    cli = argparse.ArgumentParser()
    cli.add_argument("conditions", type=Path)
    cli.add_argument("output", type=Path)
    args = cli.parse_args()
    actions = {"310": "입출금", "320": "어음", "330": "담보"}
    staff = {
        "vendor_staff_name": ("담당자", "거래처 담당자", "현재 담당자", "현재 담당자명"),
        "sales_user_name": ("당시 담당자", "당시 담당영업사원", "원본 담당영업사원", "담당영업사원명"),
        "sales_user_cd": ("당시 담당자코드", "당시 담당영업사원코드"),
        "vendor_staff_cd": ("담당자코드", "현재 담당자코드"),
    }
    with args.conditions.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert sum(r["종류"] == "서비스 필터" for r in rows) == 85
    assert sum(r["종류"] == "날짜 역할" for r in rows) == 12
    rows.append({"업무": "310", "종류": "서비스 필터", "사용자 조건": "거래처 담당자코드", "별칭 설계": "별도 없음",
                 "서비스 키/값": "filters.vendor_staff_cd", "독립 기대 조건": "filters.vendor_staff_cd=00001",
                 "예시 구절": "거래처 담당자코드 00001", "자료형": "문자 코드/식별자"})
    results = {}
    for row in rows:
        domain = actions[row["업무"]]
        label = row["사용자 조건"]
        is_role = row["종류"] == "날짜 역할"
        key = row["서비스 키/값"].removeprefix("filters.")
        aliases = {label, *staff.get(key, ())}
        if row["별칭 설계"] != "별도 없음":
            aliases.update(row["별칭 설계"].split(" / "))
        terms = {f for a in aliases for f in (a, a.replace(" ", ""), a[:1] + " " + a[1:])}
        for term in sorted(terms):
            if is_role:
                phrase = term + " 지난달"
                extra = "은행명 가상은행"
                wanted = {"bank_name": "가상은행"}
            else:
                phrase = term + row["예시 구절"][len(label):]
                extra = "오늘"
                value = row["독립 기대 조건"].partition("=")[2]
                numeric = row["자료형"] == "smallint" or "정수" in row["자료형"]
                wanted = {key: int(value) if numeric else value}
            for q in (f"{domain} {phrase} {extra}", f"{extra} {phrase} {domain} 조회", f"{phrase} {domain} {extra}"):
                try:
                    plan = resolve_registered_erp_table_nlq(q, today=date(2026, 11, 28)) or {}
                    params = plan.get("params") or {}
                    ok = (plan.get("action") == domain + " 조회" and params.get("filters") == wanted
                          and (params.get("date_from"), params.get("date_to")) ==
                          (("20261001", "20261031") if is_role else ("20261128", "20261128"))
                          and (not is_role or params.get("date_role") == key.partition("=")[2]))
                    status = "PASS" if ok else "FAIL"
                except Exception as exc:
                    status = "FAIL:" + type(exc).__name__
                results[q] = (domain, key, status)
    with args.output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("question", "domain", "key", "status"))
        writer.writerows((q, *v) for q, v in results.items())
    failed = sum(r[2] != "PASS" for r in results.values())
    print(f"conditions={len(rows)} questions={len(results)} failed={failed}")
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
