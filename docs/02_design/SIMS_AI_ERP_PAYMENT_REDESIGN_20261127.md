# SIMS AI 입출금 관리 통합 상세 재설계 | L2-R3

2026-11-27 / L2-1 기본 조회 구현 및 1호기 수동 검증 합격. **확정**은 사용자 결정, **설계**는 단계별 계약, **미확정**은 예시 데이터·업무 승인 없이 계산하거나 노출할 수 없는 항목이다. [기존 원장 설계](SIMS_AI_ERP_PAYMENT_LEDGER_DESIGN_20261126.md)는 보존한다. 그 문서 및 [R1](../01_phase_reports/payment_existing_common_architecture_review_L2_R1_20261127_status.md)·[R2](../01_phase_reports/erp_feature_query_nlq_standard_review_L2_R2_20261127_status.md)의 5개 직접 조회, 월초 기본기간, 금융 260에 별도 상태 키 부여 제안은 이번 확정 정책으로 대체한다. [공식 ERP 개발 표준](SIMS_AI_ERP_FEATURE_DEVELOPMENT_STANDARD.md)과 [단계별 계획](SIMS_AI_ERP_PAYMENT_DEVELOPMENT_PLAN.md)을 함께 사용한다.

## 1. 노출 경계와 자료 단위

|자료원|한 행과 물리 PK (동일 접두어는 뒤 열에서 생략)|사용 경계|
|---|---|---|
|Rddbc310, 38열|입출금일·순번: Rd31_Receive_YyMmDd + Receive_Seq|직접 조회. 금액은 거래의 원본값이지 반드시 입출금된 현금이 아니다.|
|Rddbc320, 29열|어음번호·순번: Rd32_Note_No + Note_No_Seq|직접 조회. 어음 금액과 분할 관련 금액을 다른 측정값으로 보존한다.|
|Rddbc330, 22열|Rd33_Ven_Cd + Mortgage_Kind + Note_No + Note_Seq|직접 조회. 같은 어음의 여러 담보 거래처·종류가 정상. 어음 총액으로 합산하지 않는다.|
|Rddbc260/270, 각 7열|Rd26_/Rd27_ Ven_Cd + Io_YyMm + Receive_Di|스키마 등록만. 장부/실 월 **변동**을 나타냄. 직접 메뉴·View·NLQ·도움말·export action·세션 키 없음. 내부 read 함수도 실소비자 생길 때까지 만들지 않는다.|
|Rddbc130|Rd13_Trans_Di + Trans_YyMmDd + Ven_Cd + Trans_Seq|거래명세서 공통. 매출·매입 발생의 근거. R4 신규 서비스 아님.|

**확정:** Gcode 0023, 문자형 업무코드 < '50'은 입금, >= '50'은 출금이다. 조회조건과 결과 분류에 같은 문자 경계를 사용하며, 정수 변환하지 않는다. NULL·공란 및 다른 대분류는 미분류로 보존한다. 00/50은 발생액, 32/82는 장부·실잔액 모두의 조정할인. 01/51 현금, 11/12/61/62/63 어음 관련 등 승인된 구분은 보존하되 입금·출금 계열 전체를 현금으로 바꾸지 않는다. 310 등록·130 발생액이 월집계에 반영되는 구조. 장부 260이 원장·잔액·회전 분석의 기본, 실 270은 명시 또는 비교 시 보조. 재고 210/220의 기본 정책을 뒤집거나 이식하지 않는다. 매출처/매입처 잔액의 독립성 유지.

