"""Optional cross-runtime checks when the Android source and Node are available."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validation import validate_state

APP = Path(__file__).resolve().parents[2] / "salon-apk/app/src/main/assets/app.js"


@unittest.skipUnless(shutil.which("node") and APP.is_file(), "Node and the sibling Android source are needed only for parity checks")
class SchemaParityTests(unittest.TestCase):
    def js(self, body):
        code = "require(process.argv[1]); const c=global.SalonCore; " + body
        return json.loads(subprocess.check_output(["node", "-e", code, str(APP)], text=True, encoding="utf-8"))

    def test_real_android_legacy_and_current_snapshots_match_python_normalization(self):
        states = self.js("""
          const old=c.createDemo('2026-10-31'), current=c.createDemo('2026-10-31');
          current.settings.theme='dark'; current.settings.workspace.salonName='Salon personalizat';
          current.settings.agendaRanges=[{id:'r1',effectiveFrom:'2020-01-01',start:'08:30',lastStart:'17:45',createdAt:'2026-10-31T10:00:00.000Z'}];
          current.services[0].duration=77; current.appointments[0].status='completed';
          current.appointments[0].color='#17a9Ef';
          current.appointments[0]=c.recordAppointmentCorrection(current.appointments[0],{...current.appointments[0],price:150},'2026-10-31T10:00:00.000Z','Corecție');
          current.expenses=[{id:'e1',date:'2026-10-31',amount:12.34,description:'Consumabile',createdAt:'',updatedAt:''}];
          console.log(JSON.stringify([c.validateState(old),c.validateState(current)]));
        """)
        for state in states:
            expected = {**state, "expenses": state.get("expenses", [])}
            self.assertEqual(validate_state(state), expected)

    def test_unicode_whitespace_and_phone_digits_normalize_like_the_android_validator(self):
        raw, expected = self.js("""
          const s=c.createDemo('2026-10-31');
          s.clients[0].name='\\ufeffAna\\ufeff'; s.clients[0].phone='٠٧٢٢';
          console.log(JSON.stringify([s,c.validateState(s)]));
        """)
        self.assertEqual(validate_state(raw), {**expected, "expenses": []})

    def test_js_utf16_length_and_ascii_clock_rejections_are_preserved(self):
        cases = self.js("""
          const long=c.createDemo('2026-10-31');long.clients[0].name='💅'.repeat(76);
          const clock=c.createDemo('2026-10-31');clock.appointments[0].start='10:١٠';
          console.log(JSON.stringify([long,clock].map(s=>{let rejected=false;try{c.validateState(s)}catch(e){rejected=true}return {s,rejected}})));
        """)
        for case in cases:
            self.assertTrue(case["rejected"])
            with self.assertRaises(ValueError): validate_state(case["s"])
