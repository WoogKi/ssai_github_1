"""Offline R4 contracts: exercise production forms, query boundary and card consumers."""
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from io import BytesIO
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd
from openpyxl import load_workbook
from app.services import erp_table_query_service as common
from app.services import rddbc310_service, rddbc320_service, rddbc330_service
from app.sims.meta import erp_table_feature_registry as registry
from app.sims.meta.rddbc_payment_meta import FIELDS, PRIMARY_KEYS, TITLES
from app.sims.views import rddbc_payment_views as views
from app.ui import chat_middleware as chat, sims_table_display as display
from app.ui.current_table_followups import analysis_facts as facts

SERVICES = {"310": rddbc310_service, "320": rddbc320_service, "330": rddbc330_service}
SCOPE = {"company_id": 8, "user_id": "42", "epoch": 12345}
AMOUNT = Decimal("999999999999999997")


def fixture(table="310", count=3):
    spec = SERVICES[table].SPEC
    rows = []
    names = [s.rsplit(" AS [", 1)[1][:-1] for s in spec["extra_select"]]
    for i in range(count):
        row = {f.column: (None if i == 2 else -AMOUNT if i == 1 else AMOUNT) if f.kind == "decimal"
               else 1 if f.kind == "smallint" else None if f.kind == "datetime"
               else "20261126" if f.size == 8 else "00001" for f in spec["fields"]}
        row[spec["pk"][-1]] = i
        row.update({k: "합성" for k in names})
        rows.append(row)
    return pd.DataFrame(rows, columns=[f.column for f in spec["fields"]]+names, dtype=object)


