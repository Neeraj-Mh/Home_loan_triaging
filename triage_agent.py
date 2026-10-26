"""
Home Loan Application Triage - multi-agent demo (synthetic data only).

Pipeline:  Document Agent -> Verification Agent -> Policy Agent -> Reviewer Agent
Every step writes to an append-only audit log. The final decision is always a
*recommendation*: a human underwriter signs off (human-in-the-loop).

Policy numbers below are ILLUSTRATIVE, not real lending policy.
Swap the rule-based agents for LLM calls (Claude Agent SDK / LangGraph) by
replacing the `run` method of each agent; the orchestration stays the same.
"""
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

# ----------------------------------------------------------------- policy
POLICY = {
    "buffer_rate": 3.0,          # serviceability buffer over product rate (%)
    "term_years": 30,
    "hem_single": 2800,          # illustrative minimum monthly living expense
    "near_miss_band": 250,       # shortfall within this ($/mo) is referred, not auto-declined
    "max_lvr_no_lmi": 80.0,
    "income_mismatch_high": 0.10,
    "gambling_flag_total": 500,
    "fhbg": {"max_income": 125_000, "min_deposit_pct": 5.0, "price_cap": 800_000},
    "doc_ref": {
        "serviceability": "Credit Policy 4.2 (Serviceability)",
        "lvr": "Credit Policy 5.1 (LVR & LMI)",
        "fhbg": "Scheme Guideline FHBG-3 (First Home Guarantee)",
        "income": "Credit Policy 3.4 (Income Verification)",
        "conduct": "Credit Policy 6.3 (Account Conduct)",
    },
}

# ------------------------------------------------------- synthetic dataset
def bank(employer, salary_credits, debits):
    rows = []
    for m, amt in zip(("2026-07", "2026-08", "2026-09"), salary_credits):
        rows.append((f"{m}-28", f"SALARY {employer.upper()}", amt))
        for d, desc, a in debits:
            rows.append((f"{m}-{d:02d}", desc, -a))
    return rows

APPLICATIONS = [
    {
        "id": "APP-1001", "name": "Priya Nair", "first_home_buyer": False,
        "loan": 640_000, "property_value": 800_000, "rate": 6.19,
        "declared_expenses": 3100, "existing_debt_repay": 0,
        "payslip": "PAYSLIP\nEmployer: Harbour Analytics Pty Ltd\nEmployee: Priya Nair\n"
                   "Gross annual salary: $145,000\nNet pay (monthly): $8,290.00",
        "bank": bank("Harbour Analytics", [8290, 8290, 8290],
                     [(3, "WOOLWORTHS", 650), (5, "RENT PAYMENT", 1900), (9, "ENERGY AUSTRALIA", 180),
                      (14, "COLES", 420), (20, "NETFLIX/SPOTIFY", 40)]),
    },
    {
        "id": "APP-1002", "name": "Rahul Verma", "first_home_buyer": False,
        "loan": 600_000, "property_value": 780_000, "rate": 6.29,
        "declared_expenses": 3200, "existing_debt_repay": 0,
        "payslip": "PAYSLIP\nEmployer: Southern Freight Co\nEmployee: Rahul Verma\n"
                   "Gross annual salary: $175,000\nNet pay (monthly): $10,200.00",
        "bank": bank("Southern Freight", [8900, 8900, 8900],
                     [(2, "RENT PAYMENT", 2100), (6, "SPORTSBET", 400), (11, "WOOLWORTHS", 700),
                      (17, "TAB WAGER", 250), (21, "TELSTRA", 90)]),
    },
    {
        "id": "APP-1003", "name": "Meera Iyer", "first_home_buyer": True,
        "loan": 494_000, "property_value": 520_000, "rate": 6.29,
        "declared_expenses": 2600, "existing_debt_repay": 0,
        "payslip": "PAYSLIP\nEmployer: Bluegum Health\nEmployee: Meera Iyer\n"
                   "Gross annual salary: $118,000\nNet pay (monthly): $7,500.00",
        "bank": bank("Bluegum Health", [7500, 7500, 7500],
                     [(4, "RENT PAYMENT", 1700), (8, "ALDI", 480), (15, "OPTUS", 70), (19, "GYM", 60)]),
    },
    {
        "id": "APP-1004", "name": "Sam Carter", "first_home_buyer": False,
        "loan": 650_000, "property_value": 720_000, "rate": 6.39,
        "declared_expenses": 2900, "existing_debt_repay": 600,
        "payslip": "PAYSLIP\nEmployer: Ridge Constructions\nEmployee: Sam Carter\n"
                   "Gross annual salary: $85,000\nNet pay (monthly): $5,400.00",
        "bank": bank("Ridge Constructions", [5400, 5400, 5400],
                     [(3, "RENT PAYMENT", 1500), (7, "CAR LOAN", 600), (12, "WOOLWORTHS", 600)]),
    },
]

