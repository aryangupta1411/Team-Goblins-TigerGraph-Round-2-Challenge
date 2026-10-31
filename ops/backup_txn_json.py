"""ops/backup_txn_json.py — read-only backup of the three Transaction attributes that exist only in the graph.

Every Transaction attribute except c_counts / d_deltas / m_flags (the Vesta C, D and M columns as JSON) is in the checked-in
base snapshot (data/hhgoa_engine.duckdb). Those three came from Round 1's raw CSVs, which are not on this machine, so they cannot
be rebuilt if a load goes wrong. Before delta mode writes into FraudGraph this tool copies them out. It only READS:

    uv run python -m ops.backup_txn_json                       # data/backup/txn_json.tsv.gz + txn_json_manifest.json
    uv run python -m ops.backup_txn_json --window-days 5 --sample 150

How: the vertex-listing endpoint with a `ts` window (epoch seconds; DATETIME filters take integers) and select=c_counts,d_deltas,
m_flags. The base snapshot says how many transactions each window must hold, so a truncated or padded answer is an error, not a
silent gap. Afterwards the file is read back and checked: ids equal the base's ids, every value is valid JSON with the expected
keys in the expected order, a random sample is fetched a second time through a different call and compared, and the Transaction
DDL is compared with the snapshot's columns to prove nothing else lives only in the graph.

The graph is reached through `ReadOnly`, which refuses every call that is not a read: this module cannot write by accident.
Restoring is a partial upsert by id (see docs/DELTA.md); it is deliberately not part of this tool.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from etl.delta_base import git_state, sha256_file
from ops.console import Col, Table, fail, header, ok, progress, step, summary, warn
from ops.ensure_awake import ensure_awake

BASE_DB = "data/hhgoa_engine.duckdb"
OUT_DIR = "data/backup"
SCHEMA_GSQL = "graph/schema.gsql"
DELTA_TXN_GLOB = "data/out_delta/txn_*.csv"
FILE_NAME = "txn_json.tsv.gz"
MANIFEST = "txn_json_manifest.json"
COLUMNS = ("c_counts", "d_deltas", "m_flags")
EXPECTED_KEYS = {"c_counts": [f"C{i}" for i in range(1, 15)], "d_deltas": [f"D{i}" for i in range(1, 16)], "m_flags": [f"M{i}" for i in range(1, 10)]}
# graph attribute -> column of the snapshot's txc, where the names differ
RENAMED = {"proxy_type": "proxy"}
READ_CALLS = frozenset({"getVertices", "getVerticesById", "getVertexCount", "getEdgeCount", "getVertexType", "getVersion"})


class BackupError(RuntimeError):
    """The backup cannot be trusted: a window is short or padded, a value is malformed, a re-read disagrees. Nothing is kept."""


class ReadOnly:
    """The only door to the graph in this module: any attribute outside READ_CALLS raises, so a write is impossible by construction."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        if name not in READ_CALLS:
            raise PermissionError(f"{name!r} is not a read call: this tool never writes to the graph")
        return getattr(self._conn, name)


@dataclass(frozen=True)
class Window:
    lo: datetime        # naive, UTC (the graph stores the dataset's naive timestamps as UTC epoch seconds)
    hi: datetime        # exclusive
    expected: int       # how many base transactions fall in [lo, hi)

    def epochs(self) -> tuple[int, int]:
        return int(self.lo.replace(tzinfo=UTC).timestamp()), int(self.hi.replace(tzinfo=UTC).timestamp())


def plan_windows(con: duckdb.DuckDBPyConnection, days: int) -> list[Window]:
    """Midnight-aligned [lo, hi) windows of `days` days covering every base transaction; empty windows are dropped."""
    lo, hi = con.execute("SELECT min(ts), max(ts) FROM txc").fetchone()
    start = datetime(lo.year, lo.month, lo.day)
    out: list[Window] = []
    while start <= hi:
        end = start + timedelta(days=days)
        n = con.execute("SELECT count(*) FROM txc WHERE ts >= ? AND ts < ?", [start, end]).fetchone()[0]
        if n:
            out.append(Window(start, end, n))
        start = end
    return out


@ensure_awake()
def fetch_window(graph, w: Window) -> list[tuple[str, str | None, str | None, str | None]]:
    lo, hi = w.epochs()
    rows = graph.getVertices("Transaction", select=",".join(COLUMNS), where=f"ts>={lo},ts<{hi}", limit=w.expected + 1000)
    return [(str(r["v_id"]), *(r["attributes"].get(c) for c in COLUMNS)) for r in rows]


def write_tsv_gz(path: Path, rows) -> tuple[int, str]:
    """Rows as TAB lines in a gzip with no name and no timestamp, so the same data is the same bytes. Returns (rows, sha256 of the text)."""
    h = hashlib.sha256()
    n = 0
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0, compresslevel=6) as gz:
        for r in rows:
            for v in r:
                if v is None or "\t" in v or "\n" in v or "\r" in v:
                    raise BackupError(f"transaction {r[0]}: a value is empty or holds a tab or newline")
            line = ("\t".join(r) + "\n").encode("utf-8")
            gz.write(line)
            h.update(line)
            n += 1
    return n, h.hexdigest()


def content_sha256(path: Path) -> str:
    """sha256 of the decompressed text: what write_tsv_gz reports, whatever the gzip header says."""
    h = hashlib.sha256()
    with gzip.open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_tsv_gz(path: Path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        for line in fh:
            yield line.rstrip("\n").split("\t")


def check_values(rows) -> dict:
    """Stream the rows: valid JSON, the expected keys in the expected order, sane value types. Returns counts and the problems."""
    problems: list[str] = []
    ids: list[str] = []
    orders: dict[str, set[tuple[str, ...]]] = {c: set() for c in COLUMNS}
    nulls: dict[str, dict[str, int]] = {c: dict.fromkeys(EXPECTED_KEYS[c], 0) for c in COLUMNS}
    for r in rows:
        ids.append(r[0])
        for col, text in zip(COLUMNS, r[1:], strict=True):
            try:
                obj = json.loads(text)
            except ValueError:
                problems.append(f"{r[0]}.{col}: not JSON")
                continue
            if not isinstance(obj, dict):
                problems.append(f"{r[0]}.{col}: not a JSON object")
                continue
            keys = tuple(obj)
            orders[col].add(keys)
            if list(keys) != EXPECTED_KEYS[col]:
                problems.append(f"{r[0]}.{col}: keys {keys[:3]}... differ from {EXPECTED_KEYS[col][:3]}...")
                continue
            for k, v in obj.items():
                if v is None:
                    nulls[col][k] += 1
                elif col == "m_flags" and not isinstance(v, str):
                    problems.append(f"{r[0]}.{col}.{k}: {v!r} is not a string")
                elif col != "m_flags" and (isinstance(v, bool) or not isinstance(v, (int, float))):
                    problems.append(f"{r[0]}.{col}.{k}: {v!r} is not a number")
    return {"rows": len(ids), "ids": ids, "problems": problems, "key_orders": {c: len(v) for c, v in orders.items()}, "nulls": nulls}


def ddl_attributes(schema_gsql: str | Path, vertex: str = "Transaction") -> list[str]:
    """Attribute names of `vertex` in the checked-in DDL (the primary id included)."""
    text = Path(schema_gsql).read_text()
    m = re.search(rf"CREATE VERTEX {vertex}\s*\((.*?)\)\s*WITH", text, re.S)
    if not m:
        raise BackupError(f"CREATE VERTEX {vertex} not found in {schema_gsql}")
    body = re.sub(r"//[^\n]*", "", m.group(1))
    names = []
    for part in body.split(","):
        toks = part.split()
        if toks:
            names.append(toks[1] if toks[0] == "PRIMARY_ID" else toks[0])
    return names


def coverage(con: duckdb.DuckDBPyConnection, schema_gsql: str | Path) -> dict:
    """Which Transaction attributes exist only in the graph? Everything the snapshot's txc lacks, minus what this tool backs up."""
    attrs = ddl_attributes(schema_gsql)
    have = {r[0] for r in con.execute("DESCRIBE txc").fetchall()} | {"id"}
    only = [a for a in attrs if RENAMED.get(a, a) not in have and a not in COLUMNS]
    return {"graph_attributes": len(attrs), "in_snapshot": sum(1 for a in attrs if RENAMED.get(a, a) in have), "backed_up": list(COLUMNS), "only_in_graph_and_not_backed_up": only}


def format_parity(path: Path, delta_glob: str) -> dict | None:
    """Do the month's new Transaction lines (etl.delta export) write the three JSON columns the way the graph already holds them?"""
    files = sorted(Path().glob(delta_glob))
    if not files:
        return None
    import yaml

    spec = yaml.safe_load(Path("contracts/csv_columns.yaml").read_text())
    names = [c["name"] for p, f in spec["files"].items() if Path(p).name == "txn_NNN.csv" for c in f["columns"]]
    idx = {c: names.index(c) for c in COLUMNS}
    seen = {c: set() for c in COLUMNS}
    n = 0
    for f in files:
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                for c in COLUMNS:
                    try:
                        seen[c].add(tuple(json.loads(parts[idx[c]])))
                    except (ValueError, TypeError):
                        seen[c].add(("<not json>",))
                n += 1
    graph = {c: set() for c in COLUMNS}
    for r in read_tsv_gz(path):
        for c, text in zip(COLUMNS, r[1:], strict=True):
            graph[c].add(tuple(json.loads(text)))
    same = {c: seen[c] == graph[c] and len(graph[c]) == 1 for c in COLUMNS}
    return {"delta_rows": n, "same_key_order": same, "graph_key_orders": {c: len(v) for c, v in graph.items()}, "delta_key_orders": {c: len(v) for c, v in seen.items()}}


def run_backup(graph, base_db: str | Path, out_dir: str | Path, *, window_days: int = 5, sample: int = 150, pause: float = 0.0,
               allow_count_mismatch: bool = False, schema_gsql: str | Path = SCHEMA_GSQL, delta_glob: str = DELTA_TXN_GLOB) -> dict:
    """Fetch every base transaction's three attributes, write, verify, and return the manifest (also written to out_dir)."""
    t0 = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = duckdb.connect(str(base_db), read_only=True)
    n_base = base.execute("SELECT count(*) FROM txc").fetchone()[0]
    live = int(graph.getVertexCount("Transaction"))
    if live != n_base and not allow_count_mismatch:
        raise BackupError(f"the graph holds {live:,} transactions, the base snapshot {n_base:,}: take this backup before any delta load "
                          "(or pass --allow-count-mismatch to back up the base's ids anyway)")
    windows = plan_windows(base, window_days)
    if sum(w.expected for w in windows) != n_base:
        raise BackupError("the windows do not cover the base's transactions")
    cover = coverage(base, schema_gsql)
    snapshot = {"vertices": dict(graph.getVertexCount("*")), "edges": dict(graph.getEdgeCount("*"))}
    version = ""
    try:
        version = next((str(p["version"]) for p in graph.getVersion() if isinstance(p, dict) and p.get("name") == "product"), "")
    except Exception:  # noqa: BLE001 - provenance only
        pass

    spool = duckdb.connect()
    spool.execute("CREATE TABLE t(id VARCHAR, c_counts VARCHAR, d_deltas VARCHAR, m_flags VARCHAR)")

    step(f"fetching {n_base:,} transactions in {len(windows)} windows of {window_days} days (GET only)")
    got = 0
    with progress("windows", total=len(windows), every=10.0) as p:
        for w in windows:
            rows = fetch_window(graph, w)
            if len(rows) != w.expected:
                raise BackupError(f"window {w.lo:%Y-%m-%d}..{w.hi:%Y-%m-%d}: the graph returned {len(rows):,} transactions, the base has {w.expected:,}")
            df = pd.DataFrame(rows, columns=["id", *COLUMNS])
            spool.register("_w", df)
            spool.execute("INSERT INTO t SELECT * FROM _w")
            spool.unregister("_w")
            got += len(rows)
            p.advance()
            if pause:
                time.sleep(pause)
    fetch_s = time.time() - t0
    dup = spool.execute("SELECT count(*) - count(DISTINCT id) FROM t").fetchone()[0]
    if got != n_base or dup:
        raise BackupError(f"fetched {got:,} rows ({dup} duplicate ids), expected {n_base:,}")

    step("writing and reading the file back")
    final = out / FILE_NAME
    part = out / (FILE_NAME + ".part")
    cur = spool.execute("SELECT id, c_counts, d_deltas, m_flags FROM t ORDER BY id::BIGINT")

    def ordered():
        while batch := cur.fetchmany(50_000):
            yield from batch

    try:
        n_written, content_sha = write_tsv_gz(part, ordered())
        checks = check_values(read_tsv_gz(part))
        ids = checks.pop("ids")
        if checks["rows"] != n_written or n_written != n_base:
            raise BackupError(f"wrote {n_written:,} rows, read back {checks['rows']:,}, expected {n_base:,}")
        base.register("_backup_ids", pd.DataFrame({"id": ids}))
        extra = base.execute("SELECT count(*) FROM _backup_ids b WHERE TRY_CAST(b.id AS BIGINT) NOT IN (SELECT TransactionID FROM txc)").fetchone()[0]
        missing = base.execute("SELECT count(*) FROM txc WHERE CAST(TransactionID AS VARCHAR) NOT IN (SELECT id FROM _backup_ids)").fetchone()[0]
        base.unregister("_backup_ids")
        if extra or missing:
            raise BackupError(f"the file's ids differ from the base's: {extra} not in the base, {missing} missing")
        if checks["problems"]:
            raise BackupError(f"{len(checks['problems']):,} malformed values, e.g. {checks['problems'][:3]}")
        if any(v != 1 for v in checks["key_orders"].values()):
            raise BackupError(f"more than one key order in a column: {checks['key_orders']}")
        if final.exists():
            if content_sha256(final) != content_sha:
                alt = out / f"txn_json.{datetime.now(UTC):%Y%m%dT%H%M%SZ}.tsv.gz"
                part.replace(alt)
                raise BackupError(f"{final.name} already exists and differs from what the graph holds now (the new copy is {alt.name}); "
                                  "compare the two before replacing either: this is the only copy of those columns")
            part.unlink()                                  # the same bytes again: keep the file that is there
        else:
            part.replace(final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    step(f"re-reading {min(sample, n_base)} random transactions through a different call and comparing")
    rng = random.Random(7)
    pick = rng.sample(ids, min(sample, len(ids))) if sample else []
    wanted = {r[0]: r[1:] for r in spool.execute("SELECT id, c_counts, d_deltas, m_flags FROM t WHERE id IN (SELECT unnest(?::VARCHAR[]))", [pick]).fetchall()} if pick else {}
    differ = []
    for tid in pick:
        res = graph.getVerticesById("Transaction", [tid])
        res = res if isinstance(res, list) else [res]
        a = res[0]["attributes"] if res else {}
        if tuple(a.get(c) for c in COLUMNS) != tuple(wanted[tid]):
            differ.append(tid)
    if differ:
        raise BackupError(f"{len(differ)} of {len(pick)} re-read transactions differ from the backup, e.g. {differ[:3]}")

    parity = format_parity(final, delta_glob)
    warnings: list[str] = []
    if cover["only_in_graph_and_not_backed_up"]:
        warnings.append(f"{len(cover['only_in_graph_and_not_backed_up'])} Transaction attributes exist only in the graph and are not backed up: {cover['only_in_graph_and_not_backed_up']}")
    manifest = {
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "git": git_state(), "graph": "FraudGraph", "tigergraph": version,
        "file": FILE_NAME, "bytes": final.stat().st_size, "sha256_file": sha256_file(final), "sha256_content": content_sha, "rows": n_written,
        "columns": ["id", *COLUMNS], "base_snapshot_rows": n_base, "windows": {"days": window_days, "count": len(windows), "largest": max(w.expected for w in windows)},
        "checks": {"ids_equal_base": True, "values": {"problems": 0, "key_orders": checks["key_orders"], "nulls_per_key": checks["nulls"]},
                   "re_read": {"sampled": len(pick), "differ": 0}, "coverage": cover, "format_parity_with_delta": parity},
        "graph_counts_before": snapshot, "seconds": round(time.time() - t0, 1), "fetch_seconds": round(fetch_s, 1),
        "restore": "partial vertex upsert by id: {id: {c_counts, d_deltas, m_flags}} (REST); never the loading jobs", "warnings": warnings,
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1, default=str))
    spool.close()
    base.close()
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE_DB, help="the checked-in base snapshot: it says what every window must hold")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--window-days", type=int, default=5, help="days per request (each request scans the Transaction vertices once)")
    ap.add_argument("--sample", type=int, default=150, help="transactions re-read through getVerticesById and compared (0 = skip)")
    ap.add_argument("--pause", type=float, default=0.0, help="seconds between requests")
    ap.add_argument("--allow-count-mismatch", action="store_true")
    a = ap.parse_args(argv)
    header("ops.backup_txn_json", "read-only copy of Transaction.c_counts / d_deltas / m_flags out of FraudGraph",
           {"base": a.base, "out": a.out, "window days": a.window_days, "sample": a.sample, "writes to the graph": "never (ReadOnly wrapper)"})
    try:
        from ops.ensure_awake import tg_connection

        step("connecting (reads only; wakes the workspace if it is suspended)")
        graph = ReadOnly(tg_connection())
        m = run_backup(graph, a.base, a.out, window_days=a.window_days, sample=a.sample, pause=a.pause, allow_count_mismatch=a.allow_count_mismatch)
    except (BackupError, PermissionError, KeyError, RuntimeError, OSError, duckdb.Error) as exc:
        fail(str(exc))
        return 1
    t = Table(Col("check", max_width=44), Col("result", max_width=60), title="backup checks")
    t.add_row("rows written = base snapshot", f"{m['rows']:,} = {m['base_snapshot_rows']:,}")
    t.add_row("ids equal the base's", "yes")
    t.add_row("JSON valid, keys and order as expected", f"yes ({m['checks']['values']['key_orders']} key orders per column)")
    t.add_row("random re-read through another call", f"{m['checks']['re_read']['sampled']} compared, 0 differ")
    cov = m["checks"]["coverage"]
    t.add_row("other Transaction attributes in the snapshot", f"{cov['in_snapshot']} of {cov['graph_attributes']}; only in the graph: {cov['only_in_graph_and_not_backed_up'] or 'none besides the 3 backed up'}")
    par = m["checks"]["format_parity_with_delta"]
    t.add_row("delta rows format the JSON like the graph", "no delta export found" if par is None else ("yes" if all(par["same_key_order"].values()) else f"NO: {par['same_key_order']}"))
    t.print()
    for w in m["warnings"]:
        warn(w)
    summary("backup complete", {"file": str(Path(a.out) / FILE_NAME), "size": f"{m['bytes'] / 1e6:,.1f} MB", "sha256": m["sha256_file"][:16], "seconds": m["seconds"],
                                "next": "the delta load can start (docs/DELTA.md); this file is the only copy of those columns"})
    ok("the graph was only read")
    return 0


if __name__ == "__main__":
    sys.exit(main())