class Contracts(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name in ("pyodbc.connect", "socket.create_connection", "sqlite3.connect"):
            stack.enter_context(patch(name, side_effect=AssertionError("real connection forbidden")))

    def result(self, table="310", frame=None, **params):
        with patch.object(common, "query_scope", return_value=SCOPE) as auth, patch.object(
                common, "execute_bound_select", return_value=fixture(table) if frame is None else frame) as reader:
            out = getattr(SERVICES[table], f"get_rddbc{table}_result")(params)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(reader.call_args.kwargs, {"company_id": 8})
        self.assertEqual(auth.call_args.kwargs["expected"], SCOPE)
        return out

    def test_schema_all_fields_and_pk(self):
        from app.db.schema_map import SCHEMA
        from app.db.labels_map import LABELS
        for table, n in {"310":38,"320":29,"330":22,"260":7,"270":7}.items():
            key = "rddbc"+table
            fields = FIELDS[table]
            self.assertEqual(len(fields), n)
            self.assertEqual(len(set(LABELS[key].values())), n)
            self.assertEqual(SCHEMA["tables"][key], "dbo.Rddbc"+table)
            ddl = (ROOT/"tablelayout"/f"Rddbc{table}.txt").read_text(encoding="utf-8-sig")
            for field in fields:
                self.assertIn("["+field.column+"]", ddl)
                self.assertIn(field.column, SCHEMA["cols"][key].values())
                self.assertEqual(LABELS[key][field.column], field.label)
            self.assertTrue(all("["+k+"]" in ddl.split("PRIMARY KEY",1)[1].split(")",1)[0] for k in PRIMARY_KEYS[table]))

    def test_registry_panel_only(self):
        actions = registry.menu_actions(business_group="입출금 관리", target="입출금")
        self.assertEqual(list(actions), list(TITLES.values()))
        self.assertFalse(set(actions) & {a.action for _,a in registry.iter_action_specs(nlq_only=True)})
        self.assertFalse(registry.registry_completeness_errors())
        for title in actions:
            feature, action = registry.get_action_spec(title)
            self.assertTrue(feature.scope_bound)
            self.assertNotEqual(action.payload_key,"__io260_last_payload")
            for path in (action.view_function,action.service_function,action.export_function):
                self.assertTrue(callable(registry.resolve_dotted_callable(path)))
        from app.ui import sims_panel
        self.assertEqual(sims_panel._current_action_payload_key("제품재고현황 조회"), "__io260_last_payload")

    def test_dates_and_binding(self):
        for table, module in SERVICES.items():
            p = getattr(module,f"normalize_rddbc{table}_params")({"filters":{"remark":"x';--"}},today=date(2024,3,31))
            self.assertEqual((p["date_from"],p["date_to"]),("20240229","20240331"))
            self.assertEqual(p["date_role"], "Receive_YyMmDd" if table=="310" else "Add_Date")
            sql, bind, _, _ = common.build_detail_select(module.SPEC, p)
            self.assertEqual(bind[-1],"%x';--%")
            self.assertNotIn("x';",sql)
            self.assertEqual(sql.count("?"),len(bind))
            self.assertNotRegex(sql,r"JOIN dbo.Rddbc(?:310|320|330|260|270)")
            self.assertIn("Rd01_Gcode",sql)
            self.assertIn("Rd06_User_Nm",sql)
        for code, direction in (("00","receipt"), ("32","receipt"), ("49","receipt"), ("4Z","receipt"),
                                ("50","payment"), ("5C","payment"), ("82","payment")):
            p=rddbc310_service.normalize_rddbc310_params({"filters":{"flow_code":code}, "direction":direction})
            sql, args, *_=common.build_detail_select(rddbc310_service.SPEC,p)
            self.assertIn("0023",sql)
            self.assertIn(code,args)

    def test_direction_codes_from_sql_to_current_table(self):
        import sqlite3
        from contextlib import closing
        from app.ui.current_table_followups.generic import _exact_role_result

        # Execute the generated projection and WHERE clause. SQLite needs only the
        # SQL Server Unicode literal prefix removed for this ASCII code sample.
        spec = rddbc310_service.SPEC
        projection = next(s for s in spec["extra_select"] if s.endswith(" AS [입금/출금 구분]"))
        cases = [
            ("00","입금"), ("01","입금"), ("49","입금"), ("4Z","입금"), ("50","출금"),
            ("51","출금"), ("82","출금"), ("9","출금"), ("100","입금"),
            *((c,"출금") for c in ("5C","5D","5E","5F","5G","5H","5I","5J")),
            (None,None), ("",None), ("   ",None), ("51",None),
        ]
        amounts = [(-1 if i % 2 else 1)*(i+1) for i in range(len(cases))]
        day = "20261127"
        frame = fixture(count=len(cases))
        frame["Rd31_Receive_Amt"] = amounts
        with closing(sqlite3.dbapi2.connect(":memory:")) as db:
            db.execute("CREATE TABLE S(id INT PRIMARY KEY,Rd31_Receive_Di_Gcode TEXT,"
                       "Rd31_Receive_Di TEXT,Rd31_Receive_YyMmDd TEXT,amt INTEGER)")
            db.executemany("INSERT INTO S VALUES(?,?,?,?,?)", [
                (i,"0023" if i < len(cases)-1 else "9999",code,day,amounts[i])
                for i,(code,_) in enumerate(cases)])
            for direction, label in (("all",None),("receipt","입금"),("payment","출금")):
                p = rddbc310_service.normalize_rddbc310_params(dict(date_from=day,date_to=day,direction=direction))
                sql, args, *_ = common.build_detail_select(spec,p)
                where = sql.split(" WHERE ")[1].split(" ORDER BY ")[0]
                if label:
                    self.assertEqual(args[-1], "50")
                rows = db.execute("SELECT id, amt,"+projection.replace("N'", "'")+" FROM S WHERE "+where+
                                  " ORDER BY id",args).fetchall()
                self.assertEqual(rows, [(i,amounts[i],side) for i,(_,side) in enumerate(cases)
                                        if direction=="all" or side==label])
                if direction=="all":
                    frame["입금/출금 구분"] = [row[2] for row in rows]
        result = self.result(frame=frame)
        full, roles = result["df"], result["meta"]["analysis_roles"]
        for label in ("입금","출금"):
            expected = [a for a,(_,side) in zip(amounts,cases) if side==label]
            for group in ("담당자","일자","월"):
                out, *_ = _exact_role_result(full,"현재표 "+group+"별 집계",10,roles)
                self.assertEqual(out[label+"건수"].sum(),len(expected))
                self.assertEqual(out[label+"금액"].sum(),sum(expected))
                self.assertEqual(out["미분류건수"].sum(),4)
            top, *_ = _exact_role_result(full,"현재표 "+label+"금액 TOP 10",10,roles)
            self.assertEqual(top["금액"].tolist(),sorted(expected,reverse=True)[:10])

    def test_invalid_conditions_no_source(self):
        for p in ({"date_from":"20260230"}, {"date_from":"20260103","date_to":"20260101"},
                  {"date_role":"unapproved"}, {"direction":"cash"}, {"filters":{"unknown":0}},
                  {"filters":{"amount":"1.2"}},{"filters":{"note_seq":32768}},
                  {"filters":{"flow_code":"50"},"direction":"receipt"}):
            with patch.object(common,"execute_bound_select") as reader, self.assertRaises(ValueError):
                rddbc310_service.get_rddbc310_result(p)
            reader.assert_not_called()

    def test_pk_amount_and_cap(self):
        for table in SERVICES:
            r=self.result(table,display_top=1)
            self.assertEqual((len(r["df"]),len(r["df_display"]),len(r["records"])),(3,1,1))
            self.assertEqual(r["df"]["금액"].tolist(),[AMOUNT,-AMOUNT,None])
            self.assertEqual(r["df"].columns[0],SERVICES[table].SPEC["leading"][0])
            self.assertTrue(set(f.label for f in FIELDS[table])<=set(r["df"]))
            self.assertEqual(r["meta"]["export_row_count"],3)
            self.assertEqual(self.result(table,fixture(table,0))["meta"]["result_status"],"no_data")
        frame=fixture("330")
        frame["Rd33_Note_Seq"]=1
        frame["Rd33_Ven_Cd"]=["00001","00002","00003"]
        self.assertEqual(len(self.result("330",frame)["df"]),3)
        frame["Rd33_Ven_Cd"]="00001"
        with self.assertRaises(ValueError): self.result("330",frame)
        with patch.dict(os.environ,{"SIMS_IO_QUERY_MAX_ROWS":"2"}),self.assertRaises(ValueError): self.result()
        frame=fixture()
        frame.loc[0,"Rd31_Receive_Amt"]=1.5
        with self.assertRaises(ValueError): self.result(frame=frame)

    def test_scope_boundaries(self):
        from app.ui import ssai_login as login
        from app.services.external_api_session import STATE_KEY
        session=SimpleNamespace(**SCOPE,permissions={"IO_READ","EXPORT_EXCEL"})
        with patch.object(login.st,"session_state",{STATE_KEY:{"epoch":12345}}), patch.object(login,"get_current_user",return_value=SimpleNamespace(user_id=42)) as user, patch.object(login,"get_selected_company",return_value={"company_id":8}), patch.object(login,"get_current_permissions",return_value=["IO_READ","EXPORT_EXCEL"]), patch("app.services.external_api_management.load_current_api_session",return_value=session) as fresh:
            self.assertEqual(common.query_scope("IO_READ",export=True),SCOPE)
            for k,v in (("company_id",9),("user_id","other"),("epoch",5)):
                self.assertFalse(common.allow_result(TITLES["310"],{"query_scope":{**SCOPE,k:v}}))
            session.permissions.remove("EXPORT_EXCEL")
            self.assertTrue(common.allow_result(TITLES["310"],{"query_scope":SCOPE}))
            self.assertFalse(common.allow_result(TITLES["310"],{"query_scope":SCOPE},export=True))
            session.permissions.clear()
            self.assertFalse(common.allow_result(TITLES["310"],{"query_scope":SCOPE}))
            user.return_value=None
            fresh.reset_mock()
            self.assertFalse(common.allow_result(TITLES["310"],{"query_scope":SCOPE}))
            fresh.assert_not_called()
        for permissions, calls in (([PermissionError()],0),([SCOPE,PermissionError()],1)):
            with patch.object(common,"query_scope",side_effect=permissions),patch.object(common,"execute_bound_select",return_value=fixture()) as reader,self.assertRaises(PermissionError):
                rddbc310_service.get_rddbc310_result({})
            self.assertEqual(reader.call_count,calls)

    def test_reader_bound_company_and_cleanup(self):
        db=MagicMock()
        cursor=db.cursor.return_value
        cursor.description=[("금액",)]
        cursor.fetchall.return_value=[(AMOUNT,)]
        with patch("app.services.ssai_auth_service.connect_company_db",return_value=db) as conn:
            frame=common.execute_bound_select("SELECT ?",("00",),company_id=8)
        conn.assert_called_once_with(8)
        self.assertEqual(db.timeout,10)
        cursor.execute.assert_called_once_with("SELECT ?","00")
        db.close.assert_called_once()
        cursor.close.assert_called_once()
        self.assertEqual(frame.iloc[0,0],AMOUNT)

    def test_display_facts_and_real_download(self):
        r=self.result(frame=fixture(count=1))
        df,meta=r["df"],r["meta"]
        formatted,_,_,_=display.build_sims_table_display_config(df,action_name=TITLES["310"],meta=meta)
        self.assertEqual(formatted.loc[0,"금액"],format(int(AMOUNT),",d"))
        self.assertEqual(formatted.loc[0,"입출금순번"],"0")
        self.assertEqual(formatted.loc[0,"거래처코드"],"00001")
        ctx=chat._build_sims_analysis_context_from_df(df,result=r,action_name=TITLES["310"],params={},meta=meta)
        self.assertNotIn("query_scope", facts.fit_analysis_context(ctx, 12000))
        evidence=ctx["whole_table_facts"]
        self.assertEqual(evidence["date_column"],"입출금일자")
        self.assertEqual(evidence["numeric_metrics"]["금액"]["sum"],int(AMOUNT))
        self.assertIn(format(int(AMOUNT),",d"),facts.build_verified_whole_table(evidence))
        self.assertIn(format(int(AMOUNT),",d"),str(facts.build_structured_analysis_options(evidence)))
        with patch.object(common,"query_scope",return_value=SCOPE),patch.object(chat.st,"session_state",{}),patch.object(chat,"_render_sims_result_actions_fragment") as render:
            chat._render_sims_result_actions_lazy(key_suffix="test",table_key="test",download_df=df,
                csv_name="test.csv",xlsx_name="test.xlsx",prompt="",expected_rows=1,display_rows=1,
                clicked_action=TITLES["310"],clicked_meta=meta)
        data=render.call_args.kwargs
        self.assertIn(str(AMOUNT),data["csv_bytes"].decode("utf-8-sig"))
        sheet=load_workbook(BytesIO(data["excel_bytes"]),read_only=True).active
        rows=list(sheet.values)
        self.assertEqual(rows[1][rows[0].index("금액")],str(AMOUNT))
        self.assertEqual(rows[1][rows[0].index("거래처코드")],"00001")
        self.assertEqual(rows[1][rows[0].index("입출금일자")],"2026-11-26")
        self.assertIn("2026-11-26",data["csv_bytes"].decode("utf-8-sig"))
        with patch.object(common,"query_scope",return_value=SCOPE),patch.object(chat.st,"session_state",{"__sims_llm_analysis_runner": lambda: None}),patch.object(chat.st,"button",return_value=True),patch.object(chat.st,"download_button"),patch.object(chat,"_run_clicked_sims_llm_analysis") as runner:
            chat._render_sims_result_actions_fragment.__wrapped__(**data)
            self.assertEqual(runner.call_args.kwargs["analysis_ctx"]["row_count"],1)

    def test_name_and_range_predicates(self):
        for module in SERVICES.values():
            spec = module.SPEC
            normalize = lambda f: common.normalize_detail_params(spec, {"filters": f})
            unfiltered, *_ = common.build_detail_select(spec, normalize({}))
            for key, (column, kind, _) in spec["filters"].items():
                if kind != "name":
                    continue
                sql, args, *_ = common.build_detail_select(spec, normalize({key: "  같은%_[명  "}))
                self.assertEqual(sql.split(" WHERE ")[0], unfiltered.split(" WHERE ")[0])
                self.assertIn(column+" LIKE ?", sql)
                self.assertEqual(args[-1], "%같은[%][_][[]명%")
                self.assertNotIn("같은", sql)
            low = -999999999999999998
            sql, args, *_ = common.build_detail_select(spec, normalize({
                "amount_from": low, "amount_to": "0", "add_date_from": "2026-10-10",
                "mod_date_to": "20261127", "note_no": "0001"}))
            for val in (low, 0, "20261010", "20261127", "0001"):
                self.assertIn(val, args)
            self.assertEqual(sql.count("?"), len(args))
            for bounds in ({"amount_from": 1, "amount_to": 0},
                           {"mod_date_from": "20261010", "mod_date_to": "20261001"}):
                with self.assertRaises(ValueError):
                    normalize(bounds)
            with patch.object(common, "query_scope", return_value=SCOPE), patch.object(
                    common, "execute_bound_select", return_value=fixture(spec["table"][-3:], 0)) as fetch:
                result = common.read_detail_result(spec, normalize({"bank_name": "미등록"}))
                self.assertEqual(result["meta"]["result_status"], "no_data")
                fetch.assert_called_once()
                self.assertIn("%미등록%", fetch.call_args.args[1])

    def test_chat_cap_matches_card_not_panel(self):
        from app.ui import sims_panel as panel
        full = pd.DataFrame({"값": range(4521)})
        for limit, panel_rows, expected in ((300, 1000, 300), (70, 1000, 70),
                                             (300, 10, 10), (0, 1000, 1000)):
            payload = {"df": full, "df_display": full.head(panel_rows),
                       "meta": {"display_row_count": panel_rows}, "title": TITLES["310"]}
            with patch.object(chat, "_chat_display_max_rows", return_value=limit), patch.object(
                    panel.st, "success") as success, patch.object(panel.st, "caption"):
                panel._render_panel_chat_only_done(payload, TITLES["310"])
                card = chat._normalize_result_for_chat(payload)
                self.assertEqual(len(card["df"]), 4521)
                self.assertEqual(len(card["df_display"]), expected)
                self.assertIn(f"채팅창에는 {expected:,}건", success.call_args.args[0])

    def test_live_result_precedes_panel(self):
        import ast
        from streamlit.testing.v1 import AppTest
        source = (ROOT/"app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8-sig")
        nodes = ast.parse(source).body
        slots = {"chat_history_container", "chat_immediate_user_slot",
                 "chat_pending_area_slot", "sims_panel_container"}
        declarations = [ast.unparse(n) for n in nodes if isinstance(n, ast.Assign)
                        and isinstance(n.targets[0], ast.Name) and n.targets[0].id in slots]
        self.assertEqual(len(declarations), 4)
        self.assertIn("with sims_panel_container:", source.split("# (D) SIMS 섹션", 1)[1][:160])
        self.assertIn("pending_area = chat_pending_area_slot.container()", source)
        # Reproduce Streamlit's actual container ordering on the submit run,
        # including a pending message written after the panel has rendered.
        script = "\n".join(["import streamlit as st", *declarations,
            "with chat_history_container:", "    st.markdown('history')",
            "    pending_area = chat_pending_area_slot.container()",
            "    with sims_panel_container:", "        st.markdown('SSAI 결과')",
            "        st.markdown('조회조건')", "        submit = st.button('조회')",
            "    if submit:", "        with pending_area:", "            st.markdown('결과표')",
            "            st.markdown('다운로드 및 분석')"])
        app = AppTest.from_string(script).run()
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual([m.value for m in app.markdown],
                         ["history", "결과표", "다운로드 및 분석", "SSAI 결과", "조회조건"])

    def test_forms_and_rerun(self):
        from streamlit.testing.v1 import AppTest
        for table,module in SERVICES.items():
            with patch.object(views,"query_scope",return_value=SCOPE),patch.object(module,f"get_rddbc{table}_result",return_value={}) as query, patch.object(views,"_vendor_candidates",return_value=[("00001","합성")]) as candidates:
                app=AppTest.from_string(f"from app.sims.views.rddbc_payment_views import view_rddbc{table}\nview_rddbc{table}()").run()
                self.assertFalse(app.exception)
                labels = [w.label for w in app.text_input]
                self.assertIn("은행명", labels)
                self.assertNotIn("은행코드", labels)
                self.assertIn("등록자명", labels)
                self.assertIn("수정자명", labels)
                date_widget = app.date_input[0]
                from streamlit.testing.v1.element_tree import Block
                row_labels = [[w.label for w in node if w.type in
                               {"text_input", "date_input", "selectbox", "button"}]
                              for node in app.main if isinstance(node, Block) and node.children
                              and all(c.type == "column" for c in node.children.values())]
                self.assertIn(["조회 기준일", "시작일자", "종료일자"], row_labels)
                self.assertIn(["거래처코드", "거래처명", "후보검색", "거래처 후보선택"], row_labels)
                self.assertIn(["등록자명", "등록일자 시작", "등록일자 종료"], row_labels)
                if table == "310":
                    self.assertIn(["입금/출금 구분", "입출금구분코드", "거래처 담당자명"], row_labels)
                bank = next(w for w in app.text_input if w.label == "은행명")
                bank.set_value("합성은행")
                date_widget.set_value(date(2025,10,1)).run()
                next(b for b in app.button if b.label=="후보검색").click().run()
                candidates.assert_called_once()
                query.assert_not_called()
                next(b for b in app.button if b.label=="조회").click().run()
                self.assertFalse(app.exception)
                self.assertEqual(query.call_args.args[0]["date_from"],"20251001")
                self.assertEqual(query.call_args.args[0]["filters"]["bank_name"], "합성은행")
                app.run()
                query.assert_called_once()

    def test_exact_current_table_contract(self):
        from app.ui.current_table_followups.action_dispatcher import handle_current_table_followup_by_action
        from app.ui.current_table_followups import generic
        def ask(result, question, *, frame=None, denied=False):
            push, notice = Mock(return_value=True), Mock(return_value=True)
            with patch.object(common, "query_scope", side_effect=PermissionError if denied else None), patch.object(
                    common, "execute_bound_select", side_effect=AssertionError("followup read")), patch.object(
                    generic, "_build_common_group_summary", wraps=generic._build_common_group_summary) as group:
                handled = handle_current_table_followup_by_action(
                    df=result["df"] if frame is None else frame, query="현재표 "+question, top_n=20,
                    table_key="bound", source_action=result["action"], source_meta=result["meta"], log=None,
                    helpers={"push_table": push, "push_notice": notice})
                self.assertTrue(handled)
                self.assertEqual(push.call_count+notice.call_count, 1)
                if push.called:
                    item = push.call_args.kwargs
                    self.assertEqual(item["source_table_key"], "bound")
                    self.assertEqual(item["action"], result["action"])
                    self.assertEqual(item["extra_meta"]["query_scope"], SCOPE)
                    if "별 집계" in question:
                        group.assert_called_once()
                    return item["df"], item["extra_meta"]
                return notice.call_args.kwargs["extra_meta"]["result_status"], {}

        for t, group in (("310","입출금구분명"),("320","어음번호분류명"),("330","담보종류명")):
            frame = fixture(t, 320)
            src = SERVICES[t].SPEC
            alternate = src["dates"][1]
            frame[src["date_columns"][alternate]] = ["20251001"]*319+["20261127"]
            field = next(f.column for f in src["fields"] if f.kind=="decimal")
            frame[field] = [AMOUNT]*317 + [-AMOUNT, None, AMOUNT-1]
            if t == "310":
                frame["입금/출금 구분"] = ["입금"]*317 + ["출금", "출금", "입금"]
            if t == "330":
                frame["Rd33_Note_Seq"] = 1
                frame["Rd33_Ven_Cd"] = [str(i).zfill(5) for i in range(320)]
            r = self.result(t, frame, date_role=alternate, display_top=2)
            expected = int(AMOUNT)*317-1
            for period in ("일자","월","연도","요일"):
                out, meta = ask(r, period+"별 집계")
                self.assertEqual(len(out), 2)
                cols = meta["exact_columns"]
                self.assertEqual(sum(int(x) for c in cols for x in out[c].dropna()), expected)
                if t=="310" and period in ("일자","월"):
                    self.assertEqual(cols, ["입금금액","출금금액"])
                    self.assertEqual(sum(out["입금건수"])+sum(out["출금건수"]),320)
                if period=="일자":
                    self.assertEqual(set(out["일자"]), {"2025-10-01","2026-11-27"})
                    ctx=chat._build_sims_analysis_context_from_df(out, result={}, action_name=r["action"], params={},
                        meta={**meta,"row_count_total":len(out)})
                    evidence = ctx["whole_table_facts"]
                    self.assertEqual(sum(evidence["numeric_metrics"][c]["sum"] for c in cols), expected)
                    value = evidence["numeric_metrics"][cols[0]]["sum"]
                    claim = f"전체 {cols[0]} 합계는 {value:,}원입니다."
                    self.assertTrue(facts._verified_interpretation_sentence(claim,evidence))
                    self.assertFalse(facts._verified_interpretation_sentence(claim.replace(f"{value:,}",f"{value+1:,}"),evidence))
                    with patch.object(common,"query_scope",return_value=SCOPE), patch.object(chat.st,"session_state",{}), patch.object(
                            chat, "_render_sims_result_actions_fragment") as render:
                        chat._render_sims_result_actions_lazy(key_suffix="child",table_key="child",download_df=out,
                            csv_name="test.csv",xlsx_name="test.xlsx",prompt="",expected_rows=len(out),display_rows=1,
                            clicked_action=r["action"],clicked_meta={**meta,"row_count_total":len(out),"current_table_followup":True})
                        data = render.call_args.kwargs
                        sheet = list(load_workbook(BytesIO(data["excel_bytes"]),read_only=True).active.values)
                        first = str(out[cols[0]].iloc[0])
                        self.assertEqual(sheet[1][sheet[0].index(cols[0])],first)
                        self.assertIn(first,data["csv_bytes"].decode("utf-8-sig"))
                        self.assertEqual(data["analysis_ctx"]["row_count"],len(out))
            for dimension in (group,"거래처","은행명"):
                out, meta = ask(r, dimension+"별 집계")
                self.assertEqual(sum(int(x) for c in meta["exact_columns"] for x in out[c].dropna()),expected)
            formatting_case = {**r, "df": r["df"].copy()}
            formatting_case["df"].loc[r["df"].index[:2],"은행명"] = [None, float("nan")]
            out,child = ask(formatting_case,"금액 TOP 10")
            self.assertEqual(out["금액"].tolist(), [AMOUNT]*10)
            original, *_ = display.build_sims_table_display_config(formatting_case["df"].head(10), action_name=r["action"], meta=r["meta"])
            result, *_ = display.build_sims_table_display_config(out, action_name=r["action"], meta=child)
            pd.testing.assert_frame_equal(original, result)
            self.assertEqual(result["은행명"].tolist()[:2], ["",""])
            date_col = r["meta"]["analysis_roles"]["date"]
            self.assertEqual(result[date_col].iloc[0], "2025-10-01")
            exported = display.prepare_exact_table_cells(out, child)
            self.assertEqual(exported[date_col].iloc[0], "2025-10-01")
            self.assertEqual(exported["금액"].iloc[0], AMOUNT)
            self.assertIsNone(out["은행명"].iloc[0])
            out,_ = ask(r,"금액 합계")
            self.assertEqual(out.iloc[0]["금액"], expected)
            out,_ = ask(r,"은행명=없는명칭 상세")
            self.assertTrue(out.empty)
            self.assertEqual(ask(r,"잔액 계산")[0],"unsupported")
            self.assertEqual(ask(r,"금액 합계",denied=True)[0],"source_unavailable")
            self.assertEqual(ask(r,"금액 합계",frame=r["df_display"])[0],"source_unavailable")
            if t=="310":
                out,_ = ask(r,"입금/출금별 집계")
                self.assertEqual(out.set_index("입금/출금 구분").at["출금","금액"],-AMOUNT)
                out,_ = ask(r,f"은행명=합성 금액 {AMOUNT} 이상 입금만 월별 집계")
                self.assertEqual(out["입금건수"].sum(),317)
                self.assertEqual(out["입금금액"].sum(),317*int(AMOUNT))

    def test_staff_master_grain_and_like(self):
        import sqlite3
        from contextlib import closing
        spec = SERVICES["310"].SPEC
        joins = [j for j in spec["joins"] if "S.[Rd31_Ven_Cd]" in j or " OwnerStaff " in j
                 or "S.[Rd31_Real_Sales_Man]" in j]
        select = [expr for expr in spec["extra_select"]
                  if any(expr.endswith(" AS [" + c + "]") for c in
                         ("거래처 담당자코드", "거래처 담당자명", "원본 담당영업사원명"))]
        with closing(sqlite3.dbapi2.connect(":memory:")) as db:
            db.executescript("""
                ATTACH DATABASE ':memory:' AS dbo;
                CREATE TABLE dbo.Rddbc030(Rd03_Ven_Cd TEXT PRIMARY KEY, Rd03_Sales_Man TEXT);
                CREATE TABLE dbo.Rddbc060(Rd06_User_Cd TEXT PRIMARY KEY, Rd06_User_Nm TEXT);
                CREATE TABLE S(seq INTEGER PRIMARY KEY, Rd31_Ven_Cd TEXT, Rd31_Real_Sales_Man TEXT, amt INTEGER);
                INSERT INTO dbo.Rddbc030 VALUES ('10001','01'),('50001','02');
                INSERT INTO dbo.Rddbc060 VALUES ('01','동명'),('02','동명'),('03','이전');
                INSERT INTO S VALUES (1,'10001','03',999999999999999997),(2,'50001',' ',-7),(3,'99999','03',0);
            """)
            label_cols = [s.rsplit(" AS [", 1)[1][:-1] for s in select]
            sql = "SELECT S.seq,S.amt," + ",".join(select) + " FROM S " + " ".join(joins)
            rows = db.execute(sql+" ORDER BY S.seq").fetchall()
            self.assertEqual([r[:2] for r in rows], db.execute("SELECT seq,amt FROM S ORDER BY seq").fetchall())
            staff = [dict(zip(label_cols, r[2:])) for r in rows]
            self.assertEqual([r["거래처 담당자코드"] for r in staff], ["01","02",None])
            self.assertEqual([r["원본 담당영업사원명"] for r in staff], ["이전",None,"이전"])
            where = " WHERE " + spec["filters"]["vendor_staff_name"][0] + " LIKE ?"
            self.assertEqual(len(db.execute(sql+where,("%동%",)).fetchall()), 2)
            self.assertEqual(db.execute(sql+where,("%없는명칭%",)).fetchall(), [])

    def test_role_search_and_composition(self):
        from app.ui.current_table_followups.action_dispatcher import handle_current_table_followup_by_action
        for table in SERVICES:
            frame = fixture(table, 4)
            spec = SERVICES[table].SPEC
            frame[spec["date_columns"][spec["dates"][0]]] = ["20261101","20261102","20261201","20261202"]
            amount_field = next(f.column for f in spec["fields"] if f.label == "금액")
            frame[amount_field] = [AMOUNT, Decimal(100000000), Decimal(-25), None]
            frame["거래처명"] = ["삼일 합성","동화 합성","삼일 엘지","별도 합성"]
            frame["은행명"] = ["하나은행","별도은행","별도은행",None]
            frame[spec["date_columns"][spec["dates"][1]]] = ["20231001"]*4
            if table == "310":
                frame["입금/출금 구분"] = ["출금","입금","출금","입금"]
                frame["거래처 담당자명"] = ["홍길동 대리","가상","가상","가상"]
                frame["입출금구분명"] = ["결제할인","현금","거래할인","현금"]
                frame["Rd31_Receive_Di"] = ["5C","01","82","01"]
            if table == "320":
                frame["어음구분명"] = ["지급어음 A","기타","지급어음 B","기타"]
            if table == "330":
                frame["담보종류명"] = ["부동산 A","기타","부동산 B","기타"]
            r = self.result(table,frame,display_top=1)
            def ask(q):
                push = Mock(return_value=True)
                with patch.object(common,"query_scope",return_value=SCOPE), patch.object(
                        common,"execute_bound_select", side_effect=AssertionError("source reread")):
                    handle_current_table_followup_by_action(df=r["df"],query="현재표 "+q,top_n=10,
                        table_key="root",source_action=r["action"],source_meta=r["meta"],
                        helpers={"push_table":push,"push_notice":self.fail},log=None)
                self.assertEqual(push.call_count,1,q)
                item = push.call_args.kwargs
                self.assertEqual((item["source_rows"],item["source_table_key"]),(4,"root"))
                return item["df"]
            cases = {"거래처명 삼일 조회":[0,2],"금액 1억원 이상":[0,1],"은행명 하나":[0],
                     "거래처명 없는이름 조회":[],"거래처명 삼일 금액 LOW 1":[2],
                     "거래처명 삼일 및 은행명 하나":[0],
                     "금액 -25원~1억원":[1,2],"금액 1억원 초과":[0],"금액 1억원 미만":[2],
                     "금액 1억원 이하":[1,2],f"금액 {AMOUNT} 이상":[0],
                     "일자 2026-11-02~2026-12-01":[1,2],"기준일 20261101":[0]}
            if table=="310":
                cases.update({"입출금구분 할인":[0,2],"입출금구분명 할인":[0,2],"담당자명 홍길동":[0],
                    "입출금구분코드 5C":[0],"입출금구분코드 01":[1,3],"입출금구분코드 1":[],
                    "출금 금액 1억원 이상":[0],"거래처명 삼일 출금":[0,2]})
                for q in ("입출금별 집계","담당자별 월별 집계"):
                    out=ask(q)
                    self.assertEqual(sum(out[c].sum() for c in out if c.endswith("건수")),4)
                self.assertEqual(ask("거래처별 출금금액 TOP 10")["출금금액"].tolist(),[AMOUNT,-25])
                self.assertEqual(ask("입출금구분명 할인 월별 집계")["출금금액"].tolist(),[AMOUNT,-25])
            elif table=="320":
                cases.update({"어음구분명 지급어음":[0,2],"거래처명 동화 조회":[1],"은행 하나":[0]})
                self.assertEqual(ask("어음구분명 지급어음 월별 집계")["금액"].tolist(),[AMOUNT,-25])
                self.assertEqual(ask("거래처별 금액 TOP 10")["금액"].tolist(),[AMOUNT,100000000,-25])
            else:
                cases.update({"담보종류명 부동산":[0,2],"거래처명 엘지 조히":[2],"거래처명 엘지 조회":[2]})
                for q in ("담보종류명별 집계","여수신구분별 집계"):
                    self.assertEqual(ask(q)["건수"].sum(),4)
            for q,indices in cases.items():
                with self.subTest(table=table,query=q):
                    self.assertEqual(ask(q)["금액"].tolist(),r["df"]["금액"].iloc[indices].tolist())
            alt = self.result(table,frame,date_role=spec["dates"][1])
            r = alt
            self.assertEqual(len(ask("기준일 20231001")),4)

    def test_all_registered_group_expressions(self):
        from app.ui.current_table_followups import generic
        from app.ui.current_table_followups.action_dispatcher import handle_current_table_followup_by_action

        for table in SERVICES:
            result = self.result(table, fixture(table, 4))
            roles = result["meta"]["analysis_roles"]
            df = result["df"].copy()
            dims = list(dict.fromkeys(roles["dimensions"]))
            time = {"일자": ["날짜", "일", roles["date"]], "월": [], "요일": [], "연도": ["년도", "년"]}
            labels = {c: set(generic._group_aliases(c)) for c in dims}
            for alias, col in roles["aliases"].items():
                labels[col].add(alias)
            labels[roles["category"]].add("업무구분")
            split = roles.get("split")
            if split:
                labels[split].add(split.removesuffix("구분").replace(" ", ""))
            labels.update({c: set(names) for c, names in time.items()})
            dates = ["20251101","20261102","20261201","20261202"]
            df[roles["date"]] = dates
            values = [AMOUNT, Decimal(-7), None, Decimal(2)]
            df["금액"] = pd.Series(values, dtype=object)
            for c in dims:
                df[c] = [c+"A", c+"B", c+"A", c+"B"]
            for code in ("거래처코드", "거래처 담당자코드"):
                if code in df:
                    df[code] = ["01","01","02","02"]
            if split:
                df[split] = ["입금","출금","입금","출금"]
            times = {"일자": ["2025-11-01","2026-11-02","2026-12-01","2026-12-02"],
                     "월": [d[:7] for d in ("2025-11","2026-11","2026-12","2026-12")],
                     "연도": [d[:4] for d in dates], "요일": ["토요일","월요일","화요일","수요일"]}
            tested = {}
            canonical = 0
            for col, aliases in labels.items():
                terms = {col+s for s in (" 집계","별 집계"," 별 집계","별 합계")}
                canonical += len(terms)
                terms.update(a+s for a in aliases for s in (" 집계","별 집계"))
                codes = []
                if col == roles.get("vendor"):
                    codes.append("거래처코드")
                if col == roles.get("staff"):
                    codes.append("거래처 담당자코드")
                expected = {}
                for i, value in enumerate(times[col] if col in times else df[col]):
                    key = tuple(df[c].iat[i] for c in codes) + (value,)
                    expected.setdefault(key, []).append(i)
                baseline = None
                for q in sorted(terms):
                    self.assertNotIn(q, tested, (table, col, q))
                    tested[q] = col
                    with self.subTest(table=table, question=q), patch.object(common, "query_scope", return_value=SCOPE):
                        push = Mock(return_value=True)
                        handle_current_table_followup_by_action(df=df, query="현재표 "+q, top_n=20, table_key="root",
                            source_action=result["action"], source_meta=result["meta"], log=None,
                            helpers={"push_table":push, "push_notice":self.fail})
                        push.assert_called_once()
                        out = push.call_args.kwargs["df"]
                        self.assertEqual(len(out), len(expected))
                        self.assertEqual(set(out[codes+[col]].itertuples(index=False,name=None)),set(expected))
                        for row in out.to_dict("records"):
                            idx = expected[tuple(row[k] for k in codes+[col])]
                            for count_col in (k for k in row if k.endswith("건수")):
                                side = count_col.removesuffix("건수")
                                selected = [i for i in idx if not side or df[split].iat[i] == side]
                                nums = [values[i] for i in selected if values[i] is not None]
                                self.assertEqual(row[count_col],len(selected))
                                self.assertEqual(row[side+"금액"],sum(nums) if nums or not selected else None)
                        if baseline is None:
                            baseline = out
                        else:
                            pd.testing.assert_frame_equal(out,baseline)
            print(f"GROUP_MATRIX {table}: business={len(dims)} time={len(time)} canonical={canonical} "
                  f"alias_forms={len(tested)-canonical} questions={len(tested)}")

    def test_group_intent_vs_literal(self):
        from app.ui.current_table_followups.generic import _exact_role_result
        r = self.result()
        roles, df = r["meta"]["analysis_roles"], r["df"].copy()
        df["거래처명"] = ["집계약국","별빛순위상사","한별"]
        def ask(q):
            return _exact_role_result(df, "현재표 "+q, 10, roles)[0]
        for text in df["거래처명"]:
            self.assertEqual(ask("거래처명 "+text+" 조회")["거래처명"].tolist(), [text])
        for a,b in (("담당자 명별 집계","거래처 담당자명 집계"), ("거래처명 월별 집계","거래처별 월별 집계"),
                    ("입출금구분 집계","입출금구분명 집계"), ("입출금별 집계","입금/출금별 집계")):
            pd.testing.assert_frame_equal(ask(a),ask(b))
        for bad in ("단당자명별 집계","미등록차원 월별 집계","거래처명 조회"):
            with self.assertRaises(ValueError,msg=bad):
                ask(bad)

    def test_scoped_staff_and_vendor_ranks(self):
        from app.ui.current_table_followups.generic import _exact_role_result
        f = fixture(count=7)
        f["입금/출금 구분"] = ["입금","출금","입금","출금","입금","출금",None]
        f["Rd31_Receive_Amt"] = [AMOUNT, 1, -7, AMOUNT-1, None, 0, 19]
        f["Rd31_Ven_Cd"] = ["50001","50001","10002","10002","50001","10002","99999"]
        f["거래처 담당자코드"] = ["01","01","02","02","01","02",None]
        f["거래처 담당자명"] = ["동명"]*6+[None]
        r = self.result(frame=f)
        def ask(q):
            return _exact_role_result(r["df"], "현재표 "+q, 20, r["meta"]["analysis_roles"])[0]
        for side, first, staff, vendor, amounts in (
                ("입금",AMOUNT,"01","50001",[AMOUNT,-7]), ("출금",AMOUNT-1,"02","10002",[AMOUNT-1,1,0])):
            metric = side+"금액"
            raw = ask(metric+" TOP 10")
            self.assertTrue(raw["입금/출금 구분"].eq(side).all())
            self.assertEqual(raw["금액"].tolist(), amounts)
            for label, key, code in (("거래처","거래처코드",vendor), ("담당자","거래처 담당자코드",staff)):
                out = ask(label+"별 "+metric+" TOP 10")
                self.assertEqual(out[key].iloc[0],code)
                self.assertEqual(out[metric].iloc[0], first)
                self.assertEqual(len(out),2)
            self.assertEqual(ask("담당자별 "+metric)[metric].sum(), sum(amounts))
        for q in ("담당자별 집계","담당자별 월별 집계"):
            out = ask(q)
            self.assertEqual(len(out), 3)
            self.assertEqual(out["미분류금액"].sum(), 19)
            self.assertEqual(sum(out[c].sum() for c in out if c.endswith("건수")),7)
        self.assertEqual(len(ask("담당자별 금액 TOP 10")),3)
        pd.testing.assert_frame_equal(ask("입출금부분명별 집계"), ask("입출금구분명별 집계"))
        self.assertEqual(len(ask("입금/출금별 집계")),3)
        with self.assertRaisesRegex(ValueError,"입출고"):
            ask("입출고구분명별 집계")
        with self.assertRaisesRegex(ValueError,"각각"):
            ask("월별 집계 현재표 거래처별 집계")

    def test_common_panel_chat_bridge_once_and_precision(self):
        from app.ui import sims_panel as panel
        from app.ui import chat_middleware as chat
        from tools.check_kpis_erp_direct import DirectChecks
        fixture_context = DirectChecks(methodName="runTest")
        fixture_context.setUp()
        self.addCleanup(fixture_context.doCleanups)
        state = fixture_context.state
        room = {"id": "synthetic-finance-room", "company_id": 8, "user_id": "42", "messages": [], "history": []}
        state.update(current_room=room["id"], chat_rooms=[room], __sims_selected={"category": "입출금", "action": TITLES["310"]})
        result = self.result(display_top=1)
        result["meta"]["_panel_submission_id"] = "synthetic-submission"
        with patch.object(common, "query_scope", return_value=SCOPE), patch.object(
                panel, "_render_panel_chat_only_done"), patch.object(chat, "_append_finalized_nlq_case"), patch.object(
                chat, "maybe_create_structured_presentation", return_value={"status": "skipped"}):
            panel._render_payload(result, TITLES["310"], submission_id="synthetic-submission")
            staged = state.pop("__sims_last_final_payload_for_chat")
            self.assertEqual(staged["meta"]["_panel_submission_id"], "synthetic-submission")
            self.assertIsInstance(staged["df"]["금액"].iloc[0], Decimal)
            pushed = chat.wssz(staged, action=TITLES["310"])
            self.assertIsNotNone(pushed)
            chat.drain_inbox_to_chat(room)
            chat.drain_inbox_to_chat(room)
            self.assertEqual(len(room["messages"]), 1)
            source = state["__sims_current_table_source_key"]
            self.assertEqual(state["sims_export_tables"][source]["금액"].tolist(), [AMOUNT, -AMOUNT, None])
            provenance = state["__sims_export_table_provenance_by_key"][source]
            self.assertEqual(provenance["query_scope"], SCOPE)
            self.assertEqual(provenance["row_count_total"], 3)
            from app.ui.current_table_followups.action_dispatcher import handle_current_table_followup_by_action
            notice = MagicMock()
            handle_current_table_followup_by_action(df=staged["df"], query="현재표 전체 건수", top_n=1,
                table_key=source, source_action=TITLES["310"], helpers={"push_notice":notice},
                log=None, source_meta=provenance)
            self.assertIn("3건", notice.call_args.kwargs["message"])
            panel._render_payload(result, TITLES["310"], submission_id="")
            self.assertNotIn("__sims_last_final_payload_for_chat", state)
            import json
            json.dumps(room["history"], ensure_ascii=False, default=str)

            # Exercise the main app's actual functions without executing its login/server startup.
            import ast
            import uuid
            import re
            from app.ui.current_table_followups import action_dispatcher as dispatch
            from app.ui import chat_middleware as cm
            module = ast.parse((ROOT/"app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8-sig"))
            fn_names = {"_current_table_get_latest_df", "_current_table_push_table", "_current_table_fill_alias_values",
                        "_current_table_is_blankish_value", "_merge_current_table_push_meta",
                        "_current_table_push_notice", "_partition_message_payload", "_compact_partition_value",
                        "_clip_partition_text", "_json_sanitize", "_normalize_current_table_followup_input",
                        "_try_handle_current_table_dataframe_followup", "_current_table_find_col", "_current_table_to_num"}
            module.body = [n for n in module.body if getattr(n, "name", "") in fn_names or
                           (isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and
                            n.targets[0].id in {"_CURRENT_TABLE_PUSH_META_PROTECTED_KEYS","_CHAT_PARTITION_META_ALLOW_KEYS",
                                                "_CHAT_PARTITION_MESSAGE_ALLOW_KEYS","CHAT_PERSISTENCE_DROP_KEYS"})]
            ns = dict(pd=pd, st=cm.st, log=logging.getLogger("offline"), uuid=uuid, Any=object,
                      re=re, _ANALYTICS_KPI_SOURCE_ACTIONS=set(),
                      handle_current_table_followup_by_action=handle_current_table_followup_by_action,
                      classify_current_table_followup_intent=dispatch.classify_current_table_followup_intent,
                      is_explicit_current_trans_doc_validation_request=dispatch.is_explicit_current_trans_doc_validation_request,
                      parse_current_table_rank_request=dispatch.parse_current_table_rank_request,
                      _CHAT_PARTITION_META_TEXT_LIMIT=8000, _CHAT_PARTITION_TEXT_LIMIT=20000)
            exec(compile(module, "<current_table_functions>", "exec"), ns)
            normalize = ns["_normalize_current_table_followup_input"]
            self.assertEqual(normalize("현재표 거래처명 별빛약국 조회"), "현재표 거래처명 별빛약국 조회")
            for question in ("현재표 입출금구분명 집계","현재표 금액 TOP 1"):
                self.assertTrue(ns["_try_handle_current_table_dataframe_followup"](
                    question, room=room, make_ts=None, next_seq=None))
                chat.drain_inbox_to_chat(room)
                self.assertEqual(state["__sims_current_table_source_key"],source)
                child=state["__sims_last_table_key"]
                self.assertNotEqual(child,source)
                exported=state["sims_export_tables"][child]
                meta=state["__sims_export_table_provenance_by_key"][child]
                self.assertEqual(meta["query_scope"],SCOPE)
                self.assertEqual(len(exported),1)
                self.assertEqual(exported["금액"].iat[0], 0 if "집계" in question else AMOUNT)
            self.assertEqual(len(room["messages"]),3)
            for question, status in (("거래처명 미등록 조회","no_data"),("잔액 계산","unsupported"),("전체 건수","success")):
                frame,key = ns["_current_table_get_latest_df"]()
                before = len(room["messages"])
                handle_current_table_followup_by_action(df=frame, query="현재표 "+question, top_n=20, table_key=key,
                    source_action=TITLES["310"], source_meta=provenance, log=None,
                    helpers={"push_table":ns["_current_table_push_table"],"push_notice":ns["_current_table_push_notice"]})
                chat.drain_inbox_to_chat(room)
                self.assertEqual(len(room["messages"]),before+1)
                item = room["messages"][-1]
                restored = ns["_partition_message_payload"](item)
                self.assertEqual(restored["meta"]["result_status"],status)
                self.assertEqual(restored["meta"]["query_scope"],SCOPE)
                self.assertEqual(restored["meta"]["source_table_key"],source)
                self.assertTrue(common.allow_result(restored["action"],restored["meta"]))
                self.assertFalse(common.allow_result(restored["action"],{}))
                with patch.object(common,"query_scope",side_effect=PermissionError):
                    self.assertFalse(common.allow_result(restored["action"],restored["meta"]))
            with patch.object(common, "query_scope", side_effect=PermissionError):
                self.assertEqual(ns["_current_table_get_latest_df"](),(None,""))
                self.assertFalse(common.allow_result(TITLES["310"],meta,export=True))


if __name__ == "__main__":
    unittest.main()
