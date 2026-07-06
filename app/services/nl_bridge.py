# app/services/nl_bridge.py
from app.services import erp_queries as q

def answer_nl(question: str) -> str:
    # TODO: 규칙 기반 파싱 or LLM chain
    # 예시: "지난 7일 출하" → recent_shipments(7)
    if "출하" in question and "7일" in question:
        df = q.recent_shipments(7)
        return f"최근 7일 출하 건수: {len(df)}"
    return "아직 이 질문 유형은 학습되지 않았어요."
