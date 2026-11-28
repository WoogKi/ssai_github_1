"""Offline calendar, entity and chat/SQL parity oracles for period-aware ERP actions."""
from datetime import date
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import io_nlq as io
from tools import check_payment_chat_runtime as chat

DAY = date(2026, 11, 28)
PERIODS = (
    ("오늘|당일|하루|최근 1 일", "20261128", "20261128"),
    ("어제", "20261127", "20261127"), ("그저께", "20261126", "20261126"),
    ("이번주|이번 주간", "20261123", "20261128"), ("지난주|지난 주간", "20261116", "20261122"),
    ("지난 목요일", "20261126", "20261126"), ("지난주 목요일|지난 주 목요일", "20261119", "20261119"),
    ("이달|이번달|이번 월|11월|2026 11월", "20261101", "20261130"),
    ("지난 달만|지난월", "20261001", "20261031"), ("다음달|다음 월", "20261201", "20261231"),
    ("최근 30일|최근 30 일간", "20261030", "20261128"),
    ("20240229|2024.2.29|2024/02/29|2024-02-29|2024년 2월 29일", "20240229", "20240229"),
    ("20240229~20240301|20240229-20240301|2024.2.29에서 2024/3/1까지|2024년 2월 29일부터 2024년 3월 1일까지", "20240229", "20240301"),
    ("20261101 이후|2026-11-01부터", "20261101", "20261128"),
    ("202402|2024년 2월|2024-2|2024.02|2024/02", "20240201", "20240229"),
    ("202401~202402|202401-202402|2024년 1월부터 2024년 2월까지|2024-01 to 2024-02", "20240101", "20240229"),
    ("2024|2024년", "20240101", "20241231"), ("2023~2024|2023년부터 2024년까지", "20230101", "20241231"),
    ("20261025~99991231", "20261025", "99991231"), ("202610~900012", "20261001", "90001231"),
)


