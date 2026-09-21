# Snapshot DB 연결 장애 대응

## 원칙

Codex sandbox의 네트워크 실패를 production DB 또는 company profile 장애로 단정하지 않는다. 실제 검증은 프로젝트 venv, 프로젝트 root `.env`, production profile helper, production company resolver를 사용하는 호스트 Python 프로세스에서 수행한다.

## 정상 경로

1. 프로젝트 root에서 프로젝트 venv를 사용한다.
2. 기존 `load_project_env`로 root `.env`를 로드한다.
3. Dashboard profile은 `load_dashboard_profile_checked`로 읽는다.
4. ERP/analytics 연결은 기존 company resolver와 analytics target resolver를 사용한다.
5. 별도 connection string, 다른 `.env`, 서버·계정 하드코딩, 회사별 factory를 만들지 않는다.

## 장애 확인 절차

1. 최근 성공한 UI 또는 Snapshot 로그의 실행 주체와 helper 경로를 확인한다.
2. sandbox와 host 실행을 구분한다.
3. sandbox 실패라면 company 번호만 바꿔 반복하지 않는다.
4. host에서 profile read-only 1회, resolver `SELECT 1` 1회를 실행한다.
5. 성공하면 즉시 본래 generation 또는 inspection 작업으로 복귀한다.
6. 실패하면 예외 유형, SQLSTATE, 실행 주체, profile 단계/ERP 단계를 기록한다. 실제 서버·계정·비밀번호·connection string은 기록하지 않는다.

## generation lock

동일 회사·평가월·scope의 중복 generation은 fail-closed다. 프로세스 중단 후 lock 또는 draft가 보이면 실행 중 작업과 manifest 상태를 먼저 확인한다. stale lock 정리는 운영자 확인 없이 수행하지 않는다.

## 금지 사항

- 임의 connection string, 별도 `.env`, 서버/계정/비밀번호 하드코딩
- TLS 옵션 임의 변경
- 회사별 우회 factory
- 무제한 retry 또는 company 번호만 바꾸는 반복 연결
