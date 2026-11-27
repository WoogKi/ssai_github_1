# SIMS AI L2-R1 | 입출금과 기존 ERP 공통 구조 재조사
2026-11-27 · 1호기 · 조사 전용

## 판단
**전용 화면에 기존 화면을 흉내 내는 부분 보정보다, 업무 SQL·필드 의미만 남기고 기존 ERP 제출 → 결과 payload → 채팅 카드 경로에 다시 연결하는 방식을 권장한다.** 입출고 프로그램 자체를 복사할 필요는 없다. 조건 UI의 기준은 110/120, 명세와 월집계 분리는 130/140·210/220, 신규 업무의 등록과 전체/표시 자료 계약은 230이 가장 가까운 선례다. KPIS는 외부 REST 인증·후보 UX의 비교 대상이지 내부 ERP 금융 조회의 결과 파이프라인 모델이 아니다.

조사 근거는 기준 커밋의 코드와 현재 작업트리의 실제 함수·diff다. 아래 위치는 저장소 루트 기준이며, 줄 번호는 식별을 돕는 현재 소스 위치다. 사용자 실측 현상과 소스에서 확인한 원인을 구분했다. 운영 데이터, 브라우저 런타임, 실행계획, 역할 배정 현황은 확인하지 않았다.

## 1. 기준 및 변경 경계
- 저장소 C:\New\Python_Project\LmStudion_project1 / feat/dashboard-stock-extension-20260727 / HEAD 6492d60c1ee73b70213f3028162abc8450a0991e.
- 시작 시 porcelain -uall: 72건, staged 0. 기존 L2-1 이전 manifest Status 54건(기존 수정 계획서 1 + 미추적 53)은 그대로이고, 그 위에 L2-1 18건(기존 소스 수정 11 + 신규 7)이 있다. 691개 기준 파일 해시와 비교해 달라진 기존 파일은 아래 11개뿐이다. Git의 디렉터리 단위 미추적 축약 건수를 파일 수로 사용하지 않았다.
- L2-1의 신규 7개는 아래 검토 대상 Python 6개와 docs/02_design/SIMS_AI_ERP_PAYMENT_LEDGER_DESIGN_20261126.md이다. 비교 대상은 전날 만든 helper가 아니라 HEAD의 함수다.
- 이번 변경은 외부 보고서 하나뿐이다. 소스/설계서/기존 WIP의 추가·수정·삭제, diff 파일 생성, 회귀·DB·외부 호출, 서버/2호기 및 Git index 작업 없음.
- 작성 후 시작 시점과 동일한 방식으로 재채취한 결과: 브랜치·HEAD·72개 Git 상태 항목 및 각 WIP 파일 SHA256 전부 동일, staged 0. 보고서 작성용 사본은 Git 무시 작업 폴더에만 두었다.

## 2. 기준 업무의 실제 실행 경로

