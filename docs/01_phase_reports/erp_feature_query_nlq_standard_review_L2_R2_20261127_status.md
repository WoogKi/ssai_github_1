# L2-R2 | 신규 ERP 테이블의 화면 조회·NLQ 공통 구조 조사
2026-11-27 · 1호기 · 조사 및 제안, 구현 승인 아님

## 판정과 조사 경계

**표준의 단위는 테이블당 화면이 아니라, 원본 행 단위가 분명한 업무 action과 그 조회 서비스다.** 입력은 두 경로지만 정규화·인가 이후에는 한 서비스, 한 SQL 소유자, 한 결과 계약이어야 한다. Rddbc230이 이 구조에 가장 가깝다. Rddbc110/120에서는 익숙한 조건 폼과 기존 채팅 결과 경로를 채택하되, 폼의 조회 후 로컬 필터와 전체자료 재조립은 표준으로 채택하지 않는다. 금융 전용 결과 렌더러나 KPIS 호출 방식을 내부 ERP 조회의 기준으로 삼지 않는다.

저장소 루트는 C:\New\Python_Project\LmStudion_project1. 아래 경로는 이 루트 기준이고 행 번호는 조사 시 작업 트리 기준이다. HEAD 원본과 현재 WIP를 구분했으며, L2-R1 보고서의 화면 문제 목록 대신 **조건이 어디에서 해석되고 전체자료가 어디서 정해지는지**를 재조사했다. 기준 브랜치 feat/dashboard-stock-extension-20260727, HEAD 6492d60c1ee73b70213f3028162abc8450a0991e. 기존 WIP 54 + L2-1 18 = 72개(전체 tracked 수정 12, untracked 60, staged 0). index SHA256 C836735662FBF9700AA0DBC8B94848425442E82168F45A00133A6CFE595E83ED. 보존 검증 기준은 상태 행과 경로별 실제 파일 SHA256이며 단순 개수 비교가 아니다. 운영 데이터, DB·LLM·KPIS, 서버, 테스트는 사용하지 않았다.

## 1. 실제 호출 흐름

### 두 진입 경로와 공통 출구

- 화면: app/Lmstudio_SSAI_chat_main.py의 SIMS 메뉴·권한 확인 → app/ui/sims_entry.py:50 _panel_sidebar()가 업무그룹·대상·action 선택 dict 반환 → app/ui/sims_panel.py:2662 render_sims_main(selected)가 _CATEGORIES에서 view 호출 → 제출된 조건과 업무 결과 payload를 받아 채팅 게시. app/sims/config.py, app/sims/router.py의 run_sims_action을 사용하는 Hub 경로도 있으나 주 패널의 직접 호출자와 혼동하지 않는다.
- 자연어: main의 _should_dispatch_current_table_followup(5982) 등 현재표/신규 질의 판별 → 신규는 app/sims/nlq/nlq_router.py의 try_handle_nlq, 업무별 handler 또는 _try_handle_io_nlq(6533) → app/services/io_nlq.py의 resolve_io_nlq(3586), 업무 식별 및 기간·조건 해석 → action/params 또는 조건 보완 → 서비스 선택·호출 → payload 정규화와 _record_io_display_source_query_meta → 게시. 후보 조회와 본 조회는 다른 호출이다. 기존표 재분석은 신규 조회 서비스를 다시 호출하는 것이 기본 계약이 아니다.
- 공통 출구: app/ui/chat_middleware.py의 push_sims_result_to_chat(=wssz) → inbox → drain_inbox_to_chat → 저장 가능한 메시지 요약과 세션 결과 참조 → 카드 렌더. _normalize_result_for_chat은 결과 정규화, _stash_sims_export_provenance 및 _download_source_context는 다운로드 원천/근거 처리, _render_sims_result_actions_plain 및 lazy/fragment 경로는 버튼 표시 담당. app/ui/sims_table_display.py:1624 render_sims_table → 표시용 DataFrame + 열 설정. 일련의 순차 자동 실행이 아니다. **CSV/Excel, LLM 버튼, 현재표 후속질문은 같은 결과에서 분기**한다. 세션에 원본이 없으면 저장된 요약을 표시할 수 있어도 전체 자료를 복원했다고 볼 수 없다.
- 분석: app/ui/sims_analysis_profiles.py의 sanitize_sims_llm_dataframe, build_sims_analysis_profile → chat_middleware의 _build_sims_analysis_context_from_df, _build_sims_sales_time_profile 등 → 분석 요청. 현재표 계산은 app/ui/current_table_followups/action_dispatcher.py 및 analysis_facts.py:163 build_whole_table_facts가 담당하고 main에서 verified_facts를 사용한다. action 등록이 곧 모든 후속 계산 지원을 뜻하지 않는다. 전체 행수와 실제 Facts 입력 행수를 따로 확인해야 한다.

