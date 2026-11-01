"""etl/delta.py — delta mode: append one month to the base snapshot, with no Round 1 raw CSVs.

The base snapshot is the checked-in facts DB (data/hhgoa_engine.duckdb): every Jul-Dec 2016 transaction with its derived
columns, the card / device / customer tables and the 5,565 closed cases. The final round adds one month of files
(transactions, identity, a replaced closed-case history, the case pack). This module layers that month on the base:

    uv run python -m etl.delta build  --month-dir data/raw/round2     # base + month -> data/hhgoa_v2.duckdb, gates printed
    uv run python -m etl.delta export                                 # only the rows that change -> data/out_delta/ (+ undo pack)
    uv run python -m etl.delta run    --month-dir data/raw/round2     # build + export + the engine facts DB (make delta)

What `build` does, in order (nothing touches the graph; every step is offline and deterministic):
  1. the month's rows go through the ETL's own TXC_SQL; with the base's rows they form one `txc`
  2. etl.features re-derives txn_feat / burst_member / device_profile / card_feat from that union (the same SQL as Round 1)
  3. the closed-case history (base cases + the appended ones) is parsed by etl.parse_closed_cases
  4. the month is scored by the FROZEN scorer in data/models (no retraining: the month has no labels); `--score none` leaves
     cms_p = -1 ("not scored") instead
  5. the gates in etl/delta_gates.py run; any FAIL stops here and `export` refuses to run
Everything the export needs lives in the work DB, which is ETL-shaped: engine.facts_from_etl can build the engine's facts DB
from it unchanged.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb

import etl.features as F
from etl import parse_closed_cases as PCC
from etl.build_db import TX_COLS
from etl.delta_base import (
    BASE_DB,
    BASE_TXC_SELECT,
    CARDMAP_SQL,
    DeltaError,
    MonthFiles,
    attach_base,
    git_state,
    quiet,
    reconstruct_base,
    require_migration_approval,
    sha256_file,
    sql_path,
)
from etl.delta_gates import FAIL, BuildContext, Gate, canon, failed, render, run_build_gates
from ops.console import fail, header, ok, step, summary, warn

WORK_DB = "data/hhgoa_v2.duckdb"
OUT_DIR = "data/out_delta"
FACTS_R2 = "data/hhgoa_engine_r2.duckdb"
MODELS_DIR = "data/models"
MONTH_DIR = "data/raw/round2"

# the raw columns the ETL itself reads (etl.build_db.TX_COLS / etl.features.TXC_SQL); anything else in a month file must be a
# scorer feature or it is reported as unmodelled drift
_ETL_COLUMNS = ({"TransactionID", "TransactionDT", "TransactionAmt", "ProductCD", "addr1", "addr2", "dist1", "dist2", "P_emaildomain",
                 "R_emaildomain", "customer_id", "ts", "channel", "risk_score", "DeviceType", "DeviceInfo"}
                | {f"card{i}" for i in range(1, 7)} | {f"C{i}" for i in range(1, 15)} | {f"D{i}" for i in range(1, 16)}
                | {f"M{i}" for i in range(1, 10)})
# scorer features that cms_train derives instead of reading from the files
_DERIVED_FEATURES = {"amt_ratio", "region_new", "dev_new", "pem_new", "pcd_new", "chan_new", "hour", "dow", "has_id",
                     "card_seq", "prior_in_region", "prior_on_dev", "prior_pem", "prior_pcd", "prior_chan", "prior_med_amt", "prior_max_amt"}
_REEXEC_FLAG = "HHG_DELTA_LIBOMP_REEXEC"


# ------------------------------------------------------------------------------------------------ inputs
def find_month_files(month_dir: str | Path) -> MonthFiles:
    """The four files of a month inside `month_dir` (the layout of the final-round dataset folder)."""
    d = Path(month_dir)
    if not d.is_dir():
        raise DeltaError(f"month directory not found: {d} (put the final-round files there; see docs/DELTA.md)")

    def one(label: str, *patterns: str) -> Path:
        hits = sorted({p for pat in patterns for p in d.glob(pat)})
        if len(hits) != 1:
            raise DeltaError(f"expected exactly one {label} file in {d} matching {' or '.join(patterns)}, found {len(hits)}")
        return hits[0]

    return MonthFiles(one("transactions", "transactions_*.csv"), one("identity", "identity_*.csv"),
                      one("closed-case history", "closed_cases_history.csv"), one("case pack", "cases/case_pack_final.csv", "case_pack_final.csv"))


def input_provenance(files: MonthFiles, base_db: str | Path, models_dir: str | Path) -> dict:
    def entry(p: Path) -> dict:
        return {"path": str(p), "bytes": p.stat().st_size, "sha256": sha256_file(p)}

    return {"base_db": entry(Path(base_db)), "transactions": entry(files.transactions), "identity": entry(files.identity),
            "closed_cases": entry(files.closed_cases), "case_pack": entry(files.case_pack),
            "model": entry(Path(models_dir) / "lgb_fraud.txt"), "calibrator": entry(Path(models_dir) / "isotonic.json"),
            "features": entry(Path(models_dir) / "cms_features.json")}


# ------------------------------------------------------------------------------------------------ lightgbm
def _bundled_libomp_dirs() -> list[str]:
    """Directories of OpenMP runtimes shipped inside other wheels of this environment (scikit-learn, torch)."""
    out = []
    for pkg, sub in (("sklearn", ".dylibs"), ("torch", "lib")):
        spec = importlib.util.find_spec(pkg)
        if spec and spec.submodule_search_locations:
            d = Path(next(iter(spec.submodule_search_locations))) / sub
            if (d / "libomp.dylib").exists():
                out.append(str(d))
    return out


def ensure_lightgbm() -> None:
    """Make `import lightgbm` work, or stop with the reason. On a Mac without libomp, re-run this process once with the OpenMP
    runtime that scikit-learn bundles (DYLD_FALLBACK_LIBRARY_PATH must be set before the process starts, hence the re-exec)."""
    try:
        import lightgbm  # noqa: F401
        return
    except OSError as exc:
        err = str(exc)
    dirs = _bundled_libomp_dirs()
    if "libomp" not in err or sys.platform != "darwin" or os.environ.get(_REEXEC_FLAG) or not dirs:
        raise DeltaError("lightgbm cannot load: it needs the OpenMP runtime (libomp). `brew install libomp`, or run with --score none "
                         f"(cms_p is then left at -1). Loader said: {err.splitlines()[0][:160]}")
    warn(f"libomp is not installed; re-running with the copy bundled in {dirs[0]} (`brew install libomp` makes this permanent)")
    env = dict(os.environ)
    env[_REEXEC_FLAG] = "1"
    env["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(p for p in (dirs[0], env.get("DYLD_FALLBACK_LIBRARY_PATH", "")) if p)
    sys.stdout.flush()
    os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], env)


# ------------------------------------------------------------------------------------------------ build
MONTH_TABLES = (
    ("tx_raw", "read_csv('{tx}', header=true, all_varchar=true, sample_size=-1, quote='\"', escape='\"')"),
    ("idn", "read_csv('{idn}', header=true, all_varchar=true, sample_size=-1)"),
    ("cc", "read_csv('{cc}', header=true, all_varchar=true, sample_size=-1)"),
    ("cp", "read_csv('{cp}', header=true, all_varchar=true, sample_size=-1)"),
)


def load_month_tables(con: duckdb.DuckDBPyConnection, files: MonthFiles) -> None:
    """tx_raw / idn / cc / cp as the ETL names them (all VARCHAR), then `tx` typed exactly as etl.build_db types it."""
    src = {"tx": sql_path(files.transactions), "idn": sql_path(files.identity), "cc": sql_path(files.closed_cases), "cp": sql_path(files.case_pack)}
    for table, reader in MONTH_TABLES:
        try:
            con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM {reader.format(**src)}")
        except duckdb.Error as exc:
            raise DeltaError(f"cannot read the month's {table}: {str(exc).splitlines()[0]}") from exc
    try:
        con.execute(f"CREATE OR REPLACE TABLE tx AS SELECT {TX_COLS} FROM tx_raw")
    except duckdb.BinderException as exc:
        raise DeltaError(f"the month's transactions lack a column the ETL reads: {str(exc).splitlines()[0]}") from exc
    con.execute("ALTER TABLE idn ALTER TransactionID TYPE BIGINT")
    con.execute("""CREATE OR REPLACE TABLE pairs AS
                   SELECT case_id, card_id, customer_id, unnest(string_split(txn_ids, '|'))::BIGINT AS TransactionID, 'closed' AS src FROM cc
                   UNION ALL SELECT case_id, card_id, customer_id, flagged_txn_id::BIGINT, 'casepack' FROM cp""")


def append_month_txc(con: duckdb.DuckDBPyConnection) -> int:
    """txc = the base rows + the month's rows (TXC_SQL, the ETL's own). Returns how many month rows found a card."""
    with quiet():
        con.execute(F.TXC_SQL)
    con.execute("ALTER TABLE txc RENAME TO txc_month")
    joined = con.execute("SELECT count(*) FROM txc_month").fetchone()[0]
    con.execute(f"""CREATE TABLE txc AS {BASE_TXC_SELECT}
                    UNION ALL BY NAME
                    SELECT TransactionID, card_id, customer_id, ts, ts_str, amt, product_cd, channel, addr1, addr2, dist1, dist2, p_email, r_email,
                           risk_score, has_identity, device_new, proxy, device_type, device_id, c_counts, d_deltas, m_flags, card4, card6
                    FROM txc_month""")
    con.execute("DROP TABLE txc_month")
    con.execute("DROP TABLE tx")
    return joined


def keep_base_order(con: duckdb.DuckDBPyConnection) -> int:
    """A card whose recurring_amounts holds the same entries as the shipped row keeps the shipped row's text: equal-ranked entries
    have no stable order across DuckDB builds, and an order-only difference is not a change to load."""
    rows = con.execute("""SELECT a.id, b.recurring_amounts, a.recurring_amounts FROM card_feat a JOIN e.card_feat b USING (id)
                          WHERE a.recurring_amounts <> b.recurring_amounts""").fetchall()
    fix = [(old, cid) for cid, old, new in rows if canon(old) == canon(new)]
    if fix:
        con.executemany("UPDATE card_feat SET recurring_amounts = ? WHERE id = ?", fix)
    return len(fix)


def schema_drift(con: duckdb.DuckDBPyConnection, models_dir: str | Path) -> tuple[list[str], list[str]]:
    """(raw columns the scorer reads that the month lacks, month columns nothing reads)."""
    features = json.loads((Path(models_dir) / "cms_features.json").read_text())["features"]
    have = {r[0] for t in ("tx_raw", "idn") for r in con.execute(f"DESCRIBE {t}").fetchall()}
    need = [f for f in features if f not in _DERIVED_FEATURES]
    return [f for f in need if f not in have], sorted(have - set(features) - _ETL_COLUMNS - {"id_" + f"{i:02d}" for i in range(1, 39)})


def score_month(con: duckdb.DuckDBPyConnection, models_dir: str | Path) -> dict:
    """Score the month's rows with the frozen scorer (data/models): same frame, same features, same calibrator as Round 1.
    Writes txn_feat.cms_p for the month's rows and returns the measurements the scorer gate reports."""
    import lightgbm as lgb
    import numpy as np
    import pandas as pd

    from etl.cms_train import load_calibrator, load_frame

    models = Path(models_dir)
    with quiet():
        df, feats, catcols = load_frame(con)
    want = json.loads((models / "cms_features.json").read_text())
    if feats != want["features"] or catcols != want["categorical"]:
        extra = [f for f in feats if f not in want["features"]][:5]
        lost = [f for f in want["features"] if f not in feats][:5]
        raise DeltaError(f"the month's frame does not match the scorer's features (extra {extra}, missing {lost})")
    booster = lgb.Booster(model_file=str(models / "lgb_fraud.txt"))
    if booster.num_feature() != len(feats):
        raise DeltaError(f"the model expects {booster.num_feature()} features, the frame has {len(feats)}")
    p = booster.predict(df[feats])
    if not np.isfinite(p).all() or p.min() < 0 or p.max() > 1:
        raise DeltaError("the frozen scorer returned a non-finite or out-of-range probability")
    masked = df.copy()
    masked["risk_score"] = np.nan
    p_masked = booster.predict(masked[feats])
    cal = load_calibrator(str(models / "isotonic.json"))
    base = con.execute("SELECT cms_p, risk_score FROM e.txc WHERE ts >= '2016-11-01'").fetchdf()   # the months the final model scored
    flagged = con.execute("SELECT TRY_CAST(flagged_txn_id AS BIGINT) FROM cp").fetchall()
    fl_ids = {r[0] for r in flagged}
    fl = p[df.TransactionID.isin(fl_ids).to_numpy()]
    fold = f"frozen-{df.ts.min():%Y-%m}"
    con.register("_month_scores", pd.DataFrame({"TransactionID": df.TransactionID.astype("int64").to_numpy(), "cms_p": p.astype("float64")}))
    con.execute("UPDATE txn_feat SET cms_p = s.cms_p, cms_fold = ? FROM _month_scores s WHERE txn_feat.TransactionID = s.TransactionID", [fold])
    con.unregister("_month_scores")
    return {
        "mode": "frozen", "fold": fold, "n": int(len(p)), "mean": float(p.mean()), "median": float(np.median(p)), "p99": float(np.quantile(p, 0.99)),
        "gt_0.5": float((p > 0.5).mean()), "gt_0.85": float((p > 0.85).mean()), "mean_calibrated": float(np.mean([cal(x) for x in p])),
        "mean_base": float(base.cms_p.mean()), "gt_0.5_base": float((base.cms_p > 0.5).mean()), "p99_base": float(base.cms_p.quantile(0.99)),
        "p99_without_risk_score": float(np.quantile(p_masked, 0.99)),
        "corr_risk_score": float(np.corrcoef(p, df.risk_score)[0, 1]), "corr_risk_score_base": float(base.cms_p.corr(base.risk_score)),
        "risk_score_p99": float(df.risk_score.quantile(0.99)), "risk_score_p99_base": float(base.risk_score.quantile(0.99)),
        "flagged_n": int(len(fl)), "flagged_max": float(fl.max()) if len(fl) else 0.0, "flagged_above_0.3": int((fl > 0.3).sum()),
        "model_sha256": sha256_file(models / "lgb_fraud.txt"), "features": len(feats),
    }


