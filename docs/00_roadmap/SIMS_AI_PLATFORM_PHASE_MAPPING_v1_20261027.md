# Phase 3~8 세부 항목 대응표

상세 행의 현 상태·옛/새 일정·완료 Gate는 [88행 CSV](SIMS_AI_PLATFORM_TRACEABILITY_v1_20261027.csv), 변경 판단은 [비교서](SIMS_AI_PLATFORM_ROADMAP_COMPARISON_v1_20261027.md)에 있다. 이 표는 **원문 절이 새 계획에서 사라지지 않았는지** 확인하기 위한 색인이다. 원문은 각각 J21(6/21), V21(7/19), V23(8/9), LMX(LM Studio 확장), S03(9/3 Git 기준), S15L(9/15 Git 미추적 로컬 판본)로 약칭한다.

| 원문 Phase·절/체크리스트 | 추적 ID | 신규 관리 위치·판정 |
| --- | --- | --- |
| J21 Phase3 설계·DB·보안·로그인·회사·사용자 분리·회귀 / V21 3.2.1~3.2.5 | P3-01~05 | Phase3 운영 유지·배포 증적 분리 |
| J21 Phase4 입출금 조회/NLQ·미수금 조회/NLQ | P4-01~02, S03-06 | Phase4 회계 조회 후보. 구현 근거 미확인·정책/권한 선행 |
| J21 Phase4 통계·거래처/영업사원/제품 KPI·후속분석/다운로드 | P4-03~06 | Phase4/4.5 지표 계약과 현재표 안정화 |
| V21 Phase4A 표시 제한·전체 summary·Arrow·다중 조건 | P4-06~07 | Phase4 UX/전체 원본 경계 |
| V21 Phase4B metric/grain/기간/부분월 | P4-08, P4-11~12 | Phase4/4.5 지표 표준화 |
| V21 Phase4C Profile·facts·LLM 설명 | P4-09, P4-13~17 | Phase4 LLM 구조화 근거/실제 품질 분리 |
| V21 Phase4D SQL·후처리·화면·저장 성능 | P4-10, P4-19, S03-02~03 | Phase4 대량 Dashboard와 공통 계약 |
| V21 08A~08G 분석/KPI 조사의 전 경로·facts 계약·Profile·Prompt·회귀·수동 품질 | P4-11~17 | 각 작업의 완료/부분/운영 검증을 따로 표시 |
| V21 09 입고·출고명세 계수 의미 / 10 성능 | P4-18~19 | Phase4 명세 Exact와 성능 Gate |
| V21 4.5-1 Validation Framework | P45-01 | Snapshot Exact 기반과 범용 검증을 분리 |
| V21 4.5-2 AI 집계 DB | P45-02, P45-09~10 | 통합 월별 IO Fact 구현; 나머지 집계 후보는 별도 수요 검토 |
| V21 4.5-3 통계 DB | P45-03 | 통계 정의/저장 계약 미완료 |
| V21 4.5-4 Feature Store | P45-04 | ML 데이터셋 이후 조건부 |
| V21 4.5-5 KPI Library | P45-05 | 개별 계산과 중앙 지표 사전 분리 |
| V21 4.5-6 AI Dashboard | P45-06 | 현재 Dashboard 품질/성능 안정화 |
| V21 4.5-7 AI Alert | P45-07 | 독립 알림 운영은 후순위 후보 |
| V21 4.5-8 ML Dataset | P45-08, P5-01~04 | 누수 없는 검증 자료 준비 |
| J21 Phase5 출고·거래처·제품·영업사원 월별 Dataset | P5-01~04 | Phase5 학습용 Dataset 미완료 |
| J21 Phase5 이동평균/전월/전년 baseline·RF/XGBoost/LSTM·평가·화면 | P5-05~08 | 규칙 예측/발주와 학습 ML을 분리 |
| V21 Phase5-1 매출예상 ML | P5-01~08 | 학습 모델은 미착수, 규칙 baseline은 사용 |
| V21 Phase5-2 재고부족 예측 | P5-09 | 규칙 위험 분류와 ML 위험 예측 분리 |
| V21 Phase5-3 발주 추천 / 9/15 실발주 후보 | P5-10~11, S03-05 | 발주계산 1차 완료, 실제 ERP 쓰기는 정책 보류 |
| V21 Phase5-4 AI Score | P5-12 | 지표·검증 후 조건부 |
| J21 Phase6 문서·ERP 요약 RAG·chunk·embedding/vector·context·권한 | P6-01~06 | Knowledge 핵심과 검색 고도화/ERP 원천 연동 분리 |
| V21 Phase6-1 업무지식 / 6-2 RAG | P6-01~05, P6-10 | 승인 corpus·품질 운영 |
| V21 Phase6-3 그룹웨어 / 6-4 HIRA API / 6-5 뉴스·제도 / 6-6 문서 권한 | P6-06~09 | 외부 권한/연결 미완료와 Web/권한 기반 구분 |
| V23 Phase4.x·LMX 문서 주입/RAG | PX-01 | Phase6 Knowledge 운영과 연결 |
| V23 Phase4.x·LMX 현재 시각 / Web / 첨부 | PX-02~04 | 독립 Tool·첨부 품질 Gate |
| V23 Phase4.x·LMX VLM / OCR / STT | PX-05~07 | 기능별 PoC와 운영 수용 구분 |
| V23 Phase4.x·LMX Structured Output / Tool routing | PX-08~09 | 사후 trace·실행 소유권 구분 |
| V23 Phase4.x·LMX Tool/MCP / Embeddings | PX-10~11, S03-07~08 | **독립 MCP 트랙**, 검색 심화는 조건부 |
| J21 Phase7 실패 사례/데이터셋·LoRA/QLoRA·GGUF·비교 | P7-01~04 | Feedback 수집 선행, 학습은 필요성·익명화 승인 후 |
| V21 Phase7-1 LoRA/QLoRA / 7-2 GGUF | P7-02~04 | 학습 미착수 |
| V21 Phase7-3 Workflow / 7-4 Agent / 7-5 Feedback | P7-01, P7-05~06 | Agent는 MCP PoC 완료만으로 시작하지 않음 |
| V21 Phase8-1 사용설명 / 8-2 문의 / 8-3 장애 | P8-01~03 | 내부 지식·Runbook과 고객 서비스 분리 |
| V21 Phase8-4 설치 / 8-5 교육 / 8-6 그룹웨어 / 8-7 개발자 | P8-04~07 | 서비스별 권한·SLA·감사·복구 Gate |
| S03 9월 Snapshot·Dashboard·대량 계약·R070·발주·입출금·MCP·Embedding 일정 / S15L 로컬 변경 | S03-01~08, P45-09~10 | 선행/지연 이력을 10/27 상황으로 재판정; S15L은 Git 공식 증거 아님 |

## 확인 결과와 제외 경계

J21의 Phase3~7, V21의 Phase3~8 및 4.5 8개 절, V23/LMX의 지능 확장 10개 목표와 S03/S15L 일정 전환을 모두 위 ID 집합에 연결했다. **명시적으로 삭제·폐기한 개발 목표는 0건**이다. 동명의 기능이 둘 이상의 계획에 실린 경우 CSV의 기존문서버전 열에 출처를 합쳤다. 실제 코드가 없는 영역은 미착수 또는 확인 부족으로 남겼으며 '완료'로 보정하지 않았다.

향후 범위를 실제로 없애거나 합치려면 이 표의 해당 ID·원문 링크·업무 영향·사용자 승인 기록을 남긴다. 특히 v2.1의 집계/통계 후보 테이블명은 설계 예시라서 테이블을 모두 만들지 않는다는 판단과, KPI/ML/외부 API라는 **목표 자체를 삭제한다는 판단**은 구분한다.
