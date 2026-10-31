"""etl/delta_export.py — the rows a month changes, in the loader's format, plus the undo pack that reverses them.

`export_delta` compares the base snapshot with the work DB (etl.delta build) stem by stem, using the SAME expressions that
etl/export_graph_csvs.py writes the full chunks with, so a delta line is byte-for-byte the line a full export would hold:

  vertex stems (customer, card, device_profile, email_domain, billing_region, closed_case)
        new = ids the base lacks, changed = same id with a different line; both go to <stem>.csv, the base's line of every
        changed id goes to undo/<stem>.csv
  edge stems (owns, involves, on_card, connected_to, matches)   new = pairs the base lacks
  txn, next                                                      only the month's transactions and the NEXT edges they add
                                                                 (the first month row on each card chains onto the base's last)
  patch_transaction_ring_hit.tsv   the few base transactions whose ring_hit flips: a partial update by id, never a full row
                                   (the base's Transaction rows have c_counts / d_deltas / m_flags only in the graph)

Anything the base has and the month lacks is a FAIL: delta mode only appends. `graph/load_all.py --data-out <out> --only ...`
uploads the chunks unchanged (the manifest prints the exact command).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import yaml

from etl import export_graph_csvs as X
from etl.delta_base import BASE_GRAPH_COUNTS, DeltaError, git_state, graph_counts, load_meta, reconstruct_base, sql_path
from etl.delta_gates import FAIL, PASS, WARN, Gate, failed, render
from ops.console import Col, Table, fail, ok, step, summary

CONTRACT = "contracts/csv_columns.yaml"
MANIFEST = "delta_manifest.json"

# (stem, graph type, number of key columns): compared line by line against the base. Edge files are their own key.
DIFF_STEMS = (("customer", "Customer", 1), ("card", "Card", 1), ("device_profile", "DeviceProfile", 1), ("email_domain", "EmailDomain", 1),
              ("billing_region", "BillingRegion", 1), ("closed_case", "ClosedCase", 1), ("owns", "OWNS", 2), ("involves", "INVOLVES", 2),
              ("on_card", "ON_CARD", 2), ("connected_to", "CONNECTED_TO", 2), ("matches", "MATCHES", 2))
VERTEX_STEMS = {"customer", "card", "device_profile", "email_domain", "billing_region", "closed_case"}
# graph/load_all.ORDER restricted to the stems a delta can carry: vertices, then transactions, then edges
LOAD_ORDER = ("customer", "card", "device_profile", "email_domain", "billing_region", "closed_case", "txn", "owns", "next", "involves",
              "on_card", "connected_to", "matches")
UNMASKED = re.compile(r"\b(?:CC-\d{4}|HHG-\d{3}|HHF-\d{3})\b|\bC\d{5}(?:-K\d)?\b|\$[\d,]+\.\d{2}")


@dataclass
class StemDelta:
    stem: str
    label: str
    base_lines: list[str]
    month_lines: list[str]
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    undo: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def key_of(line: str, n_key: int) -> tuple[str, ...]:
    return tuple(line.split("\t")[:n_key])


def diff_lines(stem: str, label: str, n_key: int, base_lines: list[str], month_lines: list[str]) -> StemDelta:
    """New / changed / removed lines of `month_lines` against `base_lines`; `undo` holds the base's line of every changed key."""
    old = {key_of(ln, n_key): ln for ln in base_lines}
    if len(old) != len(base_lines):
        raise DeltaError(f"{stem}: duplicate keys in the base export")
    d = StemDelta(stem, label, base_lines, month_lines)
    seen = set()
    for ln in month_lines:
        k = key_of(ln, n_key)
        if k in seen:
            raise DeltaError(f"{stem}: duplicate key {k} in the month export")
        seen.add(k)
        if k not in old:
            d.new.append(ln)
        elif old[k] != ln:
            d.changed.append(ln)
            d.undo.append(old[k])
    d.removed = [ln for k, ln in old.items() if k not in seen]
    return d