### 업무별 합류점과 남는 차이

아래에서 views = app/sims/views, nlq = app/sims/nlq, services = app/services. 목록/상세·집계 중 대표 진입점을 표시했다. 표의 서비스가 공통이라는 말은 두 경로가 동일 SQL 문자열·실행 횟수·최종 DF까지 보장한다는 뜻이 아니다.

|업무|화면 → 조회|NLQ → 조회|공통화 범위 및 예외|
|---|---|---|---|
|010 업무코드|views/codes.py render_codes_by_group, render_search_codes → _service_search 등 → rddbc010_service|nlq/nlq_codes.py try_handle_codes_nlq가 views/codes의 _service_search, _merge_group_results, 결과 뷰 생성 helper까지 import|G/T코드와 명칭 조회 재사용. NLQ가 view에 역의존하는 사례라 신규 표준으로 삼지 않는다.|
|030 거래처|views/vendors.py:684/959 render_vendor_list/detail → rddbc030_service.search_vendors_full 등|nlq/nlq_vendors.py try_handle_vendors_nlq → search_vendors_full|후보·명칭과 목록 서비스를 재사용하나 조건 조립 및 결과 후처리는 경로별. 대표거래처 등 역할을 단순 거래처명과 등치하지 않는다.|
|040 제품|views/goods.py:285/549 view_goods_list/detail → rddbc040_service.search_goods_full / get_goods_detail_full|nlq/nlq_goods.py try_handle_goods_nlq → search_goods_full, 자체 표시 DF 처리|제품 검색은 공통, 상세와 목록은 다른 계약. 금융에 제품 조건을 관성적으로 넣지 않는다.|
|060 사용자|views/users.py:570 render_user_list_with_dept → _service_search_users → rddbc060_service의 검색 함수|nlq/nlq_users.py try_handle_users_nlq → search_users_full|부서명 보완·안전 컬럼·표시 처리 차이가 있다. 전체 사용자 원본을 채팅 또는 LLM에 건네지 않는다.|
|110/120 입·출고명세|views/rddbc_io_inout_views.py _view_inout → _render_inout_form → _run_inout_submit → rddbc110_service.get_rddbc110_screen_result / rddbc120_service.get_rddbc120_result|resolve_io_nlq → _try_handle_io_nlq의 서비스 매핑 → get_rddbc110_result / get_rddbc120_result|입고 screen wrapper도 동일 업무 서비스로 합류. 기간·거래처·제품·입출고 코드 등을 params로 전달하나 화면에만 로컬 필터/전체자료 재부착이 있다. 아래 참조.|
|130/140 거래명세서·세금계산서|views/rddbc_io_doc_views.py:658 _view_rddbc_doc_common → view_rddbc130/140이 지정한 서비스|같은 IO NLQ dispatch → rddbc130_service.get_rddbc130_result / rddbc140_service.get_rddbc140_result|조건 폼 공통화와 테이블별 SQL 분리 사례. 문서 키와 금액은 명세행과 다른 단위. export·요약 보완 경로까지 일률적으로 한 SELECT라 할 수 없다.|
|210/220 실·장부재고 월집계|views/rddbc_io_stock_views.py:991 _view_rddbc_stock_common → view_rddbc210/220 → 개별 result|IO dispatch → rddbc210_service.get_rddbc210_result / rddbc220_service.get_rddbc220_result|월 조건, 제품·재고 의미를 공유하되 테이블이 다름. 260/270에는 월 폼 분리 방식을 적용하고 재고 수량 규칙은 복제하지 않는다.|
|230 단가별 재고|views/rddbc_io_stock_cost_views.py view_rddbc230 → services/rddbc230_service.py get_rddbc230_result|services/erp_table_nlq.py resolve_registered_erp_table_nlq → _resolve_rddbc230_nlq → 같은 get_rddbc230_result|실제 노출 action은 **최종 매입단가 조회**. 이름·코드·범위·역할 의미 해석은 Registry만으로 끝나지 않는다.|
|제품수불현황|views/rddbc_io_flow_views.py:119 view_product_flow → product_flow_service.get_product_flow_result → get_product_flow_df|IO dispatch의 동일 result|복합 원천의 수불 업무 서비스. 단일 테이블 동적 조회의 예시가 아니다.|
|제품재고장|views/rddbc_io_inventory_views.py:194 view_product_inventory → product_inventory_service.get_product_inventory_result / get_product_inventory_df|IO dispatch의 동일 result|월/정확 조회 경로의 선택·원천 근거가 중요. 기존 260 명칭/세션 키는 금융 장부잔고 전용이 아니다.|
|KPIS|app/ui/kpis_erp_query.py의 조건 폼 → _call_selected → kpis_erp_direct/kpis_rest_client → publish_result|app/ui/kpis_nlq_chat.py try_handle_kpis_chat → services/kpis_nlq.py parse_request(명시 KPIS) → 조건 보완 또는 같은 실행·게시 helper|두 입구에서 재사용할 경계의 예. 회사/세션 재인증·결과 1회 정책은 참고하고 외부 REST·KEY·후보 숫자 제한은 ERP SQL로 이식하지 않는다.|

