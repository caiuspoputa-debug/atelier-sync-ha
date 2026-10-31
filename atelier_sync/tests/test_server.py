import copy
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import sqlite3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import APIError, MAX_BODY, Server, Store, automatic_backups, canonical, runtime_options
from validation import business_snapshot, default_workspace, validate_state

PAIRING = "test-key-at-least-sixteen-characters"
STAMP = "2026-10-31T10:00:00.000Z"


def agenda():
    return {
        "version": 1, "demo": False,
        "settings": {"slotMinutes": 60, "theme": "light", "bookingEndIsLastStart": True,
                     "schedulePolicy": "daily-10-18-v1", "workspace": default_workspace(),
                     "schedule": [{"day": day, "closed": False, "start": "10:00", "end": "18:00", "breakStart": "", "breakEnd": ""} for day in range(7)], "overrides": []},
        "clients": [{"id": "c1", "name": "Ana", "phone": "", "notes": "", "archived": False}],
        "services": [{"id": "s1", "name": "Manichiură", "category": "Manichiură", "duration": 60, "price": 120, "color": "#123AbC", "active": True}],
        "appointments": [],
    }


def appointment(record_id="a1", **changes):
    return {"id": record_id, "clientId": "c1", "date": "2020-01-02", "start": "10:00", "duration": 60,
            "price": 120, "status": "confirmed", "notes": "", "createdAt": STAMP, "updatedAt": STAMP,
            "services": [{"serviceId": "s1", "name": "Manichiură", "duration": 60, "price": 120, "color": "#123AbC"}], **changes}


def expense(record_id="e1"):
    return {"id": record_id, "date": "2026-10-31", "amount": 12.34, "description": "Consumabile", "createdAt": STAMP, "updatedAt": STAMP}


def operation(record_id, *changes):
    return {"id": record_id, "createdAt": STAMP, "changes": list(changes)}