**월과 일 경계 설계:** 날짜 범위를 닫힌 구간으로 받고, 내부에서는 월의 시작 이상 다음 달 시작 미만으로 자른다. 예: 10/27~12/05의 월 변동 합이라면 10/27~31 상세, 11월 완결 월자료, 12/01~05 상세를 각각 **한 출처**에서 취한다. 한 달 안의 부분 기간은 전체 월자료를 더하지 않는다. 11/01~11/30 전체 월의 원장 *명세*가 필요하면 월합으로 상세행을 대체하지 않는다. 이월/기초, 당기, 기말은 별도 의미와 출처. 같은 월에 장부와 실을 더하지 않고 비교열로 분리한다. 집계 완결 여부·누락월·부호·원장 기초 기준·영업/회계 범위가 불명확하면 계산 중단. tablelayout/매출원장_거래명세서별.txt는 당기 120/130, 310과 과거 월 260을 UNION ALL 하는 **특정 매출 장부 사례**이며 32 제외 조건이나 NOLOCK·고정 기간을 공통 공식으로 승인한 자료가 아니다.

## 2. 업무 화면, 조건 및 결과 순서

**확정 UI:** 업무그룹 ‘입출고/명세서/재고’ 바로 다음 ‘입출금 관리’(띄어쓰기 포함). 그룹 내부에는 **입출금 조회, 어음 조회, 담보 조회**만 둔다. 패널은 조건과 조회 실행만, 결과·다운로드·분석은 채팅 카드 한 곳. 기준일은 회사 세션에서 사용하는 오늘. 세 업무 및 후속 원장·잔액 일반 조회의 최초 기본값은 전월 같은 일~오늘(양끝 포함). 2026-11-27 → 10-27~11-27, 2026-03-31 → 02-28~03-31, 윤년에는 02-29. 30일 고정 또는 전월 전체가 아니다. action·회사별 위젯 최초 생성 시만 초기화. 변경한 날짜를 rerun에서 재설정하지 않는다. 미입력·역전·불가능한 명시 날짜는 도움말, 본조회 0회.

아래 날짜 역할·고정열과 고급조건 구성은 **설계 제안**. 모든 일반 조회는 현재 회사 원본 범위에서, 명시한 조회조건만 적용. 삭제/회수·유효 상태 필드를 보고 자동 제외하지 않는다. 업무명/거래처명을 기술코드보다 앞에 두되 원본 코드는 제거하지 않는다.

|Action / 기간 역할|첫 화면 (좌→우·위→아래)|추가조건 / 의미|
|---|---|---|
|입출금 조회, 기본 receive_date|입출금일 시작·종료 → 거래처 → 입금/출금/전체 및 개별 입출금구분 → 조회|매출참조·대체 거래처, 담당영업사원, 등록·수정자, 은행, 어음번호/순번, 전표일/번호, 금액범위, 비고. 개별 구분과 계열 상충 시 안내. 전표일 필터를 추가해도 기본 분석일은 입출금일.|
|어음 조회, 기본 등록일자(Add_Date)|조회기준일(발행/만기/등록) + 기간 → 거래처 → 어음번호·순번 / 어음구분 → 조회|어음번호분류, 입출금구분, 은행, 발행처, 액면 원본 금액범위, 삭제구분, 등록자/수정자. 만기일 조건은 만기 목록이지 미결 어음 조건이 아니다.|
|담보 조회, 기본 등록일자(Add_Date)|조회기준일(발행/만기/등록, 회수일 의미 확인 후 선택 허용) + 기간 → 거래처 → 담보종류 / 어음번호·순번 → 조회|여수신구분·어음구분·은행·발행처·금액·삭제구분·등록자. 여수신구분의 미확인 값은 업무상 상태로 번역하지 않는다.|

