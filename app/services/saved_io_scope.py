"""Opt-in interpretation of the company's saved Dashboard I/O codes.

The confirmed full policy considers every saved 0xx code for inbound and price
calculation; positive paid quantity and supply amount remain SQL eligibility
checks. The legacy shadow policy retains its narrower candidate distinction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, MutableMapping

from app.services.ssai_analysis_profile_service import normalize_business_code


POLICY_VERSION = "saved_io_shadow_v1"
FULL_POLICY_VERSION = "saved_io_full_v1"
_CONFIRMED_INBOUND = frozenset(("001", "002"))
_PDA_CANDIDATE = "008"


@dataclass(frozen=True)
class SavedIoScope:
    all_codes: tuple[str, ...]
    inbound_codes: tuple[str, ...]
    inbound_return_codes: tuple[str, ...]
    outbound_codes: tuple[str, ...]
    outbound_return_codes: tuple[str, ...]
    unclassified_codes: tuple[str, ...]
    confirmed_inbound_codes: tuple[str, ...]
    normal_inbound_codes: tuple[str, ...]
    price_codes: tuple[str, ...]
    price_candidate_codes: tuple[str, ...]
    unresolved_inbound_codes: tuple[str, ...]
    policy_version: str = POLICY_VERSION

    @property
    def unresolved_role_codes(self) -> tuple[str, ...]:
        return tuple(sorted((*self.price_candidate_codes, *self.unresolved_inbound_codes, *self.unclassified_codes)))

    def fingerprint(self, legacy_profile_fingerprint: str) -> str:
        body = {
            "legacy_profile_fingerprint": legacy_profile_fingerprint,
            "io_gu_list": self.all_codes,
            "policy_version": self.policy_version,
            "normal_inbound_codes": self.normal_inbound_codes,
            "price_codes": self.price_codes,
        }
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolve_saved_io_scope(
    io_gu_list: Iterable[Any], *, include_pda_candidate: bool = False,
    confirmed_trade_roles: bool = False,
) -> SavedIoScope:
    """Classify every selected 0012 Tcode; never infer a code by numeric coercion."""
    selected: set[str] = set()
    for raw in io_gu_list:
        code = normalize_business_code(raw)
        if ":" in code:
            group, separator, code = code.partition(":")
            if not separator or group != "0012":
                raise ValueError("saved I/O code must use group 0012")
        if len(code) != 3 or not code.isascii() or not code.isdigit():
            raise ValueError("saved I/O code is not a three-digit transaction code")
        selected.add(code)
    if not selected:
        raise ValueError("saved I/O scope is empty")
    all_codes = tuple(sorted(selected))
    inbound = tuple(code for code in all_codes if code[0] == "0")
    if confirmed_trade_roles:
        confirmed = inbound
        normal_inbound = inbound
        price_codes = inbound
        candidate = ()
        policy_version = FULL_POLICY_VERSION
    else:
        confirmed = tuple(code for code in inbound if code in _CONFIRMED_INBOUND)
        candidate = tuple(code for code in inbound if code == _PDA_CANDIDATE)
        normal_inbound = tuple(sorted((*confirmed, *(candidate if include_pda_candidate else ()))))
        price_codes = normal_inbound
        policy_version = POLICY_VERSION
    return SavedIoScope(
        all_codes=all_codes,
        inbound_codes=inbound,
        inbound_return_codes=tuple(code for code in all_codes if code[0] == "1"),
        outbound_codes=tuple(code for code in all_codes if code[0] == "5"),
        outbound_return_codes=tuple(code for code in all_codes if code[0] == "6"),
        unclassified_codes=tuple(code for code in all_codes if code[0] not in "0156"),
        confirmed_inbound_codes=confirmed,
        normal_inbound_codes=normal_inbound,
        price_codes=price_codes,
        price_candidate_codes=candidate,
        unresolved_inbound_codes=tuple(
            code for code in inbound
            if not confirmed_trade_roles and code not in _CONFIRMED_INBOUND and code != _PDA_CANDIDATE
        ),
        policy_version=policy_version,
    )


def bind_io_codes(
    column: str, codes: Iterable[str], binds: MutableMapping[str, Any], *, prefix: str, style: str = "named"
) -> str:
    """Return an exact, parameterized IN predicate; empty roles fail closed."""
    values = tuple(sorted(set(codes)))
    if not values:
        return "1 = 0"
    if style not in {"named", "pyformat"}:
        raise ValueError("unsupported SQL bind style")
    placeholders: list[str] = []
    for index, code in enumerate(values):
        key = f"{prefix}_{index}"
        binds[key] = code
        placeholders.append(f":{key}" if style == "named" else f"%({key})s")
    return f"{column} IN ({', '.join(placeholders)})"
