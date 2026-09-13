"""Company-scoped product-classification authority in the shared SSAI DB.

The repository owns approved mapping provenance only. Dashboard analysis
profiles remain query-scope configuration and are deliberately not read here.
No runtime consumer imports this module yet; classification remains shadow-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable

from app.services.product_classification_contract import (
    ClassificationAuthorityType,
    ClassificationStatus,
    ProductClassification,
    ProductClassificationResult,
    ProductClassificationRule,
    classify_product,
    normalize_business_code,
)
from app.services.ssai_auth_service import connect_ssai_db


MAPPING_TABLE = "SSAI_PRODUCT_CLASSIFICATION_MAPPINGS"
MAPPING_POLICY_VERSION = "product_classification_mapping_v1"

TARGET_PRODUCT_OVERRIDE = "product_override"
TARGET_PRODUCT_GROUP = "product_group"
TARGET_PRODUCT_DI = "product_di"
TARGET_PRODUCT_CLASS = "product_class"
TARGET_PRODUCT_FLAG = "product_flag"
SUPPORTED_TARGET_TYPES = frozenset(
    {
        TARGET_PRODUCT_OVERRIDE,
        TARGET_PRODUCT_GROUP,
        TARGET_PRODUCT_DI,
        TARGET_PRODUCT_CLASS,
        TARGET_PRODUCT_FLAG,
    }
)
SUPPORTED_MAPPING_STATUSES = frozenset({"draft", "approved", "rejected", "retired"})


ConnectionFactory = Callable[[], Any]


@dataclass(frozen=True)
class ProductClassificationTargets:
    product_group_keys: tuple[str, ...] = ()
    product_di_keys: tuple[str, ...] = ()
    product_class_keys: tuple[str, ...] = ()
    product_flag_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductClassificationMapping:
    mapping_id: int
    company_id: int
    target_type: str
    target_gcode: str
    target_code: str
    classification: ProductClassification
    adjustment_only: bool
    reason_code: str
    status: str
    effective_from: date
    effective_to: date | None
    mapping_version: str
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str
    approved_by: str
    approved_at: str


@dataclass(frozen=True)
class ProductClassificationAuthorityRead:
    status: ClassificationStatus
    rules: tuple[ProductClassificationRule, ...] = ()
    mappings: tuple[ProductClassificationMapping, ...] = ()
    reason_code: str = ""
    mapping_versions: tuple[str, ...] = ()
    policy_version: str = MAPPING_POLICY_VERSION
    authority: str = "ssai_common_product_classification"


@dataclass(frozen=True)
class ProductClassificationShadowRead:
    authority: ProductClassificationAuthorityRead
    result: ProductClassificationResult


def _valid_company_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _normalize_key(value: object) -> str:
    return normalize_business_code(value)


def _normalize_key_set(values: Iterable[object]) -> frozenset[str]:
    return frozenset(value for item in values if (value := _normalize_key(item)))


def _authority_type(target_type: str) -> ClassificationAuthorityType:
    if target_type == TARGET_PRODUCT_OVERRIDE:
        return ClassificationAuthorityType.PRODUCT_OVERRIDE
    if target_type == TARGET_PRODUCT_GROUP:
        return ClassificationAuthorityType.PRODUCT_GROUP
    return ClassificationAuthorityType.PRODUCT_ATTRIBUTE


def _authority_key(mapping: ProductClassificationMapping) -> str:
    if mapping.target_type == TARGET_PRODUCT_OVERRIDE:
        return mapping.target_code
    return f"{mapping.target_gcode}:{mapping.target_code}"


def _mapping_matches(
    mapping: ProductClassificationMapping,
    *,
    product_code: str,
    targets: ProductClassificationTargets,
) -> bool:
    key = _authority_key(mapping)
    if mapping.target_type == TARGET_PRODUCT_OVERRIDE:
        return mapping.target_code == product_code
    expected = {
        TARGET_PRODUCT_GROUP: _normalize_key_set(targets.product_group_keys),
        TARGET_PRODUCT_DI: _normalize_key_set(targets.product_di_keys),
        TARGET_PRODUCT_CLASS: _normalize_key_set(targets.product_class_keys),
        TARGET_PRODUCT_FLAG: _normalize_key_set(targets.product_flag_keys),
    }
    return key in expected.get(mapping.target_type, frozenset())


def _to_rule(mapping: ProductClassificationMapping) -> ProductClassificationRule:
    return ProductClassificationRule(
        company_id=mapping.company_id,
        classification=mapping.classification,
        authority_type=_authority_type(mapping.target_type),
        authority_key=_authority_key(mapping),
        mapping_version=mapping.mapping_version,
        reason=mapping.reason_code,
        approved=mapping.status == "approved",
        adjustment_only=mapping.adjustment_only,
    )


def _as_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    return date.fromisoformat(text) if text else None


def _mapping_from_row(row: Any) -> ProductClassificationMapping:
    values = tuple(row)
    return ProductClassificationMapping(
        mapping_id=int(values[0]),
        company_id=int(values[1]),
        target_type=str(values[2] or "").strip(),
        target_gcode=str(values[3] or "").strip(),
        target_code=str(values[4] or "").strip(),
        classification=ProductClassification(str(values[5] or "").strip()),
        adjustment_only=bool(values[6]),
        reason_code=str(values[7] or "").strip(),
        status=str(values[8] or "").strip(),
        effective_from=_as_date(values[9]) or date.min,
        effective_to=_as_date(values[10]),
        mapping_version=str(values[11] or "").strip(),
        created_by=str(values[12] or "").strip(),
        created_at=str(values[13] or "").strip(),
        updated_by=str(values[14] or "").strip(),
        updated_at=str(values[15] or "").strip(),
        approved_by=str(values[16] or "").strip(),
        approved_at=str(values[17] or "").strip(),
    )


_SELECT_COLUMNS = """
mapping_id, company_id, target_type, target_gcode, target_code,
classification, adjustment_only, reason_code, status,
effective_from, effective_to, mapping_version,
created_by, created_at, updated_by, updated_at, approved_by, approved_at
""".strip()


def load_effective_classification_authority(
    *,
    company_id: int,
    product_code: object,
    targets: ProductClassificationTargets = ProductClassificationTargets(),
    as_of: date,
    connection_factory: ConnectionFactory = connect_ssai_db,
) -> ProductClassificationAuthorityRead:
    """Read approved/effective company mappings and return matched pure rules."""
    code = _normalize_key(product_code)
    if not _valid_company_id(company_id) or not code or not isinstance(as_of, date):
        return ProductClassificationAuthorityRead(
            status=ClassificationStatus.CORRUPT,
            reason_code="invalid_authority_request",
        )
    try:
        with connection_factory() as conn:
            rows = conn.cursor().execute(
                f"""SELECT {_SELECT_COLUMNS}
                    FROM dbo.{MAPPING_TABLE}
                    WHERE company_id = ?
                      AND status = 'approved'
                      AND effective_from <= ?
                      AND (effective_to IS NULL OR effective_to >= ?)
                    ORDER BY mapping_id""",
                company_id,
                as_of,
                as_of,
            ).fetchall()
    except Exception as exc:
        return ProductClassificationAuthorityRead(
            status=ClassificationStatus.UNAVAILABLE,
            reason_code=f"classification_db_unavailable:{type(exc).__name__}",
        )
    try:
        mappings = tuple(_mapping_from_row(row) for row in rows)
    except (TypeError, ValueError, IndexError):
        return ProductClassificationAuthorityRead(
            status=ClassificationStatus.CORRUPT,
            reason_code="invalid_approved_mapping_row",
        )

    invalid = tuple(
        mapping
        for mapping in mappings
        if mapping.company_id != company_id
        or mapping.target_type not in SUPPORTED_TARGET_TYPES
        or not mapping.target_code
        or not mapping.mapping_version
        or mapping.status != "approved"
        or (mapping.target_type == TARGET_PRODUCT_OVERRIDE and mapping.target_gcode)
        or (mapping.target_type != TARGET_PRODUCT_OVERRIDE and not mapping.target_gcode)
    )
    if invalid:
        return ProductClassificationAuthorityRead(
            status=ClassificationStatus.CORRUPT,
            mappings=mappings,
            reason_code="invalid_approved_mapping_row",
        )

    matched = tuple(
        mapping
        for mapping in mappings
        if _mapping_matches(mapping, product_code=code, targets=targets)
    )
    if not matched:
        return ProductClassificationAuthorityRead(
            status=ClassificationStatus.UNMAPPED,
            reason_code="classification_unmapped",
        )
    versions = tuple(dict.fromkeys(mapping.mapping_version for mapping in matched))
    return ProductClassificationAuthorityRead(
        status=ClassificationStatus.READY,
        rules=tuple(_to_rule(mapping) for mapping in matched),
        mappings=matched,
        mapping_versions=versions,
        reason_code="approved_effective_mapping",
    )


def load_effective_company_classification_authority(
    *, company_id: int, as_of: date, connection_factory: ConnectionFactory = connect_ssai_db
) -> ProductClassificationAuthorityRead:
    """Load one company's approved authority once for batch Snapshot classification."""
    if not _valid_company_id(company_id) or not isinstance(as_of, date):
        return ProductClassificationAuthorityRead(status=ClassificationStatus.CORRUPT, reason_code="invalid_authority_request")
    try:
        with connection_factory() as conn:
            rows = conn.cursor().execute(
                f"""SELECT {_SELECT_COLUMNS} FROM dbo.{MAPPING_TABLE}
                    WHERE company_id=? AND status='approved' AND effective_from<=?
                      AND (effective_to IS NULL OR effective_to>=?) ORDER BY mapping_id""",
                company_id, as_of, as_of,
            ).fetchall()
        mappings = tuple(_mapping_from_row(row) for row in rows)
    except Exception as exc:
        return ProductClassificationAuthorityRead(
            status=ClassificationStatus.UNAVAILABLE,
            reason_code=f"classification_db_unavailable:{type(exc).__name__}",
        )
    if any(
        mapping.company_id != company_id or mapping.target_type not in SUPPORTED_TARGET_TYPES
        or not mapping.target_code or not mapping.mapping_version or mapping.status != "approved"
        or (mapping.target_type == TARGET_PRODUCT_OVERRIDE and mapping.target_gcode)
        or (mapping.target_type != TARGET_PRODUCT_OVERRIDE and not mapping.target_gcode)
        for mapping in mappings
    ):
        return ProductClassificationAuthorityRead(
            status=ClassificationStatus.CORRUPT, mappings=mappings, reason_code="invalid_approved_mapping_row"
        )
    return ProductClassificationAuthorityRead(
        status=ClassificationStatus.READY,
        mappings=mappings,
        mapping_versions=tuple(dict.fromkeys(mapping.mapping_version for mapping in mappings)),
        reason_code="approved_effective_company_authority",
    )


