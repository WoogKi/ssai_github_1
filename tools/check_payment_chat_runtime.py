"""Exercise the actual Streamlit submission block without a server, DB or LLM."""

from __future__ import annotations
import ast
import builtins
import csv
import logging
import os
import re
import sys
import unittest
import uuid
from contextlib import ExitStack
from datetime import datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SourceQuery = import_module("tools.check_payment_source_nlq").SourceQuery
basic = import_module("tools.check_payment_basic_query")
SERVICES, common, fixture = basic.SERVICES, basic.common, basic.fixture
chat = import_module("app.ui.chat_middleware")

READ = common.execute_bound_select
MAIN = ROOT / "app/Lmstudio_SSAI_chat_main.py"
MODULE = ast.parse(MAIN.read_text("utf-8-sig"))
SUBMIT = next(
    n
    for n in MODULE.body
    if isinstance(n, ast.If)
    and ast.unparse(n.test) == "user_input and user_input.strip()"
)


class Rerun(BaseException):
    pass


class MainNamespace(dict):
    """Load main's real functions/imports lazily, without its startup/login side effects."""

    def __missing__(self, name):
        if hasattr(builtins, name):
            return getattr(builtins, name)
        for n in MODULE.body:
            if isinstance(n, ast.Import):
                for a in n.names:
                    if (a.asname or a.name) == name:
                        self[name] = import_module(a.name)
                        return self[name]
            if isinstance(n, ast.ImportFrom) and n.module:
                for a in n.names:
                    if (a.asname or a.name) == name:
                        self[name] = getattr(import_module(n.module), a.name)
                        return self[name]
            if (isinstance(n, ast.FunctionDef) and n.name == name) or (
                isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
            ):
                exec(compile(ast.Module([n], []), str(MAIN), "exec"), self)
                return self[name]
        raise KeyError(name)