### 입고명세·출고명세: 선택부터 분석까지
1. app/ui/sims_entry.py:_panel_sidebar는 업무그룹 → 명세/공통 등 작업그룹 → 작업을 정하고 category/action을 반환한다. app/Lmstudio_SSAI_chat_main.py:11575에서 선택을 읽는다. app/ui/sims_panel.py:_CATEGORIES(1258), render_sims_main(2662)은 카테고리/액션으로 view를 찾고, consume_panel_submission, form_id, 마지막 결과 상태로 단순 rerun과 명시적 조회를 분리한다. 기존 업무는 정적 매핑과 registry 등록이 공존한다.
2. app/sims/views/rddbc_io_inout_views.py:1088,1107의 view_rddbc110/120 → _view_inout(955). 같은 함수를 쓰되 고유 prefix, payload key, 거래처 범위 purchase/sales, 순번 키, alias map, 서비스 함수를 주입한다. _render_inout_form(350)은 첫날~오늘을 초기값으로 하고 전달 defaults 우선, _render_date_input_with_week와 거래처·제품 후보 행을 같은 입력 흐름에 쓴다. 검색/후보 선택과 실제 제출은 다른 이벤트다.
3. _run_inout_submit(727)와 rddbc_io_shared.py:_prepare_io_service_params(1751), _finalize_io_payload(1809) 계층에서 화면 조건, 후보, 서비스용 조건, 결과의 로컬 필터를 연결한다. 재사용할 것은 이 제출 계약이며 재고·제품·입고순번 전용 조건까지 금융에 가져오는 것은 아니다.
4. 110의 주입 함수 rddbc110_service.py:get_rddbc110_screen_result(623) → get_rddbc110_result(532) → get_rddbc110_df(396). 120은 rddbc120_service.py:get_rddbc120_result → get_rddbc120_df. 각 서비스가 기간·거래처·구분에 맞춘 SQL과 명칭/업무 컬럼을 소유한다. rddbc_io_common.py:build_result_payload(474)는 DF 사본의 라벨·유일한 열 이름·업무코드 쌍 명칭을 처리한다.
5. _attach_inout_full_df(114)는 _io_full_df_ready가 있으면 준비된 자료를 재사용하고, 없으면 업무별 export_df를 가져와 같은 필터를 적용한다. df=전체, df_display/records/columns=표시용, meta=조건·전체/표시/분석/다운로드 건수. 모든 기존 조회가 항상 단 한 번의 SQL이라는 뜻은 아니다. 금융은 이미 검증한 전체 DF가 있으면 재조회하지 않는 쪽이 맞다.
6. sims_panel.py:_render_payload → _apply_panel_display_limit_to_payload → _stash_panel_table_for_current_followup / _store_panel_final_payload_for_chat. 채팅 게시 경로의 chat_middleware.py:wssz → drain_inbox_to_chat가 현재 방 메시지와 표 참조를 연결한다. 저장된 조회요약과 세션의 전체 표는 다르다. 표가 만료되면 이력을 읽을 뿐 자동 재조회하지 않는 기존 계약을 유지한다.
7. chat_middleware.py:_render_chat_item_body → 공통 표 및 _render_sims_result_actions_plain/lazy/fragment. 작은 표와 큰 표가 다른 준비 시점을 쓰더라도 CSV/Excel/분석이 같은 카드의 작업이다. sims_table_display.py:build_sims_table_display_config(1519)에서 순번·고정열·정렬·형식을 정한다. 다운로드는 화면 head가 아니라 전체 자료에서 생성한다.
8. 분석은 버튼에서 표별 컨텍스트를 사용한다(chat_middleware.py:3677,4250 부근,5807). sims_analysis_profiles.py:build_sims_analysis_profile(353), sanitize_sims_llm_dataframe(317), current_table_followups/analysis_facts.py:build_whole_table_facts(163)가 해석과 검증 근거를 분리한다. 메인:14035에서 whole_table_facts와 표시행수/조건을 verified_analysis_facts로 넘기며 :11155 부근에서 검증 테이블/서술을 처리한다. 화면 DF 일부를 LLM에 넘겨 합계를 계산시키는 구조가 아니다.

