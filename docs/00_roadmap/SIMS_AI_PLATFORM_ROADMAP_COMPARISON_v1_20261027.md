# SIMS AI 과거 계획과 공식 v2.4 비교 (2026-10-27)

이 문서는 [마스터 v2.4](SIMS_AI_PLATFORM_MASTER_ROADMAP_v2.4_20261027.md)와 [일정 v1.3](SIMS_AI_PLATFORM_EXPECTED_SCHEDULE_v1.3_20261027.md)을 과거 버전과 연결한다. [88개 추적 행 CSV](SIMS_AI_PLATFORM_TRACEABILITY_v1_20261027.csv)는 ID·원문 버전·옛 Phase/항목·목표일·현재 구현/검증/운영 상태·근거·새 Phase/일정·변경유형·사유·완료 Gate로 필터링할 수 있다. 같은 목표의 중복 표현은 의미 단위로 묶되 원문 출처를 모두 적었다. Git 공식 기준은 9/3 추적본이며, `S15L`은 공식 승인 미확인 9/15 로컬 판본을 뜻한다.

## 원문 버전 색인

| 코드 | 원본(이 파일 기준 상대경로) | 사용 기준 |
| --- | --- | --- |
| J21 | 로컬 Archive `docs/90_archive/roadmap_versions/SS AI Phase 3~7 실행 계획 및 체크 보고서20260621.md` (Git 미추적) | 초기 범위·예상 마감. 체크박스는 현 상태 증거 아님. GitHub 원문 링크는 별도 보존 승인 전 제공하지 않음 |
| V21 | 로컬 Archive `docs/90_archive/misc/root/SIMS_AI_PLATFORM_MASTER_ROADMAP_v2.1_20260719.md` (Git 미추적) | Phase 3~8, 4A~4D/08A~10, 4.5의 8개 세부항목. GitHub 원문 링크는 별도 보존 승인 전 제공하지 않음 |
| V23 | [8/9 Master v2.3](../90_archive/roadmap_versions/SIMS_AI_PLATFORM_MASTER_ROADMAP_v2.3_20260809.md) | LM Studio 4.x 10개 PoC, 이후 Phase 3~8 |
| LMX | [LM Studio 확장 계획](../02_design/LM_STUDIO_INTELLIGENCE_EXTENSION_PLAN.md) | Tool/MCP·멀티모달/검색 별도 계약 및 당시 예상일 |
| S03 | [9/3 로드맵](SIMS_AI_PLATFORM_ROADMAP_20260903.md)·[일정](SIMS_AI_PLATFORM_EXPECTED_SCHEDULE_20260903.md) | Git 추적 기준. 본문은 9/14 상태까지 갱신됨 |
| S15L | `SIMS_AI_PLATFORM_ROADMAP_20260915.md`와 `SIMS_AI_PLATFORM_EXPECTED_SCHEDULE_20260915.md` | 로컬 파일은 존재하나 Git 미추적·Ignore 대상. 공식 승인 여부 미확인. 내용 비교 근거일 뿐 Git 공식 상태 증거 아님 |
| O27 | 10/27 공유 폴더의 `roadmap_review_20261027.md`와 1차 초안 | HEAD와 선행 Gate 기준의 로컬 조사. 2호기 현 설치 커밋 미확인 |

## 근거 ID 색인

