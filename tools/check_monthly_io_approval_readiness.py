"""DB-free approval, inventory and transaction-boundary regression gate."""

from __future__ import annotations

import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot_service import build_frequency_snapshot_plan  # noqa: E402
from app.services.monthly_io_components import MonthlyIoFact  # noqa: E402
from app.services.saved_io_scope import resolve_saved_io_scope  # noqa: E402
from app.services.snapshot_operation_history import (  # noqa: E402
    OperationEvent, _validated_values,
)
from app.services.sql_server_monthly_io_repository import (  # noqa: E402
    MonthlyIoMaterialization, SqlServerMonthlyIoRepository,
)
from tools.approve_monthly_io_company import (  # noqa: E402
    inspect_or_approve_initial_months, inspect_or_approve_month,
)
from tools.check_company_month_end_readiness import (  # noqa: E402
    KNOWN_COMPANY_IDS, build_inventory, classify_readiness, inspect_company,
)


def _fixture():
    scope = SimpleNamespace(
        stock_codes=("00001",), product_group_codes=(), product_di_codes=(),
        product_class_codes=(), io_gu_codes=("001", "501", "601"), stock_mode="real",
    )
    plan = build_frequency_snapshot_plan(company_id=3, evaluation_month="202610",
                                         stock_codes=scope.stock_codes,
                                         io_gu_codes=scope.io_gu_codes)
    io = resolve_saved_io_scope(plan.io_gu_codes, confirmed_trade_roles=True)
    facts = (
        MonthlyIoFact("202609", "R110", "PRICE_BUCKET", "00001", "", "001",
                      Decimal("10"), "", eligible_price_row_count=1,
                      eligible_paid_quantity=Decimal(2), eligible_supply_amount=Decimal(20)),
        MonthlyIoFact("202609", "R120", "BASE", "00001", "00001", "501",
                      Decimal(-1), "", source_row_count=1, normal_event_count=1,
                      normal_event_quantity=Decimal(3)),
    )
    item = MonthlyIoMaterialization(3, "202609", plan.profile_fingerprint,
                                    io.fingerprint(plan.profile_fingerprint),
                                    ("00001",), ("00001",), facts)
    item.validate()
    return scope, item


class FakeRepository:
    def __init__(self, item):
        self.item = item
        self.manifest_id = 42
        self.generation = 2
        self.approvals = []
        self.status = "draft"

    def read_generation(self, item, generation):
        if generation != self.generation:
            raise ValueError("generation not found")
        return SimpleNamespace(manifest_id=self.manifest_id, generation_no=generation,
                               status=self.status,
                               approval_status="pending" if self.status == "draft" else "approved",
                               materialization=self.item)

    def approve_checked(self, item, generation, **kwargs):
        assert generation == self.generation and item == self.item
        self.approvals.append(kwargs)
        self.status = "published"
        return SimpleNamespace(manifest_id=self.manifest_id, generation_no=generation,
                               status="published", approval_status="approved")

    def read_current(self, item):
        assert self.approvals and item == self.item
        return SimpleNamespace(manifest_id=self.manifest_id, generation_no=self.generation,
                               materialization=self.item)


def _approval_case(*, approve=False, item=None, repository=None, scope_loader=None,
                   manifest_id=42, checksum=None, missing=("202609",)):
    scope, fixture_item = _fixture()
    item = item or fixture_item
    repo = repository or FakeRepository(item)
    calls = []

    def evidence_builder(company_id, evaluation_month, *, apply, plan_only,
                         timeout, created_by, evidence_callback=None):
        assert company_id == 3 and evaluation_month == "202610" and not apply
        calls.append("plan" if plan_only else "source")
        if not plan_only:
            evidence_callback(item)
        return {"missing_months": missing, "erp_select_count": 0 if plan_only else 4,
                "new_months": {} if plan_only else {"202609": {"checksum": item.checksum}}}

    kwargs = dict(company_id=3, evaluation_month="202610", basis_month="202609",
                  manifest_id=manifest_id, generation_no=2,
                  expected_checksum=checksum or item.checksum,
                  approved_by="operator", approval_reason="independent Exact",
                  approve=approve, repository=repo, evidence_builder=evidence_builder,
                  scope_loader=scope_loader or (lambda *, company_id: scope),
                  target_hasher=lambda company_id: "f" * 64)
    return kwargs, calls, repo