### 업무별 차이와 재사용 경계
|업무|소스·호출과 반환 자료|금융에 적용할 점|
|---|---|---|
|010 업무코드|sims_panel의 그룹코드조회/코드명 검색 → views/codes.py:render_codes_by_group, render_search_codes → rddbc010_service의 그룹·명칭 검색. Gcode/Tcode 및 한글명으로 표시.|0023 등 그룹을 고정하고 원본 코드/명칭을 분리. 명칭으로 코드를 추정하지 않는다.|
|030 거래처|views/vendors.py:render_vendor_list(684) → _search_vendors_service, 상세(959)는 코드별 조회. 목록은 표시/집계 요약도 별도 구성. 입출고 조건용은 rddbc_io_shared.py:_lookup_vendor_candidates(710), _apply_vendor_pick(820)이며 거래처 전체 화면을 임베드하지 않는다.|역할에 맞는 명시적 후보 선택·직접 코드. 매출·매입 거래처 범위를 은행/담보 거래처에 그대로 적용하면 누락 위험.|
|040 제품|활성 메뉴는 views/goods.py:view_goods_list(285)/view_goods_detail(549) → search_goods_full/get_goods_detail_full. 주석이나 미사용 별칭 view_rddbc040이 기본 진입점은 아니다.|코드/이름 동기화와 조건/전체/표시 계약만 참고. 금융 테이블에 없는 제품 검색을 도입하지 않는다.|
|060 사용자|views/users.py:render_user_list_with_dept(570) → _service_search_users(496) → search_users_full 또는 search_rows → _build_user_list_view(223). 부서 등 010 옵션 및 안전한 표시 열을 사용.|등록자·수정자·담당자 코드/이름만 후보로 사용. 사용자 원본의 민감 열을 금융 결과나 분석에 포함하지 않는다.|
|130·140|views/rddbc_io_doc_views.py:785,800 → _view_rddbc_doc_common(658) → rddbc130_service:get_rddbc130_result(738)/rddbc140_service:get_rddbc140_result(447), 별도 export_df. 문서 헤더·반복 상세 필드를 명세 행과 구분.|공통 폼에 업무별 날짜/번호/필터만 주입하는 선례. 문서 상세 합산 규칙은 이식하지 않는다.|
|210·220|views/rddbc_io_stock_views.py:1095,1108 → _view_rddbc_stock_common(991) → rddbc210/220_service의 get_*_df/result/export_df. 기존 화면은 달력 기본 월초/오늘(332~335)을 쓰고 월 기준 조건으로 연결한다.|260/270은 같은 형태의 월 입력·원본 월별 행을 공유하되 제품·창고와 잔고 거래처의 집계 축은 다르다.|
|230 단가별 재고|erp_table_feature_registry.py의 action 정의 → rddbc_io_stock_cost_views.py:view_rddbc230 → rddbc230_service.py:normalize_rddbc230_params, _filters, _prepare_result_projection, get_rddbc230_result(243). get_*_df 1회, source_grain/full_source_ready/limit_hit/표시·전체행수 기록.|신규 ERP 기능의 가장 작은 등록 모델. 금액을 float로 바꾸는 범위 파싱까지 복사하지 말 것. 금융 상한 초과 전체 거부 유지.|
|제품수불현황|sims_panel → rddbc_io_views의 view_product_flow → rddbc_io_flow_views(제품 1개 확인·조건 입력) → product_flow_service.py:get_product_flow_df(1013)/result(1318). 입출고 및 마스터를 업무 행으로 구성.|필수 대상 확인·공통 카드만 참고. 수불 집계 SQL을 금융 원본 조회에 사용하지 않는다.|
|제품재고장|제품재고현황 조회의 명칭/자연어 별칭. rddbc_io_inventory_views.py:view_product_inventory → product_inventory_service.py:get_product_inventory_result(3988), _collect_source_df(2976), 집계/이월/상세 자료를 기준일에 맞게 구성. view_rddbc260은 이 화면의 **기존 별칭**이지 금융 R260이 아니다.|원본 입출금에서 재고 이월을 재현하지 않는다. 상태 키와 식별자를 테이블 숫자가 아닌 업무별로 분리한다.|
|KPIS|kpis_erp_query.py:render_kpis_erp_query(398) → 서비스별 기본일/후보/명시적 조회 → _call_selected(340) → 회사별 direct 서비스/REST. publish_result(118)는 성공·권한·회사/epoch 확인, 공식 필드 표시용 변환 뒤 wssz로 게시.|재사용할 것은 결과 카드 및 명시적 제출·격리. 외부 호출 승인·API 세션·요청 필드별 form 자체는 내부 SQL 화면의 기본 틀이 아니다.|

## 3. 공통 기능별 판정
**직접**은 기존 호출 유지, **설정**은 업무 데이터/선언만 추가, **보완**은 기존 기본값을 바꾸지 않는 선택적 확장이다.

