# Phase 3 Operation Runbook - 2026-07-06

## 1. 기본 경로

프로젝트 경로:

C:\New\Python_Project\LmStudion_project1

Streamlit 메인:

app\Lmstudio_SSAI_chat_main.py

## 2. 실행 명령

cd C:\New\Python_Project\LmStudion_project1

.\.venv\Scripts\python.exe -m streamlit run app\Lmstudio_SSAI_chat_main.py --server.port 8501 --server.address 0.0.0.0

## 3. 종료 명령

Get-Process streamlit -ErrorAction SilentlyContinue | Stop-Process -Force

## 4. 문법 검사

cd C:\New\Python_Project\LmStudion_project1

.\.venv\Scripts\python.exe -m py_compile `
  app\Lmstudio_SSAI_chat_main.py `
  app\ui\sims_panel.py `
  app\ui\chat_middleware.py `
  app\services\rddbc060_service.py

## 5. LM Studio 운영 권장값

Loaded Models: 1개
Model: google/gemma-3-27b
Parallel / Max Concurrent Predictions: 1
Context Length: 8192 ~ 11937
Temperature: 0.1
Limit Response Length: ON / 2048
Keep Model in Memory: ON
Flash Attention: ON
Offload KV Cache to GPU Memory: ON

## 6. .env 권장값

LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1
LMSTUDIO_API_KEY=lm-studio
LMSTUDIO_MODEL=google/gemma-3-27b
LLM_TIMEOUT_S=90
LLM_MAX_RETRY=1
SIMS_CODE_CACHE_TTL_S=300

## 7. 로그 확인 명령

최근 로그:

Get-Content logs\app.log -Tail 300

주요 이벤트 필터:

Get-Content logs\app.log -Tail 500 |
  Select-String "auth.login|auth.company|company.change.clear_sims|stashed table|chat.sims.push|llm.request|WARNING|ERROR|Traceback"

## 8. 회사 변경 테스트 절차

1. admin 로그인
2. company_id=1 선택
3. 사용자목록 + 부서명 조회
4. rows=24 확인
5. 현재표 사번 123 조회
6. company_id=4로 회사 변경
7. 회사 변경 직후 이전 rows=24가 다시 stash되지 않는지 확인
8. company_id=4에서 사용자목록 + 부서명 재조회
9. rows=227 확인

합격 기준:

회사 변경 직후 이전 회사 table_key / rows가 재사용되지 않아야 한다.

## 9. 민감 컬럼 확인

사용자목록 + 부서명 조회 후 로그의 columns_head에 아래 값이 없어야 한다.

비밀번호
Rd06_Password
Rd06_Password_ENCrypt
Rd06_Jumin
주민번호
SMS_PW
POL_PW
Work_PWD

## 10. LLM 테스트

일반 채팅 입력 후 로그 확인:

[llm.request] start ... timeout_s=90 max_retry=1
[llm.request] create_ok ...
[chat.assistant] ...

## 11. 장애 판정 기준

정상 INFO:

SIMS result has no DataFrame; skip context build
현재표 ... 자료 없음

점검 필요:

WARNING
ERROR
Traceback
ReadTimeout
ConnectionError
OutOfMemory
KV cache
