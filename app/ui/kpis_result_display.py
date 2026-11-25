"""Plain-text projection of notice HTML; never mutate the provider response."""
from html import unescape
from html.parser import HTMLParser
import re


def with_product_mapping(frame, mappings, status):
    """One projection per KPIS row; no joining that multiplies API facts."""
    import pandas as pd

    out = frame.copy()
    labels = ("ERP 제품코드", "ERP 제품명", "ERP 규격", "ERP 제약사명", "ERP 매핑 상세",
              "ERP 매핑 건수", "ERP 이력중복 건수", "ERP 매핑 상태")
    groups = {}
    if status == "ready" and not mappings.empty:
        for standard, group in mappings.groupby("표준코드", sort=False):
            group = group.sort_values(["제품코드", "제품명", "규격", "제약사명"], na_position="last")
            text = lambda v: "" if pd.isna(v) else str(v)
            values = ["\n".join(text(v) for v in group[col]) for col in ("제품코드", "제품명", "규격", "제약사명")]
            detail = "\n".join(" / ".join(text(row[c]) for c in ("제품코드", "제품명", "규격", "제약사명"))
                               for row in group.to_dict("records"))
            count = group["제품코드"].nunique()
            duplicates = int(group["연결원천건수"].sum()) - count
            # Excel cells cannot retain arbitrary-size mapping lists. Report, never truncate.
            if any(len(v) > 30000 for v in values + [detail]):
                groups[standard] = [""] * 5 + [None, None, "매핑 표시 상한 초과"]
            else:
                groups[standard] = values + [detail, count, duplicates,
                    ("다중 제품" if count > 1 else "단일 제품") + (" / 이력 중복" if duplicates else "")]
    rows = []
    for standard in out["표준코드"]:
        if status != "ready":
            value = [""] * 5 + [None, None, "ERP 매핑 조회 실패"]
        elif not isinstance(standard, str) or not re.fullmatch(r"[0-9]{13}", standard):
            value = [""] * 5 + [None, None, "표준코드 형식 확인"]
        else:
            value = groups.get(standard, [""] * 5 + [0, 0, "매핑 없음"])
        rows.append(value)
    # Repeated API history may repeat a large mapping in every exported row.
    if sum(sum(len(v) for v in row[:5]) for row in rows) > 8 * 1024 * 1024:
        rows = [[""] * 5 + [None, None, "매핑 표시 전체 상한 초과"] for _ in rows]
    for i, label in enumerate(labels):
        out[label] = [row[i] for row in rows]
    return out


class _NoticeText(HTMLParser):
    hidden_tags = frozenset({"script", "style", "head", "iframe", "object", "svg", "math", "template"})
    blocks = frozenset({"p", "div", "section", "article", "header", "footer", "address", "blockquote",
                        "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "pre", "table", "tr", "hr"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden_tags:
            self.hidden.append(tag)
        elif not self.hidden:
            if tag == "br" or tag in self.blocks:
                self.parts.append("\n")
            elif tag in {"td", "th"}:
                self.parts.append("\t")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in self.blocks:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in {"script", "style"}:
            # HTML does not close these raw-text elements with '/>'; fail closed.
            self.handle_starttag(tag, attrs)
        elif tag not in self.hidden_tags:
            self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def notice_plain_text(value):
    if not isinstance(value, str):
        return value
    decoded = value
    while True:
        next_value = unescape(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    parser = _NoticeText()
    parser.feed(decoded)
    parser.close()
    text = "".join(parser.parts).replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def attachment_label(value):
    if isinstance(value, str):
        return {"Y": "있음", "N": "없음"}.get(value.strip().upper(), "미확인")
    return "미확인"


def render_notice_detail(frame, *, key):
    """채팅의 기존 표에서 상세만 표시한다. 조회·게시·현재표 교체는 하지 않는다."""
    import streamlit as st

    if frame.empty or not {"공지번호", "제목", "내용"}.issubset(frame.columns):
        return
    with st.expander("공지 상세내용", expanded=False):
        picked = st.selectbox(
            "공지번호 / 제목", range(len(frame)), index=None,
            format_func=lambda i: f"{frame.iloc[i]['공지번호']} / {frame.iloc[i]['제목']}",
            key="__external_api_notice_" + str(key),
        )
        if picked is not None and 0 <= picked < len(frame):
            row = frame.iloc[picked]
            st.text(str(row["제목"]))
            st.text(notice_plain_text(row["내용"]) if isinstance(row["내용"], str) else "")
            if "첨부여부" in frame.columns:
                st.caption("첨부파일: " + str(row["첨부여부"]))
