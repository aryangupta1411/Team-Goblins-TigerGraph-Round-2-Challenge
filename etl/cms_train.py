"""etl/cms_train.py — the case-memory scorer (CMS): LightGBM over the Vesta columns + identity +
risk_score + leakage-free card-history features, trained on the bank's own closed-case labels.

    python -m etl.cms_train data/hhgoa.duckdb --out data/models

Procedure (PLAN §4.1 #4, §4.5):
  1. holdout run: train Jul–Sep, early-stop on October   -> best_iter, October AUC / calibration table
  2. isotonic calibrator fit on the October out-of-fold predictions  -> data/models/isotonic.json
  3. month-wise out-of-fold cms_p for EVERY Jul–Oct row (train on the other three months, best_iter rounds)
     so the 310-case QA replay never sees a score that was trained on its own label
  4. final model on Jul–Oct (best_iter rounds) scores Nov–Dec        -> data/models/lgb_fraud.txt
  5. cms_p / cms_fold written into txn_feat (card_seq and prior_* already live there; export reads them)
Everything is seeded; a full run is ~2–3 minutes on an M-series Mac (one fit ≈ 15 s).
cms_p is the RAW model probability; the engine applies cal() = load_calibrator()(cms_p).
"""
from __future__ import annotations

import argparse
import json
import os
import time

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, roc_auc_score

PARAMS = dict(objective="binary", learning_rate=0.05, num_leaves=127, min_data_in_leaf=100,
              feature_fraction=0.5, bagging_fraction=0.8, bagging_freq=1, lambda_l2=10,
              verbose=-1, num_threads=8, max_cat_to_onehot=4, cat_smooth=50, seed=42,
              bagging_seed=42, feature_fraction_seed=42, deterministic=True)
