# app/tools/inspect_runtime.py

VERSION = "chat_middleware/2025-11-01T-v1"

import importlib, inspect, json

CHECKS = {
    "app.ui.chat_middleware": [
        "drain_inbox_to_chat",
        "wire_chat_context",
        "render_sims_context_controls",
        "push_sims_result_to_chat",
        "get_current_room",
    ],
    "app.ui.chat_bridge": [
        "push_sims_table_message",
        "get_sims_context_text",
    ],
    "app.sims.views.users": [
        "render_user_list_with_dept",
        "render_user_count_by_dept",
        "render_recent_hires",
    ],
    "app.sims.views.codes": [
        "render_codes_by_group",
        "render_search_codes",
        "render_code_usage_example",
    ],
}

# 참고: CHECKS에 있는 각 모듈과 함수가 실제로 존재하는지 확인한다. 
# 존재 여부와 함수 시그니처를 출력한다. 
# 모듈이 로드되지 않았거나 정보 조회 중에 예외가 발생하면, "error" 필드에 예외 메시지를 담아서 반환한다.
# 참고: 모듈이 로드되어 있지 않으면 importlib.import_module이 ImportError를 발생시킬 것이다.
def main():
    out = {}
    for mod, funcs in CHECKS.items():
        try:
            m = importlib.import_module(mod)
            out[mod] = {}
            for f in funcs:
                ok = hasattr(m, f)
                sig = ""
                if ok:
                    try:
                        sig = str(inspect.signature(getattr(m, f)))
                    except Exception:
                        pass
                out[mod][f] = {"exists": ok, "sig": sig}
        except Exception as e:
            out[mod] = {"error": repr(e)}
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