# --------------------------------------------------------------- audit log
@dataclass
class Audit:
    entries: list = field(default_factory=list)

    def log(self, app_id, agent, action, detail):
        self.entries.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "app": app_id, "agent": agent, "action": action, "detail": detail,
        })

# ------------------------------------------------------------------ agents
def pmt(principal, annual_rate_pct, years):
    r = annual_rate_pct / 100 / 12
    n = years * 12
    return principal * r / (1 - (1 + r) ** -n)


class DocumentAgent:
    name = "DocumentAgent"

    def run(self, app, audit):
        text = app["payslip"]
        employer = re.search(r"Employer:\s*(.+)", text).group(1).strip()
        gross = int(re.search(r"Gross annual salary:\s*\$([\d,]+)", text).group(1).replace(",", ""))
        net = float(re.search(r"Net pay \(monthly\):\s*\$([\d,\.]+)", text).group(1).replace(",", ""))
        credits = [a for _, d, a in app["bank"] if d.startswith("SALARY") and a > 0]
        debits = [(d, -a) for _, d, a in app["bank"] if a < 0]
        facts = {"employer": employer, "stated_gross": gross, "stated_net_monthly": net,
                 "bank_salary_credits": credits, "bank_debits": debits}
        audit.log(app["id"], self.name, "extract", {k: facts[k] for k in ("employer", "stated_gross", "stated_net_monthly")})
        return facts


class VerificationAgent:
    name = "VerificationAgent"
    GAMBLING = ("SPORTSBET", "TAB ", "CASINO", "BET365", "WAGER")

    def run(self, app, facts, audit):
        flags = []
        verified_net = statistics.mean(facts["bank_salary_credits"])
        dev = round((verified_net - facts["stated_net_monthly"]) / facts["stated_net_monthly"], 4)
        if abs(dev) > POLICY["income_mismatch_high"]:
            flags.append({"severity": "HIGH", "code": "INCOME_MISMATCH", "ref": POLICY["doc_ref"]["income"],
                          "msg": f"Payslip net ${facts['stated_net_monthly']:,.0f}/mo vs bank-evidenced "
                                 f"${verified_net:,.0f}/mo ({dev:+.1%})."})
        gamble = sum(a for d, a in facts["bank_debits"] if any(k in d for k in self.GAMBLING))
        if gamble > POLICY["gambling_flag_total"]:
            flags.append({"severity": "MEDIUM", "code": "GAMBLING_ACTIVITY", "ref": POLICY["doc_ref"]["conduct"],
                          "msg": f"${gamble:,.0f} of gambling transactions across 3 months."})
        result = {"verified_net_monthly": round(verified_net, 2), "deviation": round(dev, 4), "flags": flags}
        audit.log(app["id"], self.name, "verify", {"verified_net": result["verified_net_monthly"],
                                                   "flags": [f["code"] for f in flags]})
        return result


class PolicyAgent:
    name = "PolicyAgent"

    def run(self, app, verification, audit):
        findings, hard_fail = [], False
        lvr = app["loan"] / app["property_value"] * 100
        deposit_pct = 100 - lvr

        # serviceability on VERIFIED income, stressed rate
        assessed_rate = app["rate"] + POLICY["buffer_rate"]
        repay = pmt(app["loan"], assessed_rate, POLICY["term_years"])
        expenses = max(app["declared_expenses"], POLICY["hem_single"])
        surplus = verification["verified_net_monthly"] - expenses - repay - app["existing_debt_repay"]
        ok = surplus >= 0
        near_miss = (not ok) and surplus >= -POLICY["near_miss_band"]
        hard_fail |= (not ok) and not near_miss
        findings.append({"rule": "serviceability", "ref": POLICY["doc_ref"]["serviceability"], "pass": ok,
                         "msg": f"Assessed at {assessed_rate:.2f}%: repayment ${repay:,.0f}/mo, "
                                f"expenses ${expenses:,.0f}, debts ${app['existing_debt_repay']:,.0f} -> "
                                f"monthly surplus ${surplus:,.0f}."})

        # scheme eligibility (illustrative First Home Guarantee)
        scheme = POLICY["fhbg"]
        eligible = (app["first_home_buyer"] and app["property_value"] <= scheme["price_cap"]
                    and facts_gross(app) <= scheme["max_income"] and deposit_pct >= scheme["min_deposit_pct"])
        findings.append({"rule": "fhbg", "ref": POLICY["doc_ref"]["fhbg"], "pass": eligible,
                         "msg": "First Home Guarantee eligible (LMI waived)." if eligible else
                                "Not eligible for First Home Guarantee."})

        # LVR / LMI
        lmi = lvr > POLICY["max_lvr_no_lmi"] and not eligible
        findings.append({"rule": "lvr", "ref": POLICY["doc_ref"]["lvr"], "pass": True,
                         "msg": f"LVR {lvr:.1f}%. " + ("LMI required." if lmi else "No LMI required.")})
        result = {"lvr": round(lvr, 1), "surplus": round(surplus), "scheme_eligible": eligible,
                  "lmi_required": lmi, "hard_fail": hard_fail, "near_miss": near_miss, "findings": findings}
        audit.log(app["id"], self.name, "policy_check", {"lvr": result["lvr"], "surplus": result["surplus"],
                                                          "scheme": eligible, "hard_fail": hard_fail})
        return result