def roundtrip(d: StemDelta, n_key: int) -> tuple[bool, bool]:
    """(base + delta == month, month - delta + undo == base) as line sets."""
    forward = {key_of(ln, n_key): ln for ln in d.base_lines}
    for ln in d.new + d.changed:
        forward[key_of(ln, n_key)] = ln
    back = {key_of(ln, n_key): ln for ln in d.month_lines}
    for ln in d.new:
        back.pop(key_of(ln, n_key), None)
    for ln in d.undo:
        back[key_of(ln, n_key)] = ln
    return sorted(forward.values()) == sorted(d.month_lines), sorted(back.values()) == sorted(d.base_lines)


def export_lines(con: duckdb.DuckDBPyConnection, key: str, cols: list[str]) -> list[str]:
    """The TAB lines etl/export_graph_csvs.py would write for `key`, in its order, with its no-tab / no-newline guarantee."""
    out = []
    for row in con.execute(X.build_select(key, cols)).fetchall():
        for v in row:
            if v is None or "\t" in v or "\n" in v or "\r" in v:
                raise DeltaError(f"{key}: a field holds a tab, a newline or NULL ({v!r} in {row[:2]})")
        out.append(X.SEP.join(row))
    return out


def write_lines(path: Path, lines: list[str]) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    path.write_bytes(data)
    return len(data), hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def prepare_out_dir(out: Path) -> None:
    """Start from an empty delta directory. A stale txn_001.csv would be uploaded by load_all's glob, so leftovers are removed,
    but only from a directory this tool wrote (it holds a manifest) or an empty one."""
    if out.exists() and any(out.iterdir()):
        if not (out / MANIFEST).exists():
            raise DeltaError(f"{out} is not empty and is not a delta output directory (no {MANIFEST}); choose another --out")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)


def count_nonempty(path: Path, cols: list[str], names: tuple[str, ...]) -> dict[str, int]:
    idx = {n: cols.index(n) for n in names}
    n = dict.fromkeys(names, 0)
    with path.open(encoding="utf-8") as fh:
        for ln in fh:
            f = ln.rstrip("\n").split("\t")
            for name, i in idx.items():
                n[name] += 1 if f[i] != "" else 0
    return n


def parse_check(path: Path, n_cols: int, n_lines: int) -> str | None:
    """None when DuckDB reads `path` back as TAB, no quote, `n_cols` columns, `n_lines` rows; otherwise what went wrong."""
    cols = ", ".join(f"'c{i}': 'VARCHAR'" for i in range(n_cols))
    try:
        got = duckdb.connect().execute(f"SELECT count(*) FROM read_csv('{sql_path(path)}', delim='\t', quote='', escape='', header=false, all_varchar=true, columns={{{cols}}})").fetchone()[0]
    except duckdb.Error as exc:
        return f"{path.name}: {str(exc).splitlines()[0][:140]}"
    return None if got == n_lines else f"{path.name}: {got} rows read back, {n_lines} written"


