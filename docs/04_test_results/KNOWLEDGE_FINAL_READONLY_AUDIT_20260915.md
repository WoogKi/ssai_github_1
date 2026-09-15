# Knowledge/RAG 최종 읽기 전용 점검

검사일: 2026-09-15. 기준 HEAD: `e75fdc49872b03cc765207715d8bc118833cca9e`.
이 문서는 검사 기록이며 운영 Knowledge 등록 대상이 아니다.

> 후속 완료: 같은 날 Wave 1~5 DOCUMENT 등록과 발주 공통 계약 GLOBAL v2 전환,
> 양 서버 stale PROJECT_SOURCE v9 퇴역을 완료했다. 최종 1호기 읽기 전용 확인은
> ACTIVE/APPROVED DOCUMENT 23건, ACTIVE PROJECT_SOURCE 0건이다. 아래 제2~7절의
> 개수와 퇴역 명령은 퇴역 전 조사 시점 기록이며 다시 실행하지 않는다.

## 1. 1호기 → 2호기 수동 복사 필요 파일

- 검사 실행에 필수: `tools/check_knowledge_final_readonly_smoke.py`.
- 비교에 필요한 1호기 결과: `docs/04_test_results/knowledge_final_readonly_host1_20260915.json`.
- 보고용: 이 문서. 운영 등록 원문이나 등록 계획은 이번 작업에서 변경하지 않았다.
- 2호기의 최신 원문 및 기존 `.knowledge.json` 계획은 현재 1호기와 동일해야 한다. Git 제외 계획이 없으면 기존 Wave 1~5 계획도 복사해야 원문 해시 비교가 가능하다.
- 운영 manifest/artifact는 복사하거나 변경하지 않는다.

## 2. 현재 등록 현황

1호기 실제 저장소: `C:\SSAI_TEST_DATA\knowledge_poc`.

| 항목 | 개수 |
|---|---:|
| 전체 버전 기록 | 37 |
| ACTIVE/APPROVED | 24 |
| SUPERSEDED | 10 |
| RETIRED | 3 |
| Wave 1~5 공식 DOCUMENT | 14 |
| 과거 ERP 구조 DOCUMENT | 9 |
| PROJECT_SOURCE | 1 |

공식 DOCUMENT 14개는 GENERAL 2개, ERP_DB_INTERNAL 12개이다.
기술 문서 12개 중 GLOBAL 11개, COMPANY 7 문서 1개이다.
동일 source_key/소유 범위의 ACTIVE 중복은 없다. 모든 37개 등록 artifact의 무결성 검사가 통과했다.
정확한 document_id, 별칭, 전체 해시 및 검사 결과는 함께 저장한 JSON에 수록했다.

### 공식 등록본과 대표 검색 질문

아래 질문은 실제 저장소의 별칭을 사용했다. 관리자 기술상세 조건에서 16개 모두 기대 source_key를 찾았으며 기본 검색 분량에서도 통과했다.

| 질문/업무 | 기대 source_key | 버전 | 범위 | 분류 |
|---|---|---:|---|---|
| SSAI에서 어떤 질문을 할 수 있어 | `document:sims-ai-business-question-examples` | 3 | GLOBAL | GENERAL |
| 마스터 정보 업무 안내 | `document:sims-ai-master-inventory-user-guide` | 1 | GLOBAL | GENERAL |
| 현재고 current-table 계약 | `document:sims-nlq-stock-current-table-contract` | 1 | GLOBAL | ERP_DB_INTERNAL |
| Legacy Dashboard 현재 운영 설계 | `document:dashboard-lite-v01-design` | 1 | GLOBAL | ERP_DB_INTERNAL |
| Dashboard KPI NLQ 공통 조회조건 | `document:dashboard-kpi-nlq-common-query-contract` | 1 | GLOBAL | ERP_DB_INTERNAL |
| 거래처 제품 마스터 조회 계약 | `document:sims-master-price-query-contract` | 1 | GLOBAL | ERP_DB_INTERNAL |
| R070 최종 계약단가 이력 | `document:sims-master-price-query-contract` | 1 | GLOBAL | ERP_DB_INTERNAL |
| R230 구매원가 상태 | `document:sims-master-price-query-contract` | 1 | GLOBAL | ERP_DB_INTERNAL |
| 제품재고장 조회 계약 | `document:product-inventory-ledger-contract` | 1 | GLOBAL | ERP_DB_INTERNAL |
| Snapshot 2.1 운영 계약 | `document:snapshot-product-information-v21-contract` | 1 | GLOBAL | ERP_DB_INTERNAL |
| NLQ 기본기간 정책 | `document:sims-ai-nlq-period-contract` | 1 | GLOBAL | ERP_DB_INTERNAL |
| 발주 계산 공식 계약 | `document:order-calculation-phase1-business-contract` | 2 | GLOBAL | ERP_DB_INTERNAL |
| 발주 1차 배포 마감 / 회사7 발주 검증 | `document:order-calculation-phase1-closeout-20260914` | 1 | COMPANY 7 | ERP_DB_INTERNAL |
| Knowledge RAG 권한 계약 | `document:knowledge-access-policy-contract` | 1 | GLOBAL | ERP_DB_INTERNAL |
| SIMS AI 공통 운영 절차 | `document:sims-ai-runbook` | 1 | GLOBAL | ERP_DB_INTERNAL |
| 2호기 운영 점검 | `document:sims-ai-2ho-operation-runbook` | 1 | GLOBAL | ERP_DB_INTERNAL |

