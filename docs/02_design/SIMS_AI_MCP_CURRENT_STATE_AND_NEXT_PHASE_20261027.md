# LM Studio 지능 확장·MCP 현황과 독립 개발계획 (2026-10-27)

**판정:** `Mock/로컬 STDIO 기술 PoC 완료`, `외부 MCP 서버·LM Studio Native MCP·운영 배포 미완료`. 1·2호기 LM Studio 설치 버전·설정은 이번에 접속하지 않아 **미확인**이다. 기존 Phase 7 Agent와 독립된 **Phase 4.x Tool/MCP 통합 트랙**으로 관리한다.

## 1. 이력 복원과 현재 코드

| 단계 | 당시 기록과 현 구현 | 완료 경계 |
| --- | --- | --- |
| Phase 1 구조 조사 | 9/1 공유 보관 기록 `mcp_poc_phase1_architecture_20260901_status.txt`: 기존 Structured Tool Routing은 실행 전 선택기가 아닌 사후 파생 trace, Knowledge/Web/NLQ와 MCP 실행기를 혼합하지 않도록 결론 | 조사 완료; 실행기 없음 |
| Phase 2 Mock Adapter | 9/1 공유 보관 기록 `mcp_poc_phase2_mock_adapter_20260901_status.txt`: `app/services/mcp_adapter.py`, `app/ui/mcp_chat_adapter.py`와 명시 `/mcp-resource` 채팅 경로, 전용 `MCP_EXTERNAL_RESOURCE_READ`, mock 응답·Bounded DTO·실패 격리 | DB/외부/LM Studio 호출 없는 Mock Gate 완료; 운영 grant 없음 |
| Phase 3 SDK·로컬 STDIO | 초기 의존성 HOLD 기록(`mcp_poc_phase3_local_transport_20260901_status.txt`) 후 SDK 채택·정적 STDIO 기록(`mcp_poc_phase3_stdio_20260901_status.txt`) 및 최종 보정 기록(`mcp_phase2_phase3_final_fix_20260901_status.txt`). 현 `requirements.txt`는 `mcp==2.1.1`; `tools/mcp_servers/official_recall_notice_server.py`는 **로컬 정적 Fixture**만 반환 | 공식 SDK handshake/discovery/read/close와 timeout 자식 종료 Gate의 당시 PASS; 공식 회수정보 실제 조회가 아님 |
| 외부 Resource | 9/1 공유 보관 기록 `mcp_external_openapi_selection_20260901_status.txt`는 의약품 회수정보를 첫 후보로 제시. 신청·키·실제 endpoint/요청 파라미터·현재 이용조건/갱신주기는 별도 확인 필요 | 현재 `build_mcp_resource_transport()`는 mock/stdio만 허용, remote 값은 unsupported. 실제 외부 MCP 서버 호출 근거 없음 |
| LM Studio Native MCP/운영 | `app/Lmstudio_SSAI_chat_main.py`의 모델 호출은 OpenAI Python client `chat.completions.create`, 설정 기본 경로 `/v1`; MCP 명시 명령은 별도 앱 adapter에서 처리 | Native `/api/v1/chat` 전환, 모델이 MCP를 직접 호출하는 경로, 운영 권한 seed, 인증/allowlist/2호기 Smoke 미구현·미검증 |

현 안전 경계: `SIMS_MCP_RESOURCE_POC` 기본 OFF, `SIMS_MCP_TRANSPORT` 기본 mock, 정확한 `/mcp-resource <등록 resource> <query>`만 인식, resource allowlist 한 항목, 전용 권한 선검사, 요청/응답 크기·1~30초 timeout, 물리 호출 최대 1회·자동 재시도 0회. 실패해도 Web/Knowledge/SIMS NLQ/일반 LLM으로 넘어가지 않는다. 일반 채팅·현재표·내보내기·ERP DB를 MCP server에 노출하지 않으며 결과는 별도 evidence kind/안전한 chat metadata로만 처리한다. 현재 운영 역할에 이 권한을 부여했다는 증거가 없다.

위 9/1 파일명은 저장소 밖 조사 결과의 서지 정보다. 저장소 내부 문서처럼 상대링크하지 않았고, 현재 코드와 당시 시험 결과를 혼동하지 않는다.

## 2. 앱 주도 Mock/STDIO와 LM Studio Native API 비교

