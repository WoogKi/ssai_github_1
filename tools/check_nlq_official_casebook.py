"""Run the versioned official workbook offline; earlier gates retain their own defaults."""
import argparse
import csv
from collections import Counter
from datetime import date
import logging
from pathlib import Path
import socket
import sys
from unittest import TestCase
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BOOK = ROOT / "docs/NLQ 정리 20261128.xlsx"
MONTH = ("20261028", "20261128")
LAST = ("20261001", "20261031")
NEXT = ("20261201", "20261231")
RECEIVE = "Rd31_Receive_YyMmDd"
BILL = "Rd32_Add_Date"
PLEDGE = "Rd33_Add_Date"
# Independent business expectations at 2026-11-28. Never derive expected dates
# or predicates from the parser, or treat a PASS cell in Excel as evidence.
SOURCE = {
    250: (RECEIVE, MONTH, ""), 251: (RECEIVE, (*MONTH, "50"), "END < ?"),
    252: (RECEIVE, (*MONTH, "50"), "END >= ?"), 253: (BILL, MONTH, ""),
    254: (PLEDGE, MONTH, ""), 255: (RECEIVE, (*MONTH, "5C"), "Rd31_Receive_Di"),
    256: (RECEIVE, (*MONTH, "00001"), "Rd31_Ven_Cd"),
    257: (RECEIVE, (*MONTH, "%가상갑%"), "OwnerStaff.Rd06_User_Nm LIKE ?"),
    258: (RECEIVE, (*MONTH, "%가상갑%"), "J7.Rd06_User_Nm LIKE ?"),
    259: ("Rd31_Slip_YyMmDd", ("20261127", "20261127"), ""),
    260: (RECEIVE, MONTH, ""), 261: (RECEIVE, ("20261030", "20261128"), ""),
    262: ("Rd32_Maturit_YyMmDd", NEXT, ""), 263: (BILL, (*MONTH, "%가상은행%"), "LIKE ?"),
    264: ("Rd33_Draft_YyMmDd", ("20261119", "20261119"), ""),
    265: (PLEDGE, (*MONTH, "%부동산%"), "LIKE ?"),
    266: (RECEIVE, (*LAST, "%가상약국%"), "LIKE ?"),
    267: (RECEIVE, (*LAST, "50"), "END >= ?"), 268: (BILL, MONTH, ""),
    269: (RECEIVE, MONTH, ""), 276: (RECEIVE, MONTH, ""),
    277: (RECEIVE, (*MONTH, "5C", "%지난주 입출금%"), "LIKE ?"),
}

KPIS = {
    278: ("MA117", "ready", {"srchStaDd": "20261127", "srchEndDd": "20261127"}),
    279: ("MA117", "defaults", {}), 280: ("MA101", "ready", {"msupStdCd": "0000000000001"}),
    281: ("MA101", "products", {}), 282: ("MA102", "ready", {"ykiho": "00000001",
          "tkrHbin": "0000000001", "msupSplyFrmCd": "5", "tkrpNm": "합성병원"}),
    283: ("MA102", "clarify", {}), 284: ("MA109", "ready", {"rpayCmmStaDd": "20260901", "rpayCmmEndDd": "20261130"}),
    285: ("MA109", "ready", {"rpayCmmYr": "2026"}), 286: ("MA110", "ready", {"splyDd": "20261127", "splyTpCd": "2"}),
    287: ("MA110", "defaults", {"splyTpCd": None}), 288: ("MA116", "clarify", {}),
}
KPIS_TITLES = dict(MA117="KPIS 공지사항", MA101="KPIS 표준코드 이력", MA102="KPIS 거래처 등록 여부",
                   MA109="KPIS 회수의약품", MA110="KPIS 공급내역", MA116="KPIS 미지원 안내")
