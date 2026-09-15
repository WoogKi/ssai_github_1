# Knowledge Wave 3 공식 기준 보강·등록 준비

- 기준일: 2026-09-14 / 기준 HEAD: e75fdc49872b03cc765207715d8bc118833cca9e
- 성격: 운영자 검토 기록. 이 보고서 자체는 RAG 등록 제외.
- 운영 등록·manifest/artifact 변경·ERP 접근·Snapshot 데이터 변경: 수행하지 않음.
- 최신 파일은 기준 HEAD 이후 이번 문서 보강분을 포함한다.

## 문서 부족 영역과 보강 방식

M: 마스터·가격 기술 계약, L: 제품재고장 기술 계약, S: Snapshot·제품정보 기술 계약,
U: 일반 업무 안내. 위 네 문서는 독립 공식 기준이 없던 영역에 신규 작성한다.
기존 업무가이드·발주·현재고·기간정책·Dashboard 문서는 대체하지 않는다.

| 영역 | 부족 사항 | 보강 문서 | 구분 | 상위·관련 기준 |
|---|---|---|---|---|
| 거래처 마스터 | 역할·활성·회사별 식별 | M/U | 신규 | 공통 권한·회사격리 |
| 제품 마스터·제품정보 | 마스터와 승인 통계 차이 | M/S/U | 신규 | 공통 제품조건 |
| 제조사·제약사 | 별도 역할·후보 확인 | M/U | 신규 | 기존 entity resolver |
| 대표매입처 | 임의 마스터 대체 금지 | M/U | 연결 | 기존 발주 계약 선정 기준 |
| 단가적용처 | 독립 필드·기본코드 적용 범위 | M/U | 신규·연결 | 기존 발주 가격 계약 |
| 재고적용처 | 독립 필드·위치와 차이 | M/U | 신규·연결 | 기존 발주/현재고 계약 |
| 재고위치 | 코드명·집계 기준 | M/L/U | 신규 | 현재고 계약 |
| R070 계약단가 | 기준일·시작일·이력·동률 제한 | M/U | 신규 | 기간정책·발주 기간 누출 방지 |
| R230 구매원가·최종매입가 | 상태 다차원 행, 거래 최신값과 차이 | M/U | 신규 | 발주 가격 fail-closed |
| 제품분류·구분·그룹 | 독립 조건·그룹별 코드명 | M | 신규 | 공통 제품조건 |
| 보험코드·바코드·규격 | 식별·정확 검색·표시 | M/U | 신규 | 제품 서비스 |
| 제품 사용여부 | 명시 조건의 실제 값 | M/U | 신규 | 제품 서비스 |
| 제품 추가·수정 | 조회 이력, 쓰기 기능 아님 | M/U | 신규 | 제품 서비스 |
| 회사별 코드명 | 현재 회사 authority | M/U | 신규 | 현재표 회사격리 |
| 마스터관리·NLQ | 현재 목록·상세·조건 범위 | M/U | 신규 | 업무가이드·기간정책 |
| 제품재고장 | 기간·집계·가격모드 제한 | L/U | 신규 | 현재고·현재표 |
| Snapshot 2.1 lifecycle/profile | 운영/초안·버전·F·원 통계 | S/U | 신규 | Dashboard·발주 |
| 현재고·CurrentTable | 충분한 독립 기준 있음 | 기존 문서 | 재사용 | Wave 2 전용 plan |
| Dashboard·KPI 공통조건 | 운영/설계 구분 유지 | 기존 문서 | 재사용 | Wave 2 전용 plan |
| 공식 Calendar·입고예정 | 이번 대상과 연결만 | 기존 발주/기간 문서 | 재사용 | 계산식 재작성 없음 |

## 등록 단위·권한·우선순위

모든 신규 계획은 DOCUMENT, version=1, GLOBAL, company_id/user_id=null이다.
등록 직전 운영 corpus에 같은 source_key가 생겼는지 재확인한다. 현재 운영본과
내용이 다르면 임의 덮어쓰기하지 않고 버전 전략을 다시 승인받는다.

| 순서 | 문서 | source_key | 권한 분류 | 별칭 수 |
|---|---|---|---|---|
| 1 | U: SIMS_AI_MASTER_INVENTORY_USER_GUIDE.md | document:sims-ai-master-inventory-user-guide | GENERAL | 6 |
| 2 | M: SIMS_MASTER_PRICE_QUERY_CONTRACT.md | document:sims-master-price-query-contract | ERP_DB_INTERNAL | 8 |
| 3 | L: PRODUCT_INVENTORY_LEDGER_CONTRACT.md | document:product-inventory-ledger-contract | ERP_DB_INTERNAL | 5 |
| 4 | S: SNAPSHOT_PRODUCT_INFORMATION_V21_CONTRACT.md | document:snapshot-product-information-v21-contract | ERP_DB_INTERNAL | 7 |

GENERAL 검색에도 RAG_USE가 필요하다. ERP_DB_INTERNAL은 RAG_USE와
KNOWLEDGE_ERP_DB_READ 및 기술 상세 모드를 요구한다. 기본 정책에서 SYSTEM_ADMIN,
SSART_MANAGER는 기술 열람 가능하고 SSART_STAFF 이하는 차단한다. 실효권한을
기준으로 판정하며 역할 이름만으로 통과시키지 않는다.
새 문서에 회사별 실데이터·가격 사례를 넣지 않아 COMPANY 문서 분리는 불필요하다.

