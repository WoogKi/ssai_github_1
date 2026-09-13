"""Pure product-classification and domain exclusion contract.

This module has no database, Streamlit, ERP, or Snapshot dependency. Callers
must resolve matched, approved authorities before passing them here. The
contract never turns a missing or conflicting authority into an exclusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


POLICY_VERSION = "product_exclusion_policy_v1"
DISCOUNT_NAME_SIGNAL = "product_name_discount_candidate"


class ProductClassification(StrEnum):
    COMMERCIAL = "commercial"
    FINANCIAL_ADJUSTMENT = "financial_adjustment"
    MANAGEMENT_ONLY = "management_only"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class ClassificationStatus(StrEnum):
    READY = "ready"
    UNMAPPED = "unmapped"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"


class ClassificationAuthorityType(StrEnum):
    PRODUCT_OVERRIDE = "product_override"
    COMPANY_SCOPE = "company_scope"
    PRODUCT_GROUP = "product_group"
    PRODUCT_ATTRIBUTE = "product_attribute"
    NONE = "none"


class AnalysisDomain(StrEnum):
    ACCOUNTING = "accounting"
    FREQUENCY = "frequency"
    INVENTORY = "inventory"
    SHORTAGE = "shortage"
    ORDERING = "ordering"
    PROFITABILITY = "profitability"


class ExclusionDecision(StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"
    SEGREGATE = "segregate"


_AUTHORITY_PRIORITY = {
    ClassificationAuthorityType.PRODUCT_OVERRIDE: 0,
    ClassificationAuthorityType.COMPANY_SCOPE: 1,
    ClassificationAuthorityType.PRODUCT_GROUP: 2,
    ClassificationAuthorityType.PRODUCT_ATTRIBUTE: 3,
}

_OPERATING_DOMAINS = {
    AnalysisDomain.FREQUENCY,
    AnalysisDomain.INVENTORY,
    AnalysisDomain.SHORTAGE,
    AnalysisDomain.ORDERING,
}


@dataclass(frozen=True)
class ProductClassificationRule:
    """One already-matched authority candidate supplied by an upstream reader."""

    company_id: int
    classification: ProductClassification
    authority_type: ClassificationAuthorityType
    authority_key: str
    mapping_version: str = ""
    reason: str = ""
    approved: bool = False
    adjustment_only: bool = False


@dataclass(frozen=True)
class ProductClassificationResult:
    company_id: int
    product_code: str
    classification: ProductClassification
    status: ClassificationStatus
    authority_type: ClassificationAuthorityType
    authority_key: str
    mapping_version: str
    policy_version: str
    reason: str
    candidate_signals: tuple[str, ...] = ()
    adjustment_only: bool = False


@dataclass(frozen=True)
class DomainExclusionDecision:
    company_id: int
    product_code: str
    domain: AnalysisDomain
    decision: ExclusionDecision
    classification: ProductClassification
    status: ClassificationStatus
    policy_version: str
    authority_type: ClassificationAuthorityType
    authority_key: str
    reason: str


def normalize_business_code(value: object) -> str:
    """Preserve ERP codes as trimmed strings without numeric coercion."""
    return value.strip() if isinstance(value, str) else ""


def candidate_signals_for_product_name(product_name: object) -> tuple[str, ...]:
    """Return non-authoritative name signals.

    Only ``할인`` is a candidate signal. ``반품/정리/조정/보정`` never produce
    an exclusion signal under the approved business contract.
    """
    name = product_name.strip() if isinstance(product_name, str) else ""
    return (DISCOUNT_NAME_SIGNAL,) if "할인" in name else ()


def _unknown_result(
    *,
    company_id: int,
    product_code: str,
    status: ClassificationStatus,
    reason: str,
    policy_version: str,
    candidate_signals: tuple[str, ...],
) -> ProductClassificationResult:
    return ProductClassificationResult(
        company_id=company_id,
        product_code=product_code,
        classification=ProductClassification.UNKNOWN,
        status=status,
        authority_type=ClassificationAuthorityType.NONE,
        authority_key="",
        mapping_version="",
        policy_version=policy_version,
        reason=reason,
        candidate_signals=candidate_signals,
    )


def _valid_rule(rule: ProductClassificationRule) -> bool:
    if rule.authority_type not in _AUTHORITY_PRIORITY:
        return False
    if not normalize_business_code(rule.authority_key):
        return False
    if rule.adjustment_only and rule.classification not in {
        ProductClassification.FINANCIAL_ADJUSTMENT,
        ProductClassification.MANAGEMENT_ONLY,
    }:
        return False
    return isinstance(rule.company_id, int) and not isinstance(rule.company_id, bool) and rule.company_id > 0


def classify_product(
    *,
    company_id: int,
    product_code: object,
    product_name: object = "",
    rules: Iterable[ProductClassificationRule] = (),
    authority_status: ClassificationStatus = ClassificationStatus.READY,
    policy_version: str = POLICY_VERSION,
) -> ProductClassificationResult:
    """Resolve one product using approved matched candidates and fixed precedence."""
    code = normalize_business_code(product_code)
    signals = candidate_signals_for_product_name(product_name)
    version = normalize_business_code(policy_version) or POLICY_VERSION
    valid_company = isinstance(company_id, int) and not isinstance(company_id, bool) and company_id > 0
    if not valid_company or not code:
        return _unknown_result(
            company_id=company_id if valid_company else 0,
            product_code=code,
            status=ClassificationStatus.CORRUPT,
            reason="invalid_product_identity",
            policy_version=version,
            candidate_signals=signals,
        )
    if authority_status is not ClassificationStatus.READY:
        safe_status = authority_status if authority_status in {
            ClassificationStatus.UNMAPPED,
            ClassificationStatus.UNAVAILABLE,
            ClassificationStatus.CONFLICT,
            ClassificationStatus.CORRUPT,
        } else ClassificationStatus.CORRUPT
        return _unknown_result(
            company_id=company_id,
            product_code=code,
            status=safe_status,
            reason=f"classification_authority_{safe_status.value}",
            policy_version=version,
            candidate_signals=signals,
        )

    company_rules = [rule for rule in rules if rule.company_id == company_id]
    malformed = [rule for rule in company_rules if not _valid_rule(rule)]
    if malformed:
        return _unknown_result(
            company_id=company_id,
            product_code=code,
            status=ClassificationStatus.CORRUPT,
            reason="invalid_classification_rule",
            policy_version=version,
            candidate_signals=signals,
        )

    matched: list[ProductClassificationRule] = []
    for rule in company_rules:
        if not rule.approved:
            continue
        if (
            rule.authority_type is ClassificationAuthorityType.PRODUCT_OVERRIDE
            and normalize_business_code(rule.authority_key) != code
        ):
            continue
        matched.append(rule)
    if not matched:
        reason = "no_approved_mapping" if company_rules else "classification_unmapped"
        return _unknown_result(
            company_id=company_id,
            product_code=code,
            status=ClassificationStatus.UNMAPPED,
            reason=reason,
            policy_version=version,
            candidate_signals=signals,
        )

    priority = min(_AUTHORITY_PRIORITY[rule.authority_type] for rule in matched)
    selected = [rule for rule in matched if _AUTHORITY_PRIORITY[rule.authority_type] == priority]
    semantic_values = {(rule.classification, bool(rule.adjustment_only)) for rule in selected}
    if len(semantic_values) != 1:
        return _unknown_result(
            company_id=company_id,
            product_code=code,
            status=ClassificationStatus.CONFLICT,
            reason="same_priority_mapping_conflict",
            policy_version=version,
            candidate_signals=signals,
        )

    selected = sorted(
        selected,
        key=lambda rule: (
            normalize_business_code(rule.authority_key),
            normalize_business_code(rule.mapping_version),
            normalize_business_code(rule.reason),
        ),
    )
    winner = selected[0]
    authority_keys = tuple(dict.fromkeys(normalize_business_code(rule.authority_key) for rule in selected))
    mapping_versions = tuple(dict.fromkeys(normalize_business_code(rule.mapping_version) for rule in selected if normalize_business_code(rule.mapping_version)))
    reasons = tuple(dict.fromkeys(normalize_business_code(rule.reason) for rule in selected if normalize_business_code(rule.reason)))
    return ProductClassificationResult(
        company_id=company_id,
        product_code=code,
        classification=winner.classification,
        status=ClassificationStatus.READY,
        authority_type=winner.authority_type,
        authority_key=",".join(authority_keys),
        mapping_version=",".join(mapping_versions),
        policy_version=version,
        reason=";".join(reasons) or "approved_mapping",
        candidate_signals=signals,
        adjustment_only=bool(winner.adjustment_only),
    )


def decide_domain_exclusion(
    classification: ProductClassificationResult,
    *,
    domain: AnalysisDomain,
) -> DomainExclusionDecision:
    """Apply one domain policy without broadening an uncertain authority."""
    decision = ExclusionDecision.INCLUDE
    reason = "include_commercial"

    if classification.status is not ClassificationStatus.READY or classification.classification is ProductClassification.UNKNOWN:
        reason = f"include_fail_closed_{classification.status.value}"
    elif domain is AnalysisDomain.ACCOUNTING:
        if classification.classification in {
            ProductClassification.FINANCIAL_ADJUSTMENT,
            ProductClassification.MANAGEMENT_ONLY,
            ProductClassification.MIXED,
        }:
            decision = ExclusionDecision.SEGREGATE
            reason = "segregate_accounting_reconciliation"
    elif domain in _OPERATING_DOMAINS:
        if classification.adjustment_only and classification.classification in {
            ProductClassification.FINANCIAL_ADJUSTMENT,
            ProductClassification.MANAGEMENT_ONLY,
        }:
            decision = ExclusionDecision.EXCLUDE
            reason = "exclude_approved_adjustment_only"
        elif classification.classification is ProductClassification.MIXED:
            reason = "include_mixed_operating_product"
        else:
            reason = "include_non_adjustment_only"
    elif domain is AnalysisDomain.PROFITABILITY:
        if classification.adjustment_only and classification.classification in {
            ProductClassification.FINANCIAL_ADJUSTMENT,
            ProductClassification.MANAGEMENT_ONLY,
        }:
            decision = ExclusionDecision.EXCLUDE
            reason = "exclude_adjustment_only_from_unit_economics"
        elif classification.classification in {
            ProductClassification.FINANCIAL_ADJUSTMENT,
            ProductClassification.MANAGEMENT_ONLY,
            ProductClassification.MIXED,
        }:
            decision = ExclusionDecision.SEGREGATE
            reason = "segregate_profitability_components"

    return DomainExclusionDecision(
        company_id=classification.company_id,
        product_code=classification.product_code,
        domain=domain,
        decision=decision,
        classification=classification.classification,
        status=classification.status,
        policy_version=classification.policy_version,
        authority_type=classification.authority_type,
        authority_key=classification.authority_key,
        reason=reason,
    )