### 110/120과 230: 실제로 어디까지 같은가

1. 110/120 화면은 _render_inout_form(350)에서 날짜·거래처·제품 등 위젯과 기존 대상별 후보 행을 구성하고, _run_inout_submit(727)에서 _prepare_io_service_params 후 result 함수를 부른다. 일자 기본은 월초~오늘 경로가 있고 사용자가 준 값과 위젯 상태가 우선한다. NLQ는 질문에서 action·기간을 판별하고 거래처의 역할/명칭, 코드, 제품 조건을 해석한다. resolve_unlabeled_io_entity_condition 등 후보 확인과 _normalize_io_detail_vendor_params, 기간 정책을 거쳐 서비스용 dict를 만든다. 모든 날짜 선택·거래처 해석을 화면과 NLQ가 같은 함수에서 하는 것은 아니다. 특히 명칭을 코드로 해결하지 못했을 때와 필터를 지정하지 않았을 때를 구별해야 한다.
2. 110/120은 각 서비스의 get_rddbc…_df와 SQL 구성 → 공통 조회/라벨/결과 builder를 사용한다. 그러나 _finalize_io_payload는 화면측 필터를 다시 적용할 수 있고 _attach_inout_full_df(114)는 export DF를 재취득할 수도 있다. 110 get_rddbc110_result는 전체 df와 잘린 df_display를 별도로 보관하지만, 이 helper는 _io_full_df_ready일 때 **df_display를 df보다 먼저 선택**한다. 두 DF 길이가 다르고 이 분기를 타면 전체자료가 표시자료로 바뀔 수 있다. 코드로 확인된 우선순위이지 특정 운영 다운로드에서 누락되었다는 관측은 아니다. 신규 업무는 양 경로에서 full을 한 번 확정하고 head를 만드는 방향으로만 파생할 것.
3. 230: Registry의 ErpTableActionSpec → view_rddbc230 또는 resolve_registered_erp_table_nlq → _resolve_rddbc230_nlq(197)가 filter_labels, 역할·숫자·명칭 추출로 action/params 생성 → 같은 normalize_rddbc230_params → get_rddbc230_result / get_rddbc230_df → _filters(매입처 코드 exact, 명칭 LIKE 등) → execute_bound_select(sql, values) → DF → 결과/표시 순서 및 전체자료 메타데이터. UI와 NLQ의 _display_context가 다를 수 있다. 상태 조회이며 nlq_period_policy=explicit_only. source와 화면 TOP이 독립이다. PK 7열이며 표시용 조회순번과 다르다. SQL 식별자는 서비스 소유이고 사용자 값만 바인딩한다. 수치 범위의 float 변환, 상한 도달 시 완전성 미확정 처리 등은 금융 금액/전체자료 계약의 대체재가 아니다.
4. nlq_router의 _try_handle_io_nlq에는 기존 업무 사전 매핑, 등록 업무 callable 해석과 호환 fallback이 공존한다. 서비스 미등록 시 UI를 대신 호출하는 식의 호환 경로를 신규 업무 정상 경로로 인정하면 조건 누락이나 이중 게시를 찾기 어렵다. 새 action은 지정된 서비스만 허용하고 실패 단계(해석/조건/권한/실행)를 분리한다.

