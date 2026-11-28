"""Exercise source query, derived card and explicit revision with synthetic ERP rows."""

from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import logging
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.check_payment_basic_query import AMOUNT, SCOPE, common, fixture
from app.sims.nlq import nlq_router as router
from app.ui import chat_middleware as chat


class SourceQuery(unittest.TestCase):
    def setUp(self):
        self.submissions = []
        from tools.check_kpis_erp_direct import DirectChecks

        context = DirectChecks()
        context.setUp()
        self.addCleanup(context.doCleanups)
        self.state = context.state
        self.room = dict(
            id="synthetic-source", user_id="42", company_id=8, messages=[], history=[]
        )
        self.state.update(current_room=self.room["id"], chat_rooms=[self.room])
        stack = self.enterContext(ExitStack())
        for target in (
            "pyodbc.connect",
            "socket.socket.connect",
            "socket.create_connection",
        ):
            stack.enter_context(patch(target, side_effect=AssertionError("offline")))
        stack.enter_context(
            patch(
                "app.services.datetime_tool.operating_now",
                return_value=datetime(2026, 11, 27),
            )
        )
        stack.enter_context(patch.object(chat, "_append_finalized_nlq_case"))
        stack.enter_context(
            patch.object(
                chat,
                "maybe_create_structured_presentation",
                return_value={"status": "skipped"},
            )
        )
        self.auth = stack.enter_context(
            patch.object(common, "query_scope", return_value=SCOPE)
        )
        frame = fixture()
        frame["입금/출금 구분"] = ["입금", "출금", "출금"]
        self.sql = stack.enter_context(
            patch.object(common, "execute_bound_select", return_value=frame)
        )

    def submit(self, question):
        self.submissions.append(question)
        candidate = router.resolve_new_sims_nlq_candidate(
            question, session_state=self.state, room=self.room
        )
        self.assertIsNotNone(candidate, question)
        self.assertTrue(
            router.try_handle_nlq(
                question,
                self.room,
                self.state,
                None,
                None,
                logging.getLogger("offline"),
            )
        )
        chat.drain_inbox_to_chat(self.room)
        return self.room["messages"][-1]

    def test_split_and_revision(self):
        self.submit("거래처별 월별 입출금 집계")
        self.assertEqual(self.sql.call_count, 1)
        self.assertEqual(len(self.room["messages"]), 1)
        s = self.state["__sims_current_table_source_key"]
        r = self.state["__sims_last_table_key"]
        self.assertNotEqual(s, r)
        stores = self.state["sims_export_tables"]
        self.assertEqual(stores[s]["금액"].tolist(), [AMOUNT, -AMOUNT, None])
        self.assertEqual(stores[r]["입금금액"].tolist(), [AMOUNT])
        self.assertEqual(stores[r]["출금금액"].tolist(), [-AMOUNT])
        for i, (q, binds) in enumerate(
            (
                ("지난달로 다시 조회", ("20261001", "20261031")),
                ("거래처 삼일로 변경", ("20261001", "20261031", "%삼일%")),
                ("출금만 보여줘", ("20261001", "20261031", "%삼일%", "50")),
            ),
            2,
        ):
            self.submit(q)
            self.assertEqual(self.sql.call_count, i)
            self.assertEqual(self.sql.call_args.args[1], binds)
        self.sql.return_value = fixture("320")
        self.submit("어음 조회")
        self.assertEqual(self.sql.call_args.args[1], ("20261027", "20261127"))

    def test_invalid_and_denied(self):
        for query in (
            "어음 조회 금액",
            "입금 금액 23원 또는 은행명 예시",
            "입금 입출금구분코드 5C",
        ):
            self.submit(query)
        self.sql.assert_not_called()
        self.auth.side_effect = PermissionError()
        self.submit("담보")
        self.sql.assert_not_called()
        self.assertFalse(self.state.get("sims_export_tables"))

    def test_permission_change_after_read(self):
        self.auth.side_effect = [SCOPE, SCOPE, PermissionError()]
        self.submit("입금")
        self.sql.assert_called_once()
        self.assertFalse(self.state.get("sims_export_tables"))

    def test_detail_rank_keeps_original_rows(self):
        self.submit("입출금 출금금액 TOP 2")
        r = self.state["sims_export_tables"][self.state["__sims_last_table_key"]]
        self.assertIn("입출금순번", r)
        self.assertEqual(r["금액"].tolist(), [-AMOUNT])

    def scenario(self, case):
        from app.ui.current_table_followups.action_dispatcher import (
            handle_current_table_followup_by_action,
        )
        from unittest.mock import Mock

        if case in ("T0448", "T0449", "T0451", "T0463"):
            self.sql.return_value = fixture("320")
            self.submit("어음 은행명 기존가상 지난달")
            if case == "T0451":
                self.auth.side_effect = PermissionError()
            q = {
                "T0448": "어음 같은 조건으로 다시 조회",
                "T0449": "어음 같은 조건으로 은행명 새가상으로 변경해서 다시 조회",
                "T0451": "어음 같은 조건으로 다시 조회",
                "T0463": "어음 지난달만",
            }[case]
            self.submit(q)
            self.assertEqual(self.sql.call_count, 1 if case == "T0451" else 2)
            expected = ("20261001", "20261031")
            if case != "T0463":
                expected += ("%새가상%" if case == "T0449" else "%기존가상%",)
            self.assertEqual(self.sql.call_args.args[1], expected)
        elif case == "T0450":
            self.submit("입출금")
            self.state["current_room"] = "B"
            self.assertIsNone(
                router._registered_detail_request(
                    "같은 조건으로 다시 조회", self.state, {"id": "B"}
                )
            )
            self.sql.assert_called_once()
        elif case == "T0452":
            self.submit("지난달 입출금")
            self.sql.return_value = fixture("330")
            self.submit("담보")
            self.assertEqual(self.sql.call_args.args[1], ("20261027", "20261127"))
        elif case in ("T0453", "T0454", "T0455"):
            with patch.object(
                common,
                "scoped_vendor_options",
                return_value=[("001", "합성"), ("002", "합성")],
            ) as candidates:
                self.submit("입출금 거래처명 합성 거래처 후보선택")
                candidates.assert_called_once()
                self.sql.assert_not_called()
                self.assertTrue(self.state.get("__sims_registered_pick"))
                if case == "T0455":
                    self.auth.side_effect = PermissionError()
                self.submit("취소" if case == "T0453" else "2번")
                if case == "T0454":
                    self.sql.assert_called_once()
                    self.assertEqual(
                        self.sql.call_args.args[1], ("20261027", "20261127", "002")
                    )
                else:
                    self.sql.assert_not_called()
                self.assertNotIn("__sims_registered_pick", self.state)
        elif case == "T0456":
            self.test_invalid_and_denied()
        elif case == "T0457":
            self.test_permission_change_after_read()
        elif case == "T0458":
            self.submit("입출금")
            s = self.state["__sims_current_table_source_key"]
            meta = self.state["__sims_export_table_provenance_by_key"][s]

            def authority(permission, *, expected, export=False, **kw):
                self.assertEqual(expected, SCOPE)
                if export:
                    raise PermissionError()
                return SCOPE

            self.auth.side_effect = authority
            self.assertTrue(common.allow_result("입출금 조회", meta))
            self.assertFalse(common.allow_result("입출금 조회", meta, export=True))
        elif case in ("T0459", "T0460", "T0461", "T1315", "T1316"):
            frame = fixture(count=305)
            frame["거래처명"] = "합성"
            frame["Rd31_Ven_Cd"] = ["001"] * 304 + ["002"]
            frame["입금/출금 구분"] = "입금"
            frame.loc[304, "Rd31_Receive_Amt"] = None
            self.sql.return_value = frame
            self.submit("입출금 거래처별 월별 집계")
            s = self.state["__sims_current_table_source_key"]
            r = self.state["__sims_last_table_key"]
            self.assertNotEqual(s, r)
            full = self.state["sims_export_tables"]
            self.assertEqual((len(full[s]), len(full[r])), (305, 2))
            meta = self.state["__sims_export_table_provenance_by_key"][s]
            if case == "T0459":
                ctx, _ = chat._select_sims_analysis_ctx_for_table(
                    table_key=r,
                    action="입출금 조회",
                    meta=self.state["__sims_export_table_provenance_by_key"][r],
                    download_df=full[r],
                )
                self.assertEqual(ctx["row_count"], 2)
                self.assertEqual(
                    self.state["__sims_current_table_source_analysis_ctx"]["row_count"],
                    305,
                )
            elif case == "T1315":
                self.assertEqual(full[r]["입금금액"].tolist(), [AMOUNT * 301, None])
                self.assertEqual(full[r]["출금금액"].tolist(), [0, 0])
            elif case == "T0461":
                del self.state["__sims_export_tables_by_key"][s]
                del full[s]
                self.assertIsNone(
                    router._registered_detail_request(
                        "지난달로 다시 조회", self.state, self.room
                    )
                )
            else:
                if case == "T1316":
                    self.state["current_room"] = "other"
                push, notice = Mock(return_value=True), Mock(return_value=True)
                handle_current_table_followup_by_action(
                    df=full[s],
                    query="현재표 월별 집계",
                    top_n=10,
                    table_key=s,
                    source_action="입출금 조회",
                    helpers={"push_table": push, "push_notice": notice},
                    log=None,
                    source_meta=meta,
                )
                if case == "T1316":
                    push.assert_not_called()
                    notice.assert_called_once()
                else:
                    push.assert_called_once()
                    self.assertEqual(push.call_args.kwargs["source_rows"], 305)
            self.sql.assert_called_once()
        elif case == "T0462":
            from app.services.chat_composer_submission import (
                queue_text_dispatch_after_echo,
                consume_text_dispatch_event_after_echo,
            )

            ctx = {"room_id": self.room["id"]}
            queue_text_dispatch_after_echo(
                self.state, text="입금", context=ctx, event_id="synthetic"
            )
            for _ in range(2):
                q, event, status = consume_text_dispatch_event_after_echo(
                    self.state, context=ctx
                )
                if status == "ready":
                    self.submit(q)
            self.sql.assert_called_once()
            self.assertEqual(len(self.room["messages"]), 1)
        elif case == "T1312":
            with patch.dict("os.environ", SIMS_IO_QUERY_MAX_ROWS="2"):
                self.submit("입출금 월별 거래처별 집계")
            self.sql.assert_called_once()
            self.assertFalse(self.state.get("sims_export_tables"))
        else:
            raise AssertionError(case)


if __name__ == "__main__":
    unittest.main()