def classify_product_from_loaded_authority(
    *, authority: ProductClassificationAuthorityRead, company_id: int, product_code: object,
    product_name: object = "", targets: ProductClassificationTargets = ProductClassificationTargets(),
) -> ProductClassificationResult:
    """Resolve one product from a previously loaded company authority."""
    code = _normalize_key(product_code)
    matched = tuple(
        mapping for mapping in authority.mappings
        if _mapping_matches(mapping, product_code=code, targets=targets)
    )
    authority_status = authority.status
    if authority_status == ClassificationStatus.READY and not matched:
        authority_status = ClassificationStatus.UNMAPPED
    return classify_product(
        company_id=company_id,
        product_code=code,
        product_name=product_name,
        rules=tuple(_to_rule(mapping) for mapping in matched),
        authority_status=authority_status,
    )


def shadow_classify_product(
    *,
    company_id: int,
    product_code: object,
    product_name: object = "",
    targets: ProductClassificationTargets = ProductClassificationTargets(),
    as_of: date,
    connection_factory: ConnectionFactory = connect_ssai_db,
) -> ProductClassificationShadowRead:
    """Resolve classification provenance without applying any domain filter."""
    authority = load_effective_classification_authority(
        company_id=company_id,
        product_code=product_code,
        targets=targets,
        as_of=as_of,
        connection_factory=connection_factory,
    )
    result = classify_product(
        company_id=company_id,
        product_code=product_code,
        product_name=product_name,
        rules=authority.rules,
        authority_status=authority.status,
    )
    return ProductClassificationShadowRead(authority=authority, result=result)