DAY = date(2026, 11, 28)
IO_PARAMS = {
    295: {"month_from": "202611", "month_to": "202611"}, 296: {"month_from": "202611", "month_to": "202611"},
    297: {"only_mismatch_trans": "Y"}, 298: {"only_mismatch_tax": "Y"},
    299: {"validation_trans": True, "validation_tax": False}, 300: {"validation_trans": False, "validation_tax": True},
    301: {"mode": "history", "date_from": "20261119", "date_to": "20261119"},
    302: {"physic_nm": "31278"}, 303: {"physic_cd": "00846", "query_mode": "확인 필요"},
    304: {"physic_cd": "00001", "query_mode": "발주해당자료만"},
    305: {"date_from": "20261123", "date_to": "20261128"}, 306: {"date_from": "20261201", "date_to": "20261231"},
    307: {"physic_cd": "00029"}, 311: {"order_vendor_manager_nm": "가상하나"},
    312: {"maker_manager_nm": "가상두나"}, 314: {"io_gu_prefix": "8"},
}


def verify_business(seq, q, title):
    from app.services import io_nlq
    from app.services.kpis_nlq import parse_request
    from app.sims.nlq import nlq_router, nlq_vendors, nlq_goods, nlq_users, nlq_codes
    import pandas as pd

    test = TestCase()
    if seq in KPIS:
        code, status, query = KPIS[seq]
        p = parse_request(q, today=DAY)
        test.assertEqual((title, p.service, p.status, p.query), (KPIS_TITLES[code], code, status, query))
        if seq == 281:
            test.assertEqual((p.product_term, p.product_limit), ("합성제품", 5))
        if seq == 288:
            test.assertEqual(p.reason, "jwt_pending")
        return f"KPIS {code}: {status}, 요청 파라미터 대조 (외부 API 없음)"
    candidate = nlq_router.resolve_new_sims_nlq_candidate(q) or {}
    if seq in IO_PARAMS:
        test.assertEqual(candidate.get("action"), title)
        plan = io_nlq.resolve_io_nlq(q, today=DAY)
        test.assertEqual(plan["action"], title)
        test.assertEqual({k: plan["params"].get(k) for k in IO_PARAMS[seq]}, IO_PARAMS[seq])
        if 297 <= seq <= 300:
            test.assertEqual([plan["params"][k] for k in ("date_from", "date_to")], ["20261119"] * 2)
        return "원천 후보·실제 업무 파서·조건"
    if seq == 310:
        test.assertEqual(candidate, {"route": "vendors", "action": title})
        test.assertEqual(nlq_vendors._extract_vendorname_keyword(q), "담보약국")
        return "거래처 목록 라우트·명칭 조건"
    if seq in (308, 309):
        test.assertEqual(candidate.get("action"), title)
        if seq == 309:
            test.assertEqual(nlq_router._extract_dashboard_nlq_conditions(q)[0]["재고위치"], "00001")
        else:
            test.assertFalse(io_nlq.extract_nlq_natural_period(q, today=DAY))
        return "Dashboard/분석 라우트·기간/위치"
    if seq == 313:
        from app.ui.knowledge_chat_adapter import parse_incomplete_sims_help_request
        test.assertEqual(title, "미지원 업무 안내")
        test.assertFalse(candidate)
        test.assertIsNone(io_nlq.resolve_io_nlq(q, today=DAY))
        test.assertEqual(parse_incomplete_sims_help_request(q).reason, "unregistered_io_status")
        return "미등록 업무 안내, 원천 대체 없음"
    kw = dict(room={"id": "fixture", "messages": []}, session_state={}, make_ts=lambda: "", next_seq=lambda: 1,
              logger=logging.getLogger("official.fixture"))
    if seq == 292:
        test.assertEqual(title, "거래처 상세 미지원")
        test.assertEqual(candidate.get("action"), "거래처 목록")
        with patch("app.services.rddbc030_service.search_vendors_full") as read:
            test.assertFalse(nlq_vendors.try_handle_vendors_nlq(q, **kw))
            read.assert_not_called()
        return "등록/실행 차이: 독립 거래처 상세 미지원. 후보만으로 성공 판정 금지"
    if seq == 289:
        with (patch("app.ui.chat_bridge.get_sims_context_data", return_value=None),
              patch.object(nlq_users, "_compute_dept_count_from_db", return_value=pd.DataFrame({"부서명": ["합성"], "인원수": [1]})) as read,
              patch.object(nlq_users, "_push_table_result", return_value=True) as push):
            test.assertTrue(nlq_users.try_handle_users_nlq(q, **kw))
            read.assert_called_once()
            test.assertEqual(push.call_args.kwargs["action"], title)
    elif seq == 290:
        with (patch("app.services.rddbc060_service.search_users_full", return_value=pd.DataFrame()) as read,
              patch.object(nlq_users, "_push_user_text", return_value=True) as push):
            test.assertTrue(nlq_users.try_handle_users_nlq(q, **kw))
            read.assert_called_once()
            test.assertEqual(push.call_args.kwargs["params"]["기준일자"], "2026-11-01")
            test.assertEqual(title, "최근 입사자")
    elif seq == 291:
        with (patch.object(nlq_codes, "_service_search", return_value=pd.DataFrame()) as read,
              patch.object(nlq_codes, "_push_codes_text", return_value=True) as push):
            test.assertTrue(nlq_codes.try_handle_codes_nlq(q, **kw))
            test.assertEqual(read.call_args.kwargs["hnm_kw"], "합성")
            test.assertEqual(push.call_args.kwargs["action"], title)
    elif seq == 293:
        with (patch("app.services.rddbc021_service.search_road_address", return_value=pd.DataFrame()) as read,
              patch("app.ui.chat_middleware.push_sims_result_to_chat", return_value=True) as push):
            test.assertTrue(nlq_router._try_handle_road_address_nlq(q, **kw))
            test.assertEqual(read.call_args.kwargs["keyword"], "합성로")
            push.assert_called_once()
            test.assertEqual(title, "도로명주소 조회")
    elif seq == 294:
        with (patch.object(nlq_goods, "get_goods_detail_full", return_value=pd.DataFrame({"제품코드": ["00001"]})) as read,
              patch.object(nlq_goods, "_push_goods_result", return_value=True) as push):
            test.assertTrue(nlq_goods.try_handle_goods_nlq(q, **kw))
            read.assert_called_once_with(physic_cd="00001")
            test.assertEqual(push.call_args.kwargs["action"], title + " (00001)")
    else:
        raise ValueError(f"등록되지 않은 추가 사례: {seq}")
    return "마스터 핸들러·합성 서비스·결과/안내"