|Action|결과 선두 열의 순서(굵게 표시한 열 고정 제안)|그 이후|
|---|---|---|
|310|**입출금일자, 거래처명**, 거래처 담당자명, 입금/출금 구분, 입출금구분명, 금액, 입출금순번, 거래처코드, 입출금구분코드, 원본 담당영업사원명, 매출참조거래처명, 어음번호·순번, 전표일자·번호, 비고|미배치 원본열을 DDL 순으로 1회, 나머지 역할 명칭 열을 메타 정의 순으로 추가|
|320|**거래처명, 어음번호**, 어음번호순번, 어음구분명, 금액, 발행년월일, 만기년월일, 은행명, 발행처, 입출금구분명, 거래처코드, 비고|부가 전표/등록·분할 등 원본열 포함, 같은 열을 두 번 내보내지 않음|
|330|**거래처명, 담보종류명**, 어음번호, 어음번호순번, 여수신구분, 금액, 발행년월일, 만기년월일, 회수 관련 원본 일자, 은행명, 거래처코드, 담보종류코드, 비고|거래처별 담보 PK 모두 보존. 회수 비고 등 미확정 명칭은 원본 식별자를 유지|

라벨은 위 표의 업무 설명으로 새로 번역하지 않고 단일 메타에서 생성. 원본 물리명에 대응되는 반환 열 ID 및 파생 명칭 열 ID를 유지한다. 특히 DDL의 [Rd33_Collect_Remark ] 끝 공백, R320 분할 필드의 이름/타입 보존. 원본 필드와 표시상 순번(행번호)을 혼동하지 않는다.

## 3. 모듈 구성과 공통 호출 계약

아래에서 ‘기존’ 함수와 ‘신규 제안’ 이름을 구분한다. 신규 함수명은 API 계약을 명시하기 위한 설계이며 아직 없음.

