from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    failures: list[str] = []

    from app.services.product_master_filter_contract import (
        append_product_master_filter_clauses,
        build_product_master_enrichment_sql,
        classify_product_di_business_semantic,
    )
    from app.services import rddbc070_service, rddbc230_service
    from app.services import rddbc170_rddbc180_order_service

    joins, expressions = build_product_master_enrichment_sql(
        product_alias="P", include_audit_price=False
    )
    if (
        "P.Rd04_Physic_Di_Gcode = PD.Rd01_Gcode" not in joins
        or "P.Rd04_Physic_Di = PD.Rd01_Tcode" not in joins
        or "LTRIM(" in " ".join(
            line.strip()
            for line in joins.splitlines()
            if line.strip().startswith("ON ") or line.strip().startswith("AND ")
        )
    ):
        failures.append("product code-name lookup is not raw string equality")

    expected = {
        ("01", "보험(일반)"): "insurance",
        ("A", "보험(전문)"): "insurance",
        ("4", "의료기기(보험)"): "insurance",
        ("05", "비보험(일반)"): "non_insurance",
        ("A", "비보험(전문)"): "non_insurance",
        ("B", "의료기기"): "",
        ("A", "기타"): "",
        ("", "보험(전문)"): "",
    }
    for pair, semantic in expected.items():
        if classify_product_di_business_semantic(*pair) != semantic:
            failures.append(f"business semantic mismatch: {pair}")

    # The screenshots show identical codes with different meanings in distinct DBs.
    from app.services.product_master_filter_contract import classify_product_prescription_semantic
    valid = "8801234567890"
    for code, name, insurance, prescription in (
        ("0", "수거품목", "", ""),
        ("0", "보험(약가유연제)", "insurance", "prescription"),
        ("D", "소모품1", "", ""),
        ("D", "약가유연제", "", "prescription"),
        ("A", "건강기능식품", "", ""),
        ("A", "의료기기", "", ""),
        ("B", "비보험(전문)", "non_insurance", "prescription"),
        ("B", "불용제품", "", ""),
        ("4", "의료기기(보험)", "insurance", ""),
        ("4", "의료기기(비보험)", "non_insurance", ""),
        ("4", "보험/비보험", "", ""),
    ):
        if classify_product_di_business_semantic(code, name) != insurance:
            failures.append(f"company insurance axis mismatch: {code}/{name}")
        if classify_product_prescription_semantic(code, valid, valid, name) != prescription:
            failures.append(f"company prescription axis mismatch: {code}/{name}")

    for group, expected_sql in {
        "insurance": "LIKE N'%|보험|%'",
        "non_insurance": "LIKE N'%|비보험|%'",
    }.items():
        clauses: list[str] = []
        values: list[object] = []
        append_product_master_filter_clauses(
            clauses,
            values,
            {"product_di_semantic_group": group},
            expressions=expressions,
        )
        sql = " ".join(clauses)
        if ("PD.Rd01_Hnm" not in sql or expected_sql not in sql
                or "NOT LIKE" not in sql or values):
            failures.append(f"code-master semantic SQL mismatch: {group}: {sql}/{values}")
        if any(token in sql.upper() for token in ("CAST(", "TRY_CONVERT", "ISNUMERIC", " < ", " >= ")):
            failures.append(f"numeric semantic leaked: {group}: {sql}")

    for requested, missing in (
        ({"product_di_semantic_group": "insurance"}, "product_di_nm"),
        ({"product_prescription_semantic": "prescription"}, "product_di_gcode"),
    ):
        try:
            append_product_master_filter_clauses(
                [], [], requested,
                expressions={key: value for key, value in expressions.items() if key != missing},
            )
        except ValueError:
            pass
        else:
            failures.append(f"missing company code-master expression silently removed {requested}")

    expression_maps = (
        rddbc070_service._PRODUCT_MASTER_EXPRESSIONS,
        rddbc230_service._PRODUCT_MASTER_EXPRESSIONS,
        rddbc170_rddbc180_order_service._PRODUCT_EXPRESSIONS,
    )
    if any(mapping.get("product_di_nm") != "PD.Rd01_Hnm" for mapping in expression_maps):
        failures.append("R070/R230/R170-R180 do not share the R010 product-name authority")

    if failures:
        print("PRODUCT BUSINESS CODE SEMANTIC CONTRACT FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("PRODUCT BUSINESS CODE SEMANTIC CONTRACT PASS")
    print("- raw string code/code-name equality and leading-zero preservation PASS")
    print("- insurance/non-insurance code-master-name semantics PASS")
    print("- character code B/medical-device preserved as a valid other group PASS")
    print("- R070/R230/R170-R180 common authority PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
