# app/services/llm_health.py
import os
from openai import OpenAI

def check_llm():
    base_url = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
    api_key  = os.getenv("LMSTUDIO_API_KEY", "lm-studio")
    client   = OpenAI(base_url=base_url, api_key=api_key)

    # 모델 리스트
    models = client.models.list()
    names  = [m.id for m in models.data]

    # 짧은 채팅 핑 테스트(선택)
    # client.chat.completions.create(model=names[0], messages=[{"role":"user","content":"ping"}], max_tokens=5)

    return {
        "ok": True,
        "base_url": base_url,
        "models": names[:20],
        "count": len(names)
    }