def change(key, before, after):
    return {"key": key, "before": copy.deepcopy(before), "after": copy.deepcopy(after)}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "agenda.sqlite3"
        self.store = Store(self.path, PAIRING)
        self.master = self.register("tablet", "master")
        self.phone = self.register("phone", "secondary")

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, device, role):
        return self.store.register({"pairingKey": PAIRING, "deviceId": device, "role": role, "name": device})

    def sync(self, operations=None, device="tablet", **extra):
        auth = self.master if device == "tablet" else self.phone
        return self.store.sync(auth["token"], {"deviceId": device, "workspaceId": auth["workspaceId"], "operations": operations or [], **extra})

    def boot(self, state=None):
        return self.sync(bootstrap=state or agenda(), bootstrapId="bootstrap-one")

    def assert_error(self, code, callback):
        with self.assertRaises(APIError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_pairing_key_policy_and_stable_registration_across_restart(self):
        with self.assertRaises(ValueError):
            Store(Path(self.tmp.name) / "bad.sqlite3", "short")
        self.assert_error("invalid_pairing_key", lambda: self.store.register({"pairingKey": "incorrect", "deviceId": "x", "role": "master", "name": "x"}))
        self.assertEqual(self.register("tablet", "master")["token"], self.master["token"])
        self.store = Store(self.path, PAIRING)
        self.assertEqual(self.register("tablet", "master")["token"], self.master["token"])
        self.assertEqual(self.register("tablet", "secondary")["token"], self.master["token"])

    def test_secondary_can_register_before_bootstrap_but_cannot_initialize(self):
        self.assertFalse(self.phone["initialized"])
        self.assert_error("needs_initialization", lambda: self.sync(device="phone"))
        self.assert_error("master_required", lambda: self.sync(device="phone", bootstrap=agenda(), bootstrapId="p-init"))
        self.assertFalse(self.store.status()["initialized"])

    def test_bootstrap_is_idempotent_and_never_overwrites_an_existing_agenda(self):
        initial = agenda()
        response = self.boot(initial)
        self.assertEqual(response["serverRevision"], 1)
        self.assertEqual(response["state"]["expenses"], [])
        old = response["state"]["clients"][0]
        updated = {**old, "notes": "Changed"}
        self.sync([operation("edit", change("clients/c1", old, updated))])
        retry = self.sync(bootstrap=initial, bootstrapId="bootstrap-one")
        self.assertEqual(retry["state"]["clients"][0]["notes"], "Changed")
        self.assertEqual(retry["serverRevision"], 2)
        self.assert_error("already_initialized", lambda: self.sync(bootstrap=initial, bootstrapId="other"))
        modified = copy.deepcopy(initial); modified["demo"] = True
        self.assert_error("bootstrap_id_reused", lambda: self.sync(bootstrap=modified, bootstrapId="bootstrap-one"))
        self.assert_error("bootstrap_id_reused", lambda: self.sync(device="phone", bootstrap=initial, bootstrapId="bootstrap-one"))

    def test_receipts_and_state_survive_restart_and_duplicate_retry(self):
        self.boot()
        op = operation("expense", change("expenses/e1", None, expense()))
        applied = self.sync([op], device="phone")
        self.assertEqual(applied["results"][0]["status"], "applied")
        self.store = Store(self.path, PAIRING)
        retry = self.sync([op], device="phone")
        self.assertEqual(retry["serverRevision"], applied["serverRevision"])
        self.assertEqual(retry["results"], applied["results"])
        self.assertEqual(len(retry["state"]["expenses"]), 1)
        altered = copy.deepcopy(op); altered["changes"][0]["after"]["amount"] = 99
        bad = self.sync([altered], device="phone")
        self.assertEqual(bad["results"][0]["status"], "rejected")
        self.assertEqual(bad["state"]["expenses"][0]["amount"], 12.34)
        self.assertEqual(self.sync([op], device="phone")["results"], applied["results"])

    def test_stale_compare_and_swap_does_not_block_later_independent_operations(self):
        initial = self.boot()["state"]["clients"][0]
        self.sync([operation("tablet-edit", change("clients/c1", initial, {**initial, "notes": "Tablet"}))])
        stale = operation("phone-edit", change("clients/c1", initial, {**initial, "notes": "Phone"}))
        response = self.sync([stale, operation("expense", change("expenses/e1", None, expense()))], device="phone")
        self.assertEqual([x["status"] for x in response["results"]], ["conflict", "applied"])
        self.assertEqual(response["state"]["clients"][0]["notes"], "Tablet")
        self.assertEqual(response["serverRevision"], 3)
        self.assertEqual(self.sync([stale], device="phone")["results"][0]["status"], "conflict")

    def test_atomic_client_and_historical_offline_appointment_operation(self):
        self.boot()
        client = {"id": "c2", "name": "Maria", "phone": "", "notes": "", "archived": False}
        a = appointment(clientId="c2", start="07:30")
        response = self.sync([operation("offline", change("clients/c2", None, client), change("appointments/a1", None, a))], device="phone")
        self.assertEqual(response["results"][0]["status"], "applied")
        self.assertEqual(response["state"]["appointments"][0], a)
        self.assertEqual(len(response["state"]["clients"]), 2)

    def test_invalid_references_and_shapes_reject_entire_atomic_operation(self):
        self.boot()
        bad = appointment(clientId="missing")
        response = self.sync([operation("bad", change("expenses/e1", None, expense()), change("appointments/a1", None, bad))])
        self.assertEqual(response["results"][0]["status"], "rejected")
        self.assertEqual(response["state"]["expenses"], [])
        self.assertEqual(response["state"]["appointments"], [])
        self.assertEqual(response["serverRevision"], 1)
        malformed = operation("invalid-id", change("clients/c2", None, {**response["state"]["clients"][0], "id": "wrong"}))
        self.assertEqual(self.sync([malformed])["results"][0]["status"], "rejected")

    def test_distinct_booking_ids_still_cannot_overlap_and_touching_endpoints_work(self):
        self.boot()
        self.sync([operation("a", change("appointments/a1", None, appointment()))])
        overlapping = operation("b", change("appointments/a2", None, appointment("a2", start="10:30")))
        result = self.sync([overlapping], device="phone")
        self.assertEqual(result["results"][0]["status"], "conflict")
        adjacent = operation("adjacent", change("appointments/a3", None, appointment("a3", start="11:00")))
        self.assertEqual(self.sync([adjacent], device="phone")["results"][0]["status"], "applied")

    def test_cancelled_booking_releases_time_and_deletion_uses_exact_before(self):
        a = appointment()
        state = agenda(); state["appointments"] = [a]
        self.boot(state)
        cancelled = {**a, "status": "cancelled"}
        self.sync([operation("cancel", change("appointments/a1", a, cancelled))])
        self.assertEqual(self.sync([operation("new", change("appointments/a2", None, appointment("a2")))], device="phone")["results"][0]["status"], "applied")
        self.assertEqual(self.sync([operation("stale-delete", change("appointments/a1", a, None))])["results"][0]["status"], "conflict")
        self.assertEqual(self.sync([operation("delete", change("appointments/a1", cancelled, None))])["results"][0]["status"], "applied")

    def test_overlapping_changes_in_one_operation_roll_back_every_record(self):
        self.boot()
        response = self.sync([operation("two-overlap", change("expenses/e1", None, expense()), change("appointments/a1", None, appointment()), change("appointments/a2", None, appointment("a2", start="10:30")))])
        self.assertEqual(response["results"][0]["status"], "conflict")
        self.assertEqual(response["state"]["appointments"], [])
        self.assertEqual(response["state"]["expenses"], [])

    def test_concurrent_compare_and_swap_serializes_without_lost_updates(self):
        old = self.boot()["state"]["clients"][0]
        ops = [operation("concurrent-" + str(i), change("clients/c1", old, {**old, "notes": str(i)})) for i in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda op: self.sync([op]), ops))
        self.assertEqual(sorted(x["results"][0]["status"] for x in replies), ["applied", "conflict"])
        self.assertEqual(self.sync()["serverRevision"], 2)

    def test_completed_edits_require_retained_exact_correction_history(self):
        a = appointment(status="completed", color="#17a9Ef")
        state = agenda(); state["appointments"] = [a]
        self.boot(state)
        edited = {**copy.deepcopy(a), "price": 150}
        untracked = self.sync([operation("untracked", change("appointments/a1", a, edited))])
        self.assertEqual(untracked["results"][0]["status"], "rejected")
        event = {"at": STAMP, "before": business_snapshot(a), "after": business_snapshot(edited), "reason": "Preț corectat"}
        edited["corrections"] = [event]
        tracked = self.sync([operation("tracked", change("appointments/a1", a, edited))], device="phone")
        self.assertEqual(tracked["results"][0]["status"], "applied")
        self.assertEqual(tracked["state"]["appointments"][0], edited)
        stripped = copy.deepcopy(edited); del stripped["corrections"]
        self.assertEqual(self.sync([operation("strip", change("appointments/a1", edited, stripped))])["results"][0]["status"], "rejected")
        self.assertEqual(self.sync()["state"]["appointments"][0]["corrections"], [event])

    def test_role_tokens_are_bound_to_device_and_workspace(self):
        self.boot()
        body = {"deviceId": "phone", "workspaceId": self.phone["workspaceId"], "operations": []}
        self.assert_error("unauthorized", lambda: self.store.sync("bad", body))
        self.assert_error("device_mismatch", lambda: self.store.sync(self.master["token"], body))
        body["workspaceId"] = "another"
        self.assert_error("workspace_mismatch", lambda: self.store.sync(self.phone["token"], body))
        self.assert_error("invalid_request", lambda: self.sync([operation("x") for _ in range(51)]))

    def test_pending_edit_accepts_one_exact_resolution_audit_but_rejects_invalid_or_multiple(self):
        a = appointment(status="pending")
        state = agenda(); state["appointments"] = [a]
        self.boot(state)
        edited = {**copy.deepcopy(a), "status": "completed", "price": 150}
        event = {"at": STAMP, "before": business_snapshot(a), "after": business_snapshot(edited), "reason": "Rezolvare conflict"}
        wrong = copy.deepcopy(edited); wrong["corrections"] = [copy.deepcopy(event)]
        wrong["corrections"][0]["before"]["price"] = 121
        self.assertEqual(self.sync([operation("wrong-audit", change("appointments/a1", a, wrong))])["results"][0]["status"], "rejected")
        multiple = copy.deepcopy(edited); multiple["corrections"] = [copy.deepcopy(event), copy.deepcopy(event)]
        self.assertEqual(self.sync([operation("multiple-audit", change("appointments/a1", a, multiple))])["results"][0]["status"], "rejected")
        edited["corrections"] = [event]
        response = self.sync([operation("resolution", change("appointments/a1", a, edited))], device="phone")
        self.assertEqual(response["results"][0]["status"], "applied")
        self.assertEqual(response["state"]["appointments"][0]["corrections"], [event])

    def test_ordinary_first_completion_does_not_require_a_correction_entry(self):
        a = appointment(status="pending")
        state = agenda(); state["appointments"] = [a]
        self.boot(state)
        after = {**copy.deepcopy(a), "status": "completed"}
        result = self.sync([operation("complete", change("appointments/a1", a, after))])
        self.assertEqual(result["results"][0]["status"], "applied")
        self.assertNotIn("corrections", result["state"]["appointments"][0])

    def test_sqlite_backup_keeps_workspace_data_and_receipts(self):
        self.boot()
        op = operation("expense", change("expenses/e1", None, expense()))
        response = self.sync([op], device="phone")
        backup = Path(self.tmp.name) / "backups" / "copy.sqlite3"
        self.store.backup(backup)
        restored = Store(backup, PAIRING)
        result = restored.sync(self.phone["token"], {"deviceId": "phone", "workspaceId": self.phone["workspaceId"], "operations": [op]})
        self.assertEqual(result, response)

    def test_key_rotation_is_not_revocation_but_explicit_revoke_invalidates_old_token(self):
        self.boot()
        self.store = Store(self.path, "a-new-pairing-key-at-least-sixteen")
        self.assertTrue(self.sync(device="phone")["initialized"])
        self.assertTrue(self.store.revoke_device("phone"))
        self.assert_error("unauthorized", lambda: self.sync(device="phone"))
        new = self.store.register({"pairingKey": "a-new-pairing-key-at-least-sixteen", "deviceId": "phone", "role": "secondary", "name": "phone"})
        self.assertNotEqual(new["token"], self.phone["token"])

    def test_state_validator_preserves_legacy_duration_colors_ranges_and_money(self):
        state = agenda()
        state["appointments"] = [appointment(status="completed", color="#17a9Ef")]
        state["expenses"] = [expense()]
        state["settings"]["agendaRanges"] = [{"id": "r1", "effectiveFrom": "2020-01-01", "start": "08:30", "lastStart": "17:45", "createdAt": STAMP}]
        self.assertEqual(validate_state(state), state)
        bad = copy.deepcopy(state); bad["expenses"][0]["amount"] = 1.005
        with self.assertRaises(ValueError): validate_state(bad)
        bad = copy.deepcopy(state); bad["appointments"][0]["date"] = "2026-02-30"
        with self.assertRaises(ValueError): validate_state(bad)

    def test_transient_failure_rolls_back_changes_and_receipt_so_exact_retry_can_apply(self):
        before = self.boot()
        op = operation("retry-after-failure", change("expenses/e1", None, expense()))
        write = self.store._write_entities
        def fail_after_write(db, entities):
            write(db, entities)
            raise sqlite3.OperationalError("simulated transient storage failure")
        with patch.object(self.store, "_write_entities", side_effect=fail_after_write):
            with self.assertRaises(sqlite3.OperationalError): self.sync([op])
        self.assertEqual(self.sync()["state"], before["state"])
        self.assertEqual(self.sync()["serverRevision"], 1)
        result = self.sync([op])
        self.assertEqual(result["results"][0]["status"], "applied")
        self.assertEqual(result["serverRevision"], 2)
        self.assertEqual(len(result["state"]["expenses"]), 1)

    def test_canonical_state_limit_rejects_bootstrap_and_growth_before_commit(self):
        with patch("server.MAX_STATE", 100):
            self.assert_error("invalid_bootstrap", lambda: self.boot())
        self.assertFalse(self.store.status()["initialized"])
        before = self.boot()
        size = len(canonical(before["state"]).encode("utf-8"))
        with patch("server.MAX_STATE", size + 10):
            result = self.sync([operation("growth", change("expenses/e1", None, expense()))])
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertEqual(result["state"], before["state"])
        self.assertEqual(result["serverRevision"], before["serverRevision"])

    def test_runtime_backup_options_validate_boundaries_and_defaults_without_logging_secrets(self):
        defaults = runtime_options({"pairing_key": PAIRING})
        self.assertEqual(defaults["backup_interval_hours"], 6)
        self.assertEqual(defaults["backup_retention_days"], 14)
        for interval, retention in [(1, 1), (24, 90)]:
            result = runtime_options({"pairing_key": PAIRING, "backup_interval_hours": interval, "backup_retention_days": retention})
            self.assertEqual((result["backup_interval_hours"], result["backup_retention_days"]), (interval, retention))
        for field, value in [("backup_interval_hours", 0), ("backup_interval_hours", 25), ("backup_interval_hours", True), ("backup_interval_hours", "6"), ("backup_retention_days", 0), ("backup_retention_days", 91)]:
            with self.assertRaises(ValueError): runtime_options({"pairing_key": PAIRING, field: value})
        self.assertEqual(runtime_options({}, PAIRING)["pairing_key"], PAIRING)

    def test_automatic_backup_uses_configured_interval_and_calendar_retention(self):
        self.boot()
        directory = Path(self.tmp.name) / "daily"
        directory.mkdir()
        today = dt.datetime.now(dt.timezone.utc).date()
        old = directory / ("atelier-sync-" + (today - dt.timedelta(days=3)).isoformat() + ".sqlite3")
        retained = directory / ("atelier-sync-" + (today - dt.timedelta(days=1)).isoformat() + ".sqlite3")
        old.write_bytes(b"old fixture")
        retained.write_bytes(b"retained fixture")
        class OneRun:
            stopped = False
            seconds = None
            def is_set(self): return self.stopped
            def wait(self, seconds): self.seconds = seconds; self.stopped = True
        stop = OneRun()
        automatic_backups(self.store, stop, directory, interval_hours=2, retention_days=2)
        self.assertEqual(stop.seconds, 7200)
        self.assertFalse(old.exists())
        self.assertTrue(retained.exists())
        latest = directory / ("atelier-sync-" + today.isoformat() + ".sqlite3")
        self.assertEqual(Store(latest, PAIRING).status(), self.store.status())


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "agenda.sqlite3", PAIRING)
        self.server = Server(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.tmp.cleanup()

    def request(self, route, body=None, token=None, method="POST", raw=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        data = raw if raw is not None else json.dumps(body or {}).encode()
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if token: request_headers["Authorization"] = "Bearer " + token
        conn.request(method, route, data, request_headers)
        response = conn.getresponse(); payload = response.read()
        status, content_type = response.status, response.getheader("Content-Type")
        conn.close()
        return status, json.loads(payload) if "application/json" in content_type else payload.decode()

    def test_public_status_and_page_never_expose_agenda_or_tokens(self):
        status, body = self.request("/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"protocol", "workspaceId", "initialized"})
        status, page = self.request("/", method="GET")
        self.assertEqual(status, 200)
        self.assertNotIn(PAIRING, page)
        self.assertNotIn("clients", page)

    def test_http_authentication_initialization_and_pull(self):
        status, registration = self.request("/v1/register", {"pairingKey": PAIRING, "deviceId": "tablet", "role": "master", "name": "Tabletă"})
        self.assertEqual(status, 200)
        body = {"deviceId": "tablet", "workspaceId": registration["workspaceId"], "operations": [], "bootstrap": agenda(), "bootstrapId": "b1"}
        self.assertEqual(self.request("/v1/sync", body)[0], 401)
        status, result = self.request("/v1/sync", body, registration["token"])
        self.assertEqual(status, 200)
        self.assertEqual(result["serverRevision"], 1)
        del body["bootstrap"]; del body["bootstrapId"]
        self.assertEqual(self.request("/v1/sync", body, registration["token"])[1]["state"]["clients"][0]["name"], "Ana")

    def test_invalid_json_nonfinite_numbers_and_oversized_body_are_rejected(self):
        self.assertEqual(self.request("/v1/status", raw=b"{")[0], 400)
        self.assertEqual(self.request("/v1/status", raw=b'{"value":NaN}')[0], 400)
        self.assertEqual(self.request("/v1/status", raw=b"", headers={"Content-Length": str(MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.request("/unknown")[0], 404)


if __name__ == "__main__":
    unittest.main()