| ID | 실제 확인 경로/범위 |
| --- | --- |
| E_AUTH | `app/services/ssai_auth_service.py`, `ssai_permission_policy.py`, `ssai_storage_service.py`; `docs/01_phase_reports/Phase3_auth_company_db/PHASE3_CLOSE_REPORT_20260706.md` |
| E_DEPLOY | Git 미추적 9/15 로컬 판본의 당시 2호기 Smoke **서술**. 설치·승인 독립 증거로 검증하지 않았으며 10/27 설치 상태는 미조회 |
| E_DASH | `app/services/dashboard_lite_facts.py`, `analytics_sales_trend_service.py`; 10/26 공유 보관 `status_followup_20261026.txt`의 회사3 Exact 기록과 그 뒤의 회사40 수동 로그 3개. 회사40 기존 상세 응답 192.929초·329.386초, Compact 83.873초(228,974행·R120 SQL 56.851초). 회사40 전체 결과 Exact·2호기 검증은 미완료 |
| E_LLM | `app/ui/current_table_followups/analysis_facts.py`, `app/Lmstudio_SSAI_chat_main.py`; 공유 폴더 10/24 LLM 분석 후속 상태 기록과 O27 |
| E_SNAP | `app/services/dashboard_inventory_frequency_snapshot_service.py`, `sql_server_monthly_io_repository.py`; 공유 폴더 10/25 게시 기록·10/27 준비 점검 기록 |
| E_MONTH | `tools/build_monthly_io_company.py`, `approve_monthly_io_company.py`, `check_company_month_end_readiness.py`; 공유 폴더 10/27 증분 승인·안전성 기록 |
| E_ORDER | `app/services/order_calculation_service.py`; [발주 1차 Closeout](../04_test_results/ORDER_CALCULATION_PHASE1_CLOSEOUT_20260914.md), 공유 폴더 10/27 발주 43/43 기록 |
| E_FORECAST | `app/services/analytics_customer_sales_forecast_service.py`의 규칙 계산; 학습 ML 완료 근거 아님 |
| E_RAG | `app/services/knowledge_document_service.py`, `knowledge_scope_policy.py`, `knowledge_role_policy.py`; [Knowledge 읽기 전용 감사](../04_test_results/KNOWLEDGE_FINAL_READONLY_AUDIT_20260915.md) |
| E_WEB | `app/services/web_search_service.py`, `tools/check_web_search.py`; MCP 외부 Resource와 별도 |
| E_ATTACH | `app/services/attachment_document_followup.py`, `app/Lmstudio_SSAI_chat_main.py`의 첨부 경로 및 관련 Gate |
| E_MULTI | `app/services/stt_service.py`, `tools/image_vlm_ocr_poc.py`, `tools/check_attachment_ocr_ui_runtime.py`; 기능별 운영 수용 별도 |
| E_TOOL | `app/services/datetime_tool.py`, `structured_tool_routing.py`, `structured_response_contract.py`, `structured_presentation_poc.py` |
| E_MCP | `app/services/mcp_adapter.py`, `app/ui/mcp_chat_adapter.py`, `tools/mcp_servers/official_recall_notice_server.py`; 공유 폴더 9/1 Phase2 Mock·Phase3 STDIO·최종 보정 기록. [MCP 현황](../02_design/SIMS_AI_MCP_CURRENT_STATE_AND_NEXT_PHASE_20261027.md) |
| E_FB | `app/services/chat_feedback_service.py`, `nlq_feedback_log_service.py` |
| E_GAP | 해당 독립 기능의 구현/운영 완료 근거를 현 소스·공식 최신 보고서에서 확인하지 못함. 없음을 절대 증명한 것은 아니며 승인 전 소유자 재확인 필요 |

## 주요 이동·분할·일정 차이