| 관점 | 현재 앱 주도 MCP SDK 경로 | LM Studio Native MCP 후보 |
| --- | --- | --- |
| 실행 소유권 | SIMS의 명시 명령·권한·resource ID validator가 한 번 호출을 결정 | 모델 요청의 `integrations`를 통해 서버 도구가 모델 호출 범위에 들어감. 앱의 명시 명령/회사 경계를 보존하려면 별도 선·후 검증 필요 |
| 현재 지원 | mock 또는 **로컬 정적** STDIO resource; remote transport 미구현 | 공식 문서상 LM Studio 0.4.0+ `/api/v1/chat` 또는 `/v1/responses`에 MCP 지원. **설치 버전/활성 설정은 미확인** |
| 기존 채팅 호환 | 현재 `/v1/chat/completions` 모델 호출을 바꾸지 않음 | 공식 비교표에서 `/v1/chat/completions` 자체는 LM Studio MCP integrations를 지원하지 않음. Native 전환은 메시지·스트리밍·저장/권한 경로 비교가 필요 |
| 보안 | 전용 권한과 앱 resource allowlist·timeout·retry0를 이미 소유. 외부 server identity/인증 추가 개발 필요 | 공식 API는 `allowed_tools`와 서버 설정을 제공하나, 허용하지 않으면 서버의 도구 전체가 노출될 수 있음. Native 서버 설정과 API token, 모델 tool-call 감사가 추가 경계 |
| 추천 | **첫 실제 외부 Resource는 현재 명시 경로를 확장한 승인된 read-only transport PoC**. 모델에게 도구 선택권을 넘기지 않음 | API/설치 버전·보안 기능표를 읽기 전용으로 확인한 뒤 격리 환경 비교만. 기능 동등성과 명시 요청 제약이 증명되기 전 운영 채팅 교체 금지 |

위 Native API 사실은 2026-10-27 열람한 [LM Studio 공식 REST 비교](https://lmstudio.ai/docs/developer/rest), [공식 MCP via API](https://lmstudio.ai/docs/developer/core/mcp), [서버 보안 설정](https://lmstudio.ai/docs/developer/core/server/settings)에 따른다. 공식 문서의 지원 설명은 **현재 1·2호기 설치 버전 지원의 증거가 아니다.** Native MCP는 일반적으로 tool-call 흐름이며, 이 프로젝트의 '명시적 요청만' 정책을 충족하는지는 별도 PoC로 확인해야 한다.

## 3. 11월 최소 개발 순서(제안)

| 단계 | 선행조건·예상 작업량(추정) | 결과/완료 Gate |
| --- | --- | --- |
| M0 현 계약·SDK 점검 | 11/1 월마감 안정 이후 0.5~1일; 로컬 `mcp==2.1.1`, 공식 SDK API/라이선스/보안 점검 | Mock/STDIO Gate 재실행, 서버 프로세스 종료·재시도0·permission deny 선검사 확인. 설정 변경 없음 |
| M1 LM Studio 기능표 | 0.5~1일; 1·2호기 버전·OpenAI 호환 vs Native/Responses API·모델·토큰/서버 설정의 승인된 읽기 전용 확인 | 채팅/저장 계약 차이표. 설치 버전 미지원이면 Native 검토 보류; 현재 앱 경로 유지 |
| M2 명시 Tool 경계 유지 | 0.5~1일; 기존 `mcp_adapter`/채팅 branch와 회사·사용자 권한 대조 | 정확한 명령 1회만, 일반 질문 자동 도구 호출 0, 결과가 Knowledge/Web/NLQ로 변환되지 않음 |
| M3 외부 서버 하나 선정 | 1~2일 설계; 공식 명세·서버 신원·읽기 Resource/URI·인증·트래픽·이용범위와 보안 승인 | 명시 승인된 서버 1개와 Resource/도구 allowlist. 승인 없는 endpoint/자격증명은 연결하지 않음 |
| M4 최소 read-only 연결 PoC | 2~4일 추정; SDK 호환 remote transport는 별도 코드 변경·리뷰 대상 | 서버 identity·TLS/credential, URI 및 response schema/크기, secret/ERP·고객값 외부 전송 0. 운영 DB 쓰기 0 |
| M5 보안·DB-free/격리 시험 | 1~2일; 실패 Fixture와 관리자 권한 표 | feature OFF·deny·미등록 서버 0회, timeout/오류/잘못된 자료 fail-closed, retry=0, 다른 업무 fallback=0, 민감정보 없는 감사·room/company 분리 |
| M6 1호기 수동 → 별도 2호기 | 1~2일+승인창; 외부 호출/운영 권한 seed/배포는 별도 명시 승인 | 실제 출처·조회시각·오류 표시와 저장/재진입, 설치 HEAD·Health·권한 Smoke. PoC 통과만으로 자동 활성화하지 않음 |

**추가 개발 경계:** 현재 SDK 앱 adapter에는 remote transport가 없다. 승인 외부 서버가 Remote MCP라면 위 M4는 신규 코드가 필요한 PoC다. 기존 정적 STDIO Fixture를 외부 데이터처럼 바꿔치기하지 않는다. LM Studio Native MCP의 모델 주도 호출은 M1 이후 별도 격리 비교이며 M4 성공의 필수 선행조건이 아니다. MCP 작업은 Agent 개발 전에도 추진 가능하지만, 월마감 중 기능 활성화·외부 연결은 하지 않는다.

## 4. 승인·미확인

필요한 사용자/보안 승인: 실제 외부 MCP 서버와 Resource ID·이용권, API/인증 비밀 보관, 네트워크 outbound allowlist, 운영 권한 `MCP_EXTERNAL_RESOURCE_READ` 부여 대상, 수집/보존·감사, 1/2호기 테스트창. 현재 설치 LM Studio 버전·Native API 설정, 실제 외부 서버 계약과 2호기 배포 상태는 **미확인**. 이번 문서 작성 중 실제 LM Studio·외부 MCP 서버·DB에 연결하거나 설정을 바꾸지 않았다.
