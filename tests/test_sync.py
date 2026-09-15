"""Backups merge instead of replace: data rows are added, hand edits go newest-wins, clears travel.  python3 -m unittest -v"""
import io
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ledger  # noqa: E402
from parsers import Parsed  # noqa: E402

import budgetmail  # noqa: E402

D = date(2026, 5, 3)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a = ledger.connect(str(Path(self.tmp.name) / "a.db"))      # say, the Pi
        self.b_path = str(Path(self.tmp.name) / "b.db")
        self.b = ledger.connect(self.b_path)                             # say, the Mac
        for con in (self.a, self.b):
            ledger.record(con, "Chase", Parsed("purchase", 12.0, "SQ *JOES", "2637", D), "s")
        self.tx = self.a.execute("SELECT id FROM transactions").fetchone()[0]

    def tearDown(self):
        self.a.close(); self.b.close(); self.tmp.cleanup()

    def merge(self):
        self.b.commit()
        return ledger.merge(self.a, self.b_path)

    def test_history_only_on_the_other_machine_is_added_and_merging_twice_changes_nothing(self):
        aid = ledger.ensure_account(self.b, "Chase", "0946")
        ledger.record_posted(self.b, "Chase", aid, Parsed("purchase", 80.0, "COSTCO", "0946", date(2026, 1, 9), posted=True))
        ledger.remember_file(self.b, "sha1", "Chase0946.pdf", aid, 1)
        ledger.record(self.a, "Chase", Parsed("purchase", 5.0, "ONLY ON A", "2637", D), "s")
        self.assertEqual(self.merge()["transactions"], 1)
        n = lambda sql: self.a.execute(sql).fetchone()[0]
        self.assertEqual((n("SELECT COUNT(*) FROM transactions"), n("SELECT COUNT(*) FROM imports")), (3, 1))
        self.assertEqual(self.merge(), {"accounts": 0, "transactions": 0, "statements": 0, "imports": 0, "edits": 0})

    def test_newer_edit_wins_in_both_directions(self):
        ledger._set_edit(self.a, "renames", self.tx, "Joe's (Pi)", "2026-09-14T10:00:00")
        ledger._set_edit(self.b, "renames", self.tx, "Joe's (Mac)", "2026-09-14T12:00:00")
        self.merge()
        self.assertEqual(ledger.names(self.a)[0][self.tx], "Joe's (Mac)")
        ledger._set_edit(self.a, "renames", self.tx, "Joe's Coffee", "2026-09-14T13:00:00")
        self.a.commit()
        ledger.merge(self.b, str(Path(self.tmp.name) / "a.db"))
        self.assertEqual(ledger.names(self.b)[0][self.tx], "Joe's Coffee")

    def test_undo_travels(self):
        ledger._set_edit(self.a, "dupes", self.tx, True, "2026-09-14T09:00:00")
        ledger._set_edit(self.b, "dupes", self.tx, True, "2026-09-14T09:00:00")
        ledger._set_edit(self.b, "dupes", self.tx, None, "2026-09-14T11:00:00")         # "not a duplicate" on the Mac
        self.merge()
        self.assertNotIn(self.tx, ledger.dupes(self.a))

    def test_older_clear_does_not_undo_a_newer_edit(self):
        ledger._set_edit(self.b, "overrides", self.tx, None, "2026-09-14T08:00:00")
        ledger._set_edit(self.a, "overrides", self.tx, "Travel", "2026-09-14T10:00:00")
        self.merge()
        self.assertEqual(ledger.overrides(self.a)[self.tx], "Travel")

    def test_edit_from_before_timestamps_loses_to_a_new_one(self):
        self.b.commit()
        old = sqlite3.connect(self.b_path)
        old.execute("INSERT INTO budgets (category, amount, updated_at) VALUES ('Shopping', 50, NULL)")
        old.commit(); old.close()
        ledger.set_budget(self.a, "Shopping", 120)
        self.merge()
        self.assertEqual(ledger.budgets(self.a)["Shopping"], 120)

    def test_restore_zip_merges_instead_of_replacing(self):
        ledger.record(self.a, "Chase", Parsed("purchase", 5.0, "ONLY ON A", "2637", D), "s")
        ledger.set_budget(self.b, "Travel", 300)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.write(self.b_path, "ledger.db")
        r = budgetmail.restore_backup(buf.getvalue(), self.a, bak=False)
        self.assertEqual((r["added"]["transactions"], r["total"]), (0, 2))
        self.assertEqual(ledger.budgets(r["con"])["Travel"], 300)


class MailBackupTests(unittest.TestCase):
    def test_last_backup_time_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "l.db")
            con = ledger.connect(path)
            send, make = budgetmail.mail.send_backup, budgetmail.make_backup
            budgetmail.mail.send_backup = lambda *a: "budgetmail backup test"
            budgetmail.make_backup = lambda: b"zip"
            try:
                budgetmail.mail_backup(con, {"gmail_user": "u", "gmail_app_password": "p"})
            finally:
                budgetmail.mail.send_backup, budgetmail.make_backup = send, make
            con.close()
            self.assertIsNotNone(ledger.get_meta(ledger.connect(path), "last_mail_backup"))


if __name__ == "__main__":
    unittest.main()