### Rddbc230 Git 증거

git log --follow 및 git show의 최초 도입 7dac1120d38a857ecd703db8bf2f88d1ca37dccb(2026-09-07)는 R070과 R230을 함께 도입한 커밋이다. 신규: services/rddbc230_service.py, services/erp_table_{query_service,nlq}.py, services/product_master_filter_contract.py, sims/meta/erp_table_feature_registry.py, sims/views/rddbc_io_stock_cost_views.py, product_master_filters.py, tools/check_rddbc230_feature.py 등. 기존 수정: io_nlq, nlq_router/action_inventory, sims/config·router, rddbc_io_views·shared, sims_entry·sims_panel·chat_middleware·sims_table_display, current_table_followups/{action_dispatcher,generic}, 권한 정책 등. **schema_map.py·labels_map.py는 이 최초 커밋에서 변경되지 않았다.** 단순 메뉴 등록이나 모든 필드를 공통 맵에 넣은 자동 생성 기능이 아니었다. 이후 dd606da, e04a3c3 등에서 공통 환경·의미 조건도 변했다. 공동 도입의 모든 변경 파일을 신규 업무 체크리스트의 필수 수정 파일로 해석하지 않는다.

## 2. 메타데이터의 실제 소유권

|정보|현재 정의 → 실제 소비|신규 표준 판단|
|---|---|---|
|물리 테이블·필드|app/db/schema_map.py SCHEMA의 tables/cols, app/sims/meta/rddbc_io_meta.py IO_TABLES/IO_COLS 병합. 010/030/040/060 서비스가 물리명 참조. 다른 서비스 SQL에는 물리명이 직접 있음.|공통 맵은 이름 해석용이지 DB 반영·전체 타입 검증기가 아니다. 테이블의 업무 표시명/action은 Registry에 별도 등록.|
|한글 컬럼명|app/db/labels_map.py LABELS, rddbc_io_meta.IO_LABELS → app/services/utils.py apply_labels / rddbc_io_common.apply_labels_safe. 060의 동적 설명 처리도 있다. 230은 SQL에서 한글 alias와 _RESULT_COLUMN_ORDER를 사용.|원본 물리열 → 한글명은 한 군데. SQL alias는 JOIN 역할/계산열 구별에만 쓰고 기존 공개 컬럼은 보존. Excel·NLQ용 번역 맵을 따로 작성하지 않는다. 정렬은 번역이 아니다.|
|타입·PK·grain|확정 DDL, 기존 IO_PK/pk_columns, Registry ErpTableFeatureSpec.primary_key. filter.kind는 질문/위젯 조건의 의미, 실제 DB dtype이 아님.|DDL 타입·길이/scale와 nullable 순번을 명시. PK 선언만으로 JOIN 후 유일성을 보장하지 않으므로 실행 결과 검사.|
|업무코드·명칭|rddbc_io_common.enrich_rddbc010_pairs와 회사별 lookup; Gcode/Tcode 및 010의 코드명. 거래처명/사용자명은 030/060.|코드값과 라벨은 별개. 동명·미등록에서도 PK·금액 보존. 0023은 금융 도메인, 입출고 구분 규칙 재사용 금지.|
|조회조건·별칭|Registry ErpFilterSpec(key,label,column,kind,aliases), ErpTableActionSpec(action,aliases,mode,…), filter_labels 및 match_action_in_text → erp_table_nlq/io_nlq의 업무 해석 → 서비스 normalize. 230은 _filters가 SQL의 실제 조건 소유.|질문 속 업무 별칭, 필터 별칭, 결과표의 열 별칭을 구별. 조합·날짜 우선순위·범위 검증은 설정만으로 생기지 않는다.|
|업무 순서·고정열·표현|230 _RESULT_COLUMN_ORDER 및 projection → sims_table_display._default_pinned_cols, _numeric_display_kind, _make_column_config, build_sims_table_display_config.|PK 순서≠사용자 우선 열. 표시 순서/고정열/날짜/문자코드/정확 금액 역할을 필요할 때 action 설정으로 전달. 표시 포맷 때문에 원본 DF를 문자열화하지 않는다.|
|분석 역할|sims_analysis_profiles와 chat_middleware의 날짜·금액 후보 추론, current_table_followups/analysis_facts의 수치 처리. build_whole_table_facts에는 float 경로 존재.|분석 기준일/거래처 역할/측정값/합산 허용 여부는 DDL이나 조회 라벨만으로 결정되지 않는다. 승인된 업무 역할 설정을 우선 사용하고 유효한 Facts만 허용.|