class Runtime(unittest.TestCase):
    verified = []

    @classmethod
    def tearDownClass(cls):
        if out := os.getenv("PAYMENT_RUNTIME_RESULTS"):
            with open(out, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.writer(stream)
                writer.writerow(("case", "check"))
                writer.writerows(cls.verified)

    def setUp(self):
        self.h = SourceQuery()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.enterContext(
            patch(
                "app.services.datetime_tool.operating_now",
                return_value=datetime(2026, 11, 28),
            )
        )
        self.env = MainNamespace(
            __name__="offline_chat_submit",
            st=SimpleNamespace(session_state=self.h.state, rerun=self.rerun),
            log=logging.getLogger("offline"),
            current_room=self.h.room,
            uuid=uuid,
            re=re,
            make_ts=lambda: "offline",
            _next_seq=lambda: 1,
            _message_meta=lambda _: {},
            save_chat_rooms=lambda: None,
            _sync_room_meta=lambda *a, **k: None,
            _chat_log_kv=lambda r: "",
            auto_user_input=False,
            pending_text_event_id=None,
        )
        self.body = compile(ast.Module([SUBMIT], []), str(MAIN), "exec")

    @staticmethod
    def rerun():
        raise Rerun

    def submit(self, text):
        self.env["user_input"] = text
        try:
            exec(self.body, self.env)
        except Rerun:
            pass
        chat.drain_inbox_to_chat(self.h.room)

    def test_submit_dates_spacing_and_source(self):
        tested = set()
        for domain, table in (
            ("입출금", "310"),
            ("입금", "310"),
            ("출금", "310"),
            ("어음", "320"),
            ("담보", "330"),
        ):
            self.h.sql.return_value = fixture(table)
            for gap in ("", " "):
                for period, start, end in (
                    ("", "20261028", "20261128"),
                    ("최근 한 달", "20261028", "20261128"),
                    ("최근한달", "20261028", "20261128"),
                    ("최근 30일", "20261030", "20261128"),
                    ("최근30일", "20261030", "20261128"),
                    ("지난달", "20261001", "20261031"),
                    ("다음 달", "20261201", "20261231"),
                    ("오늘", "20261128", "20261128"),
                ):
                    for q in dict.fromkeys(
                        (
                            f"{period} {domain}{gap}조회".strip(),
                            f"{domain}{gap}조회 {period}".strip(),
                            f"{period} {domain}".strip(),
                        )
                    ):
                        if q in tested:
                            continue
                        tested.add(q)
                        with self.subTest(q=q):
                            before = self.h.sql.call_count
                            messages = len(self.h.room["messages"])
                            self.submit(q)
                            self.assertFalse(self.h.state["__queue_ai"])
                            self.assertEqual(len(self.h.room["messages"]), messages + 2)
                            self.assertEqual(self.h.sql.call_count, before + 1)
                            self.assertEqual(
                                self.h.sql.call_args.args[1][:2], (start, end)
                            )
                            self.assertEqual(
                                self.h.room["messages"][-1]["action"],
                                SERVICES[table].SPEC["title"],
                            )
                            self.verified.append(
                                (q, f"main submit→게시 1회; {start}..{end}")
                            )
        for name, table in (("어음", "320"), ("담보", "330")):
            self.h.sql.return_value = fixture(table)
            for q in (
                f"다음달 만기 {name} 조회",
                f"다음달 {name}조회 만기일",
                f"{name} 만기일 기준 다음달",
            ):
                with self.subTest(q=q):
                    before = self.h.sql.call_count
                    self.submit(q)
                    self.assertEqual(self.h.sql.call_count, before + 1)
                    self.assertEqual(
                        self.h.sql.call_args.args[1], ("20261201", "20261231")
                    )
                    self.assertIn(
                        f"S.[Rd{table[:2]}_Maturit_YyMmDd] BETWEEN",
                        self.h.sql.call_args.args[0],
                    )
                    self.assertEqual(
                        self.h.room["messages"][-1]["action"],
                        SERVICES[table].SPEC["title"],
                    )
                    tested.add(q)
                    self.verified.append((q, "만기일 12월 바인딩·게시"))
        print("MAIN_VARIANTS", len(tested))

    def test_web_and_general_remain_external(self):
        for question, web in (
            ("최근 어음 시장 뉴스", True),
            ("담보대출이란 무엇인가", False),
            ("다음달 만가어음", False),
        ):
            self.submit(question)
            self.assertTrue(self.h.state["__queue_ai"])
            self.assertEqual(self.env["web_search_route"] is not None, web)
            self.h.sql.assert_not_called()
            self.verified.append((question, "외부 웹 또는 일반 큐; 금융 본조회 0"))

    def test_shared_calendar_from_chat(self):
        # Oracle dates are literal: neither the parser nor its calendar helpers
        # generate the expected bindings. Each row goes through the real submit block.
        cases = (
            ("오늘|당일|하루|최근 1 일", "20261128", "20261128"),
            ("어제", "20261127", "20261127"), ("그저께", "20261126", "20261126"),
            ("지난 목요일", "20261126", "20261126"), ("지난주 목요일", "20261119", "20261119"),
            ("지난 토요일", "20261121", "20261121"),
            ("지난주|지난 주간", "20261116", "20261122"), ("이번주|이번 주간", "20261123", "20261128"),
            ("이번 달|이번월|이달|11 월|2026 11월", "20261101", "20261130"),
            ("지난월|지난 달만", "20261001", "20261031"), ("다음 달", "20261201", "20261231"),
            ("한달|최근 한 달간|최근 1 개월", "20261028", "20261128"),
            ("최근 30 일간", "20261030", "20261128"),
            ("20240229|2024.2.29|2024/02/29|2024-02-29|2024년 2월 29일", "20240229", "20240229"),
            ("20240229~20240301|20240229-20240301|2024.2.29에서 2024/3/1까지|2024년 2월 29일부터 2024년 3월 1일까지", "20240229", "20240301"),
            ("20261101 이후|2026-11-01부터", "20261101", "20261128"),
            ("202402|2024년 2월|2024-2|2024.02|2024/02", "20240201", "20240229"),
            ("202401~202402|202401-202402|2024년 1월부터 2024년 2월까지|2024-01 to 2024-02", "20240101", "20240229"),
            ("2024|2024년", "20240101", "20241231"),
            ("2023~2024|2023년부터 2024년까지", "20230101", "20241231"),
            ("20261025~99991231", "20261025", "99991231"),
            ("202610~900012", "20261001", "90001231"),
        )
        for name, table, label, role in (("입출금","310","전표일","Slip_YyMmDd"),
                                         ("어음","320","만기일","Maturit_YyMmDd"),
                                         ("담보","330","발행일","Draft_YyMmDd")):
            self.h.sql.return_value = fixture(table)
            for forms, start, end in cases:
                for term in forms.split("|"):
                    for text in (f"{name} {label} {term}", f"{term} {name}조회 {label}", f"{label} 기준 {term}{name}조회"):
                        with self.subTest(text=text):
                            before, messages = self.h.sql.call_count, len(self.h.room["messages"])
                            self.submit(text)
                            self.assertEqual(self.h.sql.call_count, before + 1)
                            self.assertEqual(self.h.sql.call_args.args[1], (start, end))
                            self.assertIn(f"S.[Rd{table[:2]}_{role}] BETWEEN", self.h.sql.call_args.args[0])
                            self.assertEqual(len(self.h.room["messages"]), messages + 2)
                            self.assertEqual(self.h.room["messages"][-1]["action"], name + " 조회")
                            self.verified.append((text, f"{role}: {start}..{end}; 조회/게시 1회"))

    def test_calendar_boundaries_and_residuals(self):
        from app.services.io_nlq import extract_nlq_natural_period, nlq_period_to_date_range
        from app.services.payment_nlq import parse_payment

        for day, phrase, start, end in (
            ("2024-03-01","그저께","20240228","20240228"),
            ("2025-01-01","이번주","20241230","20250101"),
            ("2025-01-01","지난주","20241223","20241229"),
            ("2026-11-30","이번주","20261130","20261130"),
            ("2026-11-29","이번주","20261123","20261129"),
            ("2025-01-01","지난달","20241201","20241231"),
            ("2024-03-31","최근 한 달","20240229","20240331"),
            ("2025-03-31","최근 한 달","20250228","20250331"),
            ("2024-03-31","최근 30일","20240302","20240331"),
        ):
            ref = datetime.fromisoformat(day).date()
            for table, name in (("310","입출금"),("320","어음"),("330","담보")):
                args = parse_payment(name+" "+phrase, table, today=ref)["params"]
                self.assertEqual((args["date_from"], args["date_to"]), (start, end))
            if phrase != "최근 30일":
                args = nlq_period_to_date_range(extract_nlq_natural_period(phrase, today=ref))
                self.assertEqual((args["date_from"],args["date_to"]),
                                 ("20240301" if day == "2024-03-31" else "20250301",end)
                                 if phrase == "최근 한 달" else (start,end))
        self.h.sql.return_value = fixture("310")
        for q, start, end in (("그저께 입출금 조회", "20261126", "20261126"),
                              ("지난 목요일 입출금 조회", "20261126", "20261126"),
                              ("지난주 입출금 조회", "20261116", "20261122"),
                              ("이번주 입출금 조회", "20261123", "20261128")):
            n, messages = self.h.sql.call_count, len(self.h.room["messages"])
            self.submit(q)
            sql, binds = self.h.sql.call_args.args
            self.assertEqual(self.h.sql.call_count, n + 1)
            self.assertEqual(len(self.h.room["messages"]), messages + 2)
            self.assertEqual(self.h.room["messages"][-1]["action"], "입출금 조회")
            self.assertIn("S.[Rd31_Receive_YyMmDd] BETWEEN", sql)
            self.assertEqual(binds, (start, end))
            self.verified.append((q, f"1호기 원문: {start}..{end}; 입출금일자·조회/게시 1회"))
        for q, start, end in (("입출금 전표일 이번주 현재 담당자 가상갑", "20261123","20261128"),
                              ("지난주로 다시 조회", "20261116","20261122")):
            self.submit(q)
            sql, args = self.h.sql.call_args.args
            self.assertIn("S.[Rd31_Slip_YyMmDd] BETWEEN", sql)
            self.assertEqual(args, (start, end, "%가상갑%"))
        for term in ("20250229", "2026-13", "20260431부터 20260501까지",
                     "20261101~20261031", "지난주 또는 이번주", "어제 미등록조건", "지난주약국", "4321"):
            n = self.h.sql.call_count
            self.submit("입출금 "+term)
            self.assertEqual(self.h.sql.call_count, n, term)
            self.assertEqual(self.h.room["messages"][-1]["action"], "조회 안내")
        self.submit("입출금 거래처명 '지난주 입출금' 입출금구분코드 5C")
        self.assertEqual(self.h.sql.call_args.args[1], ("20261028", "20261128", "5C", "%지난주 입출금%"))
        n = self.h.sql.call_count
        self.submit("그저께 입출근 조회")
        self.assertTrue(self.h.state["__queue_ai"])
        self.assertEqual(self.h.sql.call_count, n)

    def test_staff_roles_and_incident_period(self):
        raw = fixture("310")
        raw["입금/출금 구분"] = ["입금", "출금", "입금"]
        raw["거래처 담당자명"] = ["가상갑", "가상을", "가상을"]
        raw["거래처 담당자코드"] = ["00001", "00002", "00002"]
        raw["원본 담당영업사원명"] = ["가상을", "가상갑", "가상갑"]
        raw["Rd31_Real_Sales_Man"] = ["00002", "00001", "00001"]
        for label, key, index in (
            ("담당자", "OwnerStaff.Rd06_User_Nm", 0),
            ("거래처담당자", "OwnerStaff.Rd06_User_Nm", 0),
            ("현재 담당자명", "OwnerStaff.Rd06_User_Nm", 0),
            ("당시 담당자", "J7.Rd06_User_Nm", 1),
            ("당시 담당영업사원", "J7.Rd06_User_Nm", 1),
        ):
            question = f"입출금 {label} 가상갑"
            before = len(self.h.room["messages"])
            self.h.sql.return_value = raw.iloc[[index]] if index == 0 else raw.iloc[[1, 2]]
            self.submit(question)
            sql, binds = self.h.sql.call_args.args
            self.assertIn(key + " LIKE ?", sql.partition(" WHERE ")[2])
            self.assertEqual(binds[-1], "%가상갑%")
            self.assertEqual(len(self.h.room["messages"]), before + 2)
            exported = self.h.state["sims_export_tables"][self.h.state["__sims_last_table_key"]]
            self.assertEqual(exported["입출금순번"].tolist(), [0] if index == 0 else [1, 2])
            self.verified.append((question, "역할별 SQL 바인딩·게시·전체 PK"))
        for label, col, expected in (("현재 담당자", "거래처 담당자명", 2), ("당시 담당자", "원본 담당영업사원명", 1)):
            self.h.sql.return_value = raw
            question = f"입출금 {label}별 건수 집계"
            self.submit(question)
            grouped = self.h.state["sims_export_tables"][self.h.state["__sims_last_table_key"]]
            self.assertEqual(len(grouped), 2)
            counts = [c for c in grouped if c.endswith("건수")]
            self.assertTrue(counts)
            self.assertEqual(sum(grouped.loc[grouped[col] == "가상을", counts].iloc[0]), expected)
            self.verified.append((question, "집계 게시·다운로드 전체 그룹"))
        for question, table, role, start, end in (
            ("최근 한달간 입출금", "310", "Rd31_Receive_YyMmDd", "20261028", "20261128"),
            ("다음달 만기어음조회", "320", "Rd32_Maturit_YyMmDd", "20261201", "20261231"),
            ("거래처별 월별 입출금 집계", "310", "Rd31_Receive_YyMmDd", "20261028", "20261128"),
        ):
            self.h.sql.return_value = fixture(table)
            before = self.h.sql.call_count
            self.submit(question)
            self.assertEqual(self.h.sql.call_count, before + 1)
            self.assertIn(f"S.[{role}] BETWEEN", self.h.sql.call_args.args[0])
            self.assertEqual(self.h.sql.call_args.args[1], (start, end))
            self.assertEqual(self.h.room["messages"][-1]["action"], SERVICES[table].SPEC["title"])
            self.verified.append((question, "선택일자·본조회 1회·게시"))
        before = self.h.sql.call_count
        self.submit("입출금 담당자명")
        self.assertEqual(self.h.sql.call_count, before)
        self.assertIn("거래처 담당자명 조건", str(self.h.room["messages"][-1]["content"]))

    def test_driver_schema_and_redacted_error(self):
        import pyodbc

        spec = SERVICES["310"].SPEC
        raw = fixture("310")
        extension_columns = {"Rd31_Move_Seq", "Rd31_Move_Date", "Rd31_IN_SQ"}
        for missing in (extension_columns, set()):
            db = MagicMock()
            cursor = db.cursor.return_value
            cols = [spec["pk"][0], *sorted(extension_columns - missing)]

            def run(sql, *binds):
                if sql.startswith("SELECT name FROM sys.columns"):
                    cursor.fetchall.return_value = [(k,) for k in cols]
                else:
                    if any(f"S.[{k}]" in sql for k in missing):
                        raise pyodbc.ProgrammingError(
                            "42S22", "Invalid column (207) (SQLExecDirectW)"
                        )
                    records = raw.copy()
                    for k in missing:
                        records[k] = None
                    cursor.fetchall.return_value = list(
                        records.itertuples(index=False, name=None)
                    )

            cursor.execute.side_effect = run
            cursor.description = [(k,) for k in raw.columns]
            with (
                patch.object(common, "execute_bound_select", READ),
                patch(
                    "app.services.ssai_auth_service.connect_company_db", return_value=db
                ),
            ):
                self.submit("입출금조회")
            sql = cursor.execute.call_args.args[0]
            self.assertEqual(cursor.execute.call_count, 2)
            for k in spec["optional_output"]:
                self.assertEqual(f"S.[{k}]" in sql, k not in missing)
            self.assertEqual(db.timeout, 10)
            msg = self.h.room["messages"][-1]
            self.assertEqual(msg["action"], spec["title"])
            self.assertEqual(msg["meta"]["source_call_count"], 1)
            self.assertEqual(
                "회사 ERP 미제공 열" in msg["meta"]["query_summary"], bool(missing)
            )
        cursor = MagicMock()
        cursor.execute.side_effect = pyodbc.ProgrammingError(
            "42S22", "[secret] invalid column (207) (SQLExecDirectW)"
        )
        db.cursor.return_value = cursor
        with ExitStack() as s:
            s.enter_context(patch.object(common, "execute_bound_select", READ))
            s.enter_context(
                patch(
                    "app.services.ssai_auth_service.connect_company_db", return_value=db
                )
            )
            logs = s.enter_context(self.assertLogs("ssai", level="INFO"))
            self.submit("입출금 거래처명 비공개")
        traces = "\n".join(line for line in logs.output if "[nlq.trace." in line)
        for part in (
            "stage=source_schema",
            "ProgrammingError",
            "sql_error_number=207",
            "source_call_count=0",
            "sqlstate=42S22",
        ):
            self.assertIn(part, traces)
        for part in ("비공개", "secret"):
            self.assertNotIn(part, traces)


if __name__ == "__main__":
    unittest.main(defaultTest="Runtime")