|분류 / 책임|경로·호출 및 입력 → 반환|
|---|---|
|① 신규: 업무 메타|app/sims/meta/rddbc_payment_meta.py: 5개 DDL에서 물리 식별자/논리 ID/한글 라벨/종류·길이·scale/PK·코드 짝을 한 번 정의. 공개 3종에는 조건·열 순서·고정열·분석 역할만 추가. 260/270에는 사용자 액션 메타 없음.|
|② 설정: 스키마·라벨|app/db/schema_map.py SCHEMA와 app/db/labels_map.py LABELS에 5개 정의 투영. 기존 app/sims/meta/rddbc_io_meta.py 병합 방식 참고. app/services/utils.py apply_labels(df,table) → 표시 열명. SQL에서 별도 한글사전을 유지하지 않고 JOIN 역할 열만 안정된 alias. 날짜/금액 역할은 라벨 문자열로 추정하지 않는다.|
|① 신규: 서비스|app/services/rddbc310_service.py, rddbc320_service.py, rddbc330_service.py. 각 normalize_rddbcNNN_params(raw, 기준일) → canonical 조건; get_rddbcNNN_result(params) → 공통 payload. 동일 업무 SQL 생성·정규화는 이 경계에만 둔다. export callable이 등록상 필요하면 같은 원천을 사용하며 정상 카드 다운로드가 이를 호출해 SQL을 반복하지 않게 한다.|
|① 신규: 얇은 뷰|app/sims/views/rddbc_payment_views.py 제안: view_rddbc310/320/330(params=None) → 미제출 상태 또는 해당 서비스 결과. rddbc_io_inout_views._render_inout_form/_run_inout_submit의 UX와 호출 순서를 따르되 재고 전용 필터·로컬 필터는 이식하지 않음. L2-1 범용 폼은 남기지 않는다.|
|② 설정: Registry와 메뉴|app/sims/meta/erp_table_feature_registry.py ErpTableFeatureSpec/ErpTableActionSpec/ErpFilterSpec, menu_actions/menu_targets/get_action_spec → 사용자 업무 3개, SQL source/PK/permission/서비스·뷰·payload_key. app/ui/sims_entry.py _panel_sidebar 및 app/ui/sims_panel.py render_sims_main(selected) → 동일 그룹·순서/조건 뷰. 키는 3개 작업별 충돌 없이 Registry에만 등록.|
|③ 재사용: 날짜/후보|app/sims/views/rddbc_io_shared.py _render_io_date_range(prefix, default_from, default_to) → 날짜 문자열·date 객체 dict. 기존 fallback 30일 대신 전월 동일일을 명시 전달. app/ui/kpis_erp_query.py _initial_dates의 월말 보정 원리를 공용 날짜 helper로 재사용할 수 있되 KPIS UI 역의존 금지. _render_vendor_candidate_row → 현재 회사 후보/선택; app/services/rddbc030_service.py search_vendors_full, rddbc060_service.py search_users_full, rddbc010_service.py search_name/list_by_group. 권한 범위의 코드만 보관. 후보 조회 ≠ 원천 조회.|
|③ 재사용: 코드명·행 제한|app/services/rddbc_io_common.py enrich_rddbc010_pairs 또는 서비스의 유일 키 LEFT JOIN으로 명칭 연결(같은 역할 이중 보강 금지). app/services/erp_table_query_service.py registered_query_limits(params) → 표시/원천/export 상한. 원천·내보내기 허용 중 작은 한도 적용, limit+1 감지.|
|④ 최소 보완: 읽기·인가|app/services/erp_table_query_service.py execute_bound_select(sql,params) → app/db/mssql_client.py query_to_df → DF. 기존 호출의 기본값은 유지하고 명시 회사/정밀 reader 선택 인자를 추가하는 안. app/services/ssai_auth_service.py connect_company_db(company_id), app/ui/ssai_login.py get_current_user/get_selected_company/get_current_permissions 및 기존 최신 권한 조회를 사용. L2-1 finance_query_access.authorize에서 사용한 load_current_api_session의 최신 검증 의미는 유지하되 ERP 조회가 외부 API 정책에 종속되지 않도록 회사/권한 검증만 공통 경계에서 사용.|
|③+④ 결과|app/services/erp_table_query_service.py build_feature_result(table,title,params,df,summary_md) → dict, 내부 rddbc_io_common.build_result_payload. 검증된 full로 작성 후 공통적으로 표시용 head를 파생; source/full/정밀도/권한 binding 메타가 소비자까지 전달되도록 opt-in.|
|③+④ 표·다운로드|app/ui/sims_table_display.py build_sims_table_display_config/render_sims_table → 열역할에 따른 고정열·표시 설정. app/ui/chat_middleware.py _download_source_context, _stash_sims_export_provenance, _apply_sims_excel_number_formats 및 기존 다운로드 동작 → full CSV/Excel. exact 열만 긴 수 텍스트, 숫자 우측·천단위, 순번 nullable 정수, 코드 문자, NULL 빈칸.|
|③ 재사용: 게시·저장|app/ui/chat_middleware.py push_sims_result_to_chat(=wssz) → drain_inbox_to_chat → 방에 결과 요약/세션 참조 1회. 패널 최종 결과를 독자 렌더하지 않음. 만료 시 요약만, 자동 재조회 안 함.|
|③+④ 분석·현재표|app/ui/chat_middleware.py _render_sims_result_actions_plain 및 lazy/fragment, _build_sims_analysis_context_from_df → app/ui/sims_analysis_profiles.py build_sims_analysis_profile. app/ui/current_table_followups/analysis_facts.py build_whole_table_facts → action_dispatcher.py → 기존 검증된 Facts 응답. 역할/정확정수 경로·동일 scope 확인만 선택 적용.|
|② 후속 NLQ|app/services/erp_table_nlq.py resolve_registered_erp_table_nlq; app/services/io_nlq.py resolve_io_nlq → app/sims/nlq/nlq_router.py _try_handle_io_nlq → Registry 서비스; action_inventory.py 및 app/ui/knowledge_chat_adapter.py의 검증된 예문. R4에서는 parser·별칭 도움말 비노출. R2에서 쓰던 'nlq_enabled'는 L2-1 WIP 장치. 기능 공개 시점만 제어하며 권한 대용 아님.|
|⑤ 폐기 / ⑥ 수정 불필요|폐기 범위는 6절. app/services/kpis_rest_client.py 등 KPIS 인증·REST, 기존 ERP NLQ의 타 업무 판정, 제품재고현황, Dashboard, Snapshot, DB 스키마/테이블, 로그인 및 역할 부여 정책은 수정 불필요.|

