"""Company-scoped external API profiles. Provisioning is an explicit operator action.

No ERP reads, migrations, default grants or implicit vault creation on import/UI.
The dedicated Fernet key must not be the ERP database-password encryption key.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

from cryptography.fernet import Fernet

from app.services.kpis_mcp_contract import FIRST_PRIORITY, Session

VIEW = "EXTERNAL_API_VIEW"
MANAGE = "EXTERNAL_API_MANAGE"
SECRET = "EXTERNAL_API_SECRET_MANAGE"
GRANT = "EXTERNAL_API_GRANT_MANAGE"
AUDIT = "EXTERNAL_API_AUDIT_READ"
PERMISSIONS = frozenset({VIEW, MANAGE, SECRET, GRANT, AUDIT})
log = logging.getLogger(__name__)
# User-confirmed 2026-11-23 codes, cross-checked against SOAP/REST guide IDs.
# Names, not ordinal REST numbers: integrated guide page 4. MA105 is slated
# for removal per ERP evidence and is not enabled in the first-priority client.
ERP_KEY_CODES = MappingProxyType({"MA101": "000001", "MA103": "000002", "MA104": "000003",
                 "MA105": "000004", "MA106": "000005", "MA102": "000006",
                 "MA110": "000007", "MA107": "000008", "MA109": "000009", "MA117": "000010"})


class ApiManagementError(ValueError):
    """Only static reason codes, never provider exception text."""


def future(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value > datetime.now(timezone.utc)


class ApiStore:
    def __init__(self, path: Path, key: bytes):
        self.path = Path(path)
        self.cipher = Fernet(key)

    def provision(self):
        # Exclusive create; never update an existing database or infer a path.
        with self.path.open("xb"):
            pass
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE profiles(company INTEGER, service TEXT, mode TEXT, sealed BLOB,
                    PRIMARY KEY(company, service, mode));
                CREATE TABLE grants(company INTEGER, service TEXT, mode TEXT, user TEXT,
                    expires TEXT, PRIMARY KEY(company, service, mode, user));
                CREATE TABLE events(id INTEGER PRIMARY KEY, at TEXT, company INTEGER,
                    service TEXT, mode TEXT, actor TEXT, action TEXT, result TEXT, calls INTEGER);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def db(self):
        if not self.path.is_file() or self.path.is_symlink():
            raise ApiManagementError("store_not_provisioned")
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ApiManagementError("store_version")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def scope(company, service, mode):
        if type(company) is not int or company <= 0 or service not in FIRST_PRIORITY or mode not in {"TEST", "PRODUCTION"}:
            raise ApiManagementError("invalid_scope")

    @staticmethod
    def authorize(actor, company, permission):
        if (not isinstance(actor, Session) or actor.company_id != company
                or not future(actor.expires_at) or permission not in actor.permissions):
            raise ApiManagementError("permission_denied")

    @staticmethod
    def event(db, company, service, mode, actor, action, result="OK", calls=0):
        db.execute("INSERT INTO events(at,company,service,mode,actor,action,result,calls) VALUES(?,?,?,?,?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(), company, service, mode,
                    actor.user_id, action, result, calls))

    def _profile(self, db, company, service, mode):
        row = db.execute("SELECT sealed FROM profiles WHERE company=? AND service=? AND mode=?",
                         (company, service, mode)).fetchone()
        if not row:
            raise ApiManagementError("profile_missing")
        try:
            value = json.loads(self.cipher.decrypt(row[0]))
            if value["scope"] != [company, service, mode]:
                raise ValueError()
            return value
        except Exception:
            raise ApiManagementError("profile_integrity") from None

    def _save(self, db, company, service, mode, profile):
        sealed = self.cipher.encrypt(json.dumps(profile, ensure_ascii=False).encode("utf-8"))
        db.execute("INSERT OR REPLACE INTO profiles VALUES(?,?,?,?)", (company, service, mode, sealed))

    def configure(self, actor, company, service, mode, *, applicant, enabled, expires, evidence):
        self.scope(company, service, mode)
        self.authorize(actor, company, MANAGE)
        if (not isinstance(applicant, str) or not applicant.isascii() or not applicant.isdigit()
                or len(applicant) not in {8, 10} or type(enabled) is not bool
                or not future(expires) or not isinstance(evidence, str) or not 1 <= len(evidence) <= 120):
            raise ApiManagementError("invalid_profile")
        with self.db() as db:
            exists = db.execute("SELECT 1 FROM profiles WHERE company=? AND service=? AND mode=?",
                                (company, service, mode)).fetchone()
            old = self._profile(db, company, service, mode) if exists else {}
            # A changed applicant must not silently inherit another institution's credentials.
            if old and old["applicant"] != applicant and old.get("credentials"):
                raise ApiManagementError("revoke_credentials_before_applicant_change")
            self._save(db, company, service, mode, dict(scope=[company, service, mode],
                       applicant=applicant, enabled=enabled, expires=expires.isoformat(),
                       evidence=evidence, credentials=old.get("credentials", {})))
            self.event(db, company, service, mode, actor, "CONFIGURE")

    def set_credentials(self, actor, company, service, mode, credentials):
        self.scope(company, service, mode)
        self.authorize(actor, company, SECRET)
        required = {"client_id", "client_secret"} if service == "MA116" else {"apiKey"}
        if (not isinstance(credentials, dict) or (credentials and set(credentials) != required)
                or any(not isinstance(v, str) or not 1 <= len(v) <= 4096 or any(ord(c) < 32 for c in v)
                       for v in credentials.values())):
            raise ApiManagementError("invalid_credentials")
        with self.db() as db:
            profile = self._profile(db, company, service, mode)
            profile["credentials"] = dict(credentials)
            profile["enabled"] = False
            self._save(db, company, service, mode, profile)
            self.event(db, company, service, mode, actor, "ROTATE" if credentials else "REVOKE")

    def grant(self, actor, company, service, mode, user, expires=None):
        self.scope(company, service, mode)
        self.authorize(actor, company, GRANT)
        if not isinstance(user, str) or not user.isascii() or not user.isdigit() or int(user) <= 0:
            raise ApiManagementError("invalid_user")
        if expires is not None and not future(expires):
            raise ApiManagementError("invalid_expiry")
        with self.db() as db:
            if expires is None:
                db.execute("DELETE FROM grants WHERE company=? AND service=? AND mode=? AND user=?",
                           (company, service, mode, user))
            else:
                db.execute("INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?)",
                           (company, service, mode, user, expires.isoformat()))
            self.event(db, company, service, mode, actor, "GRANT" if expires else "UNGRANT")

    def set_erp_reference(self, actor, company, service, mode):
        self.scope(company, service, mode)
        self.authorize(actor, company, SECRET)
        # ERP credentials must never be sent to the plaintext TEST endpoint.
        if mode != "PRODUCTION" or service not in ERP_KEY_CODES:
            raise ApiManagementError("erp_reference_not_supported")
        with self.db() as db:
            profile = self._profile(db, company, service, mode)
            if profile.get("credentials"):
                raise ApiManagementError("revoke_credentials_before_source_change")
            profile["credentials"] = {"source": "ERP_RDDBC0A4", "tcode": ERP_KEY_CODES[service]}
            profile["enabled"] = False
            self._save(db, company, service, mode, profile)
            self.event(db, company, service, mode, actor, "ERP_REFERENCE")

    def view(self, actor, company):
        self.authorize(actor, company, VIEW)
        with self.db() as db:
            result = []
            for service, mode in db.execute("SELECT service,mode FROM profiles WHERE company=?", (company,)).fetchall():
                p = self._profile(db, company, service, mode)
                result.append(dict(service=service, mode=mode, enabled=p["enabled"], expires=p["expires"],
                                   credential_registered=bool(p["credentials"]),
                                   credential_source=p["credentials"].get("source", "LOCAL_ENCRYPTED")))
            return result

    def history(self, actor, company):
        self.authorize(actor, company, AUDIT)
        with self.db() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(
                "SELECT at,service,mode,action,result,calls FROM events WHERE company=? ORDER BY id DESC LIMIT 100",
                (company,))]

    def grants_view(self, actor, company):
        self.authorize(actor, company, GRANT)
        with self.db() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(
                "SELECT service,mode,user,expires FROM grants WHERE company=? ORDER BY service,mode,user LIMIT 1000",
                (company,))]

    def execution_profile(self, actor, request):
        # Internal server boundary: never return this object to a UI, model or logger.
        self.scope(request.company_id, request.service, request.mode)
        self.authorize(actor, request.company_id, "MCP_EXTERNAL_RESOURCE_READ")
        if (actor.user_id, actor.epoch) != (request.user_id, request.session_epoch):
            raise ApiManagementError("stale_session")
        with self.db() as db:
            grant = db.execute("SELECT expires FROM grants WHERE company=? AND service=? AND mode=? AND user=?",
                               (request.company_id, request.service, request.mode, actor.user_id)).fetchone()
            if not grant or not future(datetime.fromisoformat(grant[0])):
                raise ApiManagementError("service_mode_denied")
            return self._profile(db, request.company_id, request.service, request.mode)

    def record_event(self, company, service, mode, actor, action, result="OK", calls=0):
        with self.db() as db:
            self.event(db, company, service, mode, actor, action, result, calls)


def configured_store():
    import os
    raw, key_file = os.environ.get("SSAI_EXTERNAL_API_STORE", ""), os.environ.get("SSAI_EXTERNAL_API_KEY_FILE", "")
    if not raw or not key_file or not Path(raw).is_absolute() or not Path(key_file).is_absolute():
        raise ApiManagementError("store_not_configured")
    path = Path(key_file)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128:
        raise ApiManagementError("key_file_invalid")
    return ApiStore(Path(raw), path.read_bytes().strip())


def load_current_api_session():
    """Server-owned current company and fresh central permissions; no admin bypass."""
    from datetime import timedelta
    from app.ui import ssai_login as login
    from app.services.external_api_session import marker, bind_verified, invalidate
    from app.services.ssai_auth_service import (
        connect_ssai_db, get_user_by_login_id, get_user_permissions,
        make_auth_user, get_active_companies_for_user,
    )
    state = login.st.session_state
    generation = marker(state)
    stage = "session_identity"

    def deny(reason, error_type="none"):
        # Never log identities, DB exception messages, company rows or credentials.
        log.warning("[external_api.session] denied stage=%s reason=%s error_type=%s",
                    stage, reason, error_type)
        if marker(state) == generation:
            invalidate(state)
        return None

    def identity():
        user, company = login.get_current_user(), login.get_selected_company()
        if not user or not company or state.get(login.SESSION_COMPANY_PICK_MODE):
            return None
        return (user.user_id, user.login_id, int(company["company_id"]))

    try:
        initial = identity()
        if initial is None:
            return deny("login_or_company_missing")
        uid, login_id, cid = initial
        stage = "central_connection"
        with closing(connect_ssai_db()) as db:
            stage = "central_user"
            current = get_user_by_login_id(db, login_id)
            if current is None:
                return deny("user_missing")
            if int(current["user_id"]) != uid:
                return deny("user_mismatch")
            if not current["is_active"]:
                return deny("user_inactive")
            if current["approval_status"] != "APPROVED":
                return deny("user_unapproved")
            stage = "company_access"
            # Use the selector's policy with fresh central role data, not cached login grants.
            companies = get_active_companies_for_user(make_auth_user(current), conn=db)
            if cid not in {int(company["company_id"]) for company in companies}:
                return deny("company_inaccessible")
            stage = "selected_company_permissions"
            permissions = frozenset(get_user_permissions(db, user_id=uid, company_id=cid))
        # Never attach an old DB response to a newly selected user/company.
        stage = "session_binding"
        if marker(state) != generation or identity() != initial:
            return deny("session_changed")
        epoch = bind_verified(state, generation, initial, permissions)
        if epoch is None:
            return deny("generation_changed")
        return Session(cid, str(uid), epoch, permissions, frozenset(),
                       "EXTERNAL_API_TEST" in permissions, datetime.now(timezone.utc) + timedelta(seconds=30))
    except Exception as exc:
        return deny("verification_error", type(exc).__name__)


def load_erp_api_key(company, service):
    """Explicit live server use only; no SELECT *, cache, replication or raw logging."""
    if type(company) is not int or company <= 0 or service not in ERP_KEY_CODES:
        raise ApiManagementError("erp_reference_not_supported")
    from app.services.ssai_auth_service import connect_company_db
    try:
        with closing(connect_company_db(company)) as db:
            db.timeout = 10
            with closing(db.cursor()) as cursor:
                cursor.execute("SELECT TOP (2) Rd0A4_Value FROM dbo.RDDBC0A4 "
                               "WHERE Rd0A4_Gcode = ? AND Rd0A4_Tcode = ?", "000010", ERP_KEY_CODES[service])
                rows = cursor.fetchmany(2)
                if len(rows) != 1 or not isinstance(rows[0][0], str):
                    raise ApiManagementError("erp_key_missing_or_duplicate")
                value = rows[0][0].strip()
                if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
                    raise ApiManagementError("erp_key_invalid")
                return value
    except ApiManagementError:
        raise
    except Exception:
        raise ApiManagementError("erp_key_unavailable") from None
