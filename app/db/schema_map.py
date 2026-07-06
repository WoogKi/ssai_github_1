# app/db/schema_map.py
from app.sims.meta.rddbc_io_meta import IO_COLS, IO_TABLES

SCHEMA = {
    'tables': {
        # 🔹 업무코드
        'rddbc010': 'dbo.Rddbc010',
        # 🔹 사용자코드
        'rddbc060': 'dbo.Rddbc060',
        # 🔹 거래처마스터
        'rddbc030': 'dbo.Rddbc030',
        # 🔹 도로명주소
        'rddbc021': 'dbo.Rddbc021',
        # 🔹 상품(제품코드)
        'rddbc040': 'dbo.Rddbc040',
        # 🔹 입출고/명세서/세금계산서/월집계
        **IO_TABLES,
    },
    'cols': {
        # 🔹 Rddbc010: 업무코드
        'rddbc010': {
            'gcode':   'Rd01_Gcode',
            'tcode':   'Rd01_Tcode',
            'hnm':     'Rd01_Hnm',
            'enm':     'Rd01_Enm',
            'snm':     'Rd01_Snm',
            'other1':  'Rd01_Other1',
            'other2':  'Rd01_Other2',
            'other3':  'Rd01_Other3',
            'colnum':  'Rd01_Col_Num',
            'modflag': 'Rd01_Mod_Flag',
            'delflag': 'Rd01_Del_Flag',
            'add_date':'Rd01_Add_Date',
            'add_cd':  'Rd01_Add_Cd',
            'mod_date':'Rd01_Mod_Date',
            'mod_cd':  'Rd01_Mod_Cd',
            'debit1':  'Rd01_Debit_Acc_Cd1',
            'debit2':  'Rd01_Debit_Acc_Cd2',
            'credit1': 'Rd01_Credit_Acc_Cd1',
            'credit2': 'Rd01_Credit_Acc_Cd2',
        },
        # 🔹 Rddbc021: 도로명주소
        'rddbc021': {
            'road_cd': 'Rd021_RoadCd',
            'dong_seq': 'Rd021_DongSeq',
            'road_nm': 'Rd021_RoadNm',
            'road_enm': 'Rd021_RoadEnm',
            'sido': 'Rd021_Sido',
            'gugun': 'Rd021_Gugun',
            'dong_gu': 'Rd021_DongGu',
            'dong_cd': 'Rd021_DongCd',
            'dong_nm': 'Rd021_DongNm',
        },
        # 🔹 Rddbc060: 사용자코드
        'rddbc060': {
            'user_cd': 'Rd06_User_Cd',
            'user_id': 'Rd06_User_ID',
            'password':'Rd06_Password',
            'sabun':   'Rd06_Sabun',
            'user_nm': 'Rd06_User_Nm',
            'sex':     'Rd06_Sex',
            'birthday':'Rd06_BirthDay',
            'jumin':   'Rd06_Jumin',
            'dept_g':  'Rd06_Department_Gcode',
            'dept':    'Rd06_Department',
            'duty_g':  'Rd06_Duty_Gcode',
            'duty':    'Rd06_Duty',
            'dist_g':  'Rd06_District_Gcode',
            'dist':    'Rd06_District',
            'phone':   'Rd06_Phone',
            'cell':    'Rd06_Cellular_Phone',
            'office':  'Rd06_Office_Phone',
            'email':   'Rd06_Email',
            'hire_date':   'Rd06_Hire_Date',
            'retire_date': 'Rd06_Retire_Date',
            'remark':  'Rd06_Remark',
            'add_date':'Rd06_Add_Date',
            'add_cd':  'Rd06_Add_Cd',
            'mod_date':'Rd06_Mod_Date',
            'mod_cd':  'Rd06_Mod_Cd',
            'modflag': 'Rd06_Mod_Flag',
            'delflag': 'Rd06_Del_Flag',
        },
        # 🔹 Rddbc030: 거래처마스터 (Ven = Vendor/거래처)
        'rddbc030': {
            # 기본키/식별
            'ven_cd':         'Rd03_Ven_Cd',
            'ven_nm':         'Rd03_Ven_Nm',
            'owner_nm':       'Rd03_Owner_Nm',
            'biz_no':         'Rd03_Ven_Num',
            'corp_reg_num':   'Rd03_CorpReg_Num',

            # 주소/연락처
            'zip':            'Rd03_Zip_Code',
            'zip_seq':        'Rd03_Zip_Seq',
            'addr1':          'Rd03_Address',
            'addr2':          'Rd03_Address2',
            'phone':          'Rd03_Phone',
            'fax':            'Rd03_Fax',
            'cell':           'Rd03_HP',
            'email':          'Rd03_EMail',

            # 그룹/분류 코드
            'ven_group_g':    'Rd03_Ven_Group_Gcode',
            'ven_group':      'Rd03_Ven_Group',
            'ven_kind_g':     'Rd03_Ven_Kind_Gcode',
            'ven_kind':       'Rd03_Ven_Kind',
            'ven_rank_g':     'Rd03_Ven_Rank_Gcode',
            'ven_rank':       'Rd03_Ven_Rank',

            # 세금/정산 관련
            'tax_ven_cd':     'Rd03_TAX_VEN_CD',
            'unify_ven_cd':   'Rd03_Unify_Ven_Cd',
            'cost_apply_cd':  'Rd03_Cost_Apply_Cd',
            'stock_apply_cd': 'Rd03_Stock_Apply_Cd',

            # 배송/거래조건 등
            'delivery_di_g':  'Rd03_Delivery_Di_Gcode',
            'delivery_di':    'Rd03_Delivery_Di',

            # 공통 메타
            'remark':         'Rd03_Remark',
            'add_date':       'Rd03_Add_Date',
            'add_cd':         'Rd03_Add_Cd',
            'mod_date':       'Rd03_Mod_Date',
            'mod_cd':         'Rd03_Mod_Cd',
            'delflag':        'Rd03_Del_Flag',
        },
        # 🔹 Rddbc040: 상품(제품코드)
        'rddbc040': {
            'physic_cd': 'Rd04_Physic_Cd',
            'insu_cd': 'Rd04_Insu_Cd',
            'physic_nm': 'Rd04_Physic_Nm',
            'physic_prt': 'Rd04_Physic_PRT',
            'physic_sm': 'Rd04_Physic_Sm',
            'ven_cd': 'Rd04_Ven_Cd',

            # 코드(대분류/상세)
            'group_g': 'Rd04_Physic_Group_Gcode',
            'group':   'Rd04_Physic_Group',
            'di_g':    'Rd04_Physic_Di_Gcode',
            'di':      'Rd04_Physic_Di',
            'flag_g':  'Rd04_Physic_Flag_Gcode',
            'flag':    'Rd04_Physic_Flag',
            'cons_g':  'Rd04_Cons_Gcode',
            'cons':    'Rd04_Cons',
            'physic_gu_g': 'Rd04_Physic_Gu_Gcode',
            'physic_gu':   'Rd04_Physic_Gu',

            # 보험/단위/가격
            'insu_date': 'Rd04_Insu_Date',
            'insu_price':'Rd04_Insu_Price',
            'unit':     'Rd04_Unit',
            'standard': 'Rd04_Standard',

            # 바코드
            'bar1': 'Rd04_Bar_Code1',
            'bar2': 'Rd04_Bar_Code2',
            'bar3': 'Rd04_Bar_Code3',
            'bar4': 'Rd04_Bar_Code4',
            'bar5': 'Rd04_Bar_Code5',

            # 플래그/등록정보
            'use_gu': 'Rd04_Use_Gu',
            'delflag':'Rd04_Del_Flag',
            'add_date':'Rd04_Add_Date',
            'add_cd':  'Rd04_Add_Cd',
            'mod_date':'Rd04_Mod_Date',
            'mod_cd':  'Rd04_Mod_Cd',
        },
        **IO_COLS,
    },
}