## 3. 신규 한 업무의 모듈 구성표

양쪽 = 화면·신규 NLQ가 같은 계약을 소비. ‘보완’은 후속 승인 대상이며 기존 프로그램의 기본 동작 변경을 뜻하지 않는다.

|분류|실제 기준 경로·진입점|해야 할 일 / 의존성 / 양쪽 사용|
|---|---|---|
|신규 작성|app/services/rddbc230_service.py normalize_rddbc230_params, get_rddbc230_df/result 예|업무 원천·조건·JOIN·PK에 맞는 서비스 1개. 정규화/조회/결과를 한 방향으로 호출. 양쪽.|
|신규 작성 또는 형제 폼 확장|app/sims/views/rddbc_io_inout_views.py _render_inout_form; rddbc_io_stock_views.py _view_rddbc_stock_common|사용자 승인된 기본조건/후보/제출만 얇게 묶음. 화면 전용, SQL·다운로드·LLM 없음.|
|설정 추가|app/sims/meta/erp_table_feature_registry.py ErpTableFeatureSpec, ErpFilterSpec, ErpTableActionSpec, get_action_spec|업무명/별칭/역할/서비스·뷰·export 경로/권한/기간 정책/고유 payload_key. menu_actions/targets가 소비. 양쪽. registry_completeness_errors 외 키 유일성·호출 가능성 검증 필요.|
|설정 추가|app/db/schema_map.py SCHEMA, app/db/labels_map.py LABELS, app/sims/meta/rddbc_io_meta.py의 병합 방식|새 테이블 논리명·원본 라벨 참조, 타입/PK는 업무 메타와 연결. SQL 출력/양쪽 결과 공유. IO 전용 도메인 규칙에 금융을 넣지는 않는다.|
|설정 + 실제 parser|app/services/erp_table_nlq.py resolve_registered_erp_table_nlq; app/services/io_nlq.py resolve_io_nlq; app/sims/nlq/action_inventory.py _registered_erp_table_actions|NLQ 공개할 때만 업무/열 별칭, 역할 해석, 조건 미해결 응답 추가. 서비스는 복제하지 않는다. main의 현재표 판별 우선순위 유지.|
|직접 재사용|app/services/erp_table_query_service.py registered_query_limits, build_feature_result; app/services/rddbc_io_common.py build_result_payload|정규화된 params, DF → 표시/상한·공통 payload 기본형. 호출자가 full/slice/count/출처를 명시해야 전체자료 보증. 양쪽.|
|직접 재사용|app/services/rddbc030_service.py search_vendors_full; rddbc060_service.py search_users_full; rddbc_io_common.py enrich_rddbc010_pairs|현재 회사 후보 → 선택된 코드/코드명. UI의 선택 이벤트와 NLQ의 모호성 안내는 각각, 정규화 이후 동일.|
|직접 재사용 + 좁은 보완|app/services/erp_table_query_service.py execute_bound_select → app/db/mssql_client.py query_to_df; app/services/ssai_auth_service.py connect_company_db|현재 연결의 명시적 회사 일치·정밀도 보존이 증명되지 않으면 금융을 일반 reader로 단순 치환 금지. 바인딩·조회 전후 인가·exact read 옵션을 필요한 소비자만 사용.|
|직접 재사용 + 설정|app/ui/sims_entry.py _panel_sidebar, app/ui/sims_panel.py render_sims_main; app/services/ssai_permission_policy.py의 기존 권한|기존 메타로 노출·실행. 조건 렌더만 화면 전용. 서비스 경계의 실제 IO_READ를 UI 메뉴 숨김으로 대체하지 않는다.|
|직접 재사용 + 최소 보완|app/ui/chat_middleware.py wssz, drain_inbox_to_chat, _download_source_context; app/ui/sims_table_display.py render_sims_table|게시·표·다운로드 공유. exact·nullable 역할과 안전한 전체 소스 선택, EXPORT_EXCEL 확인을 공통 경로에서 보장할 때만 전용 분기 제거.|
|최소 보완|app/ui/sims_analysis_profiles.py build_sims_analysis_profile; app/ui/current_table_followups/analysis_facts.py build_whole_table_facts 및 action_dispatcher.py|기준일·정확 수치·허용 계산 역할을 action에 바인딩. 불가한 후속은 안내, 범용 float 합산을 금융에 무조건 적용하지 않는다.|
|수정 불필요|app/services/kpis_rest_client.py, 기존 KPIS auth/KEY, ERP 쓰기 경로, Dashboard/Snapshot·모델/DB 설정|내부 ERP 읽기 등록과 무관. 새 테이블마다 공통 채팅/메인 라우터 전체를 복제하거나 바꿀 이유 없음.|