def _fails(action, expected):
    try:
        action()
    except Exception as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError("expected safety failure")


def test_approval() -> None:
    kwargs, calls, repo = _approval_case()
    checked = inspect_or_approve_month(**kwargs)
    assert checked["status"] == "inspected_draft" and calls == ["plan", "source"]
    assert checked["r110_exact"] and checked["r120_exact"] and not repo.approvals
    kwargs, calls, repo = _approval_case(approve=True)
    published = inspect_or_approve_month(**kwargs)
    assert published["status"] == "published_approved" and len(repo.approvals) == 1
    event = repo.approvals[0]["approval_event"]
    assert event.company_id == 3 and event.manifest_id == 42
    assert event.exact_status == "passed" and event.whole_checksum == published["checksum"]
    assert event.approved_by == "operator"
    assert event.details["approval_reason"] == "independent Exact"
    validated = _validated_values(event, "f" * 64)
    assert "independent Exact" in validated[-1]
    assert repo.approvals[0]["target_hash"] == "f" * 64
    _fails(lambda: inspect_or_approve_month(**kwargs), "draft identity")
    assert len(repo.approvals) == 1

    kwargs, _, repo = _approval_case(approve=True, manifest_id=99)
    _fails(lambda: inspect_or_approve_month(**kwargs), "draft identity")
    assert not repo.approvals
    kwargs, _, repo = _approval_case(approve=True)
    kwargs["generation_no"] = 3
    _fails(lambda: inspect_or_approve_month(**kwargs), "generation not found")
    assert not repo.approvals
    kwargs, _, repo = _approval_case(approve=True, checksum="0" * 64)
    _fails(lambda: inspect_or_approve_month(**kwargs), "fresh source evidence")
    assert not repo.approvals
    _, item = _fixture()
    foreign = replace(item, company_id=40)
    kwargs, _, repo = _approval_case(item=foreign, repository=FakeRepository(foreign))
    _fails(lambda: inspect_or_approve_month(**kwargs), "fresh source evidence")
    assert not repo.approvals
    kwargs, calls, repo = _approval_case(approve=True, missing=("202608", "202609"))
    _fails(lambda: inspect_or_approve_month(**kwargs), "exactly one")
    assert calls == ["plan"] and not repo.approvals
    _, item = _fixture()
    bad = replace(item, facts=(replace(item.facts[0], eligible_supply_amount=Decimal(21)), item.facts[1]))
    kwargs, _, repo = _approval_case(approve=True, item=bad, repository=FakeRepository(item))
    _fails(lambda: inspect_or_approve_month(**kwargs), "stored checksum")
    assert not repo.approvals
    scope, _ = _fixture()
    state = [scope, SimpleNamespace(**{**vars(scope), "stock_codes": ("00002",)})]
    kwargs, _, repo = _approval_case(approve=True, scope_loader=lambda *, company_id: state.pop(0))
    _fails(lambda: inspect_or_approve_month(**kwargs), "profile changed")
    assert not repo.approvals


