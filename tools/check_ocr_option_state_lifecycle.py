"""Static lifecycle contract checks for OCR option widget state."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _function_source(name: str) -> str:
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(SOURCE, node) or ""
    raise AssertionError(f"missing function: {name}")


def main() -> None:
    conf_source = _function_source("_ocr_conf_tuple")
    log_source = _function_source("_ocr_runtime_log_fields")
    callback_source = _function_source("_ocr_normalize_lang_widget_value")

    assert "st.session_state[" not in conf_source, "OCR config reader must not mutate widget state"
    assert "st.session_state[" not in log_source, "OCR runtime logger must be read-only"
    assert "_normalized_ocr_langs" in conf_source, "OCR config must normalize empty language state"
    assert 'st.session_state.setdefault("__ocr_langs", list(_OCR_DEFAULT_LANGS))' in SOURCE
    assert SOURCE.index('st.session_state.setdefault("__ocr_langs"') < SOURCE.index('key="__ocr_langs"')
    assert "on_change=_ocr_normalize_lang_widget_value" in SOURCE
    assert 'st.session_state["__ocr_langs"]' in callback_source
    assert "_normalized_ocr_langs" in callback_source

    print("PASS: OCR option widget state lifecycle")


if __name__ == "__main__":
    main()
