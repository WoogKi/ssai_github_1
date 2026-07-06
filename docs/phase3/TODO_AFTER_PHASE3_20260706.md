# TODO After Phase 3 - 2026-07-06

## 1. 다음 우선순위

Phase 3 마감 후 다음 작업 순서:

1. Git 기준점 생성
2. Codex Setting
3. 분석/KPI 느린 조회 속도 개선
4. 현재표 후속질문 사례 수집 후 보강
5. 운영 전 보안 설정 강화

## 2. KPI 속도 개선 후보

우선 대상:

- 품목별 매출 추세 요약표
- 품목별 매출 추세 분석
- 품목별 매출 예상
- 품목별 재고부족현황

분리 측정 대상:

- SQL 조회 시간
- pandas 가공 시간
- 화면 표시 시간
- 다운로드 준비 시간
- LLM 컨텍스트 생성 시간

## 3. 현재표 후속질문 보강 후보

사례 수집 후 보강:

- 직책 인원
- 부서별 인원
- 영업지역 없음 사용자
- 몇명
- 통계
- 전체

주의:

- "전체"를 사용자명 검색어처럼 처리하지 않도록 보강 필요
- 현재표 후속질문은 최신 회사의 현재표만 기준으로 해야 함

## 4. 운영 전 보안 강화

운영 전 검토:

- LM Studio Require Authentication ON
- LMSTUDIO_API_KEY 실제 토큰 반영
- .env Git 제외 확인
- 로그에 비밀번호 / 토큰 / 개인정보 노출 여부 점검
- 외부 접속 포트 제한
- IIS / Reverse Proxy / WebSocket 최종 확인

## 5. Git 관리

Phase 3 안정 기준점:

git tag phase3-stable-20260706

다음 개발 브랜치 후보:

git checkout -b phase4-kpi-performance

## 6. Codex 작업 원칙

Codex 사용 전 필수:

- AGENTS.md 작성
- .gitignore 정리
- phase3-stable tag 생성
- 작업 전 git status 확인
- 작업 후 py_compile 확인
- 민감 정보 포함 파일 수정 금지

## 7. 유지해야 할 핵심 정책

- SIMS 패널은 조회조건 입력 / 실행 전용
- 조회 결과는 채팅창에 1회 표시
- 현재표 후속질문은 현재 회사 기준 데이터만 사용
- 회사 변경 시 이전 회사 현재표 / context / export cache 재사용 금지
- 사용자목록에서 비밀번호 / 주민번호 계열 컬럼 노출 금지
- LLM timeout은 다중 사용자 Parallel=1 대기 시간을 고려