def export_delta(base_db: str, work_db: str, out_dir: str, yaml_path: str = CONTRACT) -> int:
    t0 = time.time()
    meta = load_meta(work_db)
    build_gates = [Gate(**g) for g in meta["gates"]]
    if failed(build_gates):
        raise DeltaError(f"the build has failing gates ({', '.join(g.name for g in failed(build_gates))}); fix them and run `etl.delta build` again")
    out = Path(out_dir)
    if out.resolve() == Path("data/out").resolve():
        raise DeltaError("refusing to write the delta into data/out (the full export directory)")
    prepare_out_dir(out)
    spec = yaml.safe_load(Path(yaml_path).read_text())
    cols = {X.file_key(p): [c["name"] for c in f["columns"]] for p, f in spec["files"].items()}

    step("replaying the base through the same SQL (shipped card and device rows)")
    base = reconstruct_base(base_db)
    work = duckdb.connect(str(work_db), read_only=True)
    drv = duckdb.connect()
    drv.execute(f"ATTACH '{sql_path(work_db)}' AS w (READ_ONLY)")
    drv.execute(f"ATTACH '{sql_path(base_db)}' AS e (READ_ONLY)")
    drv.execute("CREATE VIEW txc AS SELECT * FROM w.txc")
    # the month's transactions only: the exporter's own SQL then writes just the rows (and NEXT edges) the base lacks
    drv.execute("CREATE VIEW txn_feat AS SELECT f.* FROM w.txn_feat f WHERE NOT EXISTS (SELECT 1 FROM e.txc b WHERE b.TransactionID = f.TransactionID)")

    step("comparing every stem with the base")
    deltas: dict[str, StemDelta] = {}
    for stem, label, n_key in DIFF_STEMS:
        deltas[stem] = diff_lines(stem, label, n_key, export_lines(base, stem, cols[stem]), export_lines(work, stem, cols[stem]))

    files: dict[str, list[dict]] = {}
    new_vertices: dict[str, list[str]] = {}
    rows: dict[str, dict[str, int]] = {}
    for stem, label, _ in DIFF_STEMS:
        d = deltas[stem]
        body = d.new + d.changed
        if body:
            size, sha = write_lines(out / f"{stem}.csv", body)
            files[stem] = [{"file": f"{stem}.csv", "rows": len(body), "bytes": size, "sha256": sha}]
        if d.undo:
            write_lines(out / "undo" / f"{stem}.csv", d.undo)
        if stem in VERTEX_STEMS and d.new:
            new_vertices[label] = [key_of(ln, 1)[0] for ln in d.new]
        rows[stem] = {"new": len(d.new), "changed": len(d.changed), "removed": len(d.removed)}

    step("the month's transactions and the NEXT edges they add (the exporter's own SQL)")
    txn_parts = X.write_file(drv, "txn", cols["txn"], str(out / "txn_NNN.csv"), chunk=True)
    next_parts = X.write_file(drv, "next", cols["next"], str(out / "next.csv"))
    n_txn = sum(p[1] for p in txn_parts)
    n_next = sum(p[1] for p in next_parts)
    files["txn"] = [{"file": Path(p).name, "rows": n, "bytes": b, "sha256": file_sha256(Path(p))} for p, n, b in txn_parts]
    files["next"] = [{"file": Path(p).name, "rows": n, "bytes": b, "sha256": file_sha256(Path(p))} for p, n, b in next_parts]
    rows["txn"] = {"new": n_txn, "changed": 0, "removed": 0}
    rows["next"] = {"new": n_next, "changed": 0, "removed": 0}
    if not n_next:
        files.pop("next")
        Path(next_parts[0][0]).unlink(missing_ok=True)
    new_vertices["Transaction"] = [str(r[0]) for r in drv.execute("SELECT TransactionID FROM txn_feat ORDER BY 1").fetchall()]
    edge_cols = count_nonempty_all(txn_parts, cols["txn"])

    flips = drv.execute("SELECT t.TransactionID, t.ring_hit, b.ring_hit FROM w.txn_feat t JOIN e.txc b USING (TransactionID) WHERE t.ring_hit <> b.ring_hit ORDER BY 1").fetchall()
    patches: dict = {}
    if flips:
        tf = {True: "true", False: "false"}
        size, sha = write_lines(out / "patch_transaction_ring_hit.tsv", [f"{i}\t{tf[v2]}" for i, v2, _ in flips])
        write_lines(out / "undo" / "patch_transaction_ring_hit.tsv", [f"{i}\t{tf[v1]}" for i, _, v1 in flips])
        patches["transaction_ring_hit"] = {"file": "patch_transaction_ring_hit.tsv", "rows": len(flips), "bytes": size, "sha256": sha,
                                           "columns": ["Transaction.id", "ring_hit"], "apply": "partial vertex upsert by id (REST), never the loading job"}

    texts = drv.execute("SELECT id, embed_text FROM w.closed_case_parsed WHERE id NOT IN (SELECT case_id FROM e.cc) ORDER BY id").fetchall()
    if texts:
        with (out / "closed_case_embed.jsonl").open("w", encoding="utf-8") as fh:
            for case_id, text in texts:
                fh.write(json.dumps({"id": case_id, "text": text}) + "\n")
    (out / "undo").mkdir(exist_ok=True)
    (out / "undo" / "new_vertices.json").write_text(json.dumps(new_vertices, indent=1))

    step("totals, round trips and the loader format")
    gates: list[Gate] = []
    before = graph_counts(base)
    after = graph_counts(work)
    new_counts = {"Transaction": n_txn, "MADE": n_txn, "NEXT": n_next, **edge_cols, **{label: len(deltas[stem].new) for stem, label, _ in DIFF_STEMS}}
    off = []
    for k in BASE_GRAPH_COUNTS:
        if before[k] + new_counts.get(k, 0) != after[k]:
            off.append(f"{k}: base {before[k]:,} + {new_counts.get(k, 0):,} new = {before[k] + new_counts.get(k, 0):,}, the month derives {after[k]:,}")
    gates.append(Gate("delta_totals", FAIL if off else PASS,
                      "; ".join(off[:2]) if off else f"base + new rows = the month's totals for all {len(BASE_GRAPH_COUNTS)} vertex and edge types "
                      f"(Transaction {before['Transaction']:,} + {n_txn:,} = {after['Transaction']:,})", {"lines": off}))
    bad_rt, removed = [], []
    for stem, _, n_key in DIFF_STEMS:
        fwd, back = roundtrip(deltas[stem], n_key)
        if not (fwd and back):
            bad_rt.append(f"{stem}: base + delta {'=' if fwd else '!='} month, month - delta + undo {'=' if back else '!='} base")
        if deltas[stem].removed:
            removed.append(f"{stem}: {len(deltas[stem].removed)} base lines are gone from the month")
    gates.append(Gate("roundtrip", FAIL if bad_rt or removed else PASS,
                      "; ".join((bad_rt + removed)[:2]) if bad_rt or removed else
                      f"for {len(DIFF_STEMS)} stems: base + delta = month and month - delta + undo = base; nothing removed (append-only)", {"lines": bad_rt + removed}))
    problems = [p for stem, parts in files.items() for f in parts if (p := parse_check(out / f["file"], len(cols[stem]), f["rows"]))]
    gates.append(Gate("loader_format", FAIL if problems else PASS,
                      "; ".join(problems[:2]) if problems else f"{sum(len(v) for v in files.values())} files read back as TAB, no quote, at the contracted column counts", {"lines": problems}))
    leaks = [(i, m.group(0)) for i, t in texts if (m := UNMASKED.search(t))]
    gates.append(Gate("embed_masking", FAIL if leaks else PASS, f"{len(leaks)} unmasked ids in {len(texts)} embed texts" if leaks else
                      f"{len(texts)} embed texts for the new closed cases carry no case id, card id or cent amount", {"lines": [f"{i}: {t}" for i, t in leaks]}))
    hist = next((g for g in build_gates if g.name == "history_invariance"), None)
    want_flips = len(hist.detail.get("ring_hit_flips", [])) if hist else 0
    gates.append(Gate("patches", FAIL if want_flips != len(flips) else (WARN if flips else PASS),
                      (f"{len(flips)} base transactions get a ring_hit patch (partial update, not a row rewrite)" if flips else "no base row needs a patch")
                      if want_flips == len(flips) else f"history gate saw {want_flips} flips, the export wrote {len(flips)}",
                      {"lines": [f"{i}: ring_hit {v1} -> {v2}" for i, v2, v1 in flips[:12]]}))

    digest = hashlib.sha256(json.dumps({k: [(f["file"], f["sha256"]) for f in v] for k, v in sorted(files.items())}, sort_keys=True).encode()).hexdigest()
    order = [s for s in LOAD_ORDER if s in files]
    manifest = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "git": git_state(), "inputs": meta["stats"]["inputs"],
        "score_mode": meta["stats"]["score"], "scorer": meta["stats"].get("scorer"),
        "base_graph_counts": before, "expected_after": after, "new_rows": new_counts,
        "stems": {s: {"rows": rows[s], "files": files.get(s, [])} for s in LOAD_ORDER if s in rows},
        "patches": patches, "closed_case_embed": "closed_case_embed.jsonl" if texts else None, "delta_digest": digest,
        "load": {"order": order, "command": f"uv run python graph/load_all.py --data-out {out_dir} --only {' '.join(order)} --skip-counts",
                 "then": ["apply patch_transaction_ring_hit.tsv by id", "re-run the algorithms (a Card upsert resets wcc_id / device_degree / fraud_ppr)",
                          "embed closed_case_embed.jsonl with the base model and load the 20 vectors", "verify the live counts against expected_after"],
                 "undo": "undo/ holds the base's line of every changed row, undo/new_vertices.json the vertices to delete"},
        "build_gates": [g.as_dict() for g in build_gates], "export_gates": [g.as_dict() for g in gates],
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1, default=str))
    # the same row / column manifest etl/export_graph_csvs.py writes, so graph/load_all.py's manifest check covers the delta files too
    (out / "manifest.json").write_text(json.dumps({f["file"]: {"rows": f["rows"], "bytes": f["bytes"], "columns": len(cols[stem])}
                                                   for stem, parts in files.items() for f in parts}, indent=1))
    (out / "load_plan.md").write_text(load_plan(manifest))

    t = Table(Col("stem", max_width=16), Col("new", align="right", width=9), Col("changed", align="right", width=8), Col("undo rows", align="right", width=9),
              Col("bytes", align="right", width=12), title="delta rows (what a load would write)")
    for s in order:
        t.add_row(s, f"{rows[s]['new']:,}", f"{rows[s]['changed']:,}", f"{len(deltas[s].undo):,}" if s in deltas else "-", f"{sum(f['bytes'] for f in files[s]):,}")
    t.print()
    render(gates, "export gates")
    bad = failed(gates)
    summary("etl.delta export " + ("stopped" if bad else "complete"),
            {"out": out_dir, "delta digest": digest[:16], "files": sum(len(v) for v in files.values()), "patch rows": len(flips),
             "expected Transaction after": f"{after['Transaction']:,}", "seconds": round(time.time() - t0, 1)}, status="fail" if bad else "ok")
    if not bad:
        ok(f"next (not run here): {manifest['load']['command']}")
    else:
        for g in bad:
            fail(f"{g.name}: {g.summary}")
    work.close()
    drv.close()
    base.close()
    return 1 if bad else 0


