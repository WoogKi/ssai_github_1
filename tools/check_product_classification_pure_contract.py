"""Offline focused gate for the pure product-classification contract."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.product_classification_contract import (
    AnalysisDomain,
    ClassificationAuthorityType,
    ClassificationStatus,
    DISCOUNT_NAME_SIGNAL,
    ExclusionDecision,
    ProductClassification,
    ProductClassificationRule,
    candidate_signals_for_product_name,
    classify_product,
    decide_domain_exclusion,
)


def _rule(
    company_id: int,
    classification: ProductClassification,
    authority_type: ClassificationAuthorityType,
    authority_key: str,
    *,
    adjustment_only: bool = False,
    approved: bool = True,
) -> ProductClassificationRule:
    return ProductClassificationRule(
        company_id=company_id,
        classification=classification,
        authority_type=authority_type,
        authority_key=authority_key,
        mapping_version="fixture-v1",
        reason="fixture",
        approved=approved,
        adjustment_only=adjustment_only,
    )


def main() -> int:
    tests = 0

    a001 = classify_product(
        company_id=7,
        product_code="00077",
        product_name="금융할인",
        rules=[_rule(7, ProductClassification.FINANCIAL_ADJUSTMENT, ClassificationAuthorityType.PRODUCT_GROUP, "0013:A001")],
    )
    assert a001.classification is ProductClassification.FINANCIAL_ADJUSTMENT
    assert a001.authority_key == "0013:A001"
    assert a001.candidate_signals == (DISCOUNT_NAME_SIGNAL,)
    assert decide_domain_exclusion(a001, domain=AnalysisDomain.FREQUENCY).decision is ExclusionDecision.INCLUDE
    tests += 1

    mixed_override = classify_product(
        company_id=7,
        product_code="00077",
        rules=[
            _rule(7, ProductClassification.FINANCIAL_ADJUSTMENT, ClassificationAuthorityType.PRODUCT_GROUP, "0013:A001", adjustment_only=True),
            _rule(7, ProductClassification.MIXED, ClassificationAuthorityType.PRODUCT_OVERRIDE, "00077"),
        ],
    )
    assert mixed_override.classification is ProductClassification.MIXED
    assert mixed_override.authority_type is ClassificationAuthorityType.PRODUCT_OVERRIDE
    assert not mixed_override.adjustment_only
    tests += 1

    company4 = classify_product(company_id=4, product_code="9998", product_name="반품정리조정보정")
    assert company4.classification is ProductClassification.UNKNOWN
    assert company4.status is ClassificationStatus.UNMAPPED
    assert not company4.candidate_signals
    assert decide_domain_exclusion(company4, domain=AnalysisDomain.ORDERING).decision is ExclusionDecision.INCLUDE
    tests += 1

    discount_only = classify_product(company_id=7, product_code="00001", product_name="금융 할인")
    assert discount_only.classification is ProductClassification.UNKNOWN
    assert discount_only.candidate_signals == (DISCOUNT_NAME_SIGNAL,)
    assert decide_domain_exclusion(discount_only, domain=AnalysisDomain.INVENTORY).decision is ExclusionDecision.INCLUDE
    tests += 1

    for name in ("반품", "정리", "조정", "보정", "반품 정리 조정 보정"):
        assert candidate_signals_for_product_name(name) == ()
    tests += 1

    approved_adjustment = classify_product(
        company_id=7,
        product_code="00002",
        rules=[_rule(7, ProductClassification.FINANCIAL_ADJUSTMENT, ClassificationAuthorityType.PRODUCT_GROUP, "0013:ADJ", adjustment_only=True)],
    )
    assert decide_domain_exclusion(approved_adjustment, domain=AnalysisDomain.ACCOUNTING).decision is ExclusionDecision.SEGREGATE
    for domain in (AnalysisDomain.FREQUENCY, AnalysisDomain.INVENTORY, AnalysisDomain.SHORTAGE, AnalysisDomain.ORDERING):
        assert decide_domain_exclusion(approved_adjustment, domain=domain).decision is ExclusionDecision.EXCLUDE
    assert decide_domain_exclusion(approved_adjustment, domain=AnalysisDomain.PROFITABILITY).decision is ExclusionDecision.EXCLUDE
    tests += 1

    assert decide_domain_exclusion(mixed_override, domain=AnalysisDomain.ACCOUNTING).decision is ExclusionDecision.SEGREGATE
    assert decide_domain_exclusion(mixed_override, domain=AnalysisDomain.SHORTAGE).decision is ExclusionDecision.INCLUDE
    assert decide_domain_exclusion(mixed_override, domain=AnalysisDomain.PROFITABILITY).decision is ExclusionDecision.SEGREGATE
    tests += 1

    company_scope = classify_product(
        company_id=7,
        product_code="00003",
        rules=[
            _rule(7, ProductClassification.FINANCIAL_ADJUSTMENT, ClassificationAuthorityType.PRODUCT_GROUP, "0013:A001"),
            _rule(7, ProductClassification.COMMERCIAL, ClassificationAuthorityType.COMPANY_SCOPE, "approved-commercial-scope"),
        ],
    )
    assert company_scope.classification is ProductClassification.COMMERCIAL
    assert company_scope.authority_type is ClassificationAuthorityType.COMPANY_SCOPE
    tests += 1

    isolated = classify_product(
        company_id=4,
        product_code="00004",
        rules=[_rule(7, ProductClassification.FINANCIAL_ADJUSTMENT, ClassificationAuthorityType.PRODUCT_GROUP, "0013:A001", adjustment_only=True)],
    )
    assert isolated.status is ClassificationStatus.UNMAPPED
    assert isolated.classification is ProductClassification.UNKNOWN
    assert decide_domain_exclusion(isolated, domain=AnalysisDomain.ORDERING).decision is ExclusionDecision.INCLUDE
    tests += 1

    conflict = classify_product(
        company_id=7,
        product_code="00005",
        rules=[
            _rule(7, ProductClassification.COMMERCIAL, ClassificationAuthorityType.PRODUCT_ATTRIBUTE, "0004:01"),
            _rule(7, ProductClassification.MANAGEMENT_ONLY, ClassificationAuthorityType.PRODUCT_ATTRIBUTE, "0031:09", adjustment_only=True),
        ],
    )
    assert conflict.status is ClassificationStatus.CONFLICT
    assert conflict.classification is ProductClassification.UNKNOWN
    assert decide_domain_exclusion(conflict, domain=AnalysisDomain.INVENTORY).decision is ExclusionDecision.INCLUDE
    tests += 1

    unavailable = classify_product(
        company_id=7,
        product_code="00006",
        authority_status=ClassificationStatus.UNAVAILABLE,
        rules=[_rule(7, ProductClassification.MANAGEMENT_ONLY, ClassificationAuthorityType.PRODUCT_GROUP, "0013:MGT", adjustment_only=True)],
    )
    assert unavailable.classification is ProductClassification.UNKNOWN
    assert decide_domain_exclusion(unavailable, domain=AnalysisDomain.ORDERING).decision is ExclusionDecision.INCLUDE
    tests += 1

    leading_zero = classify_product(
        company_id=7,
        product_code=" 00007 ",
        rules=[_rule(7, ProductClassification.MIXED, ClassificationAuthorityType.PRODUCT_OVERRIDE, "00007")],
    )
    assert leading_zero.product_code == "00007"
    assert leading_zero.authority_key == "00007"
    tests += 1

    print(f"PASS product classification pure contract: tests={tests} db_calls=0 runtime_integrations=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
