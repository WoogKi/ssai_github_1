"""Deployment inquiry scope and preserved UI boundaries; offline only."""
from __future__ import annotations

import ast
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_order_routes():
    from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
    from app.services.io_nlq import resolve_io_nlq
    from app.sims.nlq.nlq_router import resolve_new_sims_nlq_candidate
    for phrase in ("발주 보여줘", "발주내역", "발주현황", "발주 내역", "발주 현황"):
        for prefix in ("", "제약사 중외제약 "):
            text = prefix + phrase
            for resolver in (resolve_registered_erp_table_nlq, resolve_io_nlq):
                result = resolver(text)
                assert result["action"] == "발주조회", (text, result)
                assert result["params"]["mode"] == "order"
                if prefix:
                    assert result["params"]["maker_nm"] == "중외제약", result
            assert resolve_new_sims_nlq_candidate(text) == {"route": "io", "action": "발주조회"}


def test_future_intent():
    from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
    from app.services.io_nlq import resolve_io_nlq
    from app.sims.nlq.nlq_router import resolve_new_sims_nlq_candidate, try_handle_nlq
    for text in ("발주 계산", "권장발주", "제약사 중외제약 발주현황 말고 발주 계산", "제품코드 발주 계산"):
        assert resolve_registered_erp_table_nlq(text) is None
        assert resolve_io_nlq(text) is None
        assert resolve_new_sims_nlq_candidate(text) is None
        state, room = {}, {"messages": []}
        assert not try_handle_nlq(text, room, state, lambda: "", lambda: 1, logging.getLogger("fixture"))
        assert not state and not room["messages"]


def test_scope():
    from app.sims.nlq.action_inventory import CANONICAL_ACTIONS
    assert all(spec.canonical_action != "발주 계산" for spec in CANONICAL_ACTIONS)
    for path in ("app/services/order_calculation_service.py", "app/sims/views/order_calculation_view.py",
                 "app/ui/order_calculation_editor.py"):
        assert not (ROOT / path).exists()
    for path in ("app/ui/sims_panel.py", "app/ui/sims_entry.py", "app/ui/chat_middleware.py",
                 "app/sims/config.py", "app/sims/nlq/action_inventory.py"):
        assert "order_calculation" not in (ROOT / path).read_text(encoding="utf-8-sig")


def test_panel_close():
    tree = ast.parse((ROOT / "app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8-sig"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_render_sims_sidebar_fragment")
    node.decorator_list = []
    state = {"__sims_open": False, "__sims_panel_active": True, "__sims_keep_open_after_push": True}
    reruns = []
    fake = SimpleNamespace(session_state=state, markdown=lambda *a: None,
                           toggle=lambda *a, **k: False, rerun=lambda **kw: reruns.append(kw))
    namespace = {"st": fake, "log": logging.getLogger("fixture")}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "panel-close", "exec"), namespace)
    namespace[node.name]()
    assert not state["__sims_open"] and not state["__sims_panel_active"] and len(reruns) == 1
    namespace[node.name]()
    assert len(reruns) == 1


def test_product_identity():
    from app.ui.current_table_followups.generic import _build_common_group_summary
    from app.ui.current_table_followups.action_dispatcher import _requested_current_table_dimensions
    frame = pd.DataFrame({"제품코드": ["00001", "00002", "00003"], "제품명": ["same", "same", "other"],
                          "손익등급": ["A", "A", "B"]})
    grouped = _build_common_group_summary(frame, "손익등급")
    assert grouped["제품수"].sum() == 3
    assert grouped.loc[grouped["손익등급"] == "A", "제품수"].iloc[0] == 2
    for label, key in (("손익등급", "profit_grade"), ("기여도등급", "contribution_grade"),
                       ("출고빈도등급", "frequency_grade")):
        assert any(dim[0] == key for dim in _requested_current_table_dimensions("현재표 " + label + "별 분석"))


if __name__ == "__main__":
    tests = (test_order_routes, test_future_intent, test_scope, test_panel_close, test_product_identity)
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"PASS deployment scope {len(tests)}/{len(tests)} (offline)")
