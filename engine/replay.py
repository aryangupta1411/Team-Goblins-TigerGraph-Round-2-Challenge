"""310-case deterministic replay of labelled closed cases — PLAN §4.10 item 2.

Samples (seeded, reproducible):
  100 cleared cases replayed as risk_score triggers          (flagged = the alert transaction)
  150 confirmed_fraud cases replayed as customer_report      (flagged = first_fraud_txn_id)
   30 confirmed_fraud cases replayed as risk_score triggers where the first fraud txn had risk >= 0.5
   30 cleared cases replayed as SYNTHETIC customer disputes  (the R7 / uncertain gate must not block them)

Scorer probabilities for Jul-Oct rows are the ETL's month-wise out-of-fold cms_p (etl/cms_train.py step 3,
`txn_feat.cms_fold = oof-2016-MM`), so the flagged transaction's own label never leaks into its score; the
calibrator is data/models/isotonic.json (D7). Case-level memory (prior closed cases) is time-boxed
to opened_at by the facts layer and the replayed case itself is stripped from every case list
(strip_self: with opened_at == as_of it would otherwise be its own "prior" case); DeviceProfile.n_fraud_cases
and Card.ring_id are all-time attributes (as in the graph) — the one known optimism of this replay.

Metrics: pattern accuracy, SAR decision accuracy, episode Jaccard, verdict accuracy per trigger,
Brier / ECE of the final probability, action-set F1 vs actions_taken per trigger, synthetic disputes
blocked in initial (target 0). Writes out/replay_metrics.json and out/replay_rows.csv.

Run:  python -m engine.replay           (~2 min)
"""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd

from engine import config
from engine.facts_duckdb import DuckFacts
from engine.run_cases import run_one
from engine.types import CaseContext

SEED = 7


def sample_cases(fx: DuckFacts) -> list[dict]:
    cc = fx.q("""SELECT c.case_id, c.customer_id, c.card_id, c.opened_at, c.outcome, c.pattern, c.first_fraud_txn_id, c.txn_ids,
                        c.exposure_usd, c.report_filed, c.actions_taken, t.risk_score first_risk
                 FROM cc c JOIN txc t ON t.TransactionID = CASE WHEN coalesce(c.first_fraud_txn_id, '') <> '' THEN c.first_fraud_txn_id::BIGINT
                                                                ELSE string_split(c.txn_ids, '|')[1]::BIGINT END
                 ORDER BY c.case_id""")          # ORDER BY: DuckDB returns rows in a non-deterministic order, which made the seeded sample drift between runs
    rng = np.random.default_rng(SEED)
    cleared = cc[cc.outcome == "cleared"].reset_index(drop=True)
    fraud = cc[cc.outcome == "confirmed_fraud"].reset_index(drop=True)
    fraud_hi = fraud[fraud.first_risk >= 0.5].reset_index(drop=True)
    def pick(df, n):
        return df.iloc[rng.choice(len(df), size=min(n, len(df)), replace=False)]

    groups = [("cleared_as_risk_score", pick(cleared, 130).iloc[:100], "risk_score"),
              ("fraud_as_dispute", pick(fraud, 150), "customer_report"),
              ("fraud_as_risk_score", pick(fraud_hi, 30), "risk_score"),
              ("cleared_as_synthetic_dispute", pick(cleared, 130).iloc[100:130], "customer_report")]
    out = []
    for name, df, trig in groups:
        for r in df.itertuples():
            ff = r.first_fraud_txn_id if isinstance(r.first_fraud_txn_id, str) else ""
            flagged = ff if ff and ff != "nan" else r.txn_ids.split("|")[0]
            out.append({"group": name, "trigger": trig, "case_id": r.case_id, "customer_id": r.customer_id, "card_id": r.card_id,
                        "opened_at": r.opened_at.strftime("%Y-%m-%d %H:%M:%S"), "flagged": str(flagged), "risk": float(r.first_risk),
                        "outcome": r.outcome, "pattern": r.pattern, "txn_ids": [str(x) for x in r.txn_ids.split("|")],
                        "report_filed": bool(r.report_filed), "actions_taken": r.actions_taken.split("|"), "exposure": float(r.exposure_usd)})
    return out


def strip_self(facts: dict, case_id: str) -> dict:
    """A replayed closed case has opened_at == as_of, so the time-boxed memory queries (opened_at <= as_of) return the
    case itself as a "prior" case — its own label would leak into the recent-case bump, the cleared precedent and
    similar_prior_cases. Remove it from every case list before scoring."""
    for key, sub in (("prior_cases_for_customer", "closed_cases"), ("card_profile", "prior_closed_cases"), ("similar_prior_cases", "cases"),
                     ("device_neighbors", "closed_cases"), ("ring_profile", "closed_cases")):
        d = facts.get(key) or {}
        if isinstance(d.get(sub), list):
            d[sub] = [c for c in d[sub] if c.get("id") != case_id]
    return facts


def f1(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    tp = len(a & b)
    return 0.0 if tp == 0 else 2 * tp / (len(a) + len(b))


def ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    tot = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p > lo) & (p <= hi) if lo > 0 else (p >= lo) & (p <= hi)
        if m.any():
            tot += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(tot)