def test_repository_transaction() -> None:
    _, item = _fixture()
    stored = SimpleNamespace(manifest_id=42, generation_no=2, status="draft",
                             approval_status="pending", source_verified_at=None,
                             materialization=item)
    event = OperationEvent(3, "monthly_build", "month_approved", "succeeded",
                           basis_month="202609", manifest_id=42, generation_no=2,
                           profile_fingerprint=item.profile_fingerprint,
                           whole_checksum=item.checksum, approved_by="operator",
                           exact_status="passed")

    class Cursor:
        rowcount = 1

        def __init__(self):
            self.statements = []

        def execute(self, statement, *params):
            self.statements.append((statement, params))
            if "OBJECT_ID" in statement:
                self.object_exists = True
            if "approval_status='approved'" in statement and "UPDATE" in statement:
                self.rowcount = getattr(self, "publish_rowcount", 1)
            return self

        def fetchone(self):
            return (1,) if getattr(self, "migration_available", True) else (None,)

    class Connection:
        def __init__(self):
            self.cursor_object = Cursor()
            self.committed = False
            self.rolled_back = False

        def cursor(self):
            return self.cursor_object

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    for insertion, expected_commit in ((True, True), (False, False)):
        connection = Connection()
        repo = SqlServerMonthlyIoRepository(lambda: None, lambda: connection)
        repo.read_generation = lambda reference, generation: stored
        with patch("app.services.snapshot_operation_history.insert_event_on_cursor", return_value=insertion):
            call = lambda: repo.approve_checked(item, 2, expected_checksum=item.checksum,
                                                 approved_by="operator", approval_reason="Exact",
                                                 exact_confirmed=True, approval_event=event,
                                                 target_hash="f" * 64)
            if expected_commit:
                assert call().status == "published"
            else:
                _fails(call, "history already exists")
        assert connection.committed is expected_commit
        assert connection.rolled_back is not expected_commit
    for setting, expected in (("migration_available", "history migration is missing"),
                              ("publish_rowcount", "lost its pending draft")):
        connection = Connection()
        setattr(connection.cursor_object, setting, False if setting == "migration_available" else 0)
        repo = SqlServerMonthlyIoRepository(lambda: None, lambda: connection)
        repo.read_generation = lambda reference, generation: stored
        call = lambda: repo.approve_checked(item, 2, expected_checksum=item.checksum,
                                             approved_by="operator", approval_reason="Exact",
                                             exact_confirmed=True, approval_event=event,
                                             target_hash="f" * 64)
        _fails(call, expected)
        assert connection.rolled_back and not connection.committed

    connection = Connection()
    repo = SqlServerMonthlyIoRepository(lambda: None, lambda: connection)
    repo.read_generation = lambda reference, generation: stored
    valid_event = replace(event, details={"approval_reason": "Exact verification"})
    approved = repo.approve_checked(item, 2, expected_checksum=item.checksum,
                                    approved_by="operator", approval_reason="Exact verification",
                                    exact_confirmed=True, approval_event=valid_event,
                                    target_hash="f" * 64)
    assert approved.status == "published" and connection.committed
    inserts = [(sql, params) for sql, params in connection.cursor_object.statements
               if sql.startswith("INSERT INTO snapshot.operation_event")]
    assert len(inserts) == 1 and "Exact verification" in inserts[0][1][-2]

    connection = Connection()
    repo = SqlServerMonthlyIoRepository(lambda: None, lambda: connection)
    repo.read_generation = lambda reference, generation: stored
    invalid_event = replace(event, details={"unapproved_key": "not allowed"})
    _fails(lambda: repo.approve_checked(
        item, 2, expected_checksum=item.checksum,
        approved_by="operator", approval_reason="Exact verification",
        exact_confirmed=True, approval_event=invalid_event, target_hash="f" * 64,
    ), "unsupported operation detail key")
    assert connection.rolled_back and not connection.committed


def test_full_initial_draft_only() -> None:
    from tools import build_monthly_io_company as builder

    scope, _ = _fixture()
    direct = pd.DataFrame({"product_code": ["00001"]})
    saves: dict[str, SimpleNamespace] = {}

    class Repository:
        def read_current(self, _item):
            raise LookupError("no approved month")

        def read_pending_exact(self, item):
            return saves.get(item.basis_month)

        def save_draft(self, item, *, created_by):
            assert created_by == "operator"
            stored = SimpleNamespace(manifest_id=len(saves) + 1, generation_no=1,
                                     materialization=item)
            saves[item.basis_month] = stored
            return stored

        def read_generation(self, item, generation):
            stored = saves[item.basis_month]
            assert generation == stored.generation_no and stored.materialization == item
            return stored

        def approve_checked(self, *_args, **_kwargs):
            raise AssertionError("full build approved a Draft")

    frames = [direct, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()]

    def source(_company_id, _sql, _binds, _timeout):
        return frames.pop(0)

    def stream(_chunks, *, projection_result, **_kwargs):
        projection_result["result"] = ([], [], [], [], [], {})
        return []

    with patch.object(builder, "resolve_dashboard_profile_stock_scope", return_value=scope), \
         patch.object(builder, "_query_decimal", side_effect=source), \
         patch.object(builder, "product_lifecycle_sql", return_value=("SELECT", {})), \
         patch.object(builder, "r110_monthly_base_sql", return_value=("SELECT", {})), \
         patch.object(builder, "r110_monthly_price_sql", return_value=("SELECT", {})), \
         patch.object(builder, "r110_historical_tail_sql", return_value=("SELECT", {})), \
         patch.object(builder, "product_statistics_raw_event_stream_sql", return_value=("SELECT", {})), \
         patch.object(builder, "_query_company_chunks", return_value=iter(())), \
         patch.object(builder, "r120_facts_from_raw_chunks", side_effect=stream), \
         patch.object(builder, "r110_facts", return_value=[]), \
         patch.object(builder, "reconstruct_purchase_prices", return_value={}), \
         patch.object(builder, "_assert_exact_prices", return_value={"price_mismatch_count": 0}), \
         patch.object(builder, "_assert_exact_r120", return_value={
             "normal_monthly_mismatch_count": 0, "return_product_mismatch_count": 0,
         }), \
         patch.object(builder, "SqlServerMonthlyIoRepository", return_value=Repository()):
        for attempt in range(2):
            frames[:] = [direct, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()]
            report = builder.run(3, "202610", apply=True, timeout=120, created_by="operator")
            assert tuple(report["monthly"]) == tuple(f"2026{month:02d}" for month in range(3, 10))
            assert len(saves) == 7 and all(
                row["status"] == "draft_pending_manual_approval"
                and row["resumed_draft"] is bool(attempt)
                for row in report["monthly"].values()
            )