def coverage(legacy, additions):
    from app.sims.nlq.action_inventory import implemented_actions
    from app.services.kpis_nlq import ALIASES
    alias = {"제품코드목록": "제품코드 목록", "사용자조회": "사용자목록 + 부서명", "업무코드 조회": "그룹코드조회"}
    old = Counter("Current Table" if r["query_kind"] == "current_table_followup" else
                  alias.get(r["actual_action"], r["actual_action"]) for r in legacy)
    new = Counter("Current Table" if r["질문"].startswith("현재표") else r["기준 기능 (action)"] for r in additions)
    required = {s.canonical_action for s in implemented_actions()}
    # R030's dedicated detail card is panel-only in practice; record rather
    # than count a list delivery as a successful independent detail action.
    missing = required - (old.keys() | new.keys())
    if missing != {"거래처 상세"} or set(ALIASES) != set(KPIS_TITLES) or set(KPIS_TITLES.values()) - new.keys():
        raise ValueError(f"업무별 범위 재검토 필요: {sorted(missing)}")
    return old, new


def verify_row(row):
    from tools.check_payment_chat_runtime import Runtime, fixture
    from tools.check_payment_basic_query import AMOUNT
    from app.ui.current_table_followups.action_dispatcher import handle_current_table_followup_by_action

    seq = int(row["순번"])
    q, title = row["질문"], row["기준 기능 (action)"]
    rt = Runtime()
    try:
        rt.setUp()
        h = rt.h
        source = fixture()
        source["입금/출금 구분"] = ["입금", "출금", "출금"]
        h.sql.return_value = source
        if preceding := row.get("선행질문"):
            rt.submit(preceding)
            rt.assertEqual(h.sql.call_count, 1)
        before = len(h.room["messages"])
        h.sql.reset_mock()
        if seq == 270:
            s = h.state["__sims_current_table_source_key"]
            rt.assertNotEqual(s, h.state["__sims_last_table_key"])
            df = h.state["sims_export_tables"][s]
            rt.assertEqual(df["금액"].tolist(), [AMOUNT, -AMOUNT, None])
            push, notice = Mock(return_value=True), Mock(return_value=True)
            handle_current_table_followup_by_action(
                df=df, query=q, top_n=10, table_key=s, source_action=title,
                helpers={"push_table": push, "push_notice": notice}, log=None,
                source_meta=h.state["__sims_export_table_provenance_by_key"][s],
            )
            push.assert_called_once()
            rt.assertEqual(push.call_args.kwargs["source_rows"], len(df))
            notice.assert_not_called()
            h.sql.assert_not_called()
            return "상세 원본 → 현재표 Dispatcher; 추가 조회 0"
        if seq in (271, 272):
            from pandas import DataFrame
            mod, day = ("rddbc110", "Rd11_In_YyMmDd") if seq == 271 else ("rddbc120", "Rd12_Out_YyMmDd")
            with patch(f"app.services.{mod}_service.query_to_df", return_value=DataFrame({"식별자": ["001"]})) as db:
                rt.submit(q)
            for call in db.call_args_list:
                sql, p = call.args
                rt.assertIn(day, sql)
                rt.assertEqual((p["date_from"], p["date_to"]), ("20261119", "20261119"))
                rt.assertFalse(p.get("nlq_unlabeled_name"))
            rt.assertGreater(db.call_count, 0)
        else:
            if seq in SOURCE:
                table = {"입출금 조회": "310", "어음 조회": "320", "담보 조회": "330"}[title]
                h.sql.return_value = source if table == "310" else fixture(table)
            rt.submit(q)
            if seq in (273, 274):
                rt.assertEqual(title, "일반 질문" if seq == 273 else "웹 검색")
                rt.assertTrue(h.state["__queue_ai"])
                rt.assertEqual(rt.env["web_search_route"] is not None, title == "웹 검색")
                h.sql.assert_not_called()
                return "일반/웹 큐 판별; 금융 본조회 0"
            if seq == 275:
                h.sql.assert_not_called()
            else:
                day, binds, where = SOURCE[seq]
                h.sql.assert_called_once()
                sql, params = h.sql.call_args.args
                rt.assertEqual(params, binds)
                rt.assertIn(f"S.[{day}] BETWEEN ? AND ?", sql)
                rt.assertIn(where, sql.rsplit(" WHERE ", 1)[-1])
                if seq == 269:
                    s, r = (h.state[k] for k in ("__sims_current_table_source_key", "__sims_last_table_key"))
                    rt.assertNotEqual(s, r)
                    grouped = h.state["sims_export_tables"][r]
                    rt.assertEqual(grouped["입금금액"].tolist(), [AMOUNT])
                    rt.assertEqual(grouped["출금금액"].tolist(), [-AMOUNT])
        rt.assertEqual(len(h.room["messages"]), before + 2)
        rt.assertEqual(h.room["messages"][-1]["action"], title)
        return "채팅 제출·바인딩/안내·카드"
    finally:
        rt.doCleanups()


