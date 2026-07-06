# app\utils\config_guard.py

import os, sys

REQUIRED_ENV = {
    "OPENAI_BASE_URL":  "LM Studio/OpenAI 호환 엔드포인트 URL (예: http://localhost:1234/v1)",
    "OPENAI_API_KEY":   "임의키 또는 lm-studio placeholder (빈 값 금지)",
    "MSSQL_DRIVER":     "ODBC Driver 18 for SQL Server",
    "MSSQL_SERVER":     "host,port 또는 host\\instance",
    "MSSQL_DATABASE":   "대상 DB명",
    # SQL Auth 사용 시
    # "MSSQL_UID":      "계정",
    # "MSSQL_PWD":      "비번",
}

def require_env():
    missing = []
    for k, hint in REQUIRED_ENV.items():
        if not os.getenv(k):
            missing.append(f"- {k}: {hint}")
    if missing:
        msg = "필수 환경변수가 누락되었습니다:\n" + "\n".join(missing)
        print(msg)
        sys.exit(2)

    # 드라이버 버전 안내
    drv = os.getenv("MSSQL_DRIVER","")
    if "ODBC Driver 18" not in drv:
        print(f"[경고] MSSQL_DRIVER='{drv}' → 'ODBC Driver 18 for SQL Server' 권장")

if __name__ == "__main__":
    require_env()