|영역|기존 입력 → 처리 → 출력|5개 조회 판정|
|---|---|---|
|메뉴·등록|sims_entry의 group/target/action → _CATEGORIES 또는 registry의 view_function/service_function/export_function|설정. 메뉴 위치는 협의. 패널 전용이고 NLQ inventory에는 넣지 않는 속성 유지.|
|달력·기간|rddbc_io_shared:171,186 날짜+주차 / stock_views:332 월 기준. 명시 defaults 우선 → YYYYMMDD 또는 YYYYMM|직접+설정. 310은 입출금일, 320/330 기준일·260/270 월 기본은 업무 승인 후. rerun 덮어쓰기 금지.|
|후보·고급조건|shared:382~529,678~843,1859~1893의 lookup/store/apply/sync; :309 등록·수정자 조건; 업무코드 그룹 조회|직접+설정. 조회/후보 이벤트 분리, 다중 후보 자동 선택 금지. 범위·비활성 이력·캐시 회사 키를 확인한 후 교체.|
|DB·보안|mssql_client.py:get_current_company_id/_get_company_engine/get_conn, read_only_request(420), read_df(476). ERP registry 서비스는 execute_bound_select 사용. 기존 로그인/권한은 ssai_login.py:238,242, ssai_auth_service.py:get_user_permissions|보완. 회사 미선택/불일치 시 연결 전 거부. 기존 read_df의 pd.read_sql은 exact Decimal 옵션을 지정하지 않는다. 검증된 opt-in 경로만 사용하고 기존 호출 기본값은 불변. 기존 named SQL 호환 경로를 그대로 복사해 값 바인딩을 약화하지 않는다.|
|필드·명칭·열순서|db/schema_map, db/labels_map, services/utils.py:apply_labels(정적 라벨, 060은 동적 가능), rddbc_io_common:403,474 코드쌍 보강; R230 _prepare_result_projection|설정. 필드/PK는 단일 정의, 표시 순서·핀·숨기지 않을 전체 필드는 업무별 projection. 코드 JOIN과 공통 enrichment 중 하나만 명칭 소유.|
|표·형식|rddbc_io_shared:_prepare_io_display_df(1192), _series_to_nullable_integer, sims_table_display:_numeric_display_kind(804), _make_column_config(1265), _default_pinned_cols(1327). 숫자·코드문자·날짜·빈칸은 표시 복사본에서 변환|설정+보완. smallint 순번과 숫자처럼 보이는 char 식별자를 구분. 금액만 exact integer, NULL은 공란; 전역 float 변환 금지.|
|원본·상한·성능|erp_table_query_service:registered_query_limits, build_feature_result → df/df_display/records/meta. R230의 display/source/export 상한은 각각의 범위. 기존 panel skip-on-rerun 및 회사별 engine/cache|직접+설정. TOP(상한+1) 초과는 전체 실패, 전체 DF 한 번 확보. cache에는 회사·권한 경계 및 action 포함. 이름 연결로 PK 증식하면 실패.|
|Excel/CSV|chat_middleware 공통 plain/lazy/fragment의 전체 df 및 공통 Excel 서식. panel의 기존 EXPORT_EXCEL 검사 :4366,4556과 채팅 경로는 동일 함수가 아님|보완. 모든 다운로드 노출/준비 경로에 IO_READ+EXPORT_EXCEL과 현재 바인딩 보장 후 공통 버튼 재사용. 16~18자리 금액을 텍스트 셀로 보존, 일반 정수는 숫자 셀, 수식형 텍스트 취급 및 NULL 정책 검증.|
|게시·채팅저장|submission → full payload stash → wssz/inbox → 현재 방에 1회 저장. table_key는 원본 데이터 자체가 아님|직접. 조회조건·건수·표·버튼은 채팅 1회, 패널은 조건만. 보관 기간/재로그인 복원 정책을 금융 전용으로 복제하지 않는다.|
|LLM·수치검증|표별 profile → 민감정보 제거 → 전체표 facts + query scope → 분석 runner → 검증된 값만 표시. analysis_facts.py:179,255,341,420에 float 변환/비교 존재|보완. 행수/범위부터 제공. 금액은 exact 합계·근거 검증 후만 허용. 어음/담보번호, 계좌·비고 등 민감 가능 열은 원문 샘플 금지. 업무근거 없는 현금/잔액 합계는 생성하지 않는다.|
|현재표·NLQ|main:5693 → handle_current_table_followup_by_action, 유형별 handler; 별도 일반 질문은 main:13458 → nlq_router:try_handle_nlq/_try_handle_io_nlq. KPIS 명시 질문은 기존 별도 경로|직접+설정. 현재표 범위/행수 등 승인 기능은 같은 table_key와 회사에서만, 재조회 0회. 새 금융 NLQ를 등록하지 않는다. 현 범위에서 후속이 없다면 가능 안내도 숨긴다.|