def test_initial_batch_approval() -> None:
    scope, item = _fixture()
    older = replace(item, basis_month="202608", facts=tuple(
        replace(fact, basis_month="202608") for fact in item.facts
    ))
    older.validate()
    items = (older, item)
    pins = [{"basis_month": material.basis_month, "manifest_id": number,
             "generation_no": 1, "checksum": material.checksum}
            for number, material in enumerate(items, start=51)]

    class Repository:
        def __init__(self):
            self.states = {material.basis_month: "draft" for material in items}
            self.approvals = []
            self.fail_month = None

        def read_generation(self, material, generation):
            number = 51 if material.basis_month == "202608" else 52
            if generation != 1:
                raise ValueError("generation missing")
            state = self.states[material.basis_month]
            return SimpleNamespace(manifest_id=number, generation_no=1, status=state,
                                   approval_status="pending" if state == "draft" else "approved",
                                   materialization=material)

        def approve_checked(self, material, generation, **kwargs):
            if material.basis_month == self.fail_month:
                raise RuntimeError("approval transaction failed")
            assert generation == 1 and self.states[material.basis_month] == "draft"
            assert kwargs["exact_confirmed"] and kwargs["expected_checksum"] == material.checksum
            assert kwargs["approval_event"].basis_month == material.basis_month
            _validated_values(kwargs["approval_event"], kwargs["target_hash"])
            self.states[material.basis_month] = "published"
            self.approvals.append(material.basis_month)
            return SimpleNamespace(manifest_id=51 if material.basis_month == "202608" else 52,
                                   generation_no=1, status="published", approval_status="approved")

        def read_current(self, material):
            assert self.states[material.basis_month] == "published"
            return self.read_generation(material, 1)

    repo = Repository()
    calls = []

    def evidence_builder(company_id, evaluation_month, *, apply, timeout, created_by,
                         evidence_callback):
        assert (company_id, evaluation_month, apply, timeout) == (3, "202610", False, 120)
        calls.append("full_source")
        for material in items:
            evidence_callback(material)
        return {"erp_select_count": 5, "price_exact": {"price_mismatch_count": 0},
                "r120_exact": {"normal_monthly_mismatch_count": 0,
                               "return_product_mismatch_count": 0}}

    kwargs = dict(company_id=3, evaluation_month="202610", drafts=pins,
                  approved_by="operator", approval_reason="independent Exact", repository=repo,
                  evidence_builder=evidence_builder, scope_loader=lambda *, company_id: scope,
                  target_hasher=lambda company_id: "f" * 64)
    checked = inspect_or_approve_initial_months(**kwargs)
    assert [row["status"] for row in checked["months"]] == ["inspected_draft"] * 2
    assert not repo.approvals and calls == ["full_source"]
    _fails(lambda: inspect_or_approve_initial_months(
        **{**kwargs, "drafts": pins[:1], "approve": True}), "month coverage")
    _fails(lambda: inspect_or_approve_initial_months(
        **{**kwargs, "drafts": [{**pins[0], "checksum": "0" * 64}, pins[1]],
           "approve": True}), "fresh source evidence")
    _fails(lambda: inspect_or_approve_initial_months(
        **{**kwargs, "drafts": [{**pins[0], "generation_no": 2}, pins[1]],
           "approve": True}), "generation")
    _fails(lambda: inspect_or_approve_initial_months(
        **{**kwargs, "drafts": [{**pins[0], "manifest_id": 99}, pins[1]],
           "approve": True}), "stored initial Draft identity")
    _fails(lambda: inspect_or_approve_initial_months(
        **{**kwargs, "drafts": [pins[0], pins[0]], "approve": True}), "duplicate")
    assert not repo.approvals
    changed_scope = SimpleNamespace(**{**vars(scope), "stock_codes": ("00002",)})
    scopes = iter((scope, changed_scope))
    _fails(lambda: inspect_or_approve_initial_months(
        **{**kwargs, "scope_loader": lambda *, company_id: next(scopes),
           "approve": True}), "profile changed")
    assert not repo.approvals
    repo.fail_month = "202609"
    _fails(lambda: inspect_or_approve_initial_months(**kwargs, approve=True), "transaction failed")
    assert repo.approvals == ["202608"]
    repo.fail_month = None
    before = len(calls)
    approved = inspect_or_approve_initial_months(**kwargs, approve=True)
    assert len(calls) == before + 1, "one batch queried the full source more than once"
    assert [row["status"] for row in approved["months"]] == ["already_approved", "published_approved"]
    assert repo.approvals == ["202608", "202609"]
    again = inspect_or_approve_initial_months(**kwargs, approve=True)
    assert all(row["status"] == "already_approved" for row in again["months"])
    assert repo.approvals == ["202608", "202609"]


