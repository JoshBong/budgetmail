"""To-dos: Venmo requests add them, payments clear them.  python3 -m unittest -v"""
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ledger  # noqa: E402
import todos  # noqa: E402
from parsers import Email, Parsed, for_sender  # noqa: E402

D = date(2026, 10, 19)
BODY = ("Shiyuan Chen requests $33.00 Shiyuan Chen requests $ 33. 00 Carrier See Request ## Transaction details"
        "### Requested by @Shiyuan-Chen-3### Requested from @Jhuang314")


def venmo(subject, body=""):
    return for_sender("venmo@venmo.com").parse(Email(subject, "venmo@venmo.com", body, D))


class TodoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = ledger.connect(str(Path(self.tmp.name) / "l.db"))

    def tearDown(self):
        self.con.close(); self.tmp.cleanup()

    def pay(self, who, amt, d):
        ledger.record(self.con, "Venmo", Parsed("zelle_out", amt, "Venmo to " + who, None, d, posted=True, extra={"ref": f"{who}{amt}{d}"}), "s")

    def open_(self):
        return [t for t in todos.listing(self.con) if t["status"] == "open"]

    def test_request_email_parses_with_the_note_from_the_body_or_the_subject(self):
        p = venmo("Shiyuan Chen requests $33.00", BODY)
        self.assertEqual((p.kind, p.merchant, p.amount, p.extra["note"]), ("request", "Shiyuan Chen", 33.0, "Carrier"))
        self.assertEqual(venmo("Abhishek Narang requests $84.40 - Aau pa").extra["note"], "Aau pa")
        self.assertTrue(venmo("Reminder: Iain Kimpton requests $10.00").extra["reminder"])
        self.assertEqual(venmo("Jose Olmo canceled a request for $5.00 - Bev").kind, "request_canceled")
        self.assertEqual(venmo("Cole Rodrigues declined your request for $20.50 - bday").kind, "skip")   # your request, not a debt

    def test_reminder_does_not_duplicate_and_payment_clears_it(self):
        todos.from_request(self.con, "Iain Kimpton", 10.0, "", D)
        self.assertEqual(todos.from_request(self.con, "Iain Kimpton", 10.0, "", date(2026, 10, 22), reminder=True), "seen")
        self.assertEqual(len(self.open_()), 1)
        self.pay("Iain Kimpton", 10.0, date(2026, 10, 23))
        self.assertEqual(todos.reconcile(self.con), 1)
        self.assertEqual(self.open_(), [])

    def test_older_or_different_payment_does_not_clear_and_one_payment_clears_one_item(self):
        todos.from_request(self.con, "Connie Chong", 5.0, "a", D)
        todos.from_request(self.con, "Connie Chong", 5.0, "b", D)
        self.pay("Connie Chong", 5.0, date(2026, 10, 1))        # before the request
        self.pay("Connie Chong", 6.0, date(2026, 10, 20))       # wrong amount
        self.pay("Connie Chen", 5.0, date(2026, 10, 20))        # wrong person
        self.assertEqual(todos.reconcile(self.con), 0)
        self.pay("Connie Chong", 5.0, date(2026, 10, 21))
        self.assertEqual(todos.reconcile(self.con), 1)
        self.assertEqual(len(self.open_()), 1)

    def test_manual_item_with_a_first_name_clears_on_payment_plain_text_never_does(self):
        todos.add(self.con, "Elliott $24 dinner")
        todos.add(self.con, "call the bank")
        e = next(t for t in self.open_() if t["who"])
        self.assertEqual((e["who"], e["amount"], e["note"]), ("Elliott", 24.0, "dinner"))
        self.pay("Elliott Kalt", 24.0, date.today())
        todos.reconcile(self.con)
        self.assertEqual([t["text"] for t in self.open_()], ["call the bank"])

    def test_cancel_closes_and_move_reorders_and_dismiss_hides(self):
        a = todos.from_request(self.con, "A B", 1.0, "", D) and self.open_()[0]["id"]
        todos.from_request(self.con, "C D", 2.0, "", D)
        todos.from_request(self.con, "E F", 3.0, "", D)
        ids = [t["id"] for t in self.open_()]
        todos.move(self.con, ids[2], ids[0])
        self.assertEqual([t["id"] for t in self.open_()], [ids[2], ids[0], ids[1]])
        todos.canceled(self.con, "C D", 2.0, D)
        todos.set_status(self.con, a, "dismissed")
        self.assertEqual([t["who"] for t in self.open_()], ["E F"])
        self.assertNotIn(a, [t["id"] for t in todos.listing(self.con)])

    def test_backup_merge_newest_wins(self):
        todos.from_request(self.con, "A B", 1.0, "", D)
        other = ledger.connect(str(Path(self.tmp.name) / "o.db"))
        todos.from_request(other, "A B", 1.0, "", D); todos.from_request(other, "X Y", 9.0, "", D); other.commit()
        tid = self.open_()[0]["id"]
        other.execute("UPDATE todos SET status='dismissed', updated_at='2999-01-01' WHERE id=?", (tid,)); other.commit(); other.close()
        ledger.merge(self.con, str(Path(self.tmp.name) / "o.db"))
        self.assertEqual([t["who"] for t in self.open_()], ["X Y"])


if __name__ == "__main__":
    unittest.main()