## 4. 수동 테스트 현상별 원인과 조치
|현상|코드상 근거와 조치|
|---|---|
|그룹 배치|sims_entry:70,375가 입출금관리를 별도 최상위로 추가해 작업그룹 단계 없이 5개를 펼친다. 기존 명세/공통·월집계와 다른 구조다. 명세/월집계에 배치하거나 기존 그룹 아래 입출금 하위 작업그룹을 두는 안 중 선택.|
|기본기간 공란|payment_query_views.py:view_payment는 날짜 value=None, 월은 빈 text_input. 서비스가 기간 필수여서 첫 조회도 불가. 110/120은 :450의 month_first/today. 원본 조회기간 검증은 유지하고 최초 UI 기본값만 정한다.|
|KPIS에 가까운 조건 UX|KPIS 화면을 통째로 호출하는 것은 아니다. 금융 view가 전체 DDL 필드 multiselect·별도 후보 expander·필드별 위젯을 자체 생성하고 일부 030 후보 로직을 공유한다. 업무 우선 조건이 없는 메타데이터 폼이라는 것이 직접 차이다.|
|결과 화면이 다름|sims_panel:_render_payload:3386 금융 조기 반환, chat_middleware:10786 금융 전용 render, sims_table_display:1548,1569의 별도 포맷. 같은 render_sims_table 이름을 써도 진입/전처리가 다르다. 일반 카드 경로에 설정으로 합류시킬 것.|
|컬럼 우선순위|payment_query_service:_projection은 fields_for의 DDL 순서대로 전부 SELECT 후 JOIN 명칭을 뒤에 추가. 310 거래처명·구분명이 금액 가까이에 오지 않는다. 업무 표시 순서와 물리 컬럼 순서를 분리하고 전체 원본은 유지.|
|순번 소수점|payment_fields에는 두 순번이 smallint이지만 get_payment_result → finance_exact_columns에는 decimal만 전달. display_copy 및 금융 전용 정렬이 순번을 지정하지 않고 공통 정수 식별자 목록에도 새 한글명이 없다. nullable 형 추론/일반 숫자 위젯으로 떨어질 수 있다. 스키마 오류가 아니라 표시 계약 누락이다. 실제 셀 dtype·브라우저 반올림은 미재현. Int64/명시 정수 포맷으로 NULL·0·선행 0 문자코드와 별도 검증.|
|다른 다운로드|chat_middleware:6182,6440이 공통 크기별 버튼 조합 이전에 render_finance_downloads로 반환한다. 작은 표도 Excel 준비 버튼을 먼저 거친다. 정확한 writer 규칙만 공통 exporter에 이식.|
|LLM 버튼 없음|동일한 반환 분기 + :5870 fragment도 금융 action 즉시 종료. 전용 함수는 CSV/Excel만 그린다. 모델 연결 장애가 아니다. 공통 분석 컨텍스트가 준비된 카드에서 기존 runner를 사용한다.|
|분석 기준 전표일자|화면 SQL 기본값은 Rd31_Receive_YyMmDd다. 반면 chat_middleware:_build_sims_sales_time_profile(:3291)와 main:_current_table_date_series(:5341)의 exact 후보에 전표일자가 있고 입출금일자가 없다. 두 열이 있을 때 전표가 일반 일자 부분일치보다 우선할 수 있다. 실제 사용자 이벤트가 어느 helper를 탔는지는 미확인. 전역 후보 순서를 바꾸지 말고 action별 분석 기준/기간 필터를 각각 명시.|
|현재표 안내 불일치|chat_middleware:9815 부근의 table_key/상태 기반 안내는 후속 지원으로 표시하지만 action_dispatcher:2079가 finance_query 전체를 안내 후 True로 소비한다. 표시 가능 여부와 실제 dispatcher가 동일한 지원 능력 설정을 읽도록 한다.|

