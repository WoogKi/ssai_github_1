"""DB-free room lifecycle regression; never import the application entrypoint."""
from __future__ import annotations

import ast
import copy
import json
import logging
from pathlib import Path
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services import chat_company_scope_policy as company

MAIN = ROOT / "app/Lmstudio_SSAI_chat_main.py"
MIDDLE = ROOT / "app/ui/chat_middleware.py"
NAMES = {
    "_room_has_any_messages", "_is_empty_auto_room", "_select_pending_new_room",
    "_get_current_room_or_pending", "_drop_empty_auto_rooms", "_sync_room_meta",
    "_clear_company_scoped_chat_runtime", "_ensure_current_company_chat_scope",
    "_selected_chat_company_id", "_current_company_chat_rooms", "_make_chat_room",
    "_load_partitioned_chat_rooms", "_ensure_partitioned_room_loaded",
    "_compute_room_sidebar_state", "_reset_chat_session_when_user_changed",
    "_render_sims_with_chat_persistence",
}


def install(path, names, ns):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in functions} == names
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
        names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), ns)


class State(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__


class Rerun(BaseException):
    pass


class RoomLifecycle(unittest.TestCase):
    def setUp(self):
        self.ss = State()
        self.selected = {"company_id": 4}
        self.user = SimpleNamespace(user_id=1)
        self.loads = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        guard = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.save = Mock()
        self.ns = dict(st=SimpleNamespace(session_state=self.ss, rerun=Mock()),
            log=logging.getLogger("synthetic.chat"), json=json, Path=Path, time=time,
            uuid=uuid, CHAT_ROOM_PAGE_SIZE=10,
            _CHAT_PARTITION_CHANNELS=("messages", "history", "sims_messages", "gen_messages"),
            get_selected_company=lambda: self.selected, get_current_user=lambda: self.user,
            _normalize_chat_company_id=company.normalize_company_id,
            _room_matches_company=company.room_matches_company,
            _rooms_for_company=company.rooms_for_company,
            _current_chat_meta=lambda: dict(company_id=self.selected["company_id"], user_id=1),
            _default_room_name=lambda: "synthetic pending", make_ts=lambda: "2026-11-24 16:32:00",
            _chat_log_kv=lambda *args, **kwargs: "synthetic", _clear_stt_pending_state=Mock(),
            _clear_chat_room_selector_request_state=Mock(), _request_sims_close_for_new_pending_room=Mock(),
            _ensure_auto_room_name=Mock(), _partitioned_chat_root=lambda *args: self.root,
            _effective_chat_file=lambda: str(self.root / "unused.json"),
            _partitioned_rooms_file=lambda root: root / "rooms.json",
            _partitioned_room_dir=lambda root, rid: root / rid, migrate_rooms=lambda rooms: rooms,
            _set_chat_storage_state=lambda *args: None, _load_partitioned_room_messages=self.load,
            save_chat_rooms=self.save, render_sims_main=Mock())
        install(MAIN, NAMES, self.ns)

    def stored(self, rid="saved", company_id=4):
        return dict(id=rid, name="synthetic", company_id=company_id, auto_created=True,
            title_initialized=False, message_count=1, relative_path="synthetic/messages.jsonl",
            messages=[], history=[], gen_messages=[], sims_messages=[], __messages_loaded=False)

    def pending(self):
        self.ss.setdefault("chat_rooms", [])
        return self.ns["_select_pending_new_room"]()

    def load(self, root, room):
        self.loads.append(room["id"])
        item = dict(id="old", role="assistant", content="synthetic")
        room.update(messages=[item], history=[dict(item)], __messages_loaded=True)
        return 100

    def drain(self, room, mid="new", source="KPIS"):
        self.ns.update(_ensure_table_json_safe=lambda x: None,
            _is_sims_table_history_item=lambda x: True, _prune_old_sims_table_history=Mock(),
            _sims_table_role_from_action=lambda *args: "source",
            _chat_runtime_log_kv=lambda *args: "synthetic")
        install(MIDDLE, {"wire_chat_context", "drain_inbox_to_chat"}, self.ns)
        self.ss["__chat_inbox"] = [dict(id=mid, type="table", title="synthetic",
            meta=dict(table_key="synthetic", source=source, result_status="success"))]
        self.ns["drain_inbox_to_chat"](room)

    def render(self, room):
        return self.ns["_render_sims_with_chat_persistence"](selected={"group": "KPIS"}, room=room)

    def test_storage_evidence_never_counts_as_pending(self):
        for evidence in ({"__messages_loaded": False}, {"relative_path": "x/messages.jsonl"},
                         {"message_count": 1}, {"message_count": 0}, {"message_count": None}):
            with self.subTest(evidence=evidence):
                room = dict(auto_created=True, messages=[], **evidence)
                self.assertFalse(self.ns["_is_empty_auto_room"](room))

    def test_fresh_pending_reused_without_save(self):
        first = self.pending()
        self.assertTrue(self.ns["_is_empty_auto_room"](first))
        self.assertIs(first, self.pending())
        self.save.assert_not_called()

    def test_login_loads_metadata_but_selects_fresh_pending(self):
        doc = dict(current_room="saved", rooms=[self.stored()])
        path = self.root / "rooms.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        before = path.read_bytes()
        rooms = self.ns["_load_partitioned_chat_rooms"]("synthetic")
        self.ss.update(chat_rooms=rooms, __chat_storage_mode="partitioned",
                       __chat_partition_root=str(self.root))
        self.assertNotIn("current_room", self.ss)
        new = self.pending()
        self.assertNotEqual(new["id"], "saved")
        self.assertIs(new, self.ns["_get_current_room_or_pending"]())
        self.assertEqual(self.loads, [])
        self.assertEqual(path.read_bytes(), before)

    def test_explicit_selection_loads_once_even_with_old_auto_flag(self):
        self.ss.update(chat_rooms=[self.stored()], current_room="saved",
                       __chat_storage_mode="partitioned", __chat_partition_root=str(self.root))
        room = self.ns["_get_current_room_or_pending"]()
        self.assertIs(room, self.ns["_get_current_room_or_pending"]())
        self.assertEqual(self.loads, ["saved"])
        self.assertTrue(room["auto_created"])
        self.assertFalse(self.ns["_is_empty_auto_room"](room))

    def test_cleanup_and_save_filter_preserve_stored_rooms(self):
        saved = self.stored()
        self.ss["chat_rooms"] = [saved]
        pending = self.pending()
        to_save = [r for r in self.ss.chat_rooms if not self.ns["_is_empty_auto_room"](r)]
        self.assertEqual(to_save, [saved])
        self.assertEqual(self.ns["_drop_empty_auto_rooms"](), 1)
        self.assertEqual(self.ss.chat_rooms, [saved])
        self.assertNotIn(pending, self.ss.chat_rooms)

    def test_production_save_passes_unloaded_room_to_partition_writer(self):
        saved = self.stored()
        self.ss["chat_rooms"] = [saved]
        self.pending()
        detail = dict(changed_room_count=0, changed_fields=[], compare_mode="synthetic",
                      bytes=0, json_serialize=0, compare=0, file_write=0, file_replace=0)
        writer = Mock(return_value=detail)
        self.ns.update(_chat_storage_mode_enabled=lambda: True,
                       _save_partitioned_chat_rooms=writer, _chat_save_perf_total=lambda d: 0)
        install(MAIN, {"save_chat_rooms"}, self.ns)
        before = copy.deepcopy(saved)
        self.ns["save_chat_rooms"]()
        writer.assert_called_once()
        self.assertEqual(writer.call_args.args[0], [saved])
        self.assertEqual(saved, before)

    def test_new_question_materializes_pending(self):
        room = self.pending()
        room["messages"].append(dict(role="user", content="synthetic question"))
        self.ns["_sync_room_meta"](room, materialize=True)
        self.assertFalse(room["auto_created"])
        self.assertTrue(room["title_initialized"])

    def test_successful_kpis_drain_materializes_before_rerun_once(self):
        room = self.pending()
        def submit(**kwargs):
            self.drain(room)
            raise Rerun()
        self.ns["render_sims_main"] = submit
        with self.assertRaises(Rerun):
            self.render(room)
        self.assertFalse(room["auto_created"])
        self.assertTrue(room["title_initialized"])
        self.assertEqual(len(room["messages"]), 1)
        self.assertEqual(len(room["history"]), 1)
        self.save.assert_called_once()
        self.ns["render_sims_main"] = Mock(return_value={"type": "form_only"})
        for _ in range(3):
            self.render(room)
        self.save.assert_called_once()
        self.assertEqual(len(room["messages"]), 1)

    def test_publish_wssz_and_main_boundary_with_synthetic_rest(self):
        from tools.check_kpis_erp_direct import DirectChecks
        from app.ui import kpis_erp_query as ui, chat_middleware as chat

        fixture = DirectChecks(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ss.update(fixture.state)
        self.selected["company_id"] = 8
        self.user = SimpleNamespace(user_id=42)
        room = self.pending()
        with patch.object(chat.st, "session_state", self.ss), patch.object(
                chat, "_append_finalized_nlq_case"):
            result = fixture.call()
            self.assertEqual(result["status"], "SUCCESS")
            def submit(**kwargs):
                self.assertTrue(ui.publish_result(result, fixture.query, (8, "42", 12345),
                    load_session=lambda: fixture.actor))
                raise Rerun()
            self.ns["render_sims_main"] = submit
            with self.assertRaises(Rerun):
                self.render(room)
            self.assertFalse(room["auto_created"])
            self.assertEqual(len(room["messages"]), 1)
            self.assertEqual(len(room["history"]), 1)
            self.assertEqual(room["messages"][0]["meta"]["source"], "KPIS")
            self.save.assert_called_once()
            self.ns["render_sims_main"] = Mock()
            self.render(room)
            self.save.assert_called_once()
            self.assertEqual(len(fixture.sent), 1)

    def test_kpis_existing_room_keeps_title_and_history(self):
        room = self.pending()
        room.update(auto_created=False, title_initialized=True, name="user title")
        room["messages"].append(dict(id="old", content="old"))
        self.ns["render_sims_main"] = lambda **kwargs: self.drain(room)
        self.render(room)
        self.assertEqual(room["name"], "user title")
        self.assertEqual([m["id"] for m in room["messages"]], ["old", "new"])
        self.ns["_ensure_auto_room_name"].assert_not_called()
        self.save.assert_called_once()

    def test_failed_query_does_not_materialize_or_save(self):
        room = self.pending()
        self.ns["render_sims_main"] = Mock(return_value={"type": "form_only"})
        self.render(room)
        self.ns["render_sims_main"] = Mock(side_effect=ValueError("synthetic failure"))
        with self.assertRaises(ValueError):
            self.render(room)
        self.assertTrue(room["auto_created"])
        self.assertEqual(room["messages"], [])
        self.save.assert_not_called()

    def test_scope_changes_do_not_materialize_wrong_room(self):
        for change in ("company", "logout", "user", "room"):
            with self.subTest(change=change):
                self.selected["company_id"] = 4
                self.user = SimpleNamespace(user_id=1)
                self.ss.clear()
                room = self.pending()
                def submit(**kwargs):
                    self.drain(room)
                    if change == "company":
                        self.selected["company_id"] = 8
                    elif change == "logout":
                        self.user = None
                    elif change == "user":
                        self.user = SimpleNamespace(user_id=2)
                    else:
                        self.ss.current_room = "another"
                self.ns["render_sims_main"] = submit
                self.render(room)
                self.assertTrue(room["auto_created"])
        self.save.assert_not_called()

    def test_general_panel_keeps_existing_persistence_contract(self):
        room = self.pending()
        self.ns["render_sims_main"] = lambda **kwargs: self.drain(room, source="ERP")
        self.render(room)
        self.save.assert_not_called()
        self.ns["_sync_room_meta"](room, materialize=True)
        self.save()
        self.save.assert_called_once()
        self.assertFalse(room["auto_created"])

    def test_relogin_after_kpis_leaves_result_room_unselected(self):
        room = self.pending()
        self.ns["render_sims_main"] = lambda **kwargs: self.drain(room)
        self.render(room)
        saved_id = room["id"]
        room.update(message_count=1, relative_path="synthetic/messages.jsonl", __messages_loaded=False,
                    messages=[], history=[])
        self.ss.pop("current_room")
        self.assertNotEqual(self.pending()["id"], saved_id)
        self.assertEqual(self.loads, [])

    def test_company_switch_and_user_change_clear_selection(self):
        rooms = [self.stored(company_id=8), self.stored("four")]
        self.ss.update(chat_rooms=rooms, current_room="saved", __chat_selected_company_id="8")
        self.assertTrue(self.ns["_ensure_current_company_chat_scope"]())
        self.assertNotIn("current_room", self.ss)
        self.assertIs(self.ss.chat_rooms, rooms)
        self.assertEqual(self.pending()["company_id"], 4)
        self.ss["__chat_owner_user_id"] = "2"
        self.ns["_reset_chat_session_when_user_changed"]()
        self.assertNotIn("current_room", self.ss)
        self.assertNotIn("chat_rooms", self.ss)

    def test_logout_clears_selection_without_restoring_any_room(self):
        login = ROOT / "app/ui/ssai_login.py"
        tree = ast.parse(login.read_text(encoding="utf-8-sig"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "logout")
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and n.id.isupper():
                self.ns[n.id] = n.id
        self.ns.update(_login_log_kv=lambda: "synthetic", invalidate_external_api_session=Mock(),
                       _clear_company_dependent_state=Mock())
        install(login, {"logout"}, self.ns)
        self.ss.update(chat_rooms=[self.stored()], current_room="saved")
        self.ns["logout"]()
        self.assertNotIn("current_room", self.ss)
        self.assertNotIn("chat_rooms", self.ss)

    def test_expired_table_only_renders_existing_summary(self):
        ui = SimpleNamespace(markdown=Mock(), info=Mock())
        self.ns.update(st=ui, _build_query_condition_text=lambda item: "synthetic condition",
            _render_sims_result_header_view=Mock(), _build_sims_result_header_view=Mock(return_value={}),
            _render_chat_summary_expander_v2=Mock())
        install(MIDDLE, {"_render_expired_sims_table_fallback"}, self.ns)
        self.ns["_render_expired_sims_table_fallback"](
            dict(title="synthetic"), dict(summary_md="stored summary", table_key="expired"))
        ui.info.assert_called_once()
        self.assertIn("만료", ui.info.call_args.args[0])
        self.ns["_render_chat_summary_expander_v2"].assert_called_once_with(
            "stored summary", "synthetic condition")
        self.save.assert_not_called()
        self.assertEqual(self.loads, [])

    def test_sidebar_does_not_mutate_stored_title_or_selection(self):
        room = self.stored()
        self.ss.update(chat_rooms=[room], current_room="")
        before = copy.deepcopy(dict(self.ss))
        result = self.ns["_compute_room_sidebar_state"]([room], current_room_id="", initial_to_last=True)
        self.assertEqual(self.ss, before)
        self.assertEqual(result["current_kind"], "none")

    def test_both_panel_entrypoints_use_persistence_boundary(self):
        tree = ast.parse(MAIN.read_text(encoding="utf-8-sig"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "_render_sims_with_chat_persistence"]
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
