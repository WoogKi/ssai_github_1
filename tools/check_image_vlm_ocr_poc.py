"""Offline contract checks for the OCR/VLM comparison harness."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = (ROOT / "tools" / "image_vlm_ocr_poc.py").read_text(encoding="utf-8")
    required = (
        "pytesseract.image_to_string",
        'lang="kor+eng"',
        'config="--psm 3 --oem 3"',
        '"type": "image_url"',
        '"input_mode": "actual_image_data_url"',
        '"extractor_kind": "image_ocr_tesseract"',
        '"extractor_kind": "lmstudio_vlm_image_url"',
        "max_retries=0",
        "--screen",
        "--document",
        "--photo-secondary",
    )
    assert all(token in source for token in required)
    assert "app/Lmstudio_SSAI_chat_main.py" not in source
    print("RESULT OK tests=11 ocr_vlm_provenance_separate no_ui_integration=1")


if __name__ == "__main__":
    main()