def count_nonempty_all(txn_parts: list, txn_cols: list[str]) -> dict[str, int]:
    """FROM_DEVICE / PURCHASER_EMAIL / RECIPIENT_EMAIL / BILLED_IN edges the month's transaction lines create (non-empty foreign keys)."""
    total = dict.fromkeys(("device_id", "p_email", "r_email", "addr1"), 0)
    for path, _, _ in txn_parts:
        for k, v in count_nonempty(Path(path), txn_cols, tuple(total)).items():
            total[k] += v
    return {"FROM_DEVICE": total["device_id"], "PURCHASER_EMAIL": total["p_email"], "RECIPIENT_EMAIL": total["r_email"], "BILLED_IN": total["addr1"]}


def load_plan(m: dict) -> str:
    rows = "\n".join(f"| {s} | {v['rows']['new']:,} | {v['rows']['changed']:,} |" for s, v in m["stems"].items() if v["files"])
    after = m["expected_after"]
    return f"""# Delta load plan (generated; nothing here has been run)

Month appended to the base snapshot. Gates: {sum(1 for g in m['build_gates'] + m['export_gates'] if g['status'] == 'FAIL')} failing.

| stem | new rows | changed rows |
|---|---:|---:|
{rows}

Load, in order:

1. `{m['load']['command']}`
2. {m['load']['then'][0]}
3. {m['load']['then'][1]}
4. {m['load']['then'][2]}
5. {m['load']['then'][3]}

Expected live counts afterwards: Transaction {after['Transaction']:,}, NEXT {after['NEXT']:,}, ClosedCase {after['ClosedCase']:,}, DeviceProfile {after['DeviceProfile']:,}, BillingRegion {after['BillingRegion']:,}.

Undo: {m['load']['undo']}.
"""