나머지 ERP 구조 문서는 source_key가 `erp-db-internal:backup_sims_ai_20260620_111025/tablelayout/`로 시작하며 `Table종류.docx`, `Rddbc010.txt`, `Rddbc040.txt`, `Rddbc110.txt`, `Rddbc120.txt`, `Rddbc130.txt`, `Rddbc140.txt`, `Rddbc210.txt`, `Rddbc220.txt`이다. 모두 GLOBAL/ERP_DB_INTERNAL v1이다.
이 9개는 과거 구조 설명의 독립 등록본이다. 파일 무결성과 권한 차단은 확인했지만 현재 ERP 스키마와의 일치까지 확인한 것은 아니다. 현재성 검토를 별도 남기며 임의 퇴역시키지 않는다.

14개 공식 문서는 현재 작업 폴더 원문과 등록 추출 내용 해시가 모두 같다.
업무 가이드의 등록 source_name은 기존 SSAI 명칭이며 실제 content_file은 SIMS 명칭이다. 파일명 일치가 아니라 전용 계획의 source_key/content_file로 비교해 동일 내용임을 확인했다.

## 3. 1호기/2호기 차이

사용자가 이전 정합성 확인을 완료했다고 보고한 상태와 이번 직접 검사는 구분한다.
이번에는 1호기만 직접 검사했다. `D:\SSAI_DATA\knowledge_poc`의 2호기 자료는 직접 접근하지 않았으므로 현재 ACTIVE 목록의 재확인 결과는 미확인이다.
아래 도구는 document_id와 과거 이력 개수 차이는 무시하고 현재 source_key/version/scope/company/user/classification/content_hash/source_kind/source_revision을 비교한다.

2호기 저장소를 읽기 전용으로 검사:

```powershell
.\.venv\Scripts\python.exe tools\check_knowledge_final_readonly_smoke.py --manifest-root D:\SSAI_DATA\knowledge_poc --other-company-id 4 --output docs\04_test_results\knowledge_final_readonly_host2_20260915.json
```

숫자 4는 회사 격리용 가상 실행 조건이다. 실제 다른 접근 가능 회사의 로그인 검사는 해당 사용자에게 허용된 회사로 수행한다.
2호기 결과 JSON만 1호기에 복사한 뒤 비교:

```powershell
.\venv\Scripts\python.exe tools\check_knowledge_final_readonly_smoke.py --compare docs\04_test_results\knowledge_final_readonly_host1_20260915.json docs\04_test_results\knowledge_final_readonly_host2_20260915.json
```

## 4. PROJECT_SOURCE v9 상태와 최종 권고

- source_key: `project-source:app/services/ssai_storage_service.py#get_user_file_path`
- 1호기 document_id: `983d68ca-a6c4-4164-8568-84704e51f6a4`, v9, ACTIVE/APPROVED, GLOBAL.
- 등록 revision: `daf1661f972ff9cfe7c234f329320eeb2a4debd9`.
- 현재 HEAD: `e75fdc49872b03cc765207715d8bc118833cca9e`.
- 등록/현재 함수 내용 해시: `a33b7b7f37b80e7ef92a41b6096e8088a7ba6ef8aca445b223a567badbc1be38`로 동일.
- 등록 artifact 해시: `15922969b79acc92e84e706f1e0377377155092ba65854b09246b30a63b9593b`, 검증 통과.
- 함수 파일 작업본은 HEAD와 같으며, 버전 정보 불일치 때문에 현재 정책상 STALE이다. 함수 내용이 달라졌다는 의미는 아니다.