def test_readiness() -> None:
    assert classify_readiness(approved_months=()) == "C"
    assert classify_readiness(approved_months=("202609",), missing_months=("202610",),
                              reused_months=("202609",), exact_metadata_ready=True,
                              checksum_verified=True) == "A"
    assert classify_readiness(approved_months=("202609",), missing_months=("202610",),
                              reused_months=("202609",), exact_metadata_ready=True) == "B"
    assert classify_readiness(approved_months=("202609",), error="checksum mismatch") == "D"
    offline = build_inventory(live_read_only=False,
                              registry_loader=lambda **kwargs: (_ for _ in ()).throw(AssertionError("DB called")),
                              inspector=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("DB called")))
    assert len(offline["companies"]) == len(KNOWN_COMPANY_IDS)
    assert all(row["grade"] == "D" for row in offline["companies"])
    _fails(lambda: build_inventory(live_read_only=True, approved_company_ids=(3,)),
           "approval reference")
    calls = []
    live = build_inventory(
        live_read_only=True, approved_company_ids=(3,), approval_reference="user-read-only-3",
        registry_loader=lambda **kwargs: [{"company_id": 3}, {"company_id": 40}],
        inspector=lambda company_id, **kwargs: calls.append(company_id) or
                   {"company_id": company_id, "grade": "A"},
    )
    assert calls == [3] and len(live["companies"]) == 2
    assert live["companies"][1]["grade"] == "D"

    scope, item = _fixture()
    metadata = {"september_snapshot": None,
                "monthly_manifests": ({"basis_month": "202609"},),
                "retained_evaluation_months": (), "operation_history_available": True}
    class FactRepo:
        def read_price_current_for_scope(self, **kwargs):
            assert kwargs["company_id"] == 3 and kwargs["basis_month"] == "202609"
            return object()
    result = inspect_company(
        3, metadata_reader=lambda *args, **kwargs: metadata,
        planner=lambda *args, **kwargs: {"reused_months": ("202609",), "missing_months": ("202610",)},
        repository_factory=lambda *args: FactRepo(), scope_loader=lambda *, company_id: scope,
    )
    assert result["grade"] == "A" and result["stored_checksum_verified"]


def main() -> None:
    with patch("pyodbc.connect", side_effect=AssertionError("DB access in DB-free gate")):
        test_approval()
        test_repository_transaction()
        test_full_initial_draft_only()
        test_initial_batch_approval()
        test_readiness()
    print("PASS monthly IO approval/readiness DB-free gate")


if __name__ == "__main__":
    main()
