# Knowledge·RAG 권한과 최신 소스 근거 공식 계약

- 버전: 1.0 / 기준일: 2026-09-15
- 기준 HEAD: e75fdc49872b03cc765207715d8bc118833cca9e
- 분류: GLOBAL / ERP_DB_INTERNAL
- 책임: 문서 열람·관리·회사격리·Project Source 최신성. 업무별 계산식은 각 업무 계약을 따른다.

## 실제 코드 authority

- `app/services/knowledge_scope_policy.py`: scope·분류 검증, can_read_document/can_manage_document
- `app/services/knowledge_role_policy.py`: 기본 역할별 권한 정책 (사용자에게 자동 권한을 부여하는 코드가 아님)
- `app/services/knowledge_document_service.py`: ACTIVE/APPROVED 후보·저장·승인·인용·소스 최신성
- `tools/knowledge_document_manage_cli.py`: 단일 DOCUMENT validate/apply 및 정확한 ID/version retire
- `tools/project_source_knowledge_cli.py`: 별도 Project Source 등록 절차

## 읽기 권한

| 자료 | 필요 조건 |
|---|---|
| DOCUMENT / GENERAL | RAG_USE, ACTIVE, 실제 조회 경로의 APPROVED 확인, scope 일치 |
| DOCUMENT / ERP_DB_INTERNAL | 위 조건 + KNOWLEDGE_ERP_DB_READ + 기술상세 모드 |
| PROJECT_SOURCE / GENERAL | RAG_USE + KNOWLEDGE_PROJECT_SOURCE_READ + 기술상세 모드 + scope·승인·최신성 |
| PROJECT_SOURCE / ERP_DB_INTERNAL | 위 소스 조건 + KNOWLEDGE_ERP_DB_READ |

실제 코드명은 `KNOWLEDGE_PROJECT_SOURCE_READ`이다. `PROJECT_SOURCE_READ`라는
별도 코드로 인가하지 않는다. GENERAL 분류라도 PROJECT_SOURCE는 일반 업무 문서가 아니다.
기술상세 모드는 실제 bool 조건으로 전달하며 문자열 'true' 같은 잘못된 입력은 차단한다.
정책 함수의 상태 검증은 ACTIVE이며 APPROVED는 repository 검색·인용 경계에서도 확인한다.

기본 정책상 SYSTEM_ADMIN/SSART_MANAGER는 기술 열람 가능하다.
SSART_STAFF/WHOLESALE_MANAGER/WHOLESALE_STAFF는 RAG_USE만으로 내부 기술 문서를 볼 수 없다.
WHOLESALE_READONLY는 기본 RAG_USE도 없다. 최종 권한은 현재 사용자·선택 회사의 실효권한이다.
선언 역할만으로 허용하거나 관리권한으로 읽기권한을 대신하지 않는다.

## 회사·사용자 범위

GLOBAL은 company_id/user_id=null이어야 한다. COMPANY는 양수 회사 ID와 선택 회사 일치를
요구하고 USER는 회사와 사용자 ID도 일치해야 한다. 관리자도 다른 회사 문서로 이 검사를
우회하지 않는다. 잘못된 소유자 정보를 null로 정규화해 GLOBAL로 넓히지 않는다.
문서 링크는 대상 문서의 열람 권한을 자동 부여하지 않는다.
회사별 실측 가격·운영 검증자료는 해당 회사 제한 문서로 분리한다.

## 관리·등록·퇴역

GLOBAL 관리는 KNOWLEDGE_GLOBAL_MANAGE, COMPANY 관리는 KNOWLEDGE_COMPANY_MANAGE와
회사 일치가 필요하다. USER 자료의 일반 관리 권한은 이 정책에 정의되어 있지 않다.
기술 DOCUMENT 등록 CLI는 ERP 기술 읽기 권한도 확인한다.
validate는 파일 검사이며 권한 DB/운영 write를 수행하지 않는다. apply는 실제 권한 DB 조회와
등록/승인을 수행한다. 승인 전 실행하지 않는다. 한 번에 DOCUMENT 1개만 허용한다.
동일 source_key/scope/company/user/version에 다른 내용·메타데이터를 덮어쓸 수 없다.
승인 시 같은 키와 동일 범위의 이전 ACTIVE만 SUPERSEDED된다. 범위 변경은 자동 대체가 아니다.
retire는 정확한 ID/version의 미리보기 후 별도 승인된 --apply로 수행하며 삭제와 다르다.

## Project Source 최신성

현재 운영 repository에 source_repo_root가 제공되면 PROJECT_SOURCE의 source_revision이
현재 Git HEAD와 같고 해당 파일이 존재하며 HEAD 대비 수정되지 않아야 최신 근거로 취급한다.
함수 본문이 같아도 revision이 다르면 최신으로 판단하지 않는다.
오래된 ACTIVE 소스는 현재 검색·도움말·인용 근거에서 차단하며 manifest의 ACTIVE 표시만으로
CURRENT라고 설명하지 않는다. 저장 artifact를 수정해서 revision만 바꾸지 않는다.
필요한 소스는 최신 승인 코드로 재추출·검증·새 version 등록을 별도 승인하고,
더 이상 필요 없는 소스는 정확한 ID/version으로 별도 퇴역을 검토한다.
DOCUMENT plan으로 PROJECT_SOURCE를 대신 등록하지 않는다.

## 운영 의존 기준

- [공통 Runbook](../03_runbook/RUNBOOK_SIMSAI.md): 등록 순서·서버 동기화·배포
- [2호기 Runbook](../03_runbook/RUNBOOK_2HO_OPERATION_CHECK.md): .venv·작업·Health·corpus 검증
- [업무 안내](../03_runbook/SIMS_AI_업무질문_사용_예시.md): GENERAL 업무 의미

Git 코드 배포와 운영 Knowledge corpus 배포는 별도다. manifest/artifact는 직접 편집하지
않으며 등록·퇴역·일괄복원 각각의 승인과 readback/검색 권한 검증을 수행한다.