def create_draft_mapping(
    *,
    company_id: int,
    target_type: str,
    target_gcode: object,
    target_code: object,
    classification: ProductClassification,
    adjustment_only: bool,
    reason_code: str,
    effective_from: date,
    effective_to: date | None,
    mapping_version: str,
    actor: str,
    connection_factory: ConnectionFactory = connect_ssai_db,
) -> int:
    """Create one draft. This API never auto-approves a mapping."""
    gcode = _normalize_key(target_gcode)
    code = _normalize_key(target_code)
    version = _normalize_key(mapping_version)
    actor_value = _normalize_key(actor)
    reason = _normalize_key(reason_code)
    if (
        not _valid_company_id(company_id)
        or target_type not in SUPPORTED_TARGET_TYPES
        or not code
        or not version
        or not actor_value
        or not isinstance(effective_from, date)
        or (effective_to is not None and effective_to < effective_from)
        or (target_type == TARGET_PRODUCT_OVERRIDE and gcode)
        or (target_type != TARGET_PRODUCT_OVERRIDE and not gcode)
        or (adjustment_only and classification not in {ProductClassification.FINANCIAL_ADJUSTMENT, ProductClassification.MANAGEMENT_ONLY})
    ):
        raise ValueError("invalid_product_classification_mapping")
    with connection_factory() as conn:
        try:
            cur = conn.cursor()
            row = cur.execute(
                f"""INSERT INTO dbo.{MAPPING_TABLE}
                    (company_id, target_type, target_gcode, target_code, classification,
                     adjustment_only, reason_code, status, effective_from, effective_to,
                     mapping_version, created_by, updated_by)
                    OUTPUT INSERTED.mapping_id
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)""",
                company_id,
                target_type,
                gcode,
                code,
                classification.value,
                int(bool(adjustment_only)),
                reason,
                effective_from,
                effective_to,
                version,
                actor_value,
                actor_value,
            ).fetchone()
            if not row:
                raise RuntimeError("classification_draft_insert_failed")
            conn.commit()
            return int(row[0])
        except Exception:
            conn.rollback()
            raise