def facts_gross(app):
    return int(re.search(r"Gross annual salary:\s*\$([\d,]+)", app["payslip"]).group(1).replace(",", ""))


class ReviewerAgent:
    name = "ReviewerAgent"

    def run(self, app, verification, policy, audit):
        flags = verification["flags"]
        high = [f for f in flags if f["severity"] == "HIGH"]
        if policy["hard_fail"]:
            decision, conf, queue = "DECLINE_RECOMMENDED", 0.90, "Underwriter (confirm decline)"
        elif high or flags or policy["near_miss"]:
            decision, conf, queue = "REFER_TO_UNDERWRITER", 0.55, "Senior underwriter (manual review)"
        else:
            decision, conf, queue = "APPROVE_RECOMMENDED", 0.92, "Underwriter (fast-track sign-off)"

        lines = [f"# Decision memo - {app['id']} ({app['name']})", "",
                 f"**Recommendation:** {decision}  |  **Confidence:** {conf:.0%}  |  **Routed to:** {queue}", "",
                 f"Loan ${app['loan']:,} on property ${app['property_value']:,} (LVR {policy['lvr']}%).", "",
                 "## Evidence"]
        lines += [f"- [{f['ref']}] {'PASS' if f['pass'] else 'FAIL'} - {f['msg']}" for f in policy["findings"]]
        if flags:
            lines += ["", "## Verification flags"]
            lines += [f"- **{f['severity']}** [{f['ref']}] {f['msg']}" for f in flags]
        lines += ["", "_AI recommendation only. Final credit decision rests with a human underwriter._"]
        memo = "\n".join(lines)
        audit.log(app["id"], self.name, "decision", {"decision": decision, "confidence": conf, "queue": queue})
        return {"decision": decision, "confidence": conf, "queue": queue, "memo": memo}


# ------------------------------------------------------------ orchestrator
def run_pipeline(apps):
    audit = Audit()
    doc, ver, pol, rev = DocumentAgent(), VerificationAgent(), PolicyAgent(), ReviewerAgent()
    results = []
    for app in apps:
        facts = doc.run(app, audit)
        v = ver.run(app, facts, audit)
        p = pol.run(app, v, audit)
        r = rev.run(app, v, p, audit)
        (OUT / f"{app['id']}_memo.md").write_text(r["memo"])
        results.append((app, v, p, r))
    (OUT / "audit_log.jsonl").write_text("\n".join(json.dumps(e) for e in audit.entries))
    return results, audit


def main():
    results, audit = run_pipeline(APPLICATIONS)
    print("=" * 78)
    print("HOME LOAN TRIAGE AGENT - demo run on 4 synthetic applications")
    print("=" * 78)
    for app, v, p, r in results:
        print(f"\n{app['id']}  {app['name']:<12} loan ${app['loan']:>9,}  LVR {p['lvr']:>5}%")
        print(f"  verified net income : ${v['verified_net_monthly']:,.0f}/mo ({v['deviation']:+.1%} vs payslip)")
        print(f"  monthly surplus     : ${p['surplus']:,}   scheme: {'FHBG' if p['scheme_eligible'] else '-'}   "
              f"LMI: {'yes' if p['lmi_required'] else 'no'}")
        for f in v["flags"]:
            print(f"  FLAG [{f['severity']}] {f['code']}: {f['msg']}")
        print(f"  => {r['decision']}  (confidence {r['confidence']:.0%}) -> {r['queue']}")
    print("\n" + "-" * 78)
    print(f"Audit log: {len(audit.entries)} entries -> {OUT/'audit_log.jsonl'}")
    print(f"Memos    : {OUT}/APP-*_memo.md")


if __name__ == "__main__":
    sys.exit(main())