## 4. 금융 5개 조회의 권장 최종 구조 (미구현)

|업무 / 서비스 제안|조건과 원본 PK(접두어 생략)|표·분석 의미|
|---|---|---|
|310 app/services/rddbc310_service.py|입출금일 기간, 거래처, 0023 계열·개별코드. Rd31_Receive_YyMmDd + Receive_Seq|입출금일·순번·거래처·구분·금액 우선. 전표일을 대체 기준으로 쓰지 않는다. 50 미만/이상은 매출·입금/매입·출금 **계열**. 00/50 발생액은 현금 아님. 32/82는 장부·실잔액 조정 할인.|
|320 app/services/rddbc320_service.py|선택한 발행/만기 등 날짜 역할, 거래처·어음. Rd32_Note_No + Note_No_Seq|어음번호는 문자, 순번은 nullable 정수. 어음 1건의 원본 금액 유지. 승인 없는 상태 계산 금지.|
|330 app/services/rddbc330_service.py|거래처·담보종류·어음 및 해당 날짜. Rd33_Ven_Cd + Mortgage_Kind + Note_No + Note_Seq|동일 어음 복수 거래처·제공/회수 가능. 310·320과 어음번호/순번으로 관계가 있어도 기본조회에서 사실행끼리 JOIN·합산하지 않는다.|
|260 app/services/rddbc_balance_month_service.py|장부, 처리월 범위·거래처·0023. Rd26_Ven_Cd + Io_YyMm + Receive_Di|월 원본의 입출금금액·공급가액·세액. 기말잔액을 임의 계산하지 않는다.|
|270 위 월 서비스 공동 사용|실잔고, 같은 계약과 Rd27_ 접두어. 테이블/컬럼은 코드 소유 allowlist로 선택|260과 별개 action·source_table·상태 키. 310/130 집계 원천과 월집계를 더하지 않는다.|

