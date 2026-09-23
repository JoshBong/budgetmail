"""The local classifier only fills what everything else left as Other, decides once per merchant, and never outranks a
drag, a rule or the bank. Uses an injected predict() so the suite needs no torch.  python3 -m unittest -v"""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import classify  # noqa: E402
import ledger  # noqa: E402
import report  # noqa: E402
from parsers import Parsed  # noqa: E402

D = date(2026, 9, 20)


def fake(answers: dict):
    """A predict() that looks the merchant text up in a table: text → (category, score)."""
    def predict(texts, cats):
        return [answers.get(t, ("", 0.0)) for t in texts]
    return predict


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.con = ledger.connect(":memory:")
        self.cfg = {"classifier": {}}
        for m in ("CURSOR, AI POWERED IDE", "PRESIDENT NEW TAIPEI", "NETFLIX.COM", "SQ *RAMEN ISHIDA"):
            ledger.record(self.con, "Chase", Parsed("purchase", 10.0, m, "2637", D), "s")
        self.con.execute("UPDATE transactions SET category='Food & Drink' WHERE description='SQ *RAMEN ISHIDA'")   # the bank knew

    def cats(self):
        cl = report.Classifier(self.con)
        return {r["description"]: cl.of(r)[0] for r in self.con.execute("SELECT * FROM transactions")}

    def test_only_other_is_asked_and_only_confident_answers_stick(self):
        cands = classify.candidates(self.con, report.Classifier(self.con))
        self.assertEqual(sorted(cands.values()), ["CURSOR, AI POWERED IDE", "PRESIDENT NEW TAIPEI"])    # NETFLIX: rule; RAMEN: bank
        r = classify.run(self.con, self.cfg, predict=fake({"CURSOR, AI POWERED IDE": ("Bills & Subscriptions", 0.91),
                                                            "PRESIDENT NEW TAIPEI": ("Other", 0.0)}))
        self.assertEqual((r["classified"], r["abstained"]), (1, 1))
        c = self.cats()
        self.assertEqual(c["CURSOR, AI POWERED IDE"], "Bills & Subscriptions")
        self.assertEqual(c["PRESIDENT NEW TAIPEI"], "Other")
        self.assertEqual(c["NETFLIX.COM"], "Bills & Subscriptions")
        self.assertEqual(c["SQ *RAMEN ISHIDA"], "Food & Dining")

    def test_decided_once_abstentions_retry_after_30_days(self):
        classify.run(self.con, self.cfg, predict=fake({"CURSOR, AI POWERED IDE": ("Bills & Subscriptions", 0.9)}))
        self.assertEqual(classify.candidates(self.con, report.Classifier(self.con)), {})               # nothing left to ask
        old = (date.today() - timedelta(days=classify.RETRY_DAYS + 1)).isoformat()
        self.con.execute("UPDATE merchant_model SET tried_at=? WHERE category IS NULL", (old,))
        self.assertEqual(list(classify.candidates(self.con, report.Classifier(self.con)).values()), ["PRESIDENT NEW TAIPEI"])

    def test_a_drag_beats_the_model_and_marks_it_not_auto(self):
        classify.run(self.con, self.cfg, predict=fake({"CURSOR, AI POWERED IDE": ("Bills & Subscriptions", 0.9)}))
        row = self.con.execute("SELECT * FROM transactions WHERE description='CURSOR, AI POWERED IDE'").fetchone()
        self.assertTrue(report.Classifier(self.con).auto(row))
        ledger.set_merchant_cat(self.con, report.merchant_key(row), "Shopping")
        cl = report.Classifier(self.con)
        self.assertEqual(cl.of(row)[0], "Shopping")
        self.assertFalse(cl.auto(row))

    def test_labels_follow_the_category_list(self):
        seen = {}
        def predict(texts, cats):
            seen["cats"] = cats
            return [("", 0.0)] * len(texts)
        classify.run(self.con, self.cfg, predict=predict)
        self.assertEqual(seen["cats"], ["Food & Dining", "Shopping", "Travel", "Transport", "Bills & Subscriptions", "People"])
        p = classify.prompt("LAWSON", seen["cats"])
        self.assertIn("Food & Dining = restaurants", p)
        self.assertIn("Merchant: LAWSON", p)

    def test_disabled_and_missing_model_are_no_ops(self):
        self.assertEqual(classify.run(self.con, {"classifier": {"enabled": False}}), {})
        if not classify.available({"classifier": {"url": "http://127.0.0.1:1"}}):
            self.assertEqual(classify.run(self.con, {"classifier": {"url": "http://127.0.0.1:1"}}), {})
        self.assertEqual(self.cats()["CURSOR, AI POWERED IDE"], "Other")


if __name__ == "__main__":
    unittest.main()
