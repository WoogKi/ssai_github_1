"""Offline plan executor. The approval CSV and baseline snapshots are external inputs."""

import argparse
import csv
from datetime import date, datetime, time
import json
from pathlib import Path
import re
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--oracle", type=Path)
    parser.add_argument("--official", type=Path)
    parser.add_argument("--official-baseline", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.baseline_root or ROOT))

    def deny(*a, **kw):
        raise AssertionError("offline")

    import pyodbc

    pyodbc.connect = socket.socket.connect = socket.socket.connect_ex = (
        socket.create_connection
    ) = deny
    from app.sims.nlq.nlq_router import resolve_new_sims_nlq_candidate
    from app.services.io_nlq import resolve_io_nlq

    rows = list(csv.DictReader(args.plan.open(encoding="utf-8-sig")))

    def historical(row):
        return row["집합"].startswith("기존 등록:") or row["집합"] in {
            "기존/일반 경계",
            "비명시 경계",
            "기존 ERP 명시 한 달",
            "CT 경계",
        }

    def sample(q):
        if "{해당 역할 합성값}" not in q:
            return q
        label = q.rsplit(" ", 2)[-2]
        value = (
            "20261105"
            if "일자" in label
            else "202611"
            if "년월" in label
            else "01"
            if any(t in label for t in ("코드", "구분", "번호", "수량", "금액"))
            else "가상"
        )
        return q.replace("{해당 역할 합성값}", value)

    def observe(q):
        return json.loads(
            json.dumps(
                [resolve_new_sims_nlq_candidate(q), resolve_io_nlq(q)], default=str
            )
        )

    if args.baseline_root:
        snapshot = {
            r["시험ID"]: observe(sample(r["문장/템플릿"]))
            for r in rows
            if historical(r)
        }
        args.output.write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
        )
        return
    from app.services.payment_nlq import match_payment, parse_payment
    from app.services import erp_table_query_service as common
    from tools.check_payment_basic_query import SERVICES, SCOPE, fixture
    from app.ui.current_table_followups.generic import _exact_role_result
    from app.sims.meta.rddbc_payment_meta import TITLES
    from app.ui.knowledge_chat_adapter import parse_incomplete_sims_help_request
    from tools.check_payment_source_nlq import SourceQuery
    from app.sims.nlq.nlq_router import try_handle_nlq
    import logging

    oracle = json.loads(args.oracle.read_text(encoding="utf-8"))

    def official(path):
        return {
            r["case_id"]: r
            for r in json.loads(path.read_text(encoding="utf-8-sig"))["cases"]
        }

    before, after = official(args.official_baseline), official(args.official)
    result = []
    actual_questions = []
    for row in rows:
        i, group, q, expected = (
            row[k] for k in ("시험ID", "집합", "문장/템플릿", "필수 판정")
        )
        q = sample(q)
        status = "PASS"
        check = ""
        submissions = 0
        parity_calls = 0
        try:
            if group == "공식 사례별":
                k = row["대상"]
                assert before[k] == after[k], k
                status = "BASELINE_FAIL" if after[k]["result"] == "FAIL" else "PASS"
                check = "공식 레코드 동일"
                actual_questions.append(after[k]["question"])
            elif historical(row):
                assert observe(q) == oracle[i], "candidate/IO contract changed"
                assert match_payment(q) is None, "claimed by finance"
                check = "후보·IO 파서 HEAD 개별 대조"
                actual_questions.append(q)
            elif group.startswith("상태/보안:") or i in {"T1312", "T1315", "T1316"}:
                test = SourceQuery()
                try:
                    test.setUp()
                    test.scenario(i)
                finally:
                    test.doCleanups()
                    actual_questions.extend(test.submissions)
                    submissions = len(test.submissions)
                check = "격리된 실제 라우터·서비스·게시/상태 시나리오"
            else:
                actual_questions.append(q)
                submissions = 1
                day = date(2026, 11, 27)
                if group == "달력 경계":
                    day = date.fromisoformat(row["선행 상태"].split()[-1])
                t = match_payment(q)
                assert t in TITLES, "no finance owner"
                assert (resolve_new_sims_nlq_candidate(q) or {}).get(
                    "action"
                ) == TITLES[t], "candidate"
                try:
                    plan = parse_payment(q, t, today=day)
                except ValueError:
                    if group not in ("조건 유실 차단", "미지원/모순") and i not in (
                        "T1311",
                        "T1313",
                        "T1314",
                    ):
                        raise
                    check = "조건 차단"
                    plan = None
                with (
                    patch(
                        "app.services.datetime_tool.operating_now",
                        return_value=datetime.combine(day, time()),
                    ),
                    patch.object(common, "query_scope", return_value=SCOPE),
                    patch.object(
                        common, "execute_bound_select", return_value=fixture(t)
                    ) as reader,
                    patch("app.ui.chat_middleware.push_sims_result_to_chat") as push,
                ):
                    handled = try_handle_nlq(
                        q,
                        {"id": "fixture"},
                        {"current_room": "fixture"},
                        None,
                        None,
                        logging.getLogger("offline"),
                    )
                    assert handled and push.call_count == 1, "dispatch/publication"
                    assert reader.call_count == int(bool(plan)), (
                        "unexpected source call"
                    )
                    card = push.call_args.args[0]
                    if plan:
                        assert (
                            card.get("action") == TITLES[t]
                            and card.get("params") == plan["params"]
                        ), "executed parameters"
                        assert (
                            reader.call_args.args[:2]
                            == common.build_detail_select(
                                SERVICES[t].SPEC, plan["params"]
                            )[:2]
                        )
                    else:
                        assert card["meta"].get("result_status") == "input_required"
                if plan:
                    assert group not in ("미지원/모순", "조건 유실 차단") and i not in (
                        "T1311",
                        "T1313",
                        "T1314",
                    ), "invalid accepted"
                    assert not parse_incomplete_sims_help_request(q), (
                        "earlier help gate"
                    )
                    p = plan["params"]
                    for k, v in re.findall(r"filters\.(\w+)=([^; ]+)", expected):
                        assert str(p["filters"].get(k)) == v, (k, p["filters"], v)
                    if span := re.search(r"(\d{8})\.\.(\d{8})", expected):
                        assert tuple(span.groups()) == (p["date_from"], p["date_to"]), (
                            p["date_from"]
                        )
                    if group == "날짜 역할":
                        assert p["date_role"] == expected.split(":")[0]
                    if group == "업무명":
                        assert p["direction"] == expected.split(";")[0].split("=")[1]
                    if group == "0023 문자 경계":
                        assert p["filters"]["flow_code"] == q.split()[-1]
                    check = "후보·조건·기간"
                    if group == "원본 동등성/0건":
                        m = re.search(r"filters\.(\w+)=([^; ]+)", expected)
                        key, want = m.groups()
                        module = SERVICES[t]
                        if module.SPEC["filters"][key][1] in ("money", "integer"):
                            want = int(want)
                        ui = common.normalize_detail_params(
                            module.SPEC,
                            {"filters": {key: want}, "_display_context": "chat"},
                            today=day,
                        )
                        for frame in (fixture(t), fixture(t).iloc[:0]):
                            with (
                                patch.object(common, "query_scope", return_value=SCOPE),
                                patch.object(
                                    common, "execute_bound_select", return_value=frame
                                ) as sql,
                            ):
                                svc = getattr(module, "get_rddbc" + t + "_result")
                                a, b = svc(ui), svc(p)
                                parity_calls += sql.call_count
                                assert (
                                    sql.call_count == 2
                                    and sql.call_args_list[0] == sql.call_args_list[1]
                                )
                                assert a["df"].equals(b["df"]) and len(a["df"]) == len(
                                    frame
                                )
                        check = "UI/NLQ 동일 서비스 SQL·바인딩·전체 PK/금액 및 정상 0건"
                    if group == "복합/집계" or group == "계약 경계":
                        bindings = common.build_detail_select(SERVICES[t].SPEC, p)[1]
                        if i in {"T1306", "T1307", "T1308", "T1310"}:
                            assert (
                                bindings[-1]
                                == {
                                    "T1306": "%가[_][%][[]상%",
                                    "T1307": "%가_%[상%",
                                    "T1308": "%ACME%",
                                    "T1310": "00112233445566778899",
                                }[i]
                            ), bindings[-1]
                        if i == "T1309":
                            assert p["filters"] == {
                                "vendor_staff_name": "가상",
                                "sales_user_name": "별도",
                            }
                        if i == "T0363":
                            assert p["filters"] == {
                                "amount_from": -20000,
                                "amount_to": 1000,
                            }
                        for key, v in re.findall(
                            r"(\w+)=([^;]+?)(?=;| AND |$)", expected
                        ):
                            assert str(p.get(key, p["filters"].get(key))) == v, (
                                key,
                                p["filters"],
                            )
                        if plan["operation"]:
                            with (
                                patch.object(common, "query_scope", return_value=SCOPE),
                                patch.object(
                                    common,
                                    "execute_bound_select",
                                    return_value=fixture(t),
                                ),
                            ):
                                payload = getattr(
                                    SERVICES[t], "get_rddbc" + t + "_result"
                                )(p)
                            _exact_role_result(
                                payload["df"],
                                plan["operation"],
                                10,
                                payload["meta"]["analysis_roles"],
                            )
                            check = "원천 서비스 + 공통 집계 실행"
        except Exception as e:
            status = "FAIL"
            check = f"{type(e).__name__}: {e}"[:220]
        result.append(
            (
                i,
                group,
                row["대상"],
                (row["대상"] if group == "공식 사례별" else q),
                status,
                check,
                submissions,
                parity_calls,
            )
        )
    with args.output.open("w", newline="", encoding="utf-8-sig") as out:
        w = csv.writer(out)
        w.writerow(
            (
                "시험ID",
                "집합",
                "대상",
                "검증질문 또는 사례ID",
                "결과",
                "검증범위",
                "금융 라우터 제출 횟수",
                "UI대조 추가 서비스 호출",
            )
        )
        w.writerows(result)
    from collections import Counter

    print(Counter(r[4] for r in result))
    print(
        "observed_question_instances",
        len(actual_questions),
        "distinct",
        len(set(actual_questions)),
        "router_submissions",
        sum(r[-2] for r in result),
        "additional_parity_calls",
        sum(r[-1] for r in result),
    )
    for r in result:
        if r[4] == "FAIL":
            print(r[0], r[5])
    return int(any(r[4] == "FAIL" for r in result))


if __name__ == "__main__":
    raise SystemExit(main())