### 정규 조건 및 SQL 경계

공통 envelope 제안: canonical action, date_role, date_from/date_to(YYYYMMDD 포함 범위), vendor_cd, role별 코드·문자 필터, amount_min/max(Decimal 정수), display_context. 회사·사용자·권한·epoch는 질문/폼에서 받지 않고 실행 시 서버 세션에서 얻는다. 표시한도는 WHERE와 독립. 거래처/영업사원/등록자 역할 분리; 어음번호 텍스트와 순번 smallint 분리. 명시 누락/모호/잘못된 범위를 임의 기본값으로 덮지 않는다.

|action|조건 ID → SQL 적용 예 (접두어 31/32/33)|분류/조회시 주의|
|---|---|---|
|입출금 조회|receive_date → Receive_YyMmDd; vendor_cd → Ven_Cd; reference_vendor_cd → Out_Ven_Cd; sales_user_cd → Real_Sales_Man; flow_code/direction → Receive_Di_Gcode + Receive_Di; amount → Receive_Amt|0023은 영숫자 문자코드 그대로 '50'과 비교한다. 조회조건·분류·현재표 계산에 같은 결과를 사용한다.|
|어음 조회|draft_date/maturity_date/registered_date → Draft_YyMmDd/Maturit_YyMmDd/Add_Date; note_no/note_seq → Note_No/Note_No_Seq; note_type → Note_Di; bank → Bank_Cd; amount → Note_Amt|Note_Gu(번호분류)와 Note_Di(어음구분) 구별. 발행처 문자열과 ERP 거래처도 구분.|
|담보 조회|위 날짜 역할 + collect_date → Collect_YyMmDd(승인 범위); note_seq → Note_Seq; collateral_kind → Mortgage_Kind; offer_kind → Offer_Di; amount → Note_Amt|담보종류와 어음구분은 별개. 담보 사실행은 거래처를 포함한 복합 PK로 식별.|

값은 전부 driver bind, 식별자·연산자·순서는 서버 측 허용 목록. 코드 정확 일치/허용 명칭 검색은 필드 계약대로, 금액 범위 양끝 포함. 030은 거래처 역할마다, 060은 담당/등록/수정마다, 010은 원본 Gcode/Tcode 쌍으로 유일한 명칭만 LEFT JOIN. Gcode 예: 0023 입출금구분, 0020 은행, 0014 어음번호분류, 0016 어음구분, 0017 담보종류. 미등록이면 코드 유지·명칭 공란. PK 중복이나 원본 열 손실 시 오류; DISTINCT나 임의 첫 행으로 가리지 않는다. SQL은 안정적 PK 정렬, 한도+1 읽기. 정상 0건과 불완전 조회/오류 분리. 상세 간 JOIN 금지.

## 4. 결과·정확성·인가 설계

