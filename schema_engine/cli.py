"""CLI for the Schema Engine (`python -m schema_engine.cli`).

Commands:
  python -m schema_engine.cli drift              # Challenge 12: detect Round 2 schema drift
  python -m schema_engine.cli approve            # Challenge 12: approve schema migration
  python -m schema_engine.cli ingest             # Merge Round 2 data into DuckDB + delta TSVs
  python -m schema_engine.cli dq                 # Challenge 11: run Data Quality Detective + taint matrix
  python -m schema_engine.cli variant --dir ...  # Challenge 13: build/map Someone Else's Schema variant
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ops.console import Col, Table, detail, header, ok, step, summary
from schema_engine.data_quality import run_data_quality_scan
from schema_engine.drift import approve_schema_migration, detect_round2_drift
from schema_engine.mapper import build_variant_fixture, infer_variant_mapping
from schema_engine.normalizer import ROOT, normalize_and_merge_round2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="HHGOA Final Round Schema Engine (Challenges 11, 12, 13)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("drift", help="Detect schema drift between Round 1 and Round 2 (Challenge 12)")
    ap_app = sub.add_parser("approve", help="Approve pending schema migration (Challenge 12)")
    ap_app.add_argument("--by", default="fraud-manager@bank")
    ap_app.add_argument("--role", default="fraud_manager")

    sub.add_parser("ingest", help="Normalize & merge Jan 2017 Round 2 data into DuckDB + delta TSVs")
    sub.add_parser("dq", help="Run Data Quality Detective & Tainted Investigations scan (Challenge 11)")

    ap_var = sub.add_parser("variant", help="Infer column mapping on a variant schema directory (Challenge 13)")
    ap_var.add_argument("--dir", default=str(ROOT / "data" / "variant_bank_b"))
    ap_var.add_argument("--generate-fixture", action="store_true", help="Generate variant dataset first")

    args = ap.parse_args(argv)

    if args.cmd == "drift":
        rep = detect_round2_drift()
        header("schema_engine.drift", "Challenge 12: Schema Drift Survival (Round 1 -> Round 2)", {"migration_id": rep["migration_id"], "approval": rep["approval"]["status"]})
        t = Table(Col("id", width=9), Col("severity", width=9), Col("title", max_width=46), Col("evidence_ids", max_width=32), title="detected schema drifts")
        for d in rep["drifts"]:
            ev = ", ".join(d["attestation"]["evidence"][:4])
            t.add_row(d["drift_id"], d["severity"], d["title"], ev)
        t.print()
        for d in rep["drifts"]:
            step(f"{d['drift_id']}: {d['title']}")
            detail(f"breaks: {d['what_breaks']}")
            detail(f"remap : {d['remapping_action']}")
        summary("drift scan complete", {"drifts": len(rep["drifts"]), "approval": rep["approval"]["status"]})
        return 0

    if args.cmd == "approve":
        rec = approve_schema_migration(approved_by=args.by, role=args.role)
        ok(f"Schema migration {rec['migration_id']} approved by {rec['approved_by']} ({rec['role']}) at {rec['approved_at']}")
        return 0

    if args.cmd == "ingest":
        header("schema_engine.ingest", "Normalizing & merging January 2017 (Round 2) into DuckDB + TigerGraph delta TSVs", {})
        res = normalize_and_merge_round2(auto_approve=True)
        t = Table(Col("table / file", max_width=28), Col("rows", align="right", width=12), title="Round 2 unified counts")
        for k, v in res["counts"].items():
            t.add_row(k, f"{v:,}")
        for k, v in res["delta_files"].items():
            t.add_row(f"delta/{k}", f"{v:,}")
        t.print()
        summary("Round 2 normalization complete", {"total txns": f"{res['counts']['txc']:,}", "closed cases": f"{res['counts']['cc']:,}", "elapsed": f"{res['elapsed_s']}s"})
        return 0

    if args.cmd == "dq":
        rep = run_data_quality_scan()
        header("schema_engine.dq", "Challenge 11: Data Quality Detective & Tainted Investigations", {"defects": len(rep["defects"]), "tainted_cases": rep["n_tainted_cases"]})
        t = Table(Col("code", width=6), Col("severity", width=9), Col("count", align="right", width=8), Col("tainted HHF cases", max_width=34), Col("title", max_width=40), title="data quality defects")
        for d in rep["defects"]:
            t.add_row(d["code"], d["severity"], f"{d['count']:,}", ", ".join(d["tainted_cases"]), d["title"])
        t.print()
        summary("data quality scan complete", {"defects": len(rep["defects"]), "tainted_cases": rep["n_tainted_cases"]})
        return 0

    if args.cmd == "variant":
        vdir = Path(args.dir)
        if args.generate_fixture or not vdir.exists():
            rep = build_variant_fixture(vdir)
        else:
            rep = infer_variant_mapping(vdir)
        header("schema_engine.variant", "Challenge 13: Someone Else's Schema (Automatic Column & Reshape Mapping)", {"dir": str(vdir), "mapped_columns": rep["n_mapped_columns"]})
        t = Table(
            Col("file", max_width=22),
            Col("source_col", max_width=22),
            Col("canonical", max_width=20),
            Col("transform", max_width=20),
            Col("conf", align="right", width=6),
            Col("sample (raw -> canonical)", max_width=36),
            title="discovered column mappings with attested samples",
        )
        for m in rep["mappings"]:
            s0 = m["sample_records"][0] if m["sample_records"] else {"raw_value": "", "canonical_value": ""}
            t.add_row(
                m["source_file"],
                m["source_column"],
                f"{m['table_role']}.{m['canonical_column']}",
                m["transform"],
                f"{m['confidence']:.2f}",
                f"{s0['raw_value']} -> {s0['canonical_value']}"[:36],
            )
        t.print()
        out_json = vdir / "discovered_mapping.json"
        out_json.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        summary("variant schema mapped", {"mapped_columns": rep["n_mapped_columns"], "manifest": str(out_json)})
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
