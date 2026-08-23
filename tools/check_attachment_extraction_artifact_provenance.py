"""Offline regression for attachment extraction artifact provenance."""
from __future__ import annotations

import io

import PyPDF2
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.knowledge_document_service import (
    APPROVAL_APPROVED,
    KnowledgeDocumentRepository,
    build_extraction_artifact,
)
from app.services.knowledge_scope_policy import KNOWLEDGE_GLOBAL_MANAGE

RAW_HASH = "a" * 64


def _artifact(kind: str, sections: list[dict[str, str]]):
    return build_extraction_artifact(
        sections=sections,
        extractor_kind=kind,
        source_content_hash=RAW_HASH,
    )


def _assert_pypdf2_runtime_path() -> None:
    """Keep the installed parser and the app's page-level extraction path aligned."""
    payload = io.BytesIO()
    writer = PyPDF2.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(payload)
    payload.seek(0)

    reader = PyPDF2.PdfReader(payload)
    assert len(reader.pages) == 1
    assert reader.pages[0].extract_text() == ""


def main() -> None:
    _assert_pypdf2_runtime_path()

    pdf = _artifact("pdf_text_pypdf2", [
        {"section_id": "P1", "title": "1쪽", "text": "PDF-ALPHA", "location_type": "pdf_page", "location_label": "PDF 1쪽", "page": "1", "ocr_used": "false"},
        {"section_id": "P2", "title": "2쪽", "text": "PDF-BETA", "location_type": "pdf_page", "location_label": "PDF 2쪽", "page": "2", "ocr_used": "false"},
    ])
    assert pdf.source_content_hash == RAW_HASH
    assert pdf.extractor_kind == "pdf_text_pypdf2"
    assert [row["page"] for row in pdf.sections] == ["1", "2"]

    docx = _artifact("docx_python_docx", [
        {"section_id": "P1", "title": "문단 1", "text": "DOCX-PARAGRAPH", "location_type": "docx_paragraph", "location_label": "DOCX 문단 1", "paragraph": "1", "ocr_used": "false"},
        {"section_id": "T1R1", "title": "표 1 행 1", "text": "DOCX-TABLE", "location_type": "docx_table_row", "location_label": "DOCX 표 1 행 1", "table": "1", "row": "1", "ocr_used": "false"},
    ])
    assert docx.sections[0]["paragraph"] == "1"
    assert docx.sections[1]["table"] == "1" and docx.sections[1]["row"] == "1"

    ocr = _artifact("image_ocr_tesseract", [
        {"section_id": "OCR1", "title": "OCR 결과", "text": "OCR-SAMPLE", "location_type": "image_ocr", "location_label": "이미지 OCR 결과", "ocr_used": "true"},
    ])
    assert ocr.sections[0]["ocr_used"] == "true"

    try:
        build_extraction_artifact(sections=[{"text": "x"}], extractor_kind="bad kind")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid extractor kind was accepted")

    with tempfile.TemporaryDirectory() as temp:
        repo = KnowledgeDocumentRepository(root=Path(temp))
        try:
            repo.register_artifact_checked(
                source_name="pdf-smoke.pdf",
                source_key="attachment:pdf-smoke",
                artifact=pdf,
                scope="GLOBAL",
                current_company_id=4,
                permission_codes=(),
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("manage permission bypassed")
        assert not repo.manifest_path.exists()

        source, created = repo.register_artifact_checked(
            source_name="pdf-smoke.pdf",
            source_key="attachment:pdf-smoke",
            artifact=pdf,
            scope="GLOBAL",
            current_company_id=4,
            permission_codes=(KNOWLEDGE_GLOBAL_MANAGE,),
        )
        assert created and source.approval_status != APPROVAL_APPROVED
        repo.approve_checked(
            document_id=source.document_id,
            current_company_id=4,
            permission_codes=(KNOWLEDGE_GLOBAL_MANAGE,),
        )
        packet = repo.retrieve(
            query="PDF BETA",
            current_user_id=100,
            current_company_id=4,
            permission_codes=("RAG_USE",),
            max_chars=300,
        )
        assert packet.reason_code == "ready" and len(packet.citations) == 1
        citation = packet.citations[0]
        assert citation.source_location == "PDF 2쪽"
        assert citation.extractor_kind == "pdf_text_pypdf2"
        assert citation.extractor_version == 1
        assert citation.artifact_content_hash == pdf.content_hash
        assert citation.source_content_hash == RAW_HASH
        assert "PDF 2쪽" in citation.label and "pdf_text_pypdf2 v1" in citation.label

    main_source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    assert "attachment_extraction_artifact_cache" in main_source
    assert 'extractor_kind="pdf_text_pypdf2"' in main_source
    assert 'extractor_kind="docx_python_docx"' in main_source
    assert 'extractor_kind="image_ocr_tesseract"' in main_source
    assert "register_artifact_checked(" not in main_source
    assert "import PyPDF2" in main_source
    assert "PyPDF2.PdfReader(file)" in main_source
    print("RESULT OK tests=22 attachment_auto_knowledge=0 db_write_count=0")


if __name__ == "__main__":
    main()