1. 실행 시 user/company/epoch/IO_READ 확인 → 승인된 회사에만 연결 → 정규 조건으로 SELECT → 응답 시 동일 binding 및 최신 권한 재확인 → 성공 시에만 게시. 로그인/회사 미선택, 변경·회수 중 도착한 결과는 폐기. 회사 미확정 시 기본 연결 fallback 금지. rerun·날짜 변경은 업무 SELECT 0회. 다른 회사/방·만료된 결과 참조 재사용 금지.
2. 일반 pandas reader의 float 변환 여부가 불확실하므로 exact 모드에서는 Decimal 원형 보존 읽기(예: 지원되는 read_sql의 coerce_float=False 또는 공통 reader 내부의 바인딩 cursor)와 dtype 검사를 함께 한다. int 또는 유한 정수 Decimal(18,0)만, float에서 Decimal로 사후 복원 금지. NULL과 0 별도. PK의 숫자형 순번과 문자형 어음번호를 분리. 합계는 overflow 없는 정수/Decimal 사용.
3. payload.df = 검증된 전체, df_display = full.head(표시한도)의 복사, records = 표시행. meta에 원천·PK·label map·정규 조건·날짜 역할·금액 역할·scope·전체/표시/export 행수·완전성 기록. 전체 행과 내보내기 수가 같을 때만 전체라고 안내. UI에서 df_display를 full로 유추하거나 다운로더에서 SQL을 재실행하지 않는다. 정상 0건/no_data, 조건·권한 오류, 초과·계약 오류를 별도로 전달.
4. 다운로드는 IO_READ + EXPORT_EXCEL 재확인 후 현재 scope의 전체 DF만 변환. 반복 렌더에서 캐시를 사용해도 소유자·epoch·결과에 결합하고 권한 검사 앞에서 파일을 반환하지 않는다. XLSX는 안전한 정수는 숫자, 15자리 초과 정수는 정확한 텍스트로 저장. 식별코드도 문자열. CSV는 쉼표 포맷이 아닌 정확한 원본 문자열과 한글 헤더. 날짜/NULL은 기존 공통 정책. 원본 DF를 표시용으로 변경하지 않는다.
5. 분석 버튼은 IO_READ·현재 scope·검증된 full이 있어야 활성. 다운로드 권한 부재만으로 조회 분석까지 막지 않는다. 질문과 입력 열을 허용 목록으로 제한하고 LLM에는 집계 근거만 전달. 권한 회수/만료 시 이전 캐시·현재표·내보내기·모델 요청 재사용 금지. 카드의 결과/분석/다운로드 경로 모두 같은 판정.
6. 타 업무의 기본 reader·표시·권한 정책은 불변. R110 _attach_inout_full_df의 df_display 선선택 조건부 위험은 별도 티켓 대상이며 범용 변경의 구실로 삼지 않는다.

## 5. 분석·NLQ·유효자료

|기능|R4 및 후속 범위|금지/실패 시 처리|
|---|---|---|
|공통 분석·현재표|R4는 기존 카드 버튼과 기존 지원 기능을 정확하게 연결. 전체 건수, 날짜 범위, 명시 조건의 부분집합, 거래처/기간/동일 업무구분별 건수·원본 금액·순위. 비교 측정값·역할·필터·분모·입력 행수 및 동점 처리 표시.|310 혼합 구분 전체를 현금 수지로 설명하지 않음. 320 액면금액과 분할 관련 숫자 혼합 금지. 330 담보행 합을 보유 어음/순담보 잔액으로 표현 금지. 정확 계산 미지원 항목은 안내.|
|날짜·근거|310 기본 입출금일, 320/330 선택한 발행/만기/등록 등 날짜 역할을 Facts와 카드에 명시. full에 근거한 exact 집계 → 수치 검증 → LLM 설명. 후속질문은 현재표의 소유권·완전성 확인 후 기존 DF 재사용.|전표일 우선 추론·표시 일부 전체화 금지. R4 미지원 분석을 미리 광고하지 않음. 새 기능은 L2-2에서 확장.|
|후속 NLQ|3개 canonical Action, 같은 조건 ID·자료형·거래처 역할·0023 등 코드쌍. 질문 → 업무 판별 → 날짜/역할/코드 해석 → 동일 normalize/get_result → 같은 SQL·projection·full·Facts·채팅. 명시 신규 업무와 명확한 현재표 후속은 기존 NLQ 정책을 따름.|미등록/미확정 조건은 원천 조회 0회. NLQ 전용 SQL, 사용자 요청하지 않은 기간, 첫 후보 선택, 별도 다운로드 금지. 260/270 직접 NLQ 비등록.|
|유효 어음·담보(L2-3)|일반 조회는 최근 한 달. 유효자료는 **기준일 현재**의 별도 업무로 설계하고 한 달 이전 발행·제공도 포함. 어음번호+순번으로 관계를 찾되 유효 상태의 근거 사건과 기준시점이 확정된 뒤만 공개.|만기 미도래는 날짜 사실일 뿐. 320의 삭제/분할 관련 열, 330의 회수일/삭제구분·여수신구분 존재만으로 유효·미결 판정 금지. 미승인 시 해당 action/버튼을 만들지 않음.|