5개 액션은 동일한 get_result(params) 계열 인터페이스. 260/270은 하나의 내부 월 함수에 테이블별 얇은 진입점만 둔다. 공통 정규화(기간, 월, 코드, 빈칸)와 바인딩을 사용하되 일/월, 발행/만기 등 역할은 남긴다. SCHEMA/LABELS의 공개 소비 경로와 Registry를 연결; 타입·PK 제공자가 기존 IO_META의 입출고 규칙을 상속할 필요는 없다. exact reader → 전체 검증 → build_feature_result + full DF/표시 slice/원천·기간·권한 binding → 기존 채팅 공통 출구. 향후 NLQ는 erp_table_nlq의 업무 해석과 action_inventory에 준비된 업무만 공개하고 동일 get_result 호출. 신규 NLQ나 별도 dispatcher는 이번에 만들지 않는다.

**키 충돌:** 기존 제품재고 경로와 금융 260의 __io260_last_payload가 겹친다. 제품재고장 기존 키를 그대로 두고 금융 쪽만 업무 고유 키(예: __erp_payment_260_monthly_last_payload)로 지정. Registry 외 기존 하드코딩 키도 함께 검사. 폼/후보 키, rerun 직후 stash·회사 전환 cleanup·현재표 출처까지 구분하고 구키로 금융 결과를 되살리는 호환 fallback은 두지 않는다.

### L2-1 18개 변경의 처분안 (아직 삭제/교체하지 않음)

