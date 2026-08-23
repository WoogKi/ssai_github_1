"""One-shot OCR/VLM comparison harness. It never changes attachment or LM Studio settings."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from openai import OpenAI
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.utils.env_config import load_project_env

MODEL_FALLBACK = "google/gemma-4-26b-a4b-qat"
TESSERACT_FALLBACK = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ocr_existing_path(image_path: Path) -> tuple[str, dict[str, Any]]:
    """Match app.ocr_image_pil defaults: kor+eng, psm/oem 3, upscale+binarize."""
    if TESSERACT_FALLBACK.exists():
        pytesseract.pytesseract.tesseract_cmd = str(TESSERACT_FALLBACK)
    image = Image.open(image_path)
    image = ImageOps.exif_transpose(image).convert("RGB")
    width, height = image.size
    if max(width, height) < 1600:
        scale = 1600 / max(width, height)
        image = image.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
    image = ImageOps.grayscale(image).point(lambda value: 255 if value > 128 else 0, mode="1").convert("L")
    image = ImageEnhance.Contrast(image).enhance(1.2)
    started = time.perf_counter()
    text = pytesseract.image_to_string(image, lang="kor+eng", config="--psm 3 --oem 3").strip()
    return text, {
        "method": "ocr_only",
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "extractor_kind": "image_ocr_tesseract",
        "extractor_version": str(pytesseract.get_tesseract_version()).splitlines()[0],
        "ocr_used": True,
        "input_mode": "pixels_to_tesseract",
    }


def _vlm_image_call(*, client: OpenAI, model: str, image_path: Path, instruction: str, ocr_text: str = "") -> tuple[str, dict[str, Any]]:
    raw = image_path.read_bytes()
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    image_url = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    suffix = f"\nOCR 참고 텍스트(원문 오류 가능):\n{ocr_text}" if ocr_text else ""
    started = time.perf_counter()
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": instruction + suffix},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }],
    )
    text = str(response.choices[0].message.content or "").strip()
    return text, {
        "method": "ocr_plus_vlm" if ocr_text else "vlm_only",
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "extractor_kind": "lmstudio_vlm_image_url",
        "extractor_version": model,
        "ocr_used": bool(ocr_text),
        "input_mode": "actual_image_data_url",
        "ocr_reference_sha256": _sha256(ocr_text.encode("utf-8")) if ocr_text else "",
    }


def compare_image(*, label: str, image_path: Path, client: OpenAI, model: str) -> dict[str, Any]:
    raw = image_path.read_bytes()
    base = {
        "label": label,
        "source_name": image_path.name,
        "source_content_hash": _sha256(raw),
        "source_location": str(image_path),
    }
    ocr_text, ocr_meta = _ocr_existing_path(image_path)
    instruction = (
        "이미지 자체를 보고 다음을 구분해 짧게 설명하세요: 보이는 글자/숫자, 표·레이아웃, "
        "장면·물체·상황. 보이지 않는 내용은 추측하지 말고 '확인 불가'라고 쓰세요."
    )
    vlm_text, vlm_meta = _vlm_image_call(client=client, model=model, image_path=image_path, instruction=instruction)
    combined_text, combined_meta = _vlm_image_call(client=client, model=model, image_path=image_path, instruction=instruction, ocr_text=ocr_text)
    return {
        **base,
        "ocr_only": {**ocr_meta, "text": ocr_text},
        "vlm_only": {**vlm_meta, "text": vlm_text},
        "ocr_plus_vlm": {**combined_meta, "text": combined_text},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screen", required=True, type=Path)
    parser.add_argument("--document", required=True, type=Path)
    parser.add_argument("--photo", type=Path)
    parser.add_argument("--photo-secondary", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    for label, path in (
        ("screen", args.screen),
        ("document", args.document),
        ("photo", args.photo),
        ("photo_secondary", args.photo_secondary),
    ):
        if path is not None and not path.is_file():
            raise SystemExit(f"{label} fixture not found: {path}")

    load_project_env(override=True)
    client = OpenAI(
        base_url=os.environ["LMSTUDIO_BASE_URL"],
        api_key=os.environ.get("LMSTUDIO_API_KEY", "lm-studio"),
        timeout=90,
        max_retries=0,
    )
    model = os.environ.get("LLM_MODEL_DEFAULT") or os.environ.get("LMSTUDIO_MODEL") or MODEL_FALLBACK
    results = [compare_image(label="screen", image_path=args.screen, client=client, model=model), compare_image(label="document", image_path=args.document, client=client, model=model)]
    if args.photo is not None:
        results.append(compare_image(label="photo", image_path=args.photo, client=client, model=model))
    if args.photo_secondary is not None:
        results.append(compare_image(label="photo_secondary", image_path=args.photo_secondary, client=client, model=model))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"model": model, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ok", "model": model, "images": len(results), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()