def fill_cms(con: duckdb.DuckDBPyConnection, score: str, models_dir: str | Path) -> dict | None:
    """cms_p for every txn_feat row: the base rows keep their stored scores, the month's rows get the frozen scorer's (or -1)."""
    con.execute("UPDATE txn_feat SET cms_p = b.cms_p, cms_fold = 'base' FROM e.txc b WHERE txn_feat.TransactionID = b.TransactionID")
    scorer = score_month(con, models_dir) if score == "frozen" else None
    con.execute("UPDATE txn_feat SET cms_p = -1.0, cms_fold = 'unscored' WHERE cms_p IS NULL")
    return scorer


def build(base_db: str | Path, files: MonthFiles, work_db: str | Path, score: str = "frozen", models_dir: str | Path = MODELS_DIR,
          allow_unapproved: bool = False) -> tuple[list[Gate], dict]:
    """Base + month -> the work DB (ETL-shaped), then the build gates. Returns (gates, stats); the work DB is kept even when a
    gate fails so the failure can be inspected, but `export` refuses to run on it. Refuses to start unless the schema
    migration is approved (`allow_unapproved` is for the offline fixtures only)."""
    if score not in ("frozen", "none"):
        raise DeltaError(f"--score must be 'frozen' or 'none', not {score!r}")
    approval = require_migration_approval(allow_unapproved)
    t0 = time.time()
    work = Path(work_db)
    work.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".wal"):
        Path(str(work) + suffix).unlink(missing_ok=True)
    stats: dict = {"score": score, "base_db": str(base_db), "work_db": str(work), "migration_approval": approval}
    step("hashing the inputs")
    stats["inputs"] = input_provenance(files, base_db, models_dir)
    con = duckdb.connect(str(work))
    con.execute("PRAGMA threads=8")
    attach_base(con, base_db)
    con.execute(CARDMAP_SQL)
    step(f"reading the month ({files.transactions.name}, {files.identity.name}, {files.closed_cases.name}, {files.case_pack.name})")
    load_month_tables(con, files)
    stats["month_rows_raw"] = con.execute("SELECT count(*) FROM tx_raw").fetchone()[0]
    stats["missing_raw_columns"], stats["unmodelled_columns"] = schema_drift(con, models_dir)
    stats["month_rows_joined"] = append_month_txc(con)
    ok(f"{stats['month_rows_raw']:,} month transactions, {stats['month_rows_joined']:,} on a base card -> txc {con.execute('SELECT count(*) FROM txc').fetchone()[0]:,} rows")
    if stats["month_rows_joined"] != stats["month_rows_raw"]:
        # a card the base does not know: keep going far enough to report it through the gate, but nothing is derived from a partial month
        gates = [Gate("month_inputs", FAIL, f"{stats['month_rows_raw'] - stats['month_rows_joined']:,} month transactions match no base card", {})]
        con.close()
        return gates, stats
    step("re-deriving txn_feat, burst_member, device_profile, card_feat from base + month (etl.features, unchanged SQL)")
    with quiet():
        F.build_all(con, rebuild_txc=False)
    stats["stage_seconds"] = {label: round(dt, 2) for label, dt in F._STAGES}
    stats["recurring_order_kept"] = keep_base_order(con)
    step("parsing the closed-case history (etl.parse_closed_cases)")
    try:
        with quiet():
            stats["parse"] = {k: v for k, v in PCC.parse(con).items() if k != "sample"}
    except ValueError as exc:
        con.close()
        raise DeltaError(f"closed-case history: {exc}") from exc
    step(f"cms_p: base rows keep their stored scores; the month is scored {'by the frozen model' if score == 'frozen' else 'not at all (-1)'}")
    stats["scorer"] = fill_cms(con, score, models_dir)
    n_null = con.execute("SELECT count(*) FROM txn_feat WHERE cms_p IS NULL").fetchone()[0]
    if n_null:
        raise DeltaError(f"{n_null:,} txn_feat rows still have no cms_p")
    base = reconstruct_base(base_db)
    gates = run_build_gates(BuildContext(con, base, score, stats, str(models_dir)))
    stats["seconds"] = round(time.time() - t0, 1)
    con.execute("CREATE OR REPLACE TABLE delta_meta(key VARCHAR, value VARCHAR)")
    meta = {"stats": stats, "gates": [g.as_dict() for g in gates], "git": git_state(), "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    for k, v in meta.items():
        con.execute("INSERT INTO delta_meta VALUES (?, ?)", [k, json.dumps(v, default=str)])
    con.close()
    base.close()
    return gates, stats


# ------------------------------------------------------------------------------------------------ facts DB
def build_facts(work_db: str | Path, out: str | Path) -> dict[str, int]:
    """The engine's facts DB for base + month, from the work DB with engine.facts_from_etl's own SQL."""
    from engine import facts_from_etl

    counts = facts_from_etl.build(str(work_db), str(out))
    con = duckdb.connect(str(work_db), read_only=True)
    want = {"txc": con.execute("SELECT count(*) FROM txc").fetchone()[0], "card_feat": con.execute("SELECT count(*) FROM card_feat").fetchone()[0],
            "device_profile": con.execute("SELECT count(*) FROM device_profile").fetchone()[0],
            "customer_feat": con.execute("SELECT count(DISTINCT customer_id) FROM card_feat").fetchone()[0],
            "cc": con.execute("SELECT count(*) FROM closed_case_parsed").fetchone()[0], "cc_txn": con.execute("SELECT count(*) FROM closed_case_txn").fetchone()[0],
            "cp": con.execute("SELECT count(*) FROM cp").fetchone()[0]}
    con.close()
    bad = {t: (counts[t], want[t]) for t in want if counts[t] != want[t]}
    if bad:
        raise DeltaError(f"facts DB row counts differ from the work DB: {bad}")
    return counts


# ------------------------------------------------------------------------------------------------ CLI
def _common(ap: argparse.ArgumentParser, month: bool, out: bool) -> None:
    ap.add_argument("--base", default=os.getenv("DELTA_BASE_DB", BASE_DB), help="the checked-in base snapshot (facts DB)")
    ap.add_argument("--work", default=os.getenv("DELTA_WORK_DB", WORK_DB), help="the ETL-shaped work DB the build writes")
    if month:
        ap.add_argument("--month-dir", default=os.getenv("MONTH_DIR", MONTH_DIR), help="folder with transactions_*.csv, identity_*.csv, closed_cases_history.csv, cases/case_pack_final.csv")
        ap.add_argument("--score", choices=("frozen", "none"), default="frozen", help="frozen = the Round 1 scorer, unchanged; none = leave cms_p at -1")
    ap.add_argument("--allow-unapproved-fixture", action="store_true",
                    help="offline fixtures only: build/export without the signed schema migration (never for the real month)")
    if out:
        ap.add_argument("--out", default=os.getenv("DELTA_OUT", OUT_DIR), help="where the delta chunks and the undo pack go")


def _print_build(gates: list[Gate], stats: dict, work: str) -> int:
    render(gates, "build gates")
    bad = failed(gates)
    n_warn = sum(1 for g in gates if g.status == "WARN")
    summary("etl.delta build " + ("stopped" if bad else "complete"),
            {"work db": work, "month rows": f"{stats.get('month_rows_raw', 0):,}", "scored": stats.get("score"),
             "gates": f"{len(gates) - len(bad)} of {len(gates)} pass or inform; {n_warn} warn; {len(bad)} fail",
             "seconds": stats.get("seconds", "-")}, status="fail" if bad else "ok")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="base + month -> the work DB and the build gates")
    _common(b, month=True, out=False)
    e = sub.add_parser("export", help="the changed rows -> delta chunks, undo pack, manifest (needs a passing build)")
    _common(e, month=False, out=True)
    r = sub.add_parser("run", help="build + export + the engine facts DB")
    _common(r, month=True, out=True)
    r.add_argument("--facts-out", default=os.getenv("FACTS_R2_DB", FACTS_R2), help="engine facts DB for base + month (never the base file)")
    r.add_argument("--no-facts", action="store_true")
    a = ap.parse_args(argv)
    try:
        require_migration_approval(a.allow_unapproved_fixture)          # before any input is read
        if a.cmd in ("build", "run"):
            files = find_month_files(a.month_dir)
            if a.score == "frozen":
                ensure_lightgbm()
            header("etl.delta " + a.cmd, "append one month to the base snapshot (delta mode)",
                   {"base": a.base, "month dir": a.month_dir, "work db": a.work, "score": a.score})
            gates, stats = build(a.base, files, a.work, a.score, allow_unapproved=a.allow_unapproved_fixture)
            code = _print_build(gates, stats, a.work)
            if code or a.cmd == "build":
                return code
        from etl import delta_export

        if a.cmd in ("export", "run"):
            header("etl.delta export", "only the rows that change, in the loader's format, plus the undo pack", {"base": a.base, "work db": a.work, "out": a.out})
            code = delta_export.export_delta(a.base, a.work, a.out, allow_unapproved=a.allow_unapproved_fixture)
            if code or a.cmd == "export":
                return code
        if a.cmd == "run" and not a.no_facts:
            step(f"engine facts DB for base + month -> {a.facts_out}")
            counts = build_facts(a.work, a.facts_out)
            ok(f"{a.facts_out}: " + ", ".join(f"{t} {n:,}" for t, n in counts.items()))
        return 0
    except DeltaError as exc:
        fail(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