운영 채팅은 `app/Lmstudio_SSAI_chat_main.py`의 저장소 생성 경로에서 `source_repo_root=_ROOT`를 전달한다.
저장소 검색은 최신성 검사를 거쳐 v9를 제외한다. 실제 운영 저장소를 사용한 검색에서도 Project Source 인용이 없었다. 저장된 소스 인용의 재인가 경로도 최신성을 확인한다.
반면 `source_repo_root`를 생략한 저수준 `retrieve` 호출은 v9를 반환한다. 현재 채팅 경로의 누수는 아니지만 향후 호출자가 검사를 우회할 위험이 있다.
이번 조사에서는 사용 로그에 근거한 과거 사용자 사용 횟수까지 확인하지 않았으므로 '아무도 사용하지 않는다'고 단정하지 않는다.

**권고 A: 승인 후 v9를 양 서버에서 퇴역시키고 Project Source 근거 제공을 일시 중단한다.**
업무 DOCUMENT 14개는 독립적으로 검색 가능하고, 현재 Project Source는 파일 경로 함수 하나뿐이다. 업무 Knowledge 마감을 위해 버전만 다른 동일 함수의 v10을 즉시 만드는 실익은 작다.
기능 코드나 권한을 비활성화한다는 뜻이 아니라 유효한 Project Source 등록본이 없는 상태로 운영한다는 뜻이다.
관리자에게 현재 코드 근거가 실제 필요해지면 공식 `tools/project_source_knowledge_cli.py`의 재추출/검증/승인 등록 경로로 별도 추진한다. 현재 HEAD 및 내용 해시 일치는 필수이고 수동 artifact 생성은 금지한다.
위험도: 정상 채팅 경로는 낮음, 저수준 호출 재사용은 주의 필요. v9 유지(C)는 권고하지 않는다.

## 5. 권한/회사격리/후속질문 검사

실제 코드의 기본 역할별 권한 집합을 사용했으며 DB 사용자 실효권한 조회나 LLM 호출은 하지 않았다.

| 조건 | 결과 |
|---|---|
| GENERAL + RAG_USE | 일반 문서 검색 가능 |
| RAG_USE 없음 | 검색 차단 |
| SYSTEM_ADMIN / SSART_MANAGER + 기술상세 + KNOWLEDGE_ERP_DB_READ | 내부 문서 검색 가능 |
| SSART_STAFF / WHOLESALE_MANAGER / WHOLESALE_STAFF | 내부 문서 인용 차단 |
| 기술상세 모드 해제 | 새 내부 문서 검색 차단 |
| PROJECT_SOURCE | RAG_USE + KNOWLEDGE_PROJECT_SOURCE_READ + 기술상세 필요 |
| 회사7 Closeout, 회사7 | 검색/인용 가능 |
| 회사7 Closeout, 다른 회사 | 검색 차단 |
| GLOBAL 발주 계약 v2, 다른 회사 | 검색 가능, Closeout 제외 |
| 회사 전환 후 저장 인용/후속질문 | 재인가 및 후속 검색 차단 |
| 권한 회수 또는 RAG_USE 회수 후 인용/후속질문 | 차단 |
| 동일 회사/권한의 저장 인용/후속질문 | 유지 |

PROJECT_SOURCE v9의 분류 필드가 GENERAL인 것은 일반 사용자에게 열린다는 뜻이 아니다. source_kind에 따른 독립적인 소스 열람 권한과 기술상세 조건이 추가된다.
역할명 자체가 권한 검사 조건은 아니며 운영에서는 DB에서 읽은 실효권한이 최종 기준이다.
기존 인용 재인가와 후속 검색은 저장 답변의 기술상세 모드를 사용하면서 현재 사용자/회사/권한을 재검사한다. 현재 화면 모드 전환만으로 이전 답변을 삭제하는 정책은 아니다.

실제 로그인 화면에서는 위 표의 역할/회사 조건으로 대표 질문을 실행하고, 답변의 인용 source_key를 함께 확인해야 한다. 특히 회사7 Closeout을 검색한 뒤 다른 회사로 전환하여 같은 답변의 재사용/후속질문 차단을 확인한다.

## 6. 오래된 자료/중복/노출 확인