def main() -> None:
    t0 = time.time()
    fx = DuckFacts()
    cases = sample_cases(fx)
    rows = []
    for i, c in enumerate(cases):
        txt = (f"Real-time model scored transaction {c['flagged']} at {c['risk']:.2f}. Review and decide." if c["trigger"] == "risk_score"
               else f"Customer {c['customer_id']} message: 'I never made this purchase. Please check my card.' Refers to {c['flagged']}.")
        ctx = CaseContext(c["case_id"], c["trigger"], txt, c["flagged"], c["card_id"], c["customer_id"], c["opened_at"],
                          c["risk"] if c["trigger"] == "risk_score" else None)
        try:
            r = run_one(ctx, fx, strip_self(fx.collect(ctx), c["case_id"]))
        except Exception as ex:   # keep going; report failures
            rows.append({**c, "error": repr(ex)[:200]})
            print("FAILED", c["case_id"], c["group"], repr(ex)[:160], flush=True)
            continue
        a = r["answer"]
        ini = {x["action"] for x in a["next_best_actions"]["initial"]}
        fin = {x["action"] for x in a["next_best_actions"]["final"]}
        aff = set(a["case"]["affected_txn_ids"])
        truth = set(c["txn_ids"]) if c["outcome"] == "confirmed_fraud" else set()
        rows.append({**c, "error": "", "verdict": a["case"]["verdict"], "p": a["case"]["fraud_probability"], "p_pre": r["sc_pre"].p_engine,
                     "cms": r["sc_pre"].cms_p, "engine_pattern": a["case"]["pattern"], "sar": a["sar"]["file"],
                     "jaccard": (len(aff & truth) / len(aff | truth)) if (aff | truth) else 1.0,
                     "initial": sorted(ini), "final": sorted(fin), "f1_final": f1(fin, set(c["actions_taken"])),
                     "blocked_initial": "BLOCK_CARD" in ini, "blocked_final": "BLOCK_CARD" in fin,
                     "request": r["request"]["type"] + "/" + r["request"]["outcome"] if r["request"] else "",
                     "f1_union": f1(ini | fin, set(c["actions_taken"])), "n_shared_fraud_cards": len(r["sc_pre"].flags.get("shared_fraud_cards", [])),
                     "sar_reason": a["sar"]["reason"][:60],
                     "r7": bool(r["sc_pre"].flags.get("recurring_match")), "ring": bool(r["sc_pre"].flags.get("ring_hit")),
                     "burst": bool(r["sc_pre"].flags.get("burst_hit")), "errors": "; ".join(r["errors"]), "violations": "; ".join(r["violations"])})
        if (i + 1) % 50 == 0:
            print(f"{i + 1}/{len(cases)} {time.time() - t0:.0f}s", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(config.OUT_DIR / "replay_rows.csv", index=False)
    ok = df[df.error == ""]
    m: dict = {"n": int(len(df)), "n_failed": int((df.error != "").sum()), "seconds": round(time.time() - t0, 1)}
    fr = ok[ok.outcome == "confirmed_fraud"]
    cl = ok[ok.outcome == "cleared"]
    m["pattern_accuracy_fraud_all"] = round(float((fr.engine_pattern == fr.pattern).mean()), 4)
    frf = fr[fr.verdict == "fraud"]
    m["pattern_accuracy_fraud_verdict_fraud"] = round(float((frf.engine_pattern == frf.pattern).mean()), 4) if len(frf) else None
    m["pattern_confusion"] = pd.crosstab(fr.pattern, fr.engine_pattern).to_dict()
    m["sar_accuracy_fraud_all"] = round(float((fr.sar == fr.report_filed).mean()), 4)
    m["sar_accuracy_fraud_verdict_fraud"] = round(float((frf.sar == frf.report_filed).mean()), 4) if len(frf) else None
    m["sar_confusion"] = pd.crosstab(fr.report_filed, fr.sar).to_dict()
    m["episode_jaccard_fraud_all"] = round(float(fr.jaccard.mean()), 4)
    m["episode_jaccard_fraud_verdict_fraud"] = round(float(frf.jaccard.mean()), 4) if len(frf) else None
    m["cleared_false_episodes"] = int((cl.jaccard < 1).sum())
    y = (ok.outcome == "confirmed_fraud").astype(int).values
    p = ok.p.values.astype(float)
    m["brier_final"] = round(float(np.mean((p - y) ** 2)), 4)
    m["ece_final"] = round(ece(p, y), 4)
    m["brier_pre"] = round(float(np.mean((ok.p_pre.values.astype(float) - y) ** 2)), 4)
    m["ece_pre"] = round(ece(ok.p_pre.values.astype(float), y), 4)
    per = {}
    for g, d in ok.groupby("group"):
        want = "fraud" if d.outcome.iloc[0] == "confirmed_fraud" else "legitimate"
        per[g] = {"n": int(len(d)), "verdict_counts": d.verdict.value_counts().to_dict(), "verdict_accuracy": round(float((d.verdict == want).mean()), 4),
                  "uncertain_share": round(float((d.verdict == "uncertain").mean()), 4), "action_f1_final_mean": round(float(d.f1_final.mean()), 4),
                  "action_f1_initial_union_final_mean": round(float(d.f1_union.mean()), 4),
                  "blocked_initial": int(d.blocked_initial.sum()), "blocked_final": int(d.blocked_final.sum()),
                  "requests": d.request.value_counts().to_dict(), "r7_fires": int(d.r7.sum()), "ring_hits": int(d.ring.sum()), "burst_hits": int(d.burst.sum()),
                  "mean_p": round(float(d.p.mean()), 4), "validator_errors": int((d.errors != "").sum()), "policy_violations": int((d.violations != "").sum())}
    m["per_group"] = per
    m["synthetic_disputes_blocked_initial"] = per.get("cleared_as_synthetic_dispute", {}).get("blocked_initial")
    m["synthetic_disputes_blocked_final"] = per.get("cleared_as_synthetic_dispute", {}).get("blocked_final")
    json.dump(m, open(config.OUT_DIR / "replay_metrics.json", "w"), indent=1, default=str)
    print(json.dumps(m, indent=1, default=str))


if __name__ == "__main__":
    main()