| 항목 | 기존 계획 | 새 제안과 이유 | CSV 행 |
| --- | --- | --- | --- |
| Phase3 | 6월 계획의 7월 중순 완료 | 당시 운영 기반 완료 판정은 유지하되 신규 회사/권한 기능의 회귀와 실제 2호기 설치 확인을 분리 | P3-01~05 |
| Phase4 업무 확장 | 6월 계획은 8월 말 입출금·미수금·통계/KPI 일괄 완료 | 구현 근거 없는 회계 조회는 정책/권한 선결로 미루고, 기존 조회·현재표·LLM 품질을 11월 우선 안정화 | P4-01~19 |
| Phase4.5 | 7월 v2.1은 집계/통계 DB·Feature Store·KPI Library까지 8개 목표 | 실제 Snapshot 2.1·통합 Fact/승인 도구를 완료 범위로 분리; 범용 Feature/KPI/Alert는 미완료. 202611 월마감 후 표준화 | P45-01~10 |
| Phase5 | 6월은 매출예정 ML 9월 말, v2.1은 ML 전제 | 규칙 기반 발주·예측 1차는 선행 완료. 11월 업무 고도화를 ML 플랫폼 완료까지 미루지 않음; 학습형 ML은 데이터셋/평가 뒤 | P5-01~12 |
| Phase6 | 6월 RAG 11월 초, v2.1 문서/그룹웨어/HIRA/뉴스 | Knowledge/RAG 핵심 선행 구현을 보존, 11~12월 운영 품질·권한·문서 확대. 외부 API/그룹웨어는 승인 조건부 | P6-01~10 |
| **LM Studio 4.x/MCP** | v2.3은 Tool/MCP PoC를 4.x, 9/15 문서는 외부 MCP를 Phase6 장기 대기로 간략화 | Mock+로컬 STDIO 기술 PoC는 완료. **독립 Tool/MCP 통합**으로 복원하고 11월 월마감 뒤 승인된 외부 1개 read-only PoC만 검토. Agent에 종속하지 않음 | PX-01~11, S03-07 |
| Phase7 | 6월 LoRA/QLoRA 12월 말, v2.1 Agent/Workflow/Feedback 확장 | Feedback 수집 기반만 부분 완료. 학습/Agent/Workflow는 필요성·익명화·승인/복구 Gate 뒤로 | P7-01~06 |
| Phase8 | v2.1 고객지원/교육/설치/그룹웨어/개발자 지원 | 내부 Runbook/RAG와 고객 공개 서비스를 분리. 현재 고객 대상 서비스 완료 근거 없음 | P8-01~07 |
| 9월 실행창 | 9/3 일정: Snapshot·대량 계약·발주·입출금·외부 MCP | 실제 선행/지연을 9/15 기록과 10/27 HEAD로 재판정. 10/30·11/1 월마감 보호 | S03-01~08 |

## 누락 검사와 보류 항목

- J21 Phase3의 설계·DB·보안·로그인·회사·격리·회귀는 P3-01~05, Phase4의 입출금/미수금/통계·KPI/후속분석/다운로드는 P4-01~06, Phase5의 거래처/제품/담당자 Dataset·모델·평가·화면은 P5-01~08, Phase6 문서/ERP 요약/vector/context/권한은 P6-01~06, Phase7 학습자료/LoRA/GGUF/평가는 P7-01~04에 연결했다.
- V21의 Phase4A~D/08A~10은 P4-07~19, Phase4.5 1~8은 P45-01~08, Phase5 1~4는 P5-05~12, Phase6 1~6은 P6-01~09, Phase7 1~5는 P7-01~06, Phase8 1~7은 P8-01~07에 대응한다. 원래 후보 테이블명은 **설계 예시**이지 모두 생성할 의무가 아니다. 대체·분할 사유는 CSV에 남겼다.
- V23/LMX의 문서·시각·Web·첨부·VLM·OCR·STT·Structured Output·Tool/MCP·Embedding 10개는 PX-01~11(Structured Tool을 분리)로 모두 추적한다. S03의 주요 실행창은 S03-01~08로 보완했다. 단순 중복 항목은 하나의 새 완료 Gate로 통합했으며 **무단 제외 항목은 없다.**
- 입출금·미수금, Feature Store, 학습 ML/AI Score, Agent/LoRA, 고객 서비스는 미완료를 명시했다. Embedding, 외부 MCP, 실발주, R210/R220·미결 매출 정책은 **보류**이며 제거가 아니다. 실제 제외/범위 축소는 업무 소유자 승인 없이는 하지 않는다.

CSV의 운영 상태는 이번 DB/2호기/실제 LM Studio 접속 결과가 아니다. 2026-10-27 현재 소스와 선행 보고서에 근거한 판정이며, 실제 운영 회사 상태는 별도 승인된 점검이 필요하다.