L2-3 협의: 결제·양도·분할의 완료 사건과 기준시점, 부분결제/재발행 원본 키, 담보 회수·재제공·취소·삭제 시점과 동일 거래처 반복 이력의 원천. 빈 어음번호는 연결하지 않는다. 310/320/330 간 거래처 일치 강제 금지. 별도 상세 리스트로 연결하고 원래 행수·금액 유지. 만기·회수·유효를 서로 다른 조건으로 검증한다.

## 6. L2-1 코드의 최종 처리 (R4 완료)

|대상|최종 처분|없애기 전에 입증할 것|
|---|---|---|
|app/sims/meta/payment_fields.py|폐기. 원본 타입·PK·정확한 물리명은 도메인 메타로 이관, LABELS/SCHEMA 단일 공급.|5개 DDL 필드·PK 대조, 3개 공개 업무만 등록.|
|app/services/payment_query_service.py|폐기. SQL 소유권은 개별 310/320/330 서비스로. execute_finance_select와 260/270 조회·export wrapper 제거.|조회 전후 scope, 바인딩·상한+1·정밀도·중복 검사 모두 등가.|
|app/sims/views/payment_query_views.py|폐기. 새 얇은 ERP 조건 View 3개로 대체. 월별 직접 View 제거.|월 기간조회 메뉴/키 없음. 새 뷰에 SQL/결과 렌더링 없음.|
|app/ui/payment_query_display.py|폐기. display_copy/excel_copy/render_finance_downloads 독립 경로 제거.|공통 카드의 작은/큰 표·다운로드·LLM에서 동일 exact/전체 결과 확인.|
|app/services/finance_query_access.py|폐기. local_binding/authorize/result_allowed 가 보장하던 현재 회사·사용자·최신 권한·epoch 검증은 공통 경계로 이관.|권한 없는 원천 호출 0, 처리 중 변경·다운로드·분석 캐시 재사용 0.|
|Registry·menu·NLQ·상태|_payment_features의 월 2건·모든 진입점/초기화/저장·도움말/등록 제거. 3개 업무의 정상 결과참조만 유지. L2-1 금융 전용 분기·메타 키·import·캐시 삭제.|__io260_last_payload는 제품재고현황 소유. **그 키를 삭제·개명·금융용 초기화하지 말 것.** 기존 저장 메시지의 요약/만료 안내 유지; 옛 금융 키 복구 금지.|
|tools/check_payment_basic_query.py 및 기존 변경 파일|검증 자산 유지, 폐기된 5메뉴/모듈 import를 3개 정상·2개 비노출·공통 소비자 검증으로 대체. 010/060의 안전 검색 helper는 실제 사용 시만 남김. ssai_permission_policy의 IO_READ 자체 보존.|역검색은 삭제 파일명뿐 아니라 action 제목, dotted callable, 분기·old key·import·wrapper·테스트까지. 무관한 기존 WIP를 파일 단위로 되돌리지 않음.|

R3 완료는 설계 문서와 증분 경계 검증까지다. 실제 코드 폐기, 업무 조회, 상태 판단, DB 반영은 수행하지 않는다.

## R4 적용 기록 (2026-11-27)

310/320/330만 등록했다. 기본일 역할은 최신 지시에 따라 입출금일/등록일/등록일이다. 발행일·만기일은 선택 조건이며 어느 쪽도 유효상태를 뜻하지 않는다. 회수 관련 원본 열은 의미를 확정하지 않았다. 260/270은 스키마와 라벨만 등록했다.

