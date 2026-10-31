"""tests/unit/test_backup_txn_json.py — the read-only backup of Transaction.c_counts / d_deltas / m_flags (ops/backup_txn_json.py).

No network: a fake graph answers the same read calls the tool makes. The point is the tool's own guarantees: it cannot write, a short
or padded window is an error, malformed values and a key-order change are errors, a re-read that disagrees is an error, the file is
deterministic, and an existing backup is never replaced by a different one.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from ops import backup_txn_json as B

TXC_COLUMNS = ["TransactionID", "id", "card_id", "customer_id", "ts", "ts_str", "amt", "product_cd", "channel", "addr1", "addr2", "dist1", "dist2",
               "p_email", "r_email", "risk_score", "has_identity", "device_new", "proxy", "device_type", "device_id", "cms_p", "card_seq",
               "prior_in_region", "prior_on_dev", "prior_pem", "prior_pcd", "prior_med_amt", "prior_max_amt", "ring_hit", "burst_id"]


def blob(prefix: str, n: int, seed: int) -> str:
    vals = [None if (prefix == "D" and (seed + i) % 4 == 0) else float(seed + i) for i in range(1, n + 1)]
    return json.dumps({f"{prefix}{i}": v for i, v in enumerate(vals, start=1)}, separators=(",", ":"))


def flags(seed: int) -> str:
    return json.dumps({f"M{i}": "T" if (seed + i) % 2 else "F" for i in range(1, 10)}, separators=(",", ":"))


class FakeGraph:
    """The read calls the tool uses, over an in-memory dict, plus a write method the ReadOnly wrapper must refuse."""

    def __init__(self, ids_by_day: dict[str, int]):
        self.rows: dict[str, dict] = {}
        n = 3_000_000
        for day, count in ids_by_day.items():
            base = datetime.fromisoformat(day).replace(tzinfo=UTC)
            for k in range(count):
                n += 1
                self.rows[str(n)] = {"ts": int((base + timedelta(minutes=7 * k)).timestamp()), "c_counts": blob("C", 14, n), "d_deltas": blob("D", 15, n), "m_flags": flags(n)}
        self.calls: list[str] = []
        self.pad = 0

    def getVertices(self, vtype, select="", where="", limit=None, **_):
        self.calls.append("getVertices")
        lo, hi = (int(x.split(">=")[1]) if ">=" in x else int(x.split("<")[1]) for x in where.split(","))
        hits = [(i, r) for i, r in self.rows.items() if lo <= r["ts"] < hi][: limit]
        return [{"v_id": i, "v_type": vtype, "attributes": {c: r[c] for c in select.split(",")}} for i, r in hits][: len(hits) - self.pad if self.pad else None]

    def getVerticesById(self, vtype, ids):
        self.calls.append("getVerticesById")
        return [{"v_id": i, "v_type": vtype, "attributes": {c: self.rows[i][c] for c in B.COLUMNS}} for i in ids]

    def getVertexCount(self, vtype):
        return {"Transaction": len(self.rows)} if vtype == "*" else len(self.rows)

    def getEdgeCount(self, etype):
        return {"MADE": len(self.rows)}

    def getVersion(self):
        return [{"name": "product", "version": "test-1"}]

    def upsertVertices(self, *a, **k):          # pragma: no cover - must never be reached through ReadOnly
        raise AssertionError("a write reached the graph")


@pytest.fixture
def base_db(tmp_path):
    path = tmp_path / "base.duckdb"
    con = duckdb.connect(str(path))
    cols = ", ".join(f"{c} {'TIMESTAMP' if c == 'ts' else 'BIGINT' if c == 'TransactionID' else 'VARCHAR'}" for c in TXC_COLUMNS)
    con.execute(f"CREATE TABLE txc({cols})")
    return path, con


def seed_base(con, graph: FakeGraph):
    for i, r in graph.rows.items():
        con.execute("INSERT INTO txc (TransactionID, ts) VALUES (?, ?)", [int(i), datetime.fromtimestamp(r["ts"], UTC).replace(tzinfo=None)])
    con.close()


DAYS = {"2016-07-02": 6, "2016-07-03": 5, "2016-07-09": 7, "2016-07-14": 4}      # a gap day and a window boundary on purpose


def run(tmp_path, base_db, graph, **kw):
    path, con = base_db
    seed_base(con, graph)
    return B.run_backup(B.ReadOnly(graph), path, tmp_path / "out", window_days=kw.pop("window_days", 5), sample=kw.pop("sample", 5),
                        schema_gsql="graph/schema.gsql", delta_glob="no/such/*.csv", **kw)


def test_read_only_wrapper_passes_reads_and_refuses_everything_else():
    g = B.ReadOnly(FakeGraph({"2016-07-02": 2}))
    assert g.getVertexCount("Transaction") == 2
    for name in ("upsertVertices", "delVertices", "gsql", "runLoadingJobWithFile", "installQuery"):
        with pytest.raises(PermissionError, match="never writes"):
            getattr(g, name)


def test_windows_partition_the_base_with_the_exact_expected_counts(base_db):
    path, con = base_db
    g = FakeGraph(DAYS)
    seed_base(con, g)
    wins = B.plan_windows(duckdb.connect(str(path), read_only=True), 5)
    assert sum(w.expected for w in wins) == sum(DAYS.values()) and all(w.expected for w in wins)
    assert all(a.hi <= b.lo for a, b in zip(wins, wins[1:], strict=False)) and wins[0].lo == datetime(2016, 7, 2)
    lo, hi = wins[0].epochs()
    assert hi - lo == 5 * 86400


def test_backup_writes_a_verified_deterministic_file_and_only_reads(tmp_path, base_db):
    g = FakeGraph(DAYS)
    m = run(tmp_path, base_db, g)
    assert m["rows"] == m["base_snapshot_rows"] == sum(DAYS.values())
    assert set(g.calls) <= {"getVertices", "getVerticesById"}
    rows = list(B.read_tsv_gz(tmp_path / "out" / B.FILE_NAME))
    assert [int(r[0]) for r in rows] == sorted(int(i) for i in g.rows)           # ordered by id, one row per transaction
    assert json.loads(rows[0][2])["D4"] is None or isinstance(json.loads(rows[0][2])["D1"], float)
    assert m["checks"]["re_read"] == {"sampled": 5, "differ": 0}
    assert m["checks"]["coverage"]["only_in_graph_and_not_backed_up"] == []         # nothing else lives only in the graph
    assert m["checks"]["coverage"]["in_snapshot"] == m["checks"]["coverage"]["graph_attributes"] - 3
    again = B.run_backup(B.ReadOnly(g), base_db[0], tmp_path / "out2", window_days=5, sample=0, schema_gsql="graph/schema.gsql", delta_glob="no/such/*.csv")
    assert again["sha256_file"] == m["sha256_file"] and again["sha256_content"] == m["sha256_content"]      # same data, same bytes


def test_a_short_window_is_an_error_and_leaves_no_file(tmp_path, base_db):
    g = FakeGraph(DAYS)
    g.pad = 1                                                                 # the graph answers one row short in every window
    with pytest.raises(B.BackupError, match="returned"):
        run(tmp_path, base_db, g)
    assert not list((tmp_path / "out").glob("txn_json*"))


def test_a_graph_with_extra_transactions_is_refused_unless_allowed(tmp_path, base_db):
    g = FakeGraph(DAYS)
    path, con = base_db
    seed_base(con, g)
    g.rows["9999999"] = {"ts": int(datetime(2017, 1, 5, tzinfo=UTC).timestamp()), "c_counts": "{}", "d_deltas": "{}", "m_flags": "{}"}
    with pytest.raises(B.BackupError, match="before any delta load"):
        B.run_backup(B.ReadOnly(g), path, tmp_path / "out", schema_gsql="graph/schema.gsql", delta_glob="no/such/*.csv")
    m = B.run_backup(B.ReadOnly(g), path, tmp_path / "out", sample=0, allow_count_mismatch=True, schema_gsql="graph/schema.gsql", delta_glob="no/such/*.csv")
    assert m["rows"] == sum(DAYS.values())                                     # only the base's own transactions are backed up


@pytest.mark.parametrize("breakage,message", [
    (lambda r: r.update(c_counts="not json"), "malformed values"),
    (lambda r: r.update(d_deltas=json.dumps({"D2": 1.0, "D1": 2.0})), "malformed values"),
    (lambda r: r.update(m_flags=json.dumps({f"M{i}": 1 for i in range(1, 10)})), "malformed values"),
    (lambda r: r.update(c_counts=""), "malformed values"),
    (lambda r: r.update(c_counts=None), "empty or holds a tab"),
])
def test_malformed_values_fail_the_backup(tmp_path, base_db, breakage, message):
    g = FakeGraph(DAYS)
    breakage(g.rows["3000003"])
    with pytest.raises(B.BackupError, match=message):
        run(tmp_path, base_db, g)
    assert not list((tmp_path / "out").glob("txn_json*"))


def test_a_reread_that_disagrees_fails_the_backup(tmp_path, base_db):
    g = FakeGraph(DAYS)

    class Drifting(FakeGraph):
        def getVerticesById(self, vtype, ids):
            res = super().getVerticesById(vtype, ids)
            res[0]["attributes"]["m_flags"] = flags(1) if res[0]["attributes"]["m_flags"] != flags(1) else flags(2)
            return res

    d = Drifting(DAYS)
    d.rows = g.rows
    with pytest.raises(B.BackupError, match="differ from the backup"):
        run(tmp_path, base_db, d)


def test_an_existing_backup_is_never_replaced_by_a_different_one(tmp_path, base_db):
    g = FakeGraph(DAYS)
    first = run(tmp_path, base_db, g)
    target = tmp_path / "out" / B.FILE_NAME
    before = target.read_bytes()
    g.rows["3000002"]["m_flags"] = flags(99)                                  # the graph changes under us
    with pytest.raises(B.BackupError, match="only copy"):
        B.run_backup(B.ReadOnly(g), base_db[0], tmp_path / "out", sample=0, schema_gsql="graph/schema.gsql", delta_glob="no/such/*.csv")
    assert target.read_bytes() == before and first["sha256_file"]            # the good file is untouched; the new copy sits beside it
    assert len(list((tmp_path / "out").glob("txn_json.*.tsv.gz"))) == 1


def test_ddl_attributes_are_the_checked_in_transaction_schema():
    attrs = B.ddl_attributes("graph/schema.gsql")
    assert attrs[0] == "id" and {"c_counts", "d_deltas", "m_flags", "proxy_type", "burst_id"} <= set(attrs) and len(attrs) == len(set(attrs))