CAT_TX = ["ProductCD", "card4", "card6", "addr1", "addr2", "P_emaildomain", "R_emaildomain",
          "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "card2", "card3", "card5", "dist1", "dist2"]
ID_COLS = [f"id_{i:02d}" for i in range(1, 39)]
ID_NUM, ID_CAT = ID_COLS[:11], ID_COLS[11:] + ["DeviceType", "DeviceInfo"]
HIST = ["card_seq", "prior_in_region", "prior_on_dev", "prior_pem", "prior_pcd", "prior_chan", "prior_med_amt", "prior_max_amt"]
BINS = [0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 1.0]


def load_frame(con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, list[str], list[str]]:
    cols = [r[0] for r in con.execute("DESCRIBE tx_raw").fetchall()]
    vcd = [c for c in cols if c[0] in "VCD" and c[1:].isdigit()]
    num_expr = ", ".join(f"TRY_CAST(t.{c} AS FLOAT) AS {c}" for c in vcd + ["TransactionAmt", "risk_score"])
    id_num = ", ".join(f"TRY_CAST(i.{c} AS FLOAT) AS {c}" for c in ID_NUM)
    id_cat = ", ".join(f"i.{c}" for c in ID_CAT)
    hist = ", ".join(f"f.{c}" for c in HIST)
    df = con.execute(f"""
        SELECT t.TransactionID::BIGINT AS TransactionID, t.ts::TIMESTAMP AS ts, t.channel,
               {num_expr}, {", ".join("t." + c for c in CAT_TX)}, {id_num}, {id_cat},
               (i.TransactionID IS NOT NULL)::INT AS has_id, {hist}
        FROM tx_raw t
        LEFT JOIN idn i USING (TransactionID)
        JOIN txn_feat f ON f.TransactionID = t.TransactionID::BIGINT
    """).fetchdf()
    df["amt_ratio"] = np.where(df.prior_med_amt > 0, df.TransactionAmt / df.prior_med_amt.replace(0, np.nan), np.nan)
    for c, src in (("region_new", "prior_in_region"), ("dev_new", "prior_on_dev"), ("pem_new", "prior_pem"),
                   ("pcd_new", "prior_pcd"), ("chan_new", "prior_chan")):
        df[c] = (df[src] == 0).astype(int)
    df["hour"] = df.ts.dt.hour
    df["dow"] = df.ts.dt.dayofweek
    fraud_ids = set(int(x) for (s,) in con.execute("SELECT txn_ids FROM cc WHERE outcome = 'confirmed_fraud'").fetchall() for x in s.split("|"))
    df["y"] = df.TransactionID.isin(fraud_ids).astype(int)
    catcols = CAT_TX + ID_CAT + ["channel"]
    for c in catcols:
        df[c] = df[c].astype("category")
    feats = [c for c in df.columns if c not in ("TransactionID", "ts", "y")]
    return df, feats, catcols


def fit(df, feats, catcols, rounds, valid=None):
    dtr = lgb.Dataset(df[feats], df.y, categorical_feature=catcols, free_raw_data=False)
    if valid is not None:
        dva = lgb.Dataset(valid[feats], valid.y, reference=dtr)
        return lgb.train(PARAMS, dtr, num_boost_round=rounds, valid_sets=[dva],
                         callbacks=[lgb.early_stopping(100, verbose=False)])
    return lgb.train(PARAMS, dtr, num_boost_round=rounds)


def calib_table(p, y):
    b = pd.cut(p, BINS, include_lowest=True)
    t = pd.DataFrame({"p": p, "y": y}).groupby(b, observed=True).agg(n=("y", "size"), fraud_rate=("y", "mean"), mean_p=("p", "mean"))
    return [{"bin": str(i), "n": int(r.n), "fraud_rate": round(float(r.fraud_rate), 4), "mean_p": round(float(r.mean_p), 4)} for i, r in t.iterrows()]


def save_calibrator(iso: IsotonicRegression, path: str) -> None:
    json.dump({"x": [float(v) for v in iso.X_thresholds_], "y": [float(v) for v in iso.y_thresholds_],
               "note": "isotonic regression fit on October out-of-fold raw cms_p; apply with numpy.interp(p, x, y)"},
              open(path, "w"), indent=1)


def load_calibrator(path: str):
    """Return cal(p) -> calibrated probability (numpy.interp over the isotonic thresholds)."""
    d = json.load(open(path))
    x, y = np.asarray(d["x"]), np.asarray(d["y"])
    return lambda p: float(np.interp(p, x, y))


def train_all(db: str, out: str) -> dict:
    t0 = time.time()
    os.makedirs(out, exist_ok=True)
    con = duckdb.connect(db)
    con.execute("PRAGMA threads=8")
    df, feats, catcols = load_frame(con)
    print(f"frame {df.shape} loaded in {time.time()-t0:.0f}s", flush=True)
    month = df.ts.dt.month
    tr, va, te = df[month <= 9], df[month == 10], df[month >= 11]
    # 1. holdout run
    m0 = fit(tr, feats, catcols, 2000, valid=va)
    best = int(m0.best_iteration)
    p_oct = m0.predict(va[feats], num_iteration=best)
    metrics = {"best_iteration": best,
               "oct_holdout": {"auc": round(roc_auc_score(va.y, p_oct), 4), "ap": round(average_precision_score(va.y, p_oct), 4),
                               "base_rate": round(float(va.y.mean()), 4), "risk_score_auc": round(roc_auc_score(va.y, va.risk_score.fillna(0)), 4),
                               "raw_bins": calib_table(p_oct, va.y.values)}}
    print(f"holdout: best_iter={best} Oct AUC={metrics['oct_holdout']['auc']} ({time.time()-t0:.0f}s)", flush=True)
    # 2. isotonic calibrator on October OOF
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(p_oct, va.y.values)
    save_calibrator(iso, os.path.join(out, "isotonic.json"))
    metrics["oct_holdout"]["cal_bins"] = calib_table(iso.predict(p_oct), va.y.values)
    # 3. month-wise out-of-fold for Jul–Oct
    julOct = df[month <= 10]
    oof = pd.Series(np.nan, index=julOct.index)
    fold_auc = {}
    for m in (7, 8, 9, 10):
        trn, tst = julOct[julOct.ts.dt.month != m], julOct[julOct.ts.dt.month == m]
        mm = fit(trn, feats, catcols, best)
        pm = mm.predict(tst[feats])
        oof.loc[tst.index] = pm
        fold_auc[f"2016-{m:02d}"] = round(roc_auc_score(tst.y, pm), 4) if tst.y.nunique() > 1 else None
        print(f"oof month {m}: n={len(tst)} auc={fold_auc[f'2016-{m:02d}']} ({time.time()-t0:.0f}s)", flush=True)
    metrics["oof_month_auc"] = fold_auc
    # 4. final model on Jul–Oct -> Nov–Dec
    final = fit(julOct, feats, catcols, best)
    p_te = final.predict(te[feats])
    final.save_model(os.path.join(out, "lgb_fraud.txt"))
    imp = pd.Series(final.feature_importance("gain"), index=feats).sort_values(ascending=False)
    metrics["top_features"] = [(k, round(float(v))) for k, v in imp.head(30).items()]
    metrics["novdec"] = {"n": int(len(te)), "p_gt_0.5": int((p_te > 0.5).sum()), "p_gt_0.3": int((p_te > 0.3).sum()), "sum_p": round(float(p_te.sum()), 1)}
    # 5. write cms_p into txn_feat
    scores = pd.concat([
        pd.DataFrame({"TransactionID": julOct.TransactionID.values, "cms_p": oof.values,
                      "cms_fold": [f"oof-2016-{m:02d}" for m in julOct.ts.dt.month]}),
        pd.DataFrame({"TransactionID": te.TransactionID.values, "cms_p": p_te, "cms_fold": "final"}),
    ])
    con.register("_scores", scores)
    con.execute("UPDATE txn_feat SET cms_p = s.cms_p, cms_fold = s.cms_fold FROM _scores s WHERE txn_feat.TransactionID = s.TransactionID")
    con.unregister("_scores")
    assert con.execute("SELECT count(*) FROM txn_feat WHERE cms_p IS NULL").fetchone()[0] == 0
    # case-pack flagged transactions (raw and calibrated)
    cal = load_calibrator(os.path.join(out, "isotonic.json"))
    rows = con.execute("""
        SELECT cp.case_id, cp.card_id, cp.flagged_txn_id, cp.risk_score, f.cms_p
        FROM cp JOIN txn_feat f ON f.TransactionID = cp.flagged_txn_id::BIGINT ORDER BY cp.case_id""").fetchall()
    metrics["case_pack"] = [{"case": r[0], "card": r[1], "txn": r[2], "risk": r[3], "cms_p": round(r[4], 4), "cal_p": round(cal(r[4]), 4)} for r in rows]
    json.dump(metrics, open(os.path.join(out, "cms_metrics.json"), "w"), indent=1, default=str)
    json.dump({"features": feats, "categorical": catcols}, open(os.path.join(out, "cms_features.json"), "w"), indent=1)
    print(json.dumps({k: metrics[k] for k in ("best_iteration", "oct_holdout", "oof_month_auc", "novdec")}, indent=1, default=str))
    for r in metrics["case_pack"]:
        print(f"  {r['case']} {r['card']:10s} txn={r['txn']} risk={r['risk']} cms_p={r['cms_p']:.4f} cal={r['cal_p']:.3f}")
    print(f"done in {time.time()-t0:.0f}s")
    con.close()
    return metrics


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default="data/hhgoa.duckdb")
    ap.add_argument("--out", default="data/models")
    a = ap.parse_args()
    train_all(a.db, a.out)
