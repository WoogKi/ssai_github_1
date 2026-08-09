# Dashboard 오늘의 조치·선택 상세 테스트 결과

## 1. 테스트 개요

- 테스트 일자: 2026-07-26
- 대상 브랜치: `feat/dashboard-actions-drilldown-20260726`
- 기준 커밋: `1ad2c69`
- 테스트 환경: 1호기
- 판정: 합격

## 2. 구현 범위

- 결정론적 Dashboard 오늘의 조치 facts와 우선순위 최대 10건 목록
- 순위, 상태, 대상, 판단 근거, 판정 기준, 권장 조치, 상세 보기
- 최신 Dashboard primary에서만 동작하는 상세 보기
- 캐시 `risk_detail_rows` 기반 선택 조치 상세
- 위험 품목 상세표 제품 검색 연동
- 로컬 UI 자동 스크롤 억제
- 과거 action schema 호환과 방 복귀 compact 읽기 전용 정책

## 3. 자동 테스트 결과

- py_compile: PASS
- runtime helper: PASS
- analytics regression: 129/129 PASS
- pip check: PASS
- git diff --check: PASS
- 신규 SQL: 0건
- DB/schema/data 변경: 0건
- callback explicit rerun: 0건
- action detail DB query: 0건
- action detail chat push: 0건
- 정상 Dashboard 원천 호출: `source_call_count=2`

## 4. 수동 테스트 시나리오와 결과

| 번호 | 테스트 항목 | 결과 | 판정 |
|---:|---|---|---|
| 1 | 첫 번째 상세 보기 클릭 | 선택 조치 상세 표시 | PASS |
| 2 | 선택 조치 상세 표시 | 조치 목록과 위험 품목 상세 사이에 표시 | PASS |
| 3 | 제품코드 정확 일치 | cached `risk_detail_rows`와 정확 일치 | PASS |
| 4 | 위험 상세표 연동 | 같은 제품코드로 검색값 연결 | PASS |
| 5 | 같은 상세 버튼 반복 클릭 | 별도 채팅 메시지 추가 없음 | PASS |
| 6 | 다른 조치 상세 보기 | 선택 상세가 새 제품으로 교체 | PASS |
| 7 | 채팅 메시지 중복 | 추가 push 없음 | PASS |
| 8 | DB 재조회 | 추가 SQL 없음 | PASS |
| 9 | 자동 스크롤 | 화면 맨 아래 이동 없음 | PASS |
| 10 | 위험 상세 토글 | 로컬 조작 시 자동 스크롤 없음 | PASS |
| 11 | 다른 방 이동 | 현재 primary 상태 정리 | PASS |
| 12 | 원래 방 복귀 | compact snapshot 시간순 표시 | PASS |
| 13 | compact 상호작용 | 상세 버튼, 상세표, Excel 없음 | PASS |
| 14 | 과거 `priority='높음'` snapshot | 숫자 변환 오류 없이 렌더 | PASS |

## 5. 로그 근거

- `[dashboard.action_detail] stage=rendered`
- `detail_match_count=1`
- `db_query_count=0`
- `chat_push_count=0`
- `[dashboard.scroll] suppress_consumed=True`
- `scroll_to_bottom=False`
- compact 렌더: `detail_rows_available=0`, `toggle_rendered=False`, `export_controls_allowed=False`

제품명, 회사명, DB명, 방 식별자, 이벤트 식별자는 본 문서에 기록하지 않는다.

## 6. 성능 참고와 잔여 과제

- 전체 제약사 범위 최초 Dashboard 조회: 약 72.66초
- stock shortage: 10,115행, 약 17.393초
- `source_call_count=2` 유지
- 선택 조치 상세는 캐시를 재사용하므로 DB 조회 0건

전체 범위 최초 조회 성능은 별도 최적화 과제이며, 이번 기능 마감의 차단 사유는 아니다.

## 7. 최종 판정

- Dashboard 오늘의 조치·선택 상세 기능: 합격
- main 병합, tag 생성, 2호기 배포: 전체 Dashboard 마감 후 별도 수행
