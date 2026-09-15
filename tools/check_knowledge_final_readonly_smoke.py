"""Read-only corpus, retrieval and evidence reauthorization audit."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.knowledge_document_service import (
    KnowledgeDocumentRepository, KnowledgeEvidenceSnapshot,
    build_knowledge_chat_request_context, extract_text_artifact,
    _PROJECT_SOURCE_ARTIFACT_PATTERN,
)
from app.services.knowledge_role_policy import KNOWLEDGE_ROLE_PERMISSION_MATRIX
from app.services.knowledge_scope_policy import can_read_document
from tools.project_source_knowledge_cli import extract_project_symbol


class ReadOnlyRepository(KnowledgeDocumentRepository):
    def _write_manifest(self, *args, **kwargs):
        raise RuntimeError("Operating writes prohibited")

    def _write_artifact_once(self, *args, **kwargs):
        raise RuntimeError("Operating writes prohibited")


def fingerprints(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def active_identity(report):
    fields = ("source_key", "version", "scope", "company_id", "user_id",
              "knowledge_classification", "content_hash", "source_kind", "source_revision")
    return {json.dumps({f: row.get(f) for f in fields}, sort_keys=True)
            for row in report["active"]}


def audit(root, other_company):
    before = fingerprints(root)
    repo = ReadOnlyRepository(root, source_repo_root=ROOT)
    sources = repo._read_manifest()
    active = [s for s in sources if s.status == "ACTIVE" and s.approval_status == "APPROVED"]
    by_id = {s.document_id: s for s in sources}
    checks = []

    def check(name, passed, **details):
        checks.append(dict(name=name, status="PASS" if passed else "FAIL", **details))

    identities = [(s.source_key, s.scope, s.company_id, s.user_id) for s in active]
    check("active_uniqueness", len(identities) == len(set(identities)))
    artifacts = {}
    for source in sources:
        if source.content_hash not in artifacts:
            try:
                artifacts[source.content_hash] = repo._read_artifact(source.content_hash)
                check("artifact_checksum", True, content_hash=source.content_hash)
            except (ValueError, OSError) as exc:
                check("artifact_checksum", False, content_hash=source.content_hash, error=str(exc))

    admin = KNOWLEDGE_ROLE_PERMISSION_MATRIX["SYSTEM_ADMIN"]

    def context(company=7, permissions=admin, technical=True):
        return build_knowledge_chat_request_context(user_id=1, company_id=company,
            permission_codes=permissions, room_owner_user_id=1, room_company_id=company,
            technical_detail_mode=technical)

    def search(question, company=7, permissions=admin, technical=True):
        packet = repo.retrieve_for_chat(query=question,
            request_context=context(company, permissions, technical), max_chars=100000)
        keys = sorted({by_id[c.document_id].source_key for c in packet.citations})
        check("only_active_citations", all(by_id[c.document_id] in active for c in packet.citations),
              question=question)
        return packet, keys

    questions = []
    wave = [s for s in active if s.source_key.startswith("document:")]
    content_paths = {}
    for plan in (ROOT / "docs").rglob("*.knowledge.json"):
        if "90_archive" in plan.parts:
            continue
        for item in json.loads(plan.read_text(encoding="utf-8-sig")).get("items", []):
            if item.get("content_file"):
                content_paths.setdefault(item["source_key"], set()).add(
                    (plan.parent / item["content_file"]).resolve())
    for source in wave:
        matches = list(content_paths.get(source.source_key, ())) or list((ROOT / "docs").rglob(source.source_name))
        matches = [p for p in matches if "90_archive" not in p.parts]
        if len(matches) == 1:
            artifact = extract_text_artifact(source_name=source.source_name, content=matches[0].read_bytes())
            check("git_document_content", artifact.content_hash == source.content_hash,
                  source_key=source.source_key, path=matches[0].relative_to(ROOT).as_posix())
        else:
            check("git_document_content", False, source_key=source.source_key, matches=len(matches))
        question = source.search_aliases[0] if source.search_aliases else source.source_name
        _, keys = search(question)
        check("representative_search", source.source_key in keys,
              question=question, expected_source_key=source.source_key, found=keys)
        questions.append(dict(question=question, expected_source_key=source.source_key, found=keys))
        for role, permissions in KNOWLEDGE_ROLE_PERMISSION_MATRIX.items():
            for technical in (False, True):
                packet, found = search(question, permissions=permissions, technical=technical)
                allowed = can_read_document(document=source.policy_document(), current_user_id=1,
                    current_company_id=7, permission_codes=permissions, technical_detail_mode=technical).allowed
                check("role_retrieval", (source.source_key in found) == allowed,
                      source_key=source.source_key, role=role, technical=technical)
                check("no_unauthorized_citations", all(can_read_document(
                    document=by_id[c.document_id].policy_document(), current_user_id=1,
                    current_company_id=7, permission_codes=permissions,
                    technical_detail_mode=technical).allowed for c in packet.citations))

    for question, key in (
        ("R070 최종 계약단가 이력", "document:sims-master-price-query-contract"),
        ("R230 구매원가 상태", "document:sims-master-price-query-contract"),
    ):
        _, found = search(question)
        check("representative_search", key in found, question=question, expected_source_key=key, found=found)
        questions.append(dict(question=question, expected_source_key=key, found=found))

    closeout_key = "document:order-calculation-phase1-closeout-20260914"
    packet, found = search("회사7 발주 검증")
    check("company7_closeout", closeout_key in found)
    citations = tuple(c for c in packet.citations if by_id[c.document_id].source_key == closeout_key)
    snapshot = KnowledgeEvidenceSnapshot(1, 7, 1, 7, True, "", citations, ())
    for label, request, expected in (
        ("same_context", context(), True),
        ("company_switch", context(other_company), False),
        ("permission_revoked", context(permissions=("RAG_USE",)), False),
        ("rag_revoked", context(permissions=()), False),
    ):
        decision = repo.authorize_evidence_snapshot(snapshot=snapshot, request_context=request)
        check("evidence_" + label, decision.allowed == expected, reason=decision.reason_code)
        followup = repo.retrieve_for_followup(query="회사7 발주 검증", parent_snapshot=snapshot,
                                             request_context=request, max_chars=100000)
        check("followup_" + label, bool(followup.citations) == expected)
    _, found = search("회사7 발주 검증", company=other_company)
    check("other_company_closeout_denied", closeout_key not in found)
    _, found = search("발주 계산 공식 계약", company=other_company)
    check("global_order_other_company", "document:order-calculation-phase1-business-contract" in found
          and closeout_key not in found)

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    for item in questions:
        packet = repo.retrieve_for_chat(query=item["question"], request_context=context())
        found = sorted({by_id[c.document_id].source_key for c in packet.citations})
        check("default_budget_search", item["expected_source_key"] in found,
              question=item["question"], expected_source_key=item["expected_source_key"], found=found)
    for source in sources:
        if source.status == "ACTIVE" and source.approval_status == "APPROVED":
            continue
        decision = can_read_document(document=source.policy_document(), current_user_id=1,
            current_company_id=7, permission_codes=admin, technical_detail_mode=True)
        check("inactive_policy_denied", not decision.allowed, document_id=source.document_id,
              version=source.version, document_status=source.status)
    project = []
    for source in active:
        if source.source_kind != "PROJECT_SOURCE":
            continue
        relative, symbol = source.source_key.removeprefix("project-source:").rsplit("#", 1)
        current = subprocess.check_output(["git", "show", f"HEAD:{relative}"], cwd=ROOT).decode("utf-8")
        _, current_hash = extract_project_symbol(source_text=current, relative_path=relative,
                                                 symbol=symbol, revision=head)
        saved = _PROJECT_SOURCE_ARTIFACT_PATTERN.fullmatch(artifacts[source.content_hash].normalized_text)
        check("project_artifact_metadata", bool(saved) and saved["commit"] == source.source_revision
              and saved["hash"] == source.source_content_hash
              and hashlib.sha256(saved["body"].encode("utf-8")).hexdigest() == source.source_content_hash)
        _, found = search(source.search_aliases[0])
        check("stale_project_blocked_in_chat", source.source_key not in found)
        unbound = ReadOnlyRepository(root)
        raw = unbound.retrieve(query=source.search_aliases[0], current_user_id=1,
            current_company_id=7, permission_codes=admin, technical_detail_mode=True, max_chars=100000)
        project.append(dict(**asdict(source), current_head=head, current_source_hash=current_hash,
            body_hash_matches=current_hash == source.source_content_hash,
            revision_matches=source.source_revision == head,
            current_in_production=repo._project_source_is_current(source),
            unbound_repository_returns_project=any(c.source_kind == "PROJECT_SOURCE" for c in raw.citations)))

    after = fingerprints(root)
    check("operating_files_unchanged", before == after)
    return dict(status="PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL",
        repo_head=head, manifest_root=str(root), total_versions=len(sources),
        status_counts=dict(Counter(s.status for s in sources)), active_count=len(active),
        wave_document_count=len(wave), active=[asdict(s) for s in active],
        artifact_count=len(artifacts), operating_fingerprints=before,
        operating_write_count=0, checks=checks, questions=questions, project_source=project,
        other_company_id=other_company, identity_mode="offline effective-permission fixture; no DB or LLM",
        peer_verification="NOT_EXECUTED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-root", type=Path)
    parser.add_argument("--other-company-id", type=int, default=4)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path)
    args = parser.parse_args()
    if args.output and not args.output.resolve().is_relative_to(ROOT):
        parser.error("Audit output must be within repository, never operating storage")
    if args.compare:
        local, peer = [json.loads(p.read_text(encoding="utf-8")) for p in args.compare]
        left, right = active_identity(local), active_identity(peer)
        result = dict(status="PASS" if left == right else "FAIL",
            only_local=[json.loads(s) for s in sorted(left - right)],
            only_peer=[json.loads(s) for s in sorted(right - left)], operating_write_count=0)
    else:
        if not args.manifest_root or not (args.manifest_root / "manifest.json").is_file():
            parser.error("Existing manifest required")
        if args.output and args.output.resolve().is_relative_to(args.manifest_root.resolve()):
            parser.error("Output must be outside operating corpus")
        result = audit(args.manifest_root, args.other_company_id)
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("checks", "active", "operating_fingerprints")}, ensure_ascii=False, indent=2))
    if "checks" in result:
        print(json.dumps(dict(check_count=len(result["checks"]),
            failures=[c for c in result["checks"] if c["status"] == "FAIL"]), ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