**추가 발견: R260 결과 상태 충돌.** 기존 rddbc_io_inventory_views.py:196,662,700은 __io260_last_payload에 제품재고현황 결과를 쓰고 미제출 시 그대로 반환한다. sims_panel.py:1384도 같은 키다. 신규 registry:322 및 payment_query_views.py:165에서 table=260인 장부잔고도 정확히 같은 키에 쓴다. 이 충돌은 코드상 확정이다. 화면 재진입 시 초기화되는 경우도 있어 실제 오게시/노출은 확인되지 않았다. 먼저 금융 키를 업무별로 구분하고, 이전 결과를 읽을 때 action·회사·사용자/epoch를 확인할 것. 기존 제품재고장 별칭이나 기존 저장 자료를 일괄 개명하지 않는다.

## 5. L2-1 코드 처분안 (실행하지 않음)
분류: **① 업무 고유 유지 / ② 기존 공통으로 대체 / ③ 공통에 선택적으로 보완 / ④ 대체 검증 후 제거**. 파일 전체 존폐와 그 안의 계약을 구분한다.

|신규 파일|판정·권장 처리|
|---|---|
|app/sims/meta/payment_fields.py|① PK·원본 식별자·DDL 한글명·자료형·Gcode 단일 정의는 필요. 260/270 공유 유지. ③ 업무 표시순서/정수 식별자/기준일 역할 추가. 기존 schema/label map이 소비하도록 연결하고 동일한 맵을 두 번 작성하지 않는다.|
|app/services/payment_query_service.py|① 기간·허용열·바인딩, 원본 PK, 업무코드/명칭, 독립 SELECT, 정확한 금액·초과 전체 거부 유지. ② 등록 payload와 조회상한 재사용. ③ exact fetch를 공통 선택 옵션으로 검증한 뒤 ④ 별도 executor·중복 결과 재작성 제거. registry가 실제 참조하는 얇은 wrappers는 참조 구조 대체 전 삭제 금지.|
|app/sims/views/payment_query_views.py|① 업무별 필터 조합만 얇은 ERP view로 남김. ② 날짜·후보·제출·복원은 기존 IO view helper로. ④ 자체 필드 생성 폼, _candidate_input·전용 캐시/검색 복제 제거. 실제 거래처 범위 및 후보 정렬을 먼저 확정.|
|app/ui/payment_query_display.py|③ Decimal/Excel 큰 정수, 안전한 문자열, 빈칸·권한을 공통 표/내보내기 옵션으로 흡수. ④ 전용 다운로드 위젯·캐시·렌더 함수 및 이 파일 제거. 선행 공통 보완 없이 삭제하면 권한/정밀도 회귀.|
|app/services/finance_query_access.py|① 회사·사용자·권한 회수 검사는 보존. ③ 기존 중앙 권한/회사 검사에서 필요한 source binding을 업무 중립적으로 소비. 동일한 거부 동작이 검증된 후 ④ 금융 action명 판정과 KPIS용 세션 객체 어댑터 제거. UI의 cached has_permission만으로 fresh 검사를 대체하지 않는다.|
|tools/check_payment_basic_query.py|① DDL/PK·금액·원본·인가·상한·1회 게시 검증 유지. ② 전용 UI/조기 반환을 정답으로 보는 테스트를 공통 110/230 결과 계약 검증으로 교체. 실제 회사/액션 교차·셀형식·분석 날짜/facts·버튼 순서 추가. 테스트 파일 자체를 제거하지 않는다.|
|설계서|확정 ERP 규칙/IO_READ 유지. UI·분석·권한 공통화의 승인 결정만 이후 반영. 현재 수정 없음.|