class SharedPeriod(unittest.TestCase):
    def test_periods(self):
        cases = 0
        for action in ("입고현황", "출고현황", "거래명세서", "세금계산서", "입출금", "어음", "담보"):
            financial = action in ("입출금", "어음", "담보")
            rolling = ("한달|최근 한 달간|최근 1 개월", "20261028" if financial else "20261029", "20261128")
            for forms, start, end in (*PERIODS, rolling):
                for term in forms.split("|"):
                    for q in (f"{term} {action} 조회", f"{action} 조회 {term}"):
                        with self.subTest(q=q):
                            plan = io.resolve_io_nlq(q, today=DAY)
                            p = plan["params"]
                            self.assertEqual((p.get("date_from"), p.get("date_to")), (start, end))
                            if not financial:
                                self.assertFalse(io._extract_unlabeled_entity_phrase(q, plan["action"]))
                                self.assertFalse(io.has_unconsumed_detail_condition(q, plan["action"], p))
                            cases += 1
        print("PERIOD_ACTION_CASES", cases)

    def test_other_roles(self):
        for action in ("제품재고장", "제품수불현황", "발주조회", "입고예정조회", "실재고월집계", "장부재고월집계"):
            with self.subTest(action=action):
                natural, absolute = (io.resolve_io_nlq(f"{action} {expr}", today=DAY)
                                     for expr in ("지난주 목요일", "20261119"))
                self.assertEqual(natural["action"], absolute["action"])
                for key in ("date_from", "date_to", "month_from", "month_to", "source_path"):
                    self.assertEqual(natural["params"].get(key), absolute["params"].get(key))

    def test_literal_and_compound(self):
        for label, key in (("거래처명", "ven_nm"), ("제품명", "physic_nm"), ("제조사명", "maker_nm"),
                           ("영업사원명", "sales_man_nm")):
            for value in ("지난주약국", "가상20261118상사", "'지난주 목요일'", '"20261118"'):
                q = f"지난주 목요일 출고현황 {label} {value}"
                with self.subTest(q=q):
                    p = io.resolve_io_nlq(q, today=DAY)["params"]
                    self.assertEqual((p.get("date_from"), p.get("date_to")), ("20261119", "20261119"))
                    self.assertEqual(p.get(key), value.strip("'").strip('"'))
                    self.assertFalse(io.has_unconsumed_detail_condition(q, "출고명세 조회", p))

    def test_registered_price_roles(self):
        def parse(q):
            return io.resolve_io_nlq(q, today=DAY)["params"]
        for term in ("지난주 목요일", "20261119"):
            self.assertEqual(parse(f"최종 계약단가 조회 기준일 {term}")["as_of"], "20261119")
            p = parse(f"계약단가 이력 조회 계약시작일 {term}")
            self.assertEqual((p["date_from"], p["date_to"]), ("20261119", "20261119"))
        p = parse("지난주 목요일 계약단가 이력 조회")
        self.assertNotIn("_registered_unlabeled_entity", p)
        self.assertEqual(p["date_from"], "20261119")
        self.assertTrue(parse("지난주 목요일 최종 매입단가 조회")["_unsupported_period_role"])
        self.assertTrue(parse("최종 계약단가 조회 기준일 지난주")["_unsupported_period_role"])
        self.assertNotIn("_unsupported_period_role", parse("최종 매입단가 조회 제품명 지난주약국"))

    def test_forecast_horizon_is_not_observation_period(self):
        self.assertFalse(io.extract_nlq_natural_period("사원별 다음달 매출 예상", today=DAY))
        p = io.extract_nlq_natural_period("지난주 목요일 사원별 다음달 매출 예상", today=DAY)
        self.assertEqual((p["date_from"], p["date_to"]), ("20261119", "20261119"))

    def test_chat_to_sql_and_card(self):
        rt = chat.Runtime()
        rt.setUp()
        self.addCleanup(rt.doCleanups)
        for action, mod, field in (("입고현황", "rddbc110", "Rd11_In_YyMmDd"),
                                   ("출고현황", "rddbc120", "Rd12_Out_YyMmDd")):
            for extra in ("", " 거래처명 가상"):
                seen = []
                with patch(f"app.services.{mod}_service.query_to_df") as db:
                    def read(sql, params):
                        if mod == "rddbc110":
                            self.assertIn(f"{field} = %(date_from)s", sql)
                        else:
                            self.assertIn(f"{field} >= %(date_from)s", sql)
                            self.assertIn(f"{field} <= %(date_to)s", sql)
                        self.assertEqual((params["date_from"], params["date_to"]), ("20261119", "20261119"))
                        self.assertFalse(params.get("nlq_unlabeled_name"))
                        self.assertNotIn("%(nlq_unlabeled_name_like)s", sql)
                        if extra:
                            self.assertIn("LIKE %(ven_nm_like)s", sql)
                            self.assertEqual(params["ven_nm_like"], "%가상%")
                        rows = [("001", "20261119", "가상약국", 123), ("002", "20261119", "목요일상사", -1),
                                ("003", "20261118", "가상약국", 999)]
                        return pd.DataFrame([(k, amount) for k, day, vendor, amount in rows
                                             if params["date_from"] <= day <= params["date_to"]
                                             and (params.get("ven_nm") or "") in vendor], columns=["식별자", "금액"])
                    db.side_effect = read
                    for q in (f"지난주 목요일 {action}{extra}", f"{action} 20261119{extra}"):
                        db.reset_mock()
                        start = len(rt.h.room["messages"])
                        rt.submit(q)
                        self.assertGreater(db.call_count, 0, q)
                        self.assertEqual(len(rt.h.room["messages"]), start + 2)
                        card = rt.h.room["messages"][-1]
                        self.assertEqual(card["action"], action[:2] + "명세 조회")
                        frame = rt.h.state["sims_export_tables"][rt.h.state["__sims_last_table_key"]]
                        self.assertEqual(frame["식별자"].tolist(), ["001"] if extra else ["001", "002"])
                        seen.append(([(c.args[0], {k: v for k,v in c.args[1].items() if k in
                                      ("date_from", "date_to", "ven_nm", "nlq_unlabeled_name", "top")})
                                      for c in db.call_args_list], frame["금액"].tolist()))
                    self.assertEqual(seen[0], seen[1])


if __name__ == "__main__":
    unittest.main()
