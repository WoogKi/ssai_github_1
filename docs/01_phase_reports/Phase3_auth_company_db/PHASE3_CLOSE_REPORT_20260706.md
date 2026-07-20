# Phase 3 Close Report - 2026-07-06

## 1. Phase 3 목표

Phase 3의 목표는 SS AI / SIMS AI의 운영 기반을 다중 사용자 환경에 맞게 안정화하는 것이다.

주요 범위:

- 로그인 / 인증
- 사용자 권한
- 회사별 ERP DB 선택 및 전환
- 사용자별 채팅방 분리
- 사용자별 파일 저장소 분리
- SIMS 조회 결과 채팅창 단일 표시
- 현재표 후속질문 컨텍스트 유지
- 회사 변경 시 stale current table 차단
- 민감 컬럼 제거
- LM Studio 다중 사용자 운영 안정화

---

## 2. 완료 항목

### 2.1 로그인 / 권한

완료:

- SSART_ADMIN 로그인
- SSART_USER 로그인
- WHOLESALE_USER 로그인
- 회사 자동 선택
- 회사 변경
- 권한별 기능 차단
- 파일 업로드 권한 차단
- Excel 다운로드 권한 차단
- KPI_READ 권한 차단

판정: 합격

---

### 2.2 회사별 ERP DB 전환

완료:

- 회사별 DB 접속정보 분리
- 회사 선택 시 해당 ERP DB로 ODBC 연결
- company_id 기준 DB 전환
- company_id=1, company_id=4 조회 결과 분리 확인

검증 예:

- company_id=1 사용자목록: 24건
- company_id=4 사용자목록: 227건

판정: 합격

---

### 2.3 사용자별 채팅방 분리

완료:

- 사용자별 chat_rooms JSON 분리
- user_id 기준 채팅 이력 저장
- 다른 사용자의 채팅방 미노출

예:

- C:\SSAI_TEST_DATA\chat\user_1_chat_rooms.json
- D:\SSAI_DATA\chat\user_8_chat_rooms.json
- D:\SSAI_DATA\chat\user_12_chat_rooms.json
- D:\SSAI_DATA\chat\user_13_chat_rooms.json
- D:\SSAI_DATA\chat\user_22_chat_rooms.json

판정: 합격

---

### 2.4 사용자별 저장소 분리

완료:

- 회사별 / 사용자별 저장소 분리
- uploads
- downloads
- reports
- temp
- logs

예:

- company_1/user_1/uploads
- company_1/user_8/uploads

판정: 합격

---

### 2.5 SIMS 조회 UX 정책

확정 정책:

- SIMS 패널은 조회조건 입력 / 조회 실행 전용
- 조회 결과는 채팅창에 1회만 표시
- 조회조건, 요약, 표, 다운로드는 채팅 메시지에 포함
- 현재표 후속질문용 내부 컨텍스트는 유지
- 중복 렌더링 제거

판정: 합격

---

### 2.6 민감 컬럼 제거

사용자목록 + 부서명 조회에서 아래 컬럼은 화면 / 채팅 / LLM 컨텍스트 / 다운로드에서 제거한다.

- Rd06_Password
- Rd06_Password_ENCrypt
- 비밀번호
- 주민번호
- Rd06_Jumin
- SMS/POL/Work password 계열

테스트 결과:

- columns_head에 비밀번호 계열 미노출
- 채팅 push rows/cols 정상
- 다운로드 정상

판정: 합격

---

### 2.7 자료 없음 / 미지원 로그 정리

기존 WARNING:

- SIMS result has no DataFrame; skip context build

변경 후:

- INFO 레벨로 조정

정상 케이스:

- 조회 결과 0건
- 현재표 후속질문 자료 없음
- 현재표 후속분석 미지원

판정: 합격

---

### 2.8 LM Studio timeout / retry 정리

권장 운영값:

- LLM_TIMEOUT_S=90
- LLM_MAX_RETRY=1
- LM Studio Parallel=1
- Loaded Models=1개

테스트 결과:

- 일반 채팅 로그에서 timeout_s=90 max_retry=1 확인
- LLM 응답 정상
- ERROR / Traceback 없음

판정: 합격

---

### 2.9 회사 변경 stale current table 차단

이슈:

- 회사 변경 직후 이전 회사 현재표가 다시 stashed table로 잡히는 현상 발생

조치:

- 회사 변경 시 current table / context / export cache 초기화
- payload에 company stamp 추가
- company_id / db_name mismatch 시 stash / push / current source 승격 차단

최종 테스트:

- company_id=1 사용자목록 24건 조회
- 현재표 사번 123 후속조회 2건 정상
- company_id=4로 회사 변경
- 이전 24건 재-stash 없음
- company_id=4 사용자목록 227건 정상 조회

판정: 합격

---

## 3. 최종 테스트 판정

| 항목 | 판정 |
|---|---|
| 로그인 / 권한 | 합격 |
| 회사별 DB 전환 | 합격 |
| 사용자별 채팅방 | 합격 |
| 사용자별 저장소 | 합격 |
| SIMS 조회 UX | 합격 |
| 민감 컬럼 제거 | 합격 |
| 자료 없음 로그 INFO 처리 | 합격 |
| LM Studio timeout/retry | 합격 |
| 회사 변경 stale table 차단 | 합격 |

---

## 4. 남은 이슈

Phase 3 마감 후 다음 작업으로 이관한다.

1. 분석/KPI 느린 조회 속도 개선
2. 현재표 후속질문 사례 추가 수집 후 보강
3. 운영 전 LM Studio API 인증 ON
4. IIS WebSocket 설정 최종 확인
5. Codex 작업 규칙 정리
6. Git 기준점 유지

---

## 5. Phase 3 최종 결론

Phase 3는 운영 안정화 기준으로 마감 가능하다.

마감 기준:

- 다중 사용자 기본 운영 가능
- 회사별 DB 전환 가능
- 사용자별 채팅/파일 분리 가능
- 민감 정보 노출 차단
- 회사 변경 시 이전 회사 현재표 재사용 차단
- LM Studio 단일 모델 / Parallel 1 운영 안정화

최종 판정: Phase 3 합격
