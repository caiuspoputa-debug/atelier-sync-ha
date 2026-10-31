"""Supplier rollout, aggregate validation, historical names, and old-device CAS."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Store
from validation import ValidationError, validate_state
from test_server import PAIRING, agenda, change, expense, operation


def supplier(record_id="supplier1", name="Furnizor consumabile"):
    return {"id": record_id, "name": name}


def tagged_expense(record_id="e1", supplier_id="supplier1", name="Nume la cumpărare"):
    return {**expense(record_id), "supplierId": supplier_id, "supplierName": name}


class SupplierTests(unittest.TestCase):
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
        return self.sync(bootstrap=state or agenda(), bootstrapId="suppliers-bootstrap")

    def test_capability_is_advertised_without_private_data_on_every_success_response(self):
        self.assertEqual(self.store.status()["capabilities"], ["suppliers-v1", "client-deletion-v1"])
        self.assertEqual(self.master["capabilities"], ["suppliers-v1", "client-deletion-v1"])
        self.assertEqual(self.phone["capabilities"], ["suppliers-v1", "client-deletion-v1"])
        self.assertEqual(self.boot()["capabilities"], ["suppliers-v1", "client-deletion-v1"])
        self.assertEqual(self.sync(device="phone")["capabilities"], ["suppliers-v1", "client-deletion-v1"])
        status = self.store.status()
        self.assertNotIn("state", status)
        self.assertNotIn("token", status)
        self.assertNotIn("pairingKey", status)

    def test_legacy_database_and_expense_shapes_remain_editable_after_restart(self):
        old = agenda(); old["expenses"] = [expense()]
        original = copy.deepcopy(old)
        initial = self.boot(old)
        self.assertEqual(old, original)
        self.assertEqual(initial["state"]["suppliers"], [])
        self.assertEqual(initial["state"]["expenses"], old["expenses"])
        self.store = Store(self.path, PAIRING)
        updated = {**old["expenses"][0], "description": "Actualizare de pe aplicația veche"}
        reply = self.sync([operation("legacy-edit", change("expenses/e1", old["expenses"][0], updated))], device="phone")
        self.assertEqual(reply["results"][0]["status"], "applied")
        self.assertEqual(reply["state"]["expenses"], [updated])
        self.assertNotIn("supplierId", reply["state"]["expenses"][0])
        self.assertNotIn("supplierName", reply["state"]["expenses"][0])
        self.assertEqual(reply["state"]["settings"], initial["state"]["settings"])
        self.assertEqual(reply["state"]["clients"], initial["state"]["clients"])

    def test_new_supplier_and_expense_commit_atomically_in_any_change_order(self):
        self.boot()
        missing = operation("missing-ref", change("expenses/e1", None, tagged_expense()))
        refused = self.sync([missing])
        self.assertEqual(refused["results"][0]["status"], "rejected")
        self.assertEqual(refused["state"]["suppliers"], [])
        self.assertEqual(refused["state"]["expenses"], [])
        bundle = operation("supplier-and-expense", change("expenses/e1", None, tagged_expense()), change("suppliers/supplier1", None, supplier()))
        accepted = self.sync([bundle], device="phone")
        self.assertEqual(accepted["results"][0]["status"], "applied")
        self.assertEqual(accepted["state"]["suppliers"], [supplier()])
        self.assertEqual(accepted["state"]["expenses"], [tagged_expense()])
        revision = accepted["serverRevision"]
        self.store = Store(self.path, PAIRING)
        retry = self.sync([bundle], device="phone")
        self.assertEqual(retry["serverRevision"], revision)
        self.assertEqual(retry["results"], accepted["results"])
        self.assertEqual(retry["state"], accepted["state"])

    def test_catalog_rename_keeps_purchase_snapshot_and_other_records(self):
        state = agenda(); state.update(suppliers=[supplier()], expenses=[tagged_expense()])
        initial = self.boot(state)
        renamed = supplier(name="Denumire actualizată")
        response = self.sync([operation("rename", change("suppliers/supplier1", supplier(), renamed))])
        self.assertEqual(response["results"][0]["status"], "applied")
        self.assertEqual(response["state"]["suppliers"], [renamed])
        self.assertEqual(response["state"]["expenses"], [tagged_expense()])
        for key in ("clients", "services", "appointments", "settings"):
            self.assertEqual(response["state"][key], initial["state"][key])
        # An old APK strips the optional fields when reading. Its stale CAS must
        # conflict, never acknowledge an update that discards the saved supplier.
        stripped = expense()
        old_update = operation("old-apk-edit", change("expenses/e1", stripped, {**stripped, "amount": 99}))
        refusal = self.sync([old_update], device="phone")
        self.assertEqual(refusal["results"][0]["status"], "conflict")
        self.assertEqual(refusal["state"]["expenses"], [tagged_expense()])

    def test_referenced_supplier_cannot_be_deleted_but_explicit_atomic_removal_can(self):
        state = agenda(); state.update(suppliers=[supplier()], expenses=[tagged_expense()])
        self.boot(state)
        response = self.sync([operation("invalid-delete", change("suppliers/supplier1", supplier(), None))])
        self.assertEqual(response["results"][0]["status"], "rejected")
        self.assertEqual(response["state"]["suppliers"], [supplier()])
        response = self.sync([operation("explicit-clear", change("suppliers/supplier1", supplier(), None), change("expenses/e1", tagged_expense(), None))])
        self.assertEqual(response["results"][0]["status"], "applied")
        self.assertEqual(response["state"]["suppliers"], [])
        self.assertEqual(response["state"]["expenses"], [])

    def test_stale_supplier_edit_does_not_block_independent_legacy_expense(self):
        state = agenda(); state["suppliers"] = [supplier()]
        self.boot(state)
        self.sync([operation("first", change("suppliers/supplier1", supplier(), supplier(name="Pe tabletă")))])
        response = self.sync([operation("stale", change("suppliers/supplier1", supplier(), supplier(name="Pe telefon"))), operation("independent", change("expenses/e1", None, expense()))], device="phone")
        self.assertEqual([x["status"] for x in response["results"]], ["conflict", "applied"])
        self.assertEqual(response["state"]["suppliers"], [supplier(name="Pe tabletă")])
        self.assertEqual(response["state"]["expenses"], [expense()])

    def test_strict_supplier_shapes_pairs_duplicates_and_snapshot_reference_validation(self):
        state = agenda(); state.update(suppliers=[supplier()], expenses=[tagged_expense()])
        cases = []
        for bad in (None, {}, "wrong", [None], [supplier(name="")], [supplier(name=" \ufeff ")], [supplier(name="💅" * 76)], [supplier("", "Valid")], [supplier("x" * 101, "Valid")], [supplier(), supplier()], [supplier(str(i)) for i in range(10001)]):
            cases.append({**copy.deepcopy(state), "suppliers": bad})
        for values in ({"supplierId": "supplier1"}, {"supplierName": "Furnizor"}, {"supplierId": None, "supplierName": "Valid"}, {"supplierId": "missing", "supplierName": "Valid"}, {"supplierId": "supplier1", "supplierName": ""}, {"supplierId": "supplier1", "supplierName": "💅" * 76}, {"supplierId": "supplier1", "supplierName": False}):
            raw = copy.deepcopy(state); raw["expenses"] = [{**expense(), **values}]; cases.append(raw)
        for index, raw in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(ValidationError): validate_state(raw)
        valid = copy.deepcopy(state)
        valid["suppliers"] = [supplier(name=" \ufeffNume actual\ufeff "), supplier("supplier2", "Nume actual")]
        valid["expenses"][0]["supplierName"] = " \ufeffNume istoric\ufeff "
        normalized = validate_state(valid)
        self.assertEqual([s["name"] for s in normalized["suppliers"]], ["Nume actual", "Nume actual"], "Duplicate names allowed for concurrent offline additions")
        self.assertEqual(normalized["expenses"][0]["supplierName"], "Nume istoric")
        self.assertEqual(valid["expenses"][0]["supplierName"], " \ufeffNume istoric\ufeff ", "Validation does not mutate input")


if __name__ == "__main__":
    unittest.main()
