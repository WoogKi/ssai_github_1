"""Source columns from the supplied ERP DDL; no runtime schema probing."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PaymentField:
    column: str
    label: str
    kind: str
    size: int = 0


FIELDS_310 = (
    PaymentField("Rd31_Receive_YyMmDd", "입출금일자", "char", 8),
    PaymentField("Rd31_Receive_Seq", "입출금순번", "smallint", 0),
    PaymentField("Rd31_Receive_Di_Gcode", "입출금구분 대분류코드", "char", 4),
    PaymentField("Rd31_Receive_Di", "입출금구분코드", "char", 6),
    PaymentField("Rd31_Ven_Cd", "거래처코드", "char", 5),
    PaymentField("Rd31_Out_Ven_Cd", "매출참조거래처코드", "char", 5),
    PaymentField("Rd31_Physic_Group_Gcode", "제품그룹 대분류코드", "char", 4),
    PaymentField("Rd31_Physic_Group", "제품그룹코드", "char", 6),
    PaymentField("Rd31_Receive_Amt", "금액", "decimal", 18),
    PaymentField("Rd31_Remark", "비고", "varchar", 60),
    PaymentField("Rd31_Note_No", "어음번호", "char", 30),
    PaymentField("Rd31_Note_No_Seq", "어음번호순번", "smallint", 0),
    PaymentField("Rd31_Permit_No", "허가번호", "char", 15),
    PaymentField("Rd31_Permit_Seq", "허가번호순번", "smallint", 0),
    PaymentField("Rd31_Account_Flag", "입출금등록구분", "char", 1),
    PaymentField("Rd31_Slip_YyMmDd", "전표일자", "char", 8),
    PaymentField("Rd31_Slip_No", "전표번호", "smallint", 0),
    PaymentField("Rd31_Slip_Seq", "전표번호순번", "smallint", 0),
    PaymentField("Rd31_Fixed_Flag", "마감Flag", "char", 1),
    PaymentField("Rd31_Record_Flag", "통장발행Flag", "char", 1),
    PaymentField("Rd31_Reform_Flag", "세무관리Flag", "char", 1),
    PaymentField("Rd31_Add_Date", "등록일자", "char", 8),
    PaymentField("Rd31_Add_Cd", "등록자코드", "char", 5),
    PaymentField("Rd31_Mod_Date", "수정일자", "char", 8),
    PaymentField("Rd31_Mod_Cd", "수정자코드", "char", 5),
    PaymentField("Rd31_Real_Sales_Man", "원본 담당영업사원코드", "char", 5),
    PaymentField("Rd31_Trans_Ven_Cd", "외상매출대체거레처코드", "char", 5),
    PaymentField("Rd31_Bank_Cd_Gcode", "은행 대분류코드", "char", 4),
    PaymentField("Rd31_Bank_Cd", "은행코드", "char", 6),
    PaymentField("Rd31_Add_Time", "등록일시", "datetime", 0),
    PaymentField("Rd31_Mod_Time", "수정일시", "datetime", 0),
    PaymentField("Rd31_Move_Seq", "더존전송 회수", "smallint", 0),
    PaymentField("Rd31_Move_Date", "더존전송일", "datetime", 0),
    PaymentField("Rd31_IN_SQ", "더존전송 처리번호", "smallint", 0),
    PaymentField("Rd31_Auxiliary_Book_GCode", "보조장항목 대분류코드", "char", 4),
    PaymentField("Rd31_Auxiliary_Book", "보조장항목코드", "char", 2),
    PaymentField("Rd31_IFType_Flag", "외부인터페이스 Flag", "char", 1),
    PaymentField("Rd31_PartitionENDS_Seq", "분할어음 타수처리 SEQ", "smallint", 0),
)

FIELDS_320 = (
    PaymentField("Rd32_Note_No", "어음번호", "char", 30),
    PaymentField("Rd32_Note_No_Seq", "어음번호순번", "smallint", 0),
    PaymentField("Rd32_Note_Gu_Gcode", "어음번호분류대분류", "char", 4),
    PaymentField("Rd32_Note_Gu", "어음번호분류", "char", 6),
    PaymentField("Rd32_Receive_Di_Gcode", "입출금구분 대분류", "char", 4),
    PaymentField("Rd32_Receive_Di", "입출금구분", "char", 6),
    PaymentField("Rd32_Note_Di_Gcode", "어음구분 대분류", "char", 4),
    PaymentField("Rd32_Note_Di", "어음구분", "char", 6),
    PaymentField("Rd32_Ven_Cd", "거래처코드", "char", 5),
    PaymentField("Rd32_Bank_Cd_Gcode", "은행코드 대분류", "char", 4),
    PaymentField("Rd32_Bank_Cd", "은행코드", "char", 6),
    PaymentField("Rd32_Draft_YyMmDd", "발행년월일", "char", 8),
    PaymentField("Rd32_Maturit_YyMmDd", "만기년월일", "char", 8),
    PaymentField("Rd32_Note_Amt", "금액", "decimal", 18),
    PaymentField("Rd32_Flotation", "발행처", "varchar", 15),
    PaymentField("Rd32_Remark", "비고", "varchar", 60),
    PaymentField("Rd32_Acount_Flag", "입출금등록구분", "char", 1),
    PaymentField("Rd32_Slip_YyMmDd", "전표일자", "char", 8),
    PaymentField("Rd32_Slip_No", "전표번호", "smallint", 0),
    PaymentField("Rd32_Slip_Seq", "전표번호순번", "smallint", 0),
    PaymentField("Rd32_Del_Flag", "삭제구분", "char", 1),
    PaymentField("Rd32_Add_Date", "등록일자", "char", 8),
    PaymentField("Rd32_Add_Cd", "등록자코드", "char", 5),
    PaymentField("Rd32_Mod_Date", "수정일자", "char", 8),
    PaymentField("Rd32_Mod_Cd", "수정자코드", "char", 5),
    PaymentField("Rd32_PartitionENDS_Degree", "Rd32_PartitionENDS_Degree", "smallint", 0),
    PaymentField("Rd32_PartitionENDS_CumuAmt", "Rd32_PartitionENDS_CumuAmt", "decimal", 18),
    PaymentField("Rd32_PartitionENDS_Maturit_Flag", "Rd32_PartitionENDS_Maturit_Flag", "char", 1),
    PaymentField("Rd32_PartitionENDS_DCAmt", "Rd32_PartitionENDS_DCAmt", "decimal", 18),
)

FIELDS_330 = (
    PaymentField("Rd33_Ven_Cd", "거래처코드", "char", 5),
    PaymentField("Rd33_Mortgage_Kind", "담보종류코드", "char", 6),
    PaymentField("Rd33_Note_No", "어음번호", "char", 30),
    PaymentField("Rd33_Note_Seq", "어음번호순번", "smallint", 0),
    PaymentField("Rd33_Mortgage_Kind_Gcode", "담보종류 대분류코드", "char", 4),
    PaymentField("Rd33_Note_Di_Gcode", "어음구분 대분류코드", "char", 4),
    PaymentField("Rd33_Note_Di", "어음구분코드", "char", 6),
    PaymentField("Rd33_Bank_Cd_Gcode", "은행코드 대분류코드", "char", 4),
    PaymentField("Rd33_Bank_Cd", "은행코드", "char", 6),
    PaymentField("Rd33_Draft_YyMmDd", "발행년월일", "char", 8),
    PaymentField("Rd33_Maturit_YyMmDd", "만기년월일", "char", 8),
    PaymentField("Rd33_Note_Amt", "금액", "decimal", 18),
    PaymentField("Rd33_Remark", "비고", "varchar", 250),
    PaymentField("Rd33_Flotation", "발행처", "varchar", 100),
    PaymentField("Rd33_Del_Flag", "삭제구분", "char", 1),
    PaymentField("Rd33_Add_Date", "등록일", "char", 8),
    PaymentField("Rd33_Add_Cd", "등록자코드", "char", 5),
    PaymentField("Rd33_Mod_Date", "수정일", "char", 8),
    PaymentField("Rd33_Mod_Cd", "수정자코드", "char", 5),
    PaymentField("Rd33_Offer_Di", "여수신구분", "char", 1),
    PaymentField("Rd33_Collect_YyMmDd", "Rd33_Collect_YyMmDd", "char", 8),
    PaymentField("Rd33_Collect_Remark ", "Rd33_Collect_Remark ", "varchar", 60),
)

FIELDS_260 = (
    PaymentField("Rd26_Ven_Cd", "거래처코드", "char", 5),
    PaymentField("Rd26_Io_YyMm", "처리년월", "char", 6),
    PaymentField("Rd26_Receive_Di", "입출금구분", "char", 6),
    PaymentField("Rd26_Receive_Di_Gcode", "입출금구분 대분류", "char", 4),
    PaymentField("Rd26_Receive_Amt", "입출금금액", "decimal", 18),
    PaymentField("Rd26_Supply_Price", "공급가액", "decimal", 18),
    PaymentField("Rd26_Tax_Price", "세액", "decimal", 18),
)

FIELDS_270 = tuple(PaymentField(f.column.replace("Rd26_", "Rd27_"), f.label, f.kind, f.size) for f in FIELDS_260)

FIELDS = {"310": FIELDS_310, "320": FIELDS_320, "330": FIELDS_330, "260": FIELDS_260, "270": FIELDS_270}
PRIMARY_KEYS = {
    "310": ("Rd31_Receive_YyMmDd", "Rd31_Receive_Seq"),
    "320": ("Rd32_Note_No", "Rd32_Note_No_Seq"),
    "330": ("Rd33_Ven_Cd", "Rd33_Mortgage_Kind", "Rd33_Note_No", "Rd33_Note_Seq"),
    "260": ("Rd26_Ven_Cd", "Rd26_Io_YyMm", "Rd26_Receive_Di"),
    "270": ("Rd27_Ven_Cd", "Rd27_Io_YyMm", "Rd27_Receive_Di"),
}

FILTER_COLUMNS = {
    "310": {
        "vendor_cd": "Ven_Cd",
        "note_no": "Note_No",
        "note_seq": "Note_No_Seq",
        "amount": "Receive_Amt",
        "bank": "Bank_Cd",
        "registered_by": "Add_Cd",
        "modified_by": "Mod_Cd",
        "remark": "Remark",
        "flow_code": "Receive_Di",
        "reference_vendor_cd": "Out_Ven_Cd",
        "sales_user_cd": "Real_Sales_Man",
        "transfer_vendor_cd": "Trans_Ven_Cd",
        "slip_no": "Slip_No",
        "slip_seq": "Slip_Seq",
        "permit_no": "Permit_No",
        "permit_seq": "Permit_Seq",
    },
    "320": {
        "vendor_cd": "Ven_Cd",
        "note_no": "Note_No",
        "note_seq": "Note_No_Seq",
        "amount": "Note_Amt",
        "bank": "Bank_Cd",
        "registered_by": "Add_Cd",
        "modified_by": "Mod_Cd",
        "remark": "Remark",
        "note_type": "Note_Di",
        "note_class": "Note_Gu",
        "slip_no": "Slip_No",
        "slip_seq": "Slip_Seq",
        "flow_code": "Receive_Di",
        "issuer": "Flotation",
        "deleted": "Del_Flag",
    },
    "330": {
        "vendor_cd": "Ven_Cd",
        "note_no": "Note_No",
        "note_seq": "Note_Seq",
        "amount": "Note_Amt",
        "bank": "Bank_Cd",
        "registered_by": "Add_Cd",
        "modified_by": "Mod_Cd",
        "remark": "Remark",
        "collateral_kind": "Mortgage_Kind",
        "offer_kind": "Offer_Di",
        "note_type": "Note_Di",
        "issuer": "Flotation",
        "deleted": "Del_Flag",
    },
}


def detail_spec(table, category_label, *, direction_sql=None):
    filters = FILTER_COLUMNS[table]
    fields = FIELDS[table]
    prefix = "Rd" + table[:2] + "_"
    lookup = {f.column[len(prefix):]: f for f in fields}
    joins, extra = [], []
    specs, labels, sources = {}, {}, {}
    name_ids = {
        "Ven_Cd": "vendor_name", "Out_Ven_Cd": "reference_vendor_name",
        "Trans_Ven_Cd": "transfer_vendor_name", "Real_Sales_Man": "sales_user_name",
        "Add_Cd": "add_user_nm", "Mod_Cd": "mod_user_nm", "Bank_Cd": "bank_name",
        "Physic_Group": "product_group_name", "Receive_Di": "flow_name",
        "Note_Di": "note_type_name", "Note_Gu": "note_class_name",
        "Mortgage_Kind": "collateral_kind_name",
    }
    for suffix, field in lookup.items():
        alias = "J" + str(len(joins))
        if suffix in {"Ven_Cd", "Out_Ven_Cd", "Trans_Ven_Cd"}:
            source, key, name = "Rddbc030", "Rd03_Ven_Cd", "Rd03_Ven_Nm"
        elif suffix in {"Add_Cd", "Mod_Cd", "Real_Sales_Man"}:
            source, key, name = "Rddbc060", "Rd06_User_Cd", "Rd06_User_Nm"
        else:
            group = next((f for s, f in lookup.items() if s.lower() == suffix.lower()+"_gcode"), None)
            if not group:
                continue
            source, key, name = "Rddbc010", "Rd01_Tcode", "Rd01_Hnm"
        join = f"LEFT JOIN dbo.{source} {alias} ON {alias}.{key}=S.[{field.column}]"
        if source == "Rddbc010":
            join += f" AND {alias}.Rd01_Gcode=S.[{group.column}]"
        joins.append(join)
        if table == "310" and suffix == "Ven_Cd":
            from app.services.rddbc030_service import COL060_USERNM, COL_SALES_MAN, vendor_staff_key_match
            joins.append("LEFT JOIN dbo.Rddbc060 OwnerStaff ON " + vendor_staff_key_match(alias, "OwnerStaff"))
            extra.extend((f"{alias}.{COL_SALES_MAN} AS [거래처 담당자코드]",
                          f"OwnerStaff.{COL060_USERNM} AS [거래처 담당자명]"))
            specs["vendor_staff_name"] = (f"OwnerStaff.{COL060_USERNM}", "name", 100)
            sources["vendor_staff_name"] = (COL060_USERNM, "text")
            labels["vendor_staff_name"] = "거래처 담당자명"
        label = field.label.removesuffix("코드") + "명"
        extra.append(f"{alias}.{name} AS [{label}]")
        if suffix in name_ids:
            filter_id = name_ids[suffix]
            specs[filter_id] = (f"{alias}.{name}", "name", 100)
            sources[filter_id] = (name, "text")
            labels[filter_id] = label.replace("거레처", "거래처")
    for key, suffix in filters.items():
        f = lookup[suffix]
        kind = {"decimal": "money", "smallint": "integer", "varchar": "text"}.get(f.kind, "code")
        specs[key] = (f"S.[{f.column}]", kind, f.size)
        labels[key] = f.label
        sources[key] = (f.column, f.kind)
    for stem, column, kind, size in (
        ("add_date", "Add_Date", "date", 8), ("mod_date", "Mod_Date", "date", 8),
        ("amount", filters["amount"], "money", 18),
    ):
        field = lookup[column]
        for end, word in (("from", "시작"), ("to", "종료")):
            key = f"{stem}_{end}"
            specs[key] = (f"S.[{field.column}]", kind, size)
            labels[key] = field.label + " " + word
            sources[key] = (field.column, field.kind)
    dates = DATE_ROLES[table]
    # 0023 is a character code in the ERP ledger, including card codes.
    direction_boundary = "50"
    if direction_sql:
        extra.append(f"CASE WHEN {direction_sql} < '{direction_boundary}' THEN N'입금' "
                     f"WHEN {direction_sql} >= '{direction_boundary}' THEN N'출금' END AS [입금/출금 구분]")
    return dict(table="rddbc"+table, title=TITLES[table], fields=fields, pk=PRIMARY_KEYS[table],
                filters=specs, filter_labels=labels, filter_sources=sources, dates=dates,
                date_columns={s: prefix+s for s in dates},
                leading=LEADING[table], direction_sql=direction_sql, direction_boundary=direction_boundary,
                joins=joins, extra_select=extra,
                group_aliases={"입출금": "입금/출금 구분", "입출금부분명": "입출금구분명", "담당영업사원명": "원본 담당영업사원명",
                               "담당자": "거래처 담당자명", "담당자명": "거래처 담당자명",
                               "거래처담당자": "거래처 담당자명"} if table == "310" else {},
                distinct_terms={"입출고구분": "입출금구분명"} if table == "310" else {},
                category_label=category_label, amount_label="금액")

TITLES = {"310": "입출금 조회", "320": "어음 조회", "330": "담보 조회"}
CODE_GROUPS = {"Receive_Di": "0023", "Bank_Cd": "0020", "Note_Gu": "0014", "Note_Di": "0016", "Mortgage_Kind": "0017", "Physic_Group": "0013"}
TABLES = {"rddbc" + t: "dbo.Rddbc" + t for t in FIELDS}
COLS = {"rddbc" + t: {f.column.split("_", 1)[1]: f.column for f in fields} for t, fields in FIELDS.items()}
LABELS = {"rddbc" + t: {f.column: f.label for f in fields} for t, fields in FIELDS.items()}
DATE_ROLES = {"310": ("Receive_YyMmDd", "Slip_YyMmDd", "Add_Date", "Mod_Date"),
              "320": ("Add_Date", "Draft_YyMmDd", "Maturit_YyMmDd", "Mod_Date"),
              "330": ("Add_Date", "Draft_YyMmDd", "Maturit_YyMmDd", "Mod_Date")}
LEADING = {
    "310": ("입출금일자", "거래처명", "거래처 담당자명", "입금/출금 구분", "입출금구분명", "금액", "입출금순번", "거래처코드", "입출금구분코드", "원본 담당영업사원명", "매출참조거래처명", "어음번호", "어음번호순번", "전표일자", "전표번호", "비고"),
    "320": ("거래처명", "어음번호", "어음번호순번", "어음구분명", "금액", "발행년월일", "만기년월일", "은행명", "발행처", "입출금구분명", "거래처코드", "비고"),
    "330": ("거래처명", "담보종류명", "어음번호", "어음번호순번", "여수신구분", "금액", "발행년월일", "만기년월일", "Rd33_Collect_YyMmDd", "은행명", "거래처코드", "담보종류코드", "비고"),
}
