import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import triage_agent as t  # noqa: E402


class TriageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        results, audit = t.run_pipeline(t.APPLICATIONS)
        cls.by_id = {app["id"]: (v, p, r) for app, v, p, r in results}
        cls.audit = audit

    def decision(self, app_id):
        return self.by_id[app_id][2]["decision"]

    def test_clean_first_home_buyer_is_approved(self):
        self.assertEqual(self.decision("APP-1003"), "APPROVE_RECOMMENDED")
        self.assertTrue(self.by_id["APP-1003"][1]["scheme_eligible"])

    def test_income_mismatch_and_gambling_are_flagged_and_referred(self):
        v, _, r = self.by_id["APP-1002"]
        codes = {f["code"] for f in v["flags"]}
        self.assertEqual(codes, {"INCOME_MISMATCH", "GAMBLING_ACTIVITY"})
        self.assertEqual(r["decision"], "REFER_TO_UNDERWRITER")

    def test_small_shortfall_is_referred_not_declined(self):
        _, p, r = self.by_id["APP-1001"]
        self.assertLess(p["surplus"], 0)
        self.assertTrue(p["near_miss"])
        self.assertEqual(r["decision"], "REFER_TO_UNDERWRITER")

    def test_large_shortfall_is_declined(self):
        self.assertEqual(self.decision("APP-1004"), "DECLINE_RECOMMENDED")

    def test_every_application_is_audited_by_all_four_agents(self):
        agents = {e["agent"] for e in self.audit.entries}
        self.assertEqual(agents, {"DocumentAgent", "VerificationAgent", "PolicyAgent", "ReviewerAgent"})
        self.assertEqual(len(self.audit.entries), 4 * len(t.APPLICATIONS))

    def test_memo_always_states_human_signoff(self):
        for _, _, r in self.by_id.values():
            self.assertIn("human underwriter", r["memo"])

    def test_repayment_formula(self):
        # 100k at 6% over 30y is about $599.55/month
        self.assertAlmostEqual(t.pmt(100_000, 6.0, 30), 599.55, places=1)


if __name__ == "__main__":
    unittest.main()
