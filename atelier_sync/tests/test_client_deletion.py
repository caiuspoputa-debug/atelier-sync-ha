"""Deleted client identities retain accounting/audit references without becoming selectable clients."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Store
from validation import ValidationError, business_snapshot, validate_state
from test_server import PAIRING, STAMP, agenda, appointment, change, expense, operation


def tombstone(client):
    return {**client, "deleted": True, "archived": True, "phone": "", "notes": ""}


class ClientDeletionTests(unittest.TestCase):
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
        return self.sync(bootstrap=state or agenda(), bootstrapId="deleted-client-bootstrap")

    def test_legacy_absence_and_explicit_boolean_false_round_trip_without_rewriting_old_clients(self):
        old = agenda()
        self.assertNotIn("deleted", validate_state(old)["clients"][0])
        explicit = copy.deepcopy(old)
        explicit["clients"][0]["deleted"] = False
        self.assertIs(validate_state(explicit)["clients"][0]["deleted"], False)
        self.assertNotIn("deleted", old["clients"][0])
        initial = self.boot(old)
        self.assertNotIn("deleted", initial["state"]["clients"][0])
        self.store = Store(self.path, PAIRING)
        self.assertEqual(self.sync(device="phone")["state"], initial["state"])

    def test_deleted_marker_is_strict_and_deleted_contact_fields_must_be_empty_and_archived(self):
        for invalid in [None, 0, 1, "true", [], {}]:
            with self.subTest(marker=invalid):
                state = agenda(); state["clients"][0]["deleted"] = invalid
                with self.assertRaises(ValidationError):
                    validate_state(state)
        for fields in [{"archived": False}, {"phone": "0722123456"}, {"notes": "Preferințe personale"}]:
            with self.subTest(fields=fields):
                state = agenda(); state["clients"][0] = {**tombstone(state["clients"][0]), **fields}
                with self.assertRaises(ValidationError):
                    validate_state(state)

    def test_bootstrap_and_sqlite_backup_allow_deleted_identity_with_existing_history(self):
        state = agenda(); state["clients"][0] = tombstone(state["clients"][0])
        before = appointment(status="completed")
        after = {**before, "price": 175, "updatedAt": STAMP}
        after["corrections"] = [{"at": STAMP, "before": business_snapshot(before), "after": business_snapshot(after), "reason": "Istoric păstrat"}]
        state["appointments"] = [after]
        original = copy.deepcopy(state)
        result = self.boot(state)
        self.assertEqual(state, original)
        self.assertEqual(result["state"]["clients"], state["clients"])
        self.assertEqual(result["state"]["appointments"], state["appointments"])
        retry = self.sync(bootstrap=state, bootstrapId="deleted-client-bootstrap")
        self.assertEqual(retry["serverRevision"], 1)
        self.assertEqual(retry["state"], result["state"])
        backup = Path(self.tmp.name) / "backup.sqlite3"
        self.store.backup(backup)
        self.store = Store(backup, PAIRING)
        self.assertEqual(self.sync(device="phone")["state"], result["state"])

    def test_soft_delete_only_updates_contact_and_preserves_names_bookings_money_and_audit(self):
        state = agenda(); state["clients"][0].update(phone="+40722123456", notes="Notă privată")
        state["clients"].append({"id": "same-name", "name": "Ana", "phone": "", "notes": "Alt client", "archived": False})
        completed = appointment("history", status="completed")
        corrected = {**completed, "price": 150}
        corrected["corrections"] = [{"at": STAMP, "before": business_snapshot(completed), "after": business_snapshot(corrected)}]
        state["appointments"] = [corrected, appointment("active-past", date="2021-01-01"), appointment("active-future", date="2030-01-01")]
        state["expenses"] = [expense()]
        initial = self.boot(state)
        before = initial["state"]["clients"][0]
        response = self.sync([operation("delete-contact", change("clients/c1", before, tombstone(before)))])
        self.assertEqual(response["results"][0]["status"], "applied")
        self.assertEqual(response["state"]["clients"][0], tombstone(before))
        self.assertEqual(response["state"]["clients"][0]["name"], "Ana")
        self.assertEqual(response["state"]["clients"][1], initial["state"]["clients"][1])
        for kind in ["appointments", "services", "settings", "expenses", "suppliers"]:
            self.assertEqual(response["state"][kind], initial["state"][kind])

    def test_physical_delete_is_allowed_only_when_current_and_audit_references_are_absent(self):
        state = agenda(); state["appointments"] = [appointment()]
        original = self.boot(state)["state"]
        refused = self.sync([operation("delete-referenced", change("clients/c1", original["clients"][0], None))])
        self.assertEqual(refused["results"][0]["status"], "rejected")
        self.assertEqual(refused["state"], original)
        self.sync([operation("remove-booking", change("appointments/a1", original["appointments"][0], None))])
        deleting = operation("delete-unused", change("clients/c1", original["clients"][0], None))
        removed = self.sync([deleting])
        self.assertEqual(removed["results"][0]["status"], "applied")
        self.assertEqual(removed["state"]["clients"], [])
        self.store = Store(self.path, PAIRING)
        retry = self.sync([deleting])
        self.assertEqual(retry["serverRevision"], removed["serverRevision"])
        self.assertEqual(retry["results"], removed["results"])

    def test_audit_only_client_identity_remains_required(self):
        state = agenda(); state["clients"].append({"id": "c2", "name": "Alt client", "phone": "", "notes": "", "archived": False})
        before = appointment(status="completed")
        after = {**before, "clientId": "c2"}
        after["corrections"] = [{"at": STAMP, "before": business_snapshot(before), "after": business_snapshot(after)}]
        state["appointments"] = [after]
        original = self.boot(state)["state"]
        response = self.sync([operation("delete-audit-client", change("clients/c1", original["clients"][0], None))])
        self.assertEqual(response["results"][0]["status"], "rejected")
        self.assertEqual(response["state"], original)

    def test_exact_cas_cannot_revive_a_deleted_identity_by_removing_or_clearing_marker(self):
        state = agenda(); state["clients"][0] = tombstone(state["clients"][0])
        original = self.boot(state)["state"]
        for index, explicit_false in enumerate([False, True]):
            revived = copy.deepcopy(original["clients"][0])
            if explicit_false:
                revived["deleted"] = False
            else:
                del revived["deleted"]
            revived["archived"] = False
            response = self.sync([operation("revive-" + str(index), change("clients/c1", original["clients"][0], revived), change("expenses/e1", None, expense()))])
            self.assertEqual(response["results"][0]["status"], "rejected")
            self.assertIn("reactivat", response["results"][0]["reason"])
            self.assertEqual(response["state"], original)

    def test_old_apk_stripped_marker_gets_cas_conflict_without_erasing_tombstone(self):
        state = agenda(); state["clients"][0] = tombstone(state["clients"][0])
        original = self.boot(state)["state"]
        legacy = copy.deepcopy(original["clients"][0]); del legacy["deleted"]
        response = self.sync([operation("old-apk", change("clients/c1", legacy, {**legacy, "name": "Legacy edit"}))], device="phone")
        self.assertEqual(response["results"][0]["status"], "conflict")
        self.assertEqual(response["state"], original)

    def test_new_appointments_for_deleted_clients_are_rejected_for_every_status_atomically(self):
        state = agenda(); state["clients"][0] = tombstone(state["clients"][0])
        original = self.boot(state)["state"]
        for status in ["pending", "confirmed", "completed", "cancelled", "no-show"]:
            response = self.sync([operation("new-" + status, change("appointments/a1", None, appointment(status=status)), change("expenses/e1", None, expense()))])
            self.assertEqual(response["results"][0]["status"], "rejected")
            self.assertIn("client șters", response["results"][0]["reason"])
            self.assertEqual(response["state"], original)

    def test_atomic_soft_deletion_and_new_appointment_to_same_client_is_rejected(self):
        original = self.boot()["state"]
        client = original["clients"][0]
        response = self.sync([operation("delete-and-new", change("clients/c1", client, tombstone(client)), change("appointments/a1", None, appointment()))])
        self.assertEqual(response["results"][0]["status"], "rejected")
        self.assertEqual(response["state"], original)

    def test_reassignment_to_deleted_client_rejected_but_existing_same_identity_edits_work(self):
        state = agenda(); state["clients"][0] = tombstone(state["clients"][0])
        state["clients"].append({"id": "c2", "name": "Alt client", "phone": "", "notes": "", "archived": False})
        state["appointments"] = [appointment("existing-deleted"), appointment("other", clientId="c2", date="2021-01-01")]
        initial = self.boot(state)["state"]
        other = next(a for a in initial["appointments"] if a["id"] == "other")
        refused = self.sync([operation("reassign-deleted", change("appointments/other", other, {**other, "clientId": "c1"}))])
        self.assertEqual(refused["results"][0]["status"], "rejected")
        self.assertEqual(refused["state"], initial)
        existing = next(a for a in initial["appointments"] if a["id"] == "existing-deleted")
        completed = {**existing, "status": "completed", "price": 135}
        accepted = self.sync([operation("complete-existing", change("appointments/existing-deleted", existing, completed))])
        self.assertEqual(accepted["results"][0]["status"], "applied")
        corrected = {**completed, "price": 165, "notes": "Corecție explicită"}
        corrected["corrections"] = [{"at": STAMP, "before": business_snapshot(completed), "after": business_snapshot(corrected), "reason": "Încasare"}]
        result = self.sync([operation("correct-existing", change("appointments/existing-deleted", completed, corrected))])
        self.assertEqual(result["results"][0]["status"], "applied")
        self.assertEqual(next(a for a in result["state"]["appointments"] if a["id"] == "existing-deleted"), corrected)
        self.assertEqual(result["state"]["clients"], initial["clients"])
        invalid = copy.deepcopy(corrected); invalid["price"] += 1
        refused = self.sync([operation("unaudited-existing", change("appointments/existing-deleted", corrected, invalid))])
        self.assertEqual(refused["results"][0]["status"], "rejected")
        self.assertEqual(refused["state"], result["state"])


if __name__ == "__main__":
    unittest.main()