def update_draft_mapping(
    *,
    mapping_id: int,
    classification: ProductClassification,
    adjustment_only: bool,
    reason_code: str,
    effective_from: date,
    effective_to: date | None,
    mapping_version: str,
    actor: str,
    connection_factory: ConnectionFactory = connect_ssai_db,
) -> bool:
    """Update mutable fields only while a row is still draft."""
    if (
        not isinstance(mapping_id, int)
        or isinstance(mapping_id, bool)
        or mapping_id <= 0
        or not _normalize_key(mapping_version)
        or not _normalize_key(actor)
        or (effective_to is not None and effective_to < effective_from)
        or (adjustment_only and classification not in {ProductClassification.FINANCIAL_ADJUSTMENT, ProductClassification.MANAGEMENT_ONLY})
    ):
        raise ValueError("invalid_product_classification_mapping_update")
    with connection_factory() as conn:
        try:
            cur = conn.cursor()
            cur.execute(
                f"""UPDATE dbo.{MAPPING_TABLE}
                    SET classification = ?, adjustment_only = ?, reason_code = ?,
                        effective_from = ?, effective_to = ?, mapping_version = ?,
                        updated_by = ?, updated_at = SYSUTCDATETIME()
                    WHERE mapping_id = ? AND status = 'draft'""",
                classification.value,
                int(bool(adjustment_only)),
                _normalize_key(reason_code),
                effective_from,
                effective_to,
                _normalize_key(mapping_version),
                _normalize_key(actor),
                mapping_id,
            )
            changed = int(cur.rowcount or 0) == 1
            if not changed:
                raise RuntimeError("classification_draft_not_mutable")
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def transition_mapping_status(
    *,
    mapping_id: int,
    target_status: str,
    actor: str,
    connection_factory: ConnectionFactory = connect_ssai_db,
) -> bool:
    """Apply the fixed draft approval lifecycle without hidden auto-approval."""
    transitions = {
        "approved": "draft",
        "rejected": "draft",
        "retired": "approved",
    }
    if target_status not in transitions or not _normalize_key(actor):
        raise ValueError("invalid_classification_status_transition")
    approved_sql = ", approved_by = ?, approved_at = SYSUTCDATETIME()" if target_status == "approved" else ""
    params: list[object] = [target_status, _normalize_key(actor)]
    if target_status == "approved":
        params.append(_normalize_key(actor))
    params.extend([mapping_id, transitions[target_status]])
    with connection_factory() as conn:
        try:
            cur = conn.cursor()
            cur.execute(
                f"""UPDATE dbo.{MAPPING_TABLE}
                    SET status = ?, updated_by = ?, updated_at = SYSUTCDATETIME(){approved_sql}
                    WHERE mapping_id = ? AND status = ?""",
                *params,
            )
            if int(cur.rowcount or 0) != 1:
                raise RuntimeError("classification_status_transition_rejected")
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def delete_draft_mapping(
    *,
    mapping_id: int,
    connection_factory: ConnectionFactory = connect_ssai_db,
) -> bool:
    """Delete drafts only; approved history must be retired, never deleted."""
    with connection_factory() as conn:
        try:
            cur = conn.cursor()
            cur.execute(
                f"DELETE FROM dbo.{MAPPING_TABLE} WHERE mapping_id = ? AND status = 'draft'",
                mapping_id,
            )
            if int(cur.rowcount or 0) != 1:
                raise RuntimeError("classification_draft_delete_rejected")
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