기존 파일 11개에 대한 정리:
1. rddbc010_service.py:273, rddbc060_service.py:367: 기존 마스터 검색이 금융 executor를 역으로 import하는 선택 경로를 제거할 후보. 회사/epoch가 캐시 키에서 사라져서는 안 된다. 공통 회사 바인딩 조회가 먼저 준비되어야 함.
2. ssai_permission_policy.py:32: 5개는 IO_READ. 기존 category에 통합되면 중복 category 매핑만 제거. 회사 인가/EXPORT_EXCEL 검사 삭제 금지.
3. erp_table_feature_registry.py:310: 5개 등록 유지, payload key·menu target·표시 메타 교정. action_inventory.py:148,173 및 nlq_router.py:7417의 nlq_only는 단순 잔여물이 아니다. 패널 전용 필터를 없애면 미승인 금융 NLQ가 자동 등록되므로 유지하거나 동등 계약 검증 후 교체.
4. sims_entry.py:70,239,375: 협의한 기존 그룹 매핑으로 이동한 뒤 불필요한 최상위 옵션·역매핑·전용 selectbox 제거.
5. sims_panel.py:3342,3386: 일반 제출/stash 흐름으로 합류. 금융 이름에 의한 강제 경로와 import는 제거하되 source binding 검증은 공통 게시 전으로 이전.
6. chat_middleware.py:5870,6182,6440,7591,10158,10786: fragment/작은 표/큰 표/게시/렌더 전부 점검. 보안 검사를 공통 호출의 전제조건으로 만들고 중복 분기·import 및 조기 return 제거.
7. sims_table_display.py:1548,1569,1585: 금융 라벨로 경로 전환하지 않고 열 역할로 포맷. 기존 금액/재고 전체의 dtype·형식을 전역 변경하지 않는다.
8. current_table_followups/action_dispatcher.py:2079: 지원 능력 설정과 실제 허용 계산으로 교체. 미지원 계산은 여전히 차단. 일반 handler로 무조건 통과시키지 않는다.

## 6. 조회조건 컨셉과 5개 재설계
**ERP 방식**은 날짜·거래처·구분을 처음부터 보여 주고 공통 후보 행과 고급조건을 쓰므로 기존 사용자의 작업 순서가 유지된다. 단, 110/120의 제조사·창고·수량 및 구매/판매 범위를 그대로 복제하면 의미가 틀린다. **KPIS 방식**은 필수값이 적고 외부 요청의 계약이 엄격한 경우 짧고 명확하며 검색형 거래처 콤보도 유용하다. 반면 조건 종류가 많은 내부 원본 명세를 전부 선택형 필드로 만들면 핵심 조건이 숨고 준비/조회 단계가 늘어난다. **권장**: 업무별 얇은 ERP 폼 + 필요한 곳에만 후보 위젯, 결과 경로는 UI 선택과 무관하게 하나. 메뉴 및 최종 컨셉은 사용자 결정 전 미확정.

|업무|권장 기본 조건·앞쪽 표시열(제안)|보존해야 할 의미|
|---|---|---|
|310 입출금|입출금일 기간(가안: 당월 1일~오늘), 거래처, 0023 계열/코드. 입출금일·순번·거래처코드/명·구분코드/명·금액 우선. 전표일은 보조.|PK 일자+순번, 50 미만/이상은 계열이다. 00/50은 발생금액, 32/82는 장부·실잔액 조정. 단순 현금 합계 금지.|
|320 어음|발행/만기 등 기준일 명시, 거래처·어음번호/순번. 번호·순번·거래처·발행/만기·금액 우선.|어음번호+순번 원본 1행. 분할/결제 상태 추정 금지.|
|330 담보|거래처·담보종류·어음번호·기준일. 거래처·종류·어음번호/순번·여수신구분·원본 금액 우선.|거래처+담보종류+어음번호+순번. 같은 어음의 제공·회수와 여러 거래처를 합치지 않음. 만기/회수일을 일반 입금일로 취급하지 않음.|
|260 장부잔고|처리월 범위·거래처·0023. 처리월·거래처·구분·입출금금액·공급가액·세액 우선.|거래처+월+구분. 표에 없는 기초/기말잔액 계산 안 함. 제품재고장과 다른 상태 키.|
|270 실잔고|260과 동일한 월 폼·표시 계약, 테이블/접두어·업무명만 변경.|장부와 실자료를 독립 조회. 310/130 원천과 월집계를 동시에 더하지 않음.|