- ACTIVE 중복: 0건.
- 공식 DOCUMENT 원문과 등록 내용 불일치: 0건.
- RETIRED/SUPERSEDED 등록본: 13개 모두 읽기 권한 판정에서 차단. 실제 검색 결과 인용도 ACTIVE/APPROVED만 포함했다.
- 기술 DOCUMENT가 GENERAL로 잘못 등록된 경우: 이번 공식 14개에는 없음.
- 타회사 Closeout 노출: 검사 조건에서 없음.
- STALE 소스: v9 1개. 운영 채팅 검색에서 차단, 저수준 저장소 경로 생략 시 반환되는 위험은 위에 별도 기록.
- 2026-06-20 ERP 구조 자료 9개의 현재성: 미판정. 업무별 공식 문서와 일부 같은 주제가 나온다는 이유만으로 중복/폐기로 결정하지 않는다.
- 대표 질문은 관련 공식 문서도 함께 반환할 수 있다. 기대 source_key의 존재와 권한을 검증했으며 항상 한 문서만 검색된다는 보장은 아니다. LLM 답변의 최종 인용 선택은 로그인 검사 항목이다.

## 7. 승인 후 실행 절차 — 아직 실행하지 않음

순서: 1호기 퇴역 미리보기 → 승인된 퇴역 실행 → 재조회/검색 검사 → 2호기 동일 절차 → 양 서버 ACTIVE 비교.
문서 등록/퇴역 명령은 이번 작업에서 실행하지 않았다. 아래 actor 1은 앞선 확인값이며 실행 직전에 해당 서버의 활성 관리자/실효권한을 확인한다.

1호기 미리보기와 승인 후 실행:

```powershell
.\venv\Scripts\python.exe tools\project_source_knowledge_cli.py retire --manifest-root C:\SSAI_TEST_DATA\knowledge_poc --document-id 983d68ca-a6c4-4164-8568-84704e51f6a4 --version 9 --actor-user-id 1 --selected-company-id 7
.\venv\Scripts\python.exe tools\project_source_knowledge_cli.py retire --manifest-root C:\SSAI_TEST_DATA\knowledge_poc --document-id 983d68ca-a6c4-4164-8568-84704e51f6a4 --version 9 --actor-user-id 1 --selected-company-id 7 --apply
```

2호기의 document_id는 추측하지 않는다. 해당 서버에서 아래 읽기 전용 조회로 대상 한 건임을 확인한다.

```powershell
$rows = (Get-Content -LiteralPath D:\SSAI_DATA\knowledge_poc\manifest.json -Raw | ConvertFrom-Json).documents
$target = @($rows | Where-Object { $_.source_key -eq 'project-source:app/services/ssai_storage_service.py#get_user_file_path' -and $_.version -eq 9 -and $_.status -eq 'ACTIVE' -and $_.approval_status -eq 'APPROVED' })
if ($target.Count -ne 1) { throw '대상 등록본을 다시 확인하세요.' }
$target | Select-Object document_id, version, source_key, status, source_revision
$TargetDocumentId = [string]$target[0].document_id
```

그 조회값을 사용한 2호기 미리보기 및 승인 후 실행:

```powershell
.\.venv\Scripts\python.exe tools\project_source_knowledge_cli.py retire --manifest-root D:\SSAI_DATA\knowledge_poc --document-id $TargetDocumentId --version 9 --actor-user-id 1 --selected-company-id 7
.\.venv\Scripts\python.exe tools\project_source_knowledge_cli.py retire --manifest-root D:\SSAI_DATA\knowledge_poc --document-id $TargetDocumentId --version 9 --actor-user-id 1 --selected-company-id 7 --apply
```

퇴역 뒤 같은 검사 도구를 새 결과 파일로 실행한다. ACTIVE 23개, PROJECT_SOURCE 0개, DOCUMENT 14개 유지, v9 질문의 소스 인용 0개를 기대한다. 소스 권한을 가진 관리자라도 퇴역 등록본을 답변에 인용하지 않아야 한다.

## 8. 검사 결과와 마감 판단

- 새 읽기 전용 검사: 635/635 통과. 대표 검색 16개 및 기본 검색 분량 검사 포함.
- 기존 범위/권한 검사: 49/49 통과.
- 기존 역할 정책 검사: 10/10 통과.
- 공식 문서 링크 검사: 53개 통과.
- 필수 Python 파일 7개 및 신규 도구의 py_compile 통과. git diff --check 통과(기존 작업본의 줄바꿈 경고만 있음).
- 운영 저장소 전체 파일의 검사 전후 해시 동일, 운영 변경 0건.

1호기 DOCUMENT 검사는 통과했다. 양 서버 전체의 최종 마감은 2호기 재검사/비교, 실제 로그인 권한·인용 검사, v9 퇴역 승인 및 실행 후 확인까지 남아 있다.
ERP 조회/쓰기, Snapshot 변경, 운영 Knowledge 등록/퇴역, Git stage/commit/push/reset/revert는 수행하지 않았다.