|대상|최종 권장 처분 및 제거 전 전제|
|---|---|
|app/sims/meta/payment_fields.py|**독립된 병렬 라벨 사전은 폐지**. fields_for의 원본 필드·타입·길이·PK는 유실 금지. 기존 IO_TABLES/IO_COLS/IO_LABELS 병합 패턴처럼 도메인 타입 메타에서 schema_map/labels_map으로 투영하는 단일 정의로 정리(필요 시 app/sims/meta/rddbc_payment_meta.py). 단순 파일명 변경이 아니라 서비스·폼·Excel 각자의 라벨 소유를 없앤다.|
|app/services/payment_query_service.py|**모놀리식 서비스 폐지**. build_query/_projection의 검증된 원본 SELECT·마스터 연결·안정된 PK 정렬을 업무별 3 + 월 1 서비스로 이관. execute_finance_select의 명시 company_id·timeout·Decimal 보존, limit+1 초과 거부·필드 검사·PK 중복 검출·조회 전후 인가는 공통 reader/결과 계약에 먼저 확보. float 기본 reader로 일괄 치환 금지.|
|app/sims/views/payment_query_views.py|**파일 유지, 역할 축소**. view_rddbc310/320/330/260/270은 화면 진입점. 범용 필드 폼 view_payment를 업무 명세형/월형 얇은 조건 어댑터로 교체. 기존 ERP 후보/달력/제출 재사용; 세금·재고 필터를 그대로 복사하지 않는다.|
|app/ui/payment_query_display.py|**폐지**. display_copy/excel_copy/render_finance_downloads의 필요한 nullable·Decimal·긴 숫자 보존만 공통 열 역할/워크북 정책으로 이관. plain/lazy/fragment에서 같은 원본과 두 권한을 확인한 후 전용 버튼 제거.|
|app/services/finance_query_access.py|**전용 모듈 폐지, 보안 검사는 유지**. local_binding/authorize/result_allowed의 user+company+epoch·IO_READ·다운로드 시 EXPORT_EXCEL·사후 폐기를 공통 경계에서 동등하게 구현/입증한 뒤 제거. 기존 로컬 로그인/권한과 신선한 권한 확인을 구별. 일반 MSSQL 기본 연결 fallback으로 회사 선택을 대체하지 않는다.|
|tools/check_payment_basic_query.py; docs/02_design의 L2-1 설계서|테스트는 유지하되 구현 모듈명보다 행 단위·정밀도·인가·채널 동등성에 맞춰 개편 예정. 승인된 설계서 보존.|
|app/services/rddbc010_service.py, rddbc060_service.py, ssai_permission_policy.py|후보/명칭과 기존 IO_READ 정책의 필요한 부분은 보존. 역할 부여 또는 금융 액션 이름에 의한 별도 정책을 증식시키지 않는다.|
|app/sims/meta/erp_table_feature_registry.py, app/sims/nlq/action_inventory.py, nlq_router.py|5개 업무 등록·고유 키는 필요. 현재 WIP의 nlq_enabled/nlq_only는 미구현 NLQ 비노출 목적; 이전 HEAD의 기본 표준이라고 서술하지 않는다. 실제 parser 완성 전 노출하지 않는다.|
|app/ui/sims_entry.py, sims_panel.py, chat_middleware.py, sims_table_display.py, current_table_followups/action_dispatcher.py|메뉴/조건 진입과 기존 공통 게시 유지. 금융명으로 분기하는 render/export/LLM 차단은 역할·지원능력 설정으로 대체. 권한·정밀도·전체 원본 검증 전에 분기만 삭제하지 않는다.|

## 5. 동일성 검증 설계와 승인 사항

동반 표준 초안 6절의 DB-free 4단계(파서, 서비스, 결과 소비자, 통합)로 분리한다. 동일 회사·사용자·고정한 오늘과 후보 fixture에서 화면/NLQ의 정규 조건과 SQL bind 순서, 정렬된 복합 PK·원본 금액, 라벨·열 순서, 표시/다운로드 행수, 분석 Facts·현재표 근거를 대조. 명칭 미해결/역전 기간과 인가 실패는 업무 SELECT 0회. 행 상한 초과·중복 PK는 성공이나 0건이 아니다. 18자리 음수·앞자리 0·NULL·동일 어음 복수 담보·표시상한+1을 반드시 포함. 양 경로가 똑같이 틀릴 수 있으므로 서로의 출력뿐 아니라 독립 기대값과 비교한다.

결정 필요: 메뉴/핵심 조건과 날짜 기본값(310 월초~오늘은 제안), 320/330의 분석 날짜 역할, 업무별 허용 Facts/후속문, 운영에서 허용할 자료 범위. 공통 reader의 Decimal 보존과 회사 일치, 저장/만료 시 권한 재평가는 실제 교체 전 별도 검증 필요. 문서·정적 호출로 확인한 범위와 운영에서 발생한 결과를 구분한다. 기존 ERP·KPIS의 의심 경로를 이번 조사 명목으로 수정하지 않는다. 승인 순서는 업무 계약 → 공통 안전 경계 → 원본 서비스와 폼 → 공통 카드 → NLQ 동등성 → 잔여 전용 코드 역참조 제거. **이번에 소스·WIP·설계서 변경, 회귀 실행, diff 생성 없음.**

조사 종료 대조 결과: git status --porcelain -uall의 경로·상태 72/72 동일, 해당 파일의 SHA256 72/72 동일. 브랜치·HEAD·index 원본 SHA256도 조사 전과 일치했다. 저장소에 표준 문서를 등록하지 않았다.