## 확인된 제한·충돌

1. 제품재고장 계약단가 모드는 현재 제품 기준가 대체 경로다. 완료된 R070 연결로
   설명하지 않는다. production 수정 없이 제한을 명시했다.
2. R230 이름의 '최종'은 거래 시각 최신 한 건을 보장하지 않는다. 다차원 원가 상태다.
3. R070 최신 시작일 동률의 추가 선택 기준은 현재 코드에 없다. 유일성 보장을 하지 않는다.
4. Snapshot 읽기는 하위 운영 버전도 확인한다. 2.1 필드 완비와 회사별 생성 완료는 별도다.
5. 과거 master/io/analytics 요약과 월별 마트 shadow 설계는 운영 authority로 복원하지 않는다.

## 검증·승인

`tools/check_knowledge_wave3_authority.py`는 문서·링크·분류·계획 유효성·별칭과
원본 hash를 파일만으로 검사한다. 기존 마스터/R070/R230/제품정보/수명주기·통계
회귀검사와 py_compile, git diff --check도 수행한다. 실행 결과는 작업 완료 보고에서
별도로 판정하며 코드 검토만으로 실제 사용자 화면 PASS를 주장하지 않는다.

실제 등록 전 문서 범위·분류·신규 source_key·버전 승인과 2호기 corpus 확인이 필요하다.
승인 이후에도 문서별 등록→readback/hash→일반/기술 권한 검색 Smoke 순으로 진행한다.

### 실행 결과

| 검사 | 결과 |
|---|---|
| Wave 3 문서·링크·분류·source_key 중복 검사 | PASS |
| 관리 CLI validate, 신규 계획 4개 | 4/4 PASS, 각각 write_count=0 |
| 마스터 자연어 회귀 | 23/23 PASS |
| R070 계약단가·R230 구매원가 회귀 | 각각 PASS |
| 제품정보 분석 문맥 회귀 | PASS, DB/LLM 요청 없음 |
| Snapshot 수명주기·버전 호환 회귀 | 11/11 PASS |
| Snapshot 2.1 통계·가격·checksum 회귀 | 8/8 PASS |
| 전체 Analytics 회귀 | RESULT: OK |
| 필수 7개 Python 파일과 신규 검사 도구 py_compile | PASS |
| git diff --check | PASS |

검사는 기존 fixture와 파일 검증으로 수행했다. 일반 Python 실행에서 Streamlit의
runtime/session 경고가 발생했으며, R070의 소유 action 불일치 교정 fixture도 경고를
출력했다. 검사 실패나 예기치 않은 Traceback은 없었다. Git은 기존 LF/CRLF 변환
주의를 출력했다. 실제 사용자 화면·2호기 검색·운영 등록은 이번 검증 범위가 아니다.

### 원문·등록 본문 SHA-256

각 문서에서 원문 파일 hash와 validate가 읽은 본문 hash는 동일하다.

| 문서 | SHA-256 |
|---|---|
| U | c71226cb1eca8eb5efdc67dc0e4f766ebfe51fc0e82ebbd6eff18da21b81b063 |
| M | 6317b2eebc07fd7acf7c44ce1fdf7fccf856b8c961bd1180c63d60e5b93900a3 |
| L | 50702c3ec1d0237f1d532c67c7b9c18bc34831b77204f66247e7b1fdc681b515 |
| S | 833d1473f08305b77b9c8dd9f3529e99c1b0f0776ca18044812cfcbdd26bdb16 |

## 2호기 수동 복사 목록

저장소의 같은 상대경로로 아래 파일을 복사한다. 대응 원문과 계획은 반드시 함께 복사한다.

- docs/03_runbook/SIMS_AI_MASTER_INVENTORY_USER_GUIDE.md
- docs/03_runbook/SIMS_AI_MASTER_INVENTORY_USER_GUIDE.knowledge.json
- docs/02_design/SIMS_MASTER_PRICE_QUERY_CONTRACT.md
- docs/02_design/SIMS_MASTER_PRICE_QUERY_CONTRACT.knowledge.json
- docs/02_design/PRODUCT_INVENTORY_LEDGER_CONTRACT.md
- docs/02_design/PRODUCT_INVENTORY_LEDGER_CONTRACT.knowledge.json
- docs/02_design/SNAPSHOT_PRODUCT_INFORMATION_V21_CONTRACT.md
- docs/02_design/SNAPSHOT_PRODUCT_INFORMATION_V21_CONTRACT.knowledge.json
- docs/README.md
- docs/04_test_results/KNOWLEDGE_WAVE3_AUTHORITY_REVIEW_20260914.md
- tools/check_knowledge_wave3_authority.py

보고서·검사 도구는 운영자 검토용이며 RAG 본문에 등록하지 않는다.
운영 knowledge_poc manifest/artifact, .env와 기존 Snapshot 변경 파일은 복사 대상이 아니다.
이 복사는 문서 준비일 뿐 운영 등록이 아니다. 2호기의 실제 corpus 상태는 이번에 확인하지 않았다.
