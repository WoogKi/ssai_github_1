"""DB-free event validation and isolated browser fixture for the real component."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
from app.ui import kpis_erp_query as ui
from app.services import rddbc030_service as vendors


class Events(unittest.TestCase):
    def test_replay_stale_and_invalid_selection_do_not_inject_numbers(self):
        state = {}
        rows = pd.DataFrame([("50001", "합성", "0000000001")], columns=["ERP 거래처코드", "ERP 거래처명", "사업자번호"])
        with patch.object(ui, "st", SimpleNamespace(session_state=state)), patch.object(
            ui, "_fresh_actor", return_value=SimpleNamespace(company_id=8)), patch.object(
            vendors, "search_vendor_candidates", return_value=rows) as search:
            def emit(i, kind, term="합성", **kwargs):
                return ui._vendor_event((8, "42", 1), "v", "MA102", "number", dict(id=i, kind=kind, term=term, **kwargs))
            emit(1, "search")
            emit(1, "search")
            emit(2, "search")
            search.assert_called_once()
            emit(3, "select", "old", index=0)
            emit(4, "select", index=-1)
            emit(5, "select", index=True)
            self.assertNotIn("number", state)
            emit(6, "select", index=0)
            self.assertEqual(state["number"], "0000000001")
            ui._clear_vendor_selection("v", "number", edited=True)
            self.assertIn("v_autofill", state)
            emit(7, "invalidate", "변경")
            self.assertEqual(state["number"], "")
            self.assertTrue(state["v_dirty"])
            state["number_input_1"] = "0000000001"
            ui._vendor_number_edited("v", "number", "number_input_1")
            self.assertEqual(state["number"], "")
            emit(8, "select", index=0)
            self.assertEqual(state["number"], "")
            emit(9, "search", "")
            self.assertFalse(state["v_dirty"])
            search.assert_called_once()

    def test_type5_and_type6_success_contract(self):
        import httpx
        from dataclasses import replace
        from tools.check_kpis_erp_direct import DirectChecks
        f = DirectChecks(methodName="runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        for code in ("5", "6"):
            result = f.call(request=replace(f.req, service="MA102"),
                query={"msupSplyFrmCd": code, "tkrHbin": "0000000001", "ykiho": "00000001"},
                handler=lambda req: httpx.Response(200, json={"result": "success", "msupBscqList": [{"bscqNm": "", "bscqRegYn": "Y", "cloDd": ""}]}))
            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(result["physical_call_count"], 1)


_fixture = None


def browser_fixture():
    """Run only in a separate local Streamlit process. All ERP/HTTP is mocked."""
    global _fixture
    import json
    import time
    import streamlit as st
    from streamlit.runtime.state import SessionStateProxy
    import httpx
    from dataclasses import replace
    from tools.check_kpis_query_ui import QueryUI
    from app.services import kpis_erp_direct as direct
    from app.services.kpis_rest_client import KpisClient
    from app.ui import ssai_login as login
    from app.services.ssai_login_profile_service import ERP_IDENTITY_KEY
    if _fixture is None:
        fixture = QueryUI(methodName="runTest")
        fixture.setUp()
        f = fixture.f
        f.searches = []
        f.pushes = []
        f.slow = False
        rows = pd.DataFrame([("50001", "합성거래처", "000-00-00001"),
                             ("50002", "합성거래처", "0000000002"),
                             ("50003", "번호없음", None)], columns=["ERP 거래처코드", "ERP 거래처명", "사업자번호"])
        def search(company, **conditions):
            f.searches.append((company, conditions))
            if f.slow:
                time.sleep(1.2)
            if conditions.get("ven_nm_kw") == "다수":
                frame = pd.DataFrame([(f"{50000+i}", "합성", f"{i:010d}") for i in range(200)], columns=rows.columns)
                frame.attrs["kpis_candidates_partial"] = True
                return frame
            if conditions.get("ven_nm_kw") == "없음":
                return rows.iloc[:0]
            return rows
        client = KpisClient(direct.ErpDirectAccess(audit=f.events.append), lambda: f.actor, enabled=True,
                            erp_key_loader=f.key, transport=httpx.MockTransport(f.handler))
        for guard in (patch.object(login, "st", SimpleNamespace(session_state=f.state)),
                      patch("streamlit.session_state", SessionStateProxy()),
                      patch.object(ui, "load_current_api_session", side_effect=lambda: f.actor),
                      patch.object(ui, "configured_direct_client", return_value=client),
                      patch.object(vendors, "search_vendor_candidates", side_effect=search),
                      patch("app.ui.chat_middleware.wssz", side_effect=lambda payload, **kwargs: f.pushes.append(1) or {})):
            guard.start()
            fixture.addCleanup(guard.stop)
        _fixture = fixture
    f = _fixture.f
    st.set_page_config(page_title="Synthetic KPIS UI test", layout="wide")
    service = st.selectbox("Test service", ["MA102", "MA110"])
    f.slow = st.checkbox("Synthetic slow search")
    if st.button("Synthetic company switch"):
        company = 9 if f.actor.company_id == 8 else 8
        f.actor = replace(f.actor, company_id=company, epoch=f.actor.epoch+1)
        f.state[login.SESSION_COMPANY] = {"company_id": company, "is_active": True}
        f.state[ERP_IDENTITY_KEY] = replace(f.identity, company_id=company)
    if st.button("Synthetic rerun"):
        pass
    ui.render_kpis_erp_query(service)
    st.code(json.dumps({"searches": len(f.searches), "api": len(f.sent), "push": len(f.pushes),
                        "last_search": f.searches[-1][1] if f.searches else None,
                        "auto_owned": any(str(k).endswith("_autofill") for k in st.session_state),
                        "dirty": any(bool(v) for k, v in st.session_state.items() if str(k).endswith("_dirty"))}), language="json")


if __name__ == "__main__":
    unittest.main()
