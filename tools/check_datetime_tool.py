"""Offline regression for deterministic Date/Time Tool routing and boundaries."""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.datetime_tool import OPERATING_TIMEZONE_NAME, month_range, operating_now, resolve_datetime_question, week_range

def _answer(question: str, now: datetime):
    answer = resolve_datetime_question(question, now=now)
    assert answer is not None, question
    assert answer.timezone_name == "Asia/Seoul"
    assert "기준 시각:" in answer.text and "Asia/Seoul" in answer.text
    return answer

def main() -> None:
    now = datetime(2026, 8, 22, 9, 5, 6, tzinfo=ZoneInfo("Asia/Seoul"))
    assert OPERATING_TIMEZONE_NAME == "Asia/Seoul"
    assert operating_now(now=now).tzinfo is not None
    try:
        operating_now(now=datetime(2026, 8, 22, 9, 5, 6))
    except ValueError:
        pass
    else:
        raise AssertionError("naive fixture time was accepted")
    assert "2026년 8월 22일" in _answer("오늘 날짜가 뭐야", now).text
    assert "09시 05분 06초" in _answer("지금 몇 시야", now).text
    assert "2026년 8월 21일" in _answer("어제 날짜", now).text
    assert "일요일" in _answer("내일은 무슨 요일이야", now).text
    assert "2026-08-17" in _answer("이번 주 기간", now).text
    assert "2026-08-10" in _answer("지난 주 기간", now).text
    assert "2026-08-24" in _answer("다음 주 시작일과 종료일", now).text
    assert "2026-07-01" in _answer("지난달 기간", now).text
    assert "2026-09-01" in _answer("다음달 시작일/종료일", now).text
    assert "월요일" in _answer("2026-08-24는 무슨 요일이야", now).text
    assert resolve_datetime_question("오늘 입고현황", now=now) is None
    assert resolve_datetime_question("이번 달 매출 추이를 보여줘", now=now) is None
    year_end = datetime(2026, 1, 1, 0, 1, tzinfo=ZoneInfo("Asia/Seoul"))
    assert month_range(year_end.date(), offset_months=-1).start.isoformat() == "2025-12-01"
    assert month_range(year_end.date(), offset_months=-1).end.isoformat() == "2025-12-31"
    assert week_range(year_end.date(), offset_weeks=-1).start.isoformat() == "2025-12-22"
    source = (ROOT / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    assert "resolve_datetime_question" in source
    datetime_route = "None if raw_mcp_route is not None else resolve_datetime_question(user_input)"
    assert datetime_route in source
    assert source.index(datetime_route) < source.index("is_sims_result_followup =")
    assert "[datetime.tool] result=stored" in source
    print("RESULT OK tests=18 llm_call_count=0 db_write_count=0")
if __name__ == "__main__":
    main()
