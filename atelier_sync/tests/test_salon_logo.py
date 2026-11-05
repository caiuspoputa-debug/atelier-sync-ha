"""Shared salon identity survives backup, synchronization and mixed-version edits."""
import base64
import copy
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Store
from validation import ValidationError, default_workspace, valid_salon_logo, validate_state
from test_server import PAIRING, agenda, change, expense, operation


def chunk(kind, data=b""):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width=2, height=2, extras=()):
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    pixels = b"\0" * (max(1, width) * max(1, height) * 4 + max(1, height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + b"".join(extras) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND")


def data_url(data):
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


LOGO = data_url(png())
APP = next((p / "salon-apk/app/src/main/assets/app.js" for p in Path(__file__).resolve().parents if (p / "salon-apk/app/src/main/assets/app.js").is_file()), None)


class SalonLogoTests(unittest.TestCase):
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

    def boot(self, logo="absent"):
        state = agenda()
        if logo != "absent":
            state["settings"]["workspace"]["logoData"] = logo
        return self.sync(bootstrap=state, bootstrapId="salon-logo-bootstrap")

    def test_capability_is_advertised_on_status_register_bootstrap_and_pull(self):
        for reply in [self.store.status(), self.master, self.phone, self.boot(), self.sync(device="phone")]:
            self.assertIn("salon-logo-v1", reply["capabilities"])

    def test_legacy_absence_is_preserved_without_default_or_implicit_write(self):
        self.assertNotIn("logoData", default_workspace())
        state = agenda()
        self.assertNotIn("logoData", validate_state(state)["settings"]["workspace"])
        result = self.boot()
        self.assertNotIn("logoData", result["state"]["settings"]["workspace"])
        self.assertEqual(self.sync(device="phone")["state"], result["state"])
        self.assertEqual(self.sync()["serverRevision"], 1)

    def test_strict_validation_accepts_png_and_explicit_null_reset(self):
        for logo in [LOGO, None]:
            state = agenda(); state["settings"]["workspace"]["logoData"] = logo
            original = copy.deepcopy(state)
            self.assertEqual(validate_state(state)["settings"]["workspace"]["logoData"], logo)
            self.assertEqual(state, original)

    def test_validator_rejects_remote_svg_noncanonical_oversized_and_corrupt_data(self):
        corrupted = bytearray(png()); corrupted[29] ^= 1
        remaining = 524288 - len(png()) - 12
        valid_max = data_url(png(extras=[chunk(b"tEXt", b"A" * remaining)]))
        self.assertTrue(valid_salon_logo(valid_max))
        bad = [False, 0, [], {}, "", "https://example.com/logo.png", "file:///logo.png",
               "data:image/svg+xml;base64,PHN2Zy8+", "data:image/jpeg;base64,/9j/", LOGO + "=",
               LOGO.replace("base64,", "base64,\n"), data_url(bytes(corrupted)), data_url(png()[:33]),
               data_url(png() + b"trailing"), data_url(png(0, 2)), data_url(png(2, 0)),
               data_url(png(513, 2)), data_url(png(2, 513)),
               data_url(png(extras=[chunk(b"tEXt", b"A" * (remaining + 1))])),
               data_url(png(extras=[chunk(b"acTL", b"\0" * 4)]))]
        for logo in bad:
            with self.subTest(logo_type=type(logo).__name__):
                self.assertFalse(valid_salon_logo(logo))
                state = agenda(); state["settings"]["workspace"]["logoData"] = logo
                with self.assertRaises(ValidationError): validate_state(state)
        self.assertTrue(valid_salon_logo(data_url(png(512, 512))))

    def test_bootstrap_png_and_sqlite_backup_retain_identity_and_business_records(self):
        original = self.boot(LOGO)
        self.assertEqual(original["state"]["settings"]["workspace"]["logoData"], LOGO)
        backup = Path(self.tmp.name) / "backup.sqlite3"
        self.store.backup(backup)
        self.store = Store(backup, PAIRING)
        self.assertEqual(self.sync(device="phone")["state"], original["state"])

    def test_logo_change_reaches_phone_with_existing_clients_services_and_prices_unchanged(self):
        original = self.boot()["state"]
        after = copy.deepcopy(original["settings"]); after["workspace"]["logoData"] = LOGO
        result = self.sync([operation("set-logo", change("settings", original["settings"], after))])
        self.assertEqual(result["results"][0]["status"], "applied")
        for kind in ["clients", "services", "appointments", "expenses", "suppliers"]:
            self.assertEqual(result["state"][kind], original[kind])
        self.assertEqual(self.sync(device="phone")["state"], result["state"])
        reset = copy.deepcopy(after); reset["workspace"]["logoData"] = None
        result = self.sync([operation("reset-logo", change("settings", after, reset))], device="phone")
        self.assertEqual(result["results"][0]["status"], "applied")
        self.assertIsNone(self.sync()["state"]["settings"]["workspace"]["logoData"])

    def test_offline_logo_retry_is_idempotent_and_competing_settings_get_conflict(self):
        original = self.boot()["state"]
        after = copy.deepcopy(original["settings"]); after["workspace"]["logoData"] = LOGO
        edit = operation("offline-logo", change("settings", original["settings"], after))
        first = self.sync([edit]); retry = self.sync([edit])
        self.assertEqual(retry["results"], first["results"])
        self.assertEqual(retry["serverRevision"], first["serverRevision"])
        competing = copy.deepcopy(original["settings"]); competing["workspace"]["salonName"] = "Alt nume"
        result = self.sync([operation("competing", change("settings", original["settings"], competing))], device="phone")
        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(result["state"], first["state"])

    def test_legacy_stripped_before_gets_conflict_without_losing_logo(self):
        original = self.boot(LOGO)["state"]
        old = copy.deepcopy(original["settings"]); del old["workspace"]["logoData"]
        after = copy.deepcopy(old); after["workspace"]["salonName"] = "Telefon vechi"
        result = self.sync([operation("old-stripped", change("settings", old, after))], device="phone")
        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(result["state"], original)

    def test_exact_before_with_after_stripped_is_atomically_rejected_including_null_reset(self):
        for logo in [LOGO, None]:
            with self.subTest(reset=logo is None):
                # A separate store isolates bootstrap and operation receipts for each case.
                if logo is None:
                    self.store = Store(Path(self.tmp.name) / "null.sqlite3", PAIRING)
                    self.master = self.register("tablet", "master"); self.phone = self.register("phone", "secondary")
                original = self.boot(logo)["state"]
                stripped = copy.deepcopy(original["settings"]); del stripped["workspace"]["logoData"]
                result = self.sync([operation("old-exact", change("settings", original["settings"], stripped), change("expenses/e1", None, expense()))], device="phone")
                self.assertEqual(result["results"][0]["status"], "rejected")
                self.assertIn("Logo-ul", result["results"][0]["reason"])
                self.assertEqual(result["state"], original)
                self.assertEqual(result["serverRevision"], 1)

    def test_ordinary_legacy_bookings_continue_with_logo_and_reset_present(self):
        original = self.boot(LOGO)["state"]
        client = copy.deepcopy(original["clients"][0]); client["name"] = "Nume editat"
        result = self.sync([operation("ordinary-old", change("clients/c1", original["clients"][0], client))], device="phone")
        self.assertEqual(result["results"][0]["status"], "applied")
        self.assertEqual(result["state"]["settings"]["workspace"]["logoData"], LOGO)

    @unittest.skipUnless(shutil.which("node") and APP, "Node and Salon source needed for schema parity")
    def test_actual_android_and_server_have_logo_backup_schema_parity(self):
        bad_crc = bytearray(png()); bad_crc[29] ^= 1
        cases = []
        for value in ["absent", LOGO, None, False, "https://bad.invalid/", data_url(png(512, 512)), data_url(png(513, 1)), data_url(bytes(bad_crc))]:
            state = agenda()
            if value != "absent": state["settings"]["workspace"]["logoData"] = value
            cases.append(state)
        script = "require(process.argv[1]);const fs=require('fs'),c=global.SalonCore;console.log(JSON.stringify(JSON.parse(fs.readFileSync(0,'utf8')).map(s=>{try{return {valid:true,state:c.validateState(s)}}catch(_){return {valid:false}}})));"
        result = json.loads(subprocess.check_output(["node", "-e", script, str(APP)], input=json.dumps(cases), text=True, encoding="utf-8"))
        for state, js in zip(cases, result):
            try:
                py = validate_state(state)
            except ValidationError:
                self.assertFalse(js["valid"])
            else:
                self.assertTrue(js["valid"])
                self.assertEqual(py, {**js["state"], "suppliers": js["state"].get("suppliers", []), "expenses": js["state"].get("expenses", [])})


if __name__ == "__main__":
    unittest.main()
