"""Exercise production prompt projection and Chat/Panel ownership without DB."""
from __future__ import annotations

import ast
import logging
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from app.sims.nlq.nlq_router import is_general_writing_request
    source = (ROOT / "app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    names = {
        "_is_sims_owned_history_message", "_build_current_room_compact_context",
        "_has_explicit_sims_context_reference", "_is_explicit_current_table_writing_request",
        "_should_dispatch_current_table_followup",
        "is_sims_related_question",
        "build_messages_with_system",
    }
    nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == len(names)
    query = "신규 거래처에 보낼 제품 소개 이메일 초안을 작성해줘"
    ordinary = {"role": "user", "content": "거래처 안내문은 정중하게 써줘"}
    result = {"role": "assistant", "action": "입고예정조회", "content": "STALE_SUMMARY", "meta": {"nlq": True}}
    unsupported = {"role": "assistant", "content": "STALE_UNSUPPORTED", "meta": {"action": "unsupported"}}
    room = {"id": "room", "company_id": "7", "messages": [ordinary, result, unsupported]}
    state = {"current_room": "room", "chat_rooms": [room]}
    ns = dict(re=re, time=time, Optional=Optional, st=SimpleNamespace(session_state=state),
              log=logging.getLogger("boundary"), is_general_writing_request=is_general_writing_request,
              is_sims_result_followup_question=lambda text: False,
              BASE_SYSTEM_PROMPT="business", GENERAL_SYSTEM_PROMPT="general",
              get_sims_context_data=lambda **kw: None,
              get_sims_context_text=lambda **kw: "STALE_TABLE",
              _room_matches_company=lambda room, company: room.get("company_id") == company,
              _normalize_chat_company_id=str, get_selected_company=lambda: {"company_id": "7"},
              _build_room_render_messages=lambda room: room["messages"],
              KNOWLEDGE_ANSWER_MESSAGE_TYPE="knowledge_answer",
              _clip_partition_text=lambda text, limit: text[:limit],
              _clip_for_model=lambda text: text, _script_perf_add=lambda *args: None)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "production_prompt", "exec"), ns)
    history = [ordinary, result, unsupported, {"role": "user", "content": query}]
    for _ in range(2):
        prompt = ns["build_messages_with_system"](history, user_text=query)
        content = str(prompt)
        assert "STALE_" not in content, content
        assert ordinary["content"] in content and query in content
    assert room["messages"] == [ordinary, result, unsupported]
    for explicit in ("현재표를 참고해서 이메일 작성", "위 결과를 바탕으로 이메일 작성", "방금 조회한 자료로 이메일 작성"):
        assert ns["is_sims_related_question"](explicit)
        prompt = ns["build_messages_with_system"]([], user_text=explicit)
        assert "STALE_TABLE" in str(prompt)
    for explicit_writing in (
        "현재표를 참고해서 신규 거래처에 보낼 제품 소개 이메일 초안을 작성해줘",
        "현재표를 바탕으로 안내문 작성해줘",
        "위 결과를 참고해서 거래처에 보낼 메일 작성해줘",
        "방금 조회한 자료로 보고 메일 초안 작성해줘",
    ):
        assert ns["_is_explicit_current_table_writing_request"](explicit_writing)
        assert not ns["_should_dispatch_current_table_followup"](
            explicit_writing,
            explicit_reference=True,
            implicit_analytics=False,
        )
    for analytics_followup in (
        "현재표에서 제조사별 매출 추세 보여줘",
        "현재표 기준으로 추세판정별 집계해줘",
        "현재표에서 부족등급별 분석해줘",
    ):
        assert not ns["_is_explicit_current_table_writing_request"](analytics_followup)
        assert ns["_should_dispatch_current_table_followup"](
            analytics_followup,
            explicit_reference=True,
            implicit_analytics=False,
        )
    room["company_id"] = "1"
    assert ns["_build_current_room_compact_context"](include_sims=False) == ""
    assert 'note = st.session_state.get("__sims_context_note")' not in source

    import pandas as pd
    from app.sims.nlq import nlq_vendors as vendors
    from app.services import rddbc030_service as service
    from app.sims.views import vendors as view
    panel = {"__vendors_ven_nm__0": "기존조건", "__sims_panel_last_final_payload": {"final": True, "params": {"ven_nm": "기존조건"}}}
    session = dict(panel)
    pushed = []
    with patch.object(service, "search_vendors_full", lambda **kw: pd.DataFrame({"거래처명": ["신규1", "신규2"], "거래처코드": ["00001", "00002"]})), patch.object(view, "_prepare_vendor_display", lambda df: df.copy()), patch.object(vendors, "push_sims_result_to_chat", lambda payload, *a, **kw: pushed.append(payload)), patch.object(vendors, "_build_vendor_master_llm_summary", None):
        assert vendors.try_handle_vendors_nlq("신규 거래처 조회", room={"messages": []}, session_state=session, make_ts=lambda: "fixture", next_seq=lambda: 1, logger=logging.getLogger("boundary"))
    assert pushed and pushed[-1]["action"] == "거래처 목록"
    assert all(session[key] == value for key, value in panel.items())
    assert session["__sims_ctx"]["action"] == "거래처 목록"
    print("PASS: writing prompt/history ownership, explicit context, rerun/company and Chat/Panel state")


if __name__ == "__main__":
    main()