def main():
    from tools.audit_nlq_excel_intent import _read_rows
    from tools.check_nlq_official_206_regression import run

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--casebook", type=Path, default=BOOK)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = _read_rows(args.casebook)
    if [int(r["순번"]) for r in rows] != list(range(1, 315)):
        raise ValueError("Expected consecutive official case IDs 0001..0314")
    import pyodbc

    with (patch.object(pyodbc, "connect", side_effect=AssertionError("offline")),
          patch.object(socket.socket, "connect", side_effect=AssertionError("offline")),
          patch.object(socket, "create_connection", side_effect=AssertionError("offline"))):
        legacy = run(args.casebook, last_id=249)["cases"]
        coverage(legacy, rows[249:])
        result = [(r["case_id"], r["result"], "; ".join(r["reasons"]) or "기존 공식 감사") for r in legacy]
        for row in rows[249:]:
            try:
                seq = int(row["순번"])
                status, detail = "PASS", (verify_row(row) if seq <= 277 else
                                         verify_business(seq, row["질문"], row["기준 기능 (action)"]))
            except Exception as exc:
                status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
            result.append((f"NLQ-{int(row['순번']):04d}", status, detail))
    if args.output:
        with args.output.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(("ID", "result", "evidence"))
            writer.writerows(result)
    for case, status, detail in result:
        if status != "PASS":
            print(case, status, detail)
    passed = sum(status == "PASS" for _, status, _ in result)
    print(f"official: {passed}/{len(result)} PASS")
    return int(passed != len(rows))


if __name__ == "__main__":
    raise SystemExit(main())