업무 메타는 rddbc_payment_meta, 정규화와 조회는 세 rddbcNNN_service, 화면은 rddbc_payment_views가 담당한다. erp_table_query_service의 회사 지정 바인딩 SELECT에서 원본 PK·상한·금액을 검사한다. 공통 panel → wssz → 채팅 카드에서 전체와 표시본을 분리한다. XLSX 16자리 이상 금액은 텍스트다. 분석 Facts는 원본 금액·선택 기준일·거래처·업무구분을 사용한다. 현재표의 기본 행수 확인과 분석 버튼은 지원하지만 금융 자연어 신규 조회나 잔액·유효상태 계산은 지원하지 않는다.

L2-1은 1호기 최종 수동 합격했다. R4 당시의 건수 중심 현재표 범위는 아래 R5 완료 기록으로 대체한다. 배포 후 대상 환경의 도달성·권한·표시·다운로드는 별도 Smoke로 확인한다.

## R5 조회조건 및 결과 순서

세 업무의 첫 행은 기준일·시작·종료, 다음 행은 공통 거래처 후보 입력이다. 입출금은 계열·구분코드, 어음·담보는 어음번호·순번·각 업무 구분명으로 좁힌다. 추가조건에서 명칭은 기존 명칭 조인의 열에 바인딩된 부분일치로, 번호는 원래 형식의 값으로 비교한다. 금액과 등록·수정기간은 한쪽 경계만으로도 조회할 수 있다.

공통 채팅 영역은 이력 → 이번 결과 → SSAI 조건 패널 순서다. 제출 플래그, 게시·저장 횟수 및 전체자료는 변경하지 않는다. 완료 안내는 패널 중간 행수가 아닌 채팅 카드의 제한을 적용한다.

## R5 현재표 연계 (1호기 최종 합격)

현재표는 검증된 원본 DF와 조회 당시의 날짜·금액·업무구분 역할을 table_key별로 함께 보관한다. 입출금 0023을 문자열 경계 `'50'`로 나눈 결과는 ‘입금/출금 구분’의 ‘입금/출금’이며 현금흐름·잔액이 아니다. 일자·월·거래처별로 두 분류의 건수와 부호 있는 금액을 나눠 표시한다. 미분류는 숨기지 않는다. 어음·담보의 동일 어음번호를 중복 제거하지 않는다.

### R5 담당자 및 후속 결과 표시 보정

거래처 담당자명은 주 거래처의 현재 영업사원(Rd03_Sales_Man)이며 매입·매출에 같은 사용자 PK를 사용한다. 원본 담당영업사원은 Rd31_Real_Sales_Man으로 별도 표시한다. 현재 담당자명을 전표 담당자의 대체값으로 쓰지 않는다. 기본 조건 3행은 입금/출금 구분, 입출금구분코드, 거래처 담당자명 LIKE이다. 담당자별 집계는 코드와 이름으로 나누며 입금금액·출금금액 지정 시 해당 원본 행만 남겨 순위를 계산한다. 표시·다운로드는 원본 DF를 변경하지 않고 같은 날짜·빈칸 정책을 사용한다.

### R5 현재표의 집계 차원 및 검색값

입출금·어음·담보는 일반 ERP와 등록 차원의 집계절 해석을 공유한다. 업무별 허용 컬럼, 선택한 기준일, 거래처·담당자 식별 코드, 정확 금액 및 원본 권한 검사는 기존 계약을 유지한다. 입출금구분은 입출금구분명, 입출금 또는 입금/출금은 입금/출금 구분을 뜻한다. 거래처명 집계와 거래처별 집계는 같은 코드·명칭 그룹이다.

값이 있는 검색과 차원만 지정한 집계를 구별한다. 예를 들어 거래처명 삼일 월별 집계는 검색 후 월별 집계, 거래처명 월별 집계는 거래처와 월의 복합 집계다. 세 업무의 등록 차원·별칭 표현을 자동 생성해 동일 그룹 키·건수·금액을 대조한다. 미등록 차원은 미지원으로 안내하고 범위를 넓히지 않는다.