기본 표시 열의 뒤에 원본 열을 보존한다. Excel/CSV는 전체 행·열, 현재표는 같은 회사의 전체 원본, 표시는 복사본. 320·330의 우선 기준일·기간과 월집계 최초 월값은 확정 전 자동 부여하지 않는다.

## 7. 변경 순서와 인수 기준 (후속 승인 시)
부분 보정은 버튼 하나를 빨리 복원하지만 plain/lazy/fragment, 게시, 날짜 탐색, float/Excel을 두 구현에서 계속 맞춰야 한다. 반면 공통 기반 재구성은 초기 영향 범위가 크므로 **기존 동작을 기본값으로 둔 좁은 옵션 → 금융 호출처 전환 → 전용 코드 제거** 순서가 적합하다.

1. 메뉴/기간/핵심 조건·컬럼 순서 및 현 단계에 허용할 분석·후속 범위 승인. 현금·잔액·어음 상태 계산은 승인 대상에 포함하지 않는다.
2. action별 상태 키 유일성 검사, 제품재고장 ↔ 장부잔고 양방향 재진입, 다른 회사·로그아웃·회수 중 요청 결과 폐기. 이 검증 없이 공통 인가를 단순화하지 않는다.
3. DDL 필드와 PK는 그대로 두고 exact SELECT·source limit·명칭 유일성·등록 기능 payload부터 연결. 5개 원본 행수/금액, 0건/상한 초과, 0023/00/50/32/82, 330 다중 매핑에서 합산 없음 검증.
4. 110 입력 경험 및 230 자료 계약에 따라 카드·다운로드 공통화. 작은 표/큰 표·만료/재로그인·표시 일부/전체 다운로드·두 권한·nullable 순번·18자리 음수·업무 순서/고정열 테스트. 기존 110/120/230·제품재고장·KPIS 카드와 나란히 비교.
5. LLM은 전체 자료에 근거한 승인된 사실만; 미지원 질문의 UI 안내와 dispatcher 결과 일치. 기준일이 두 개 이상이어도 조회 필터/분석 기준·사용한 행수 명시. 정확 합계 경로와 민감정보 제거가 확인되기 전에는 단순히 버튼만 복구하지 않는다.
6. 전환 완료 후 저장소 전체에서 finance_query_access, payment_query_display, execute_finance_select, 전용 import/메타/state key/wrappers 호출자 및 테스트 참조를 역검색. 필요한 구형 저장 메시지는 공통 만료 안내로 안전하게 읽되 사용되지 않는 호환 함수는 두지 않는다. 실제 삭제는 별도 승인 후. 기존 IO_READ/EXPORT_EXCEL·KPIS/기존 일반 NLQ·채팅 저장 정책 변경 없음 확인.

## 8. 확인 한계 및 협의 항목
정적 코드로 판정한 것은 실행 분기·기본값·날짜 탐색 우선순위·세션 키 충돌이다. 화면에 보인 특정 숫자의 런타임 dtype, 당시 분석 helper, 실제 타 업무 결과 오표시 여부는 실행 재현 없이 확정하지 않았다. 기존 테스트 통과 기록은 이번 사용자 UX의 합격 증거로 사용하지 않았다. 운영 DB·회귀 테스트는 실행하지 않았고 diff는 만들지 않았다.

먼저 협의할 사항: (1) 기존 명세/월집계에 분산 배치 또는 기존 그룹 아래 입출금 하위 그룹, (2) 310 당월 기본 및 320/330/월집계의 기준 기간, (3) 조회조건은 ERP 기본행+고급조건 또는 후보 위젯 결합안, (4) 현재 단계에서 공통 분석이 제공할 사실·후속 기능 범위. **사용자 검토 전 구현·삭제하지 않는다.**
