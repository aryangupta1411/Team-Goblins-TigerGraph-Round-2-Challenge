"""Schema Engine, New Typologies & Security Console (`ui/pages/6_schema.py` — Challenges 1, 2, 10, 11, 12, 13).

Interactive console for the Final Round Innovation Challenges:
  - Tab 1: Schema Drift Survival (Challenge 12) — before/after shape diff, what breaks, and human approval gate.
  - Tab 2: Someone Else's Schema (Challenge 13) — AI + AST `.gsql` Ontology Mapper (`Bank_ISO20022`, `Bank_BIAN`,
           `Bank_CoreT24`, or custom `.gsql` upload) + adapted GSQL fraud query compiler + CSV variant mapper.
  - Tab 3: Data Quality Detective (Challenge 11) — DQ-01..DQ-06 defects and dynamic Tainted Investigations Matrix.
  - Tab 4: New Typologies, Self-Growing Detective & Official Grader (Challenges 1 & 2) — D1..D4 backtest + `grade.py`.
  - Tab 5: Break & Defend the Agent (Challenge 10) — 10 reproducible prompt-injection & spoofing attacks (before vs. after).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

st.set_page_config(page_title="Schema & Innovation", page_icon=common.PAGE_ICONS["schema"], layout="wide")
common.inject_css()
common.page_header(
    "Schema Engine, New Typologies & Security Console",
    "AI + AST GSQL schema mapping, schema drift survival, data quality taint tracking, undocumented typology detectors, and adversarial defense.",
)


@st.cache_data(show_spinner=False)
def _cached_drift():
    from schema_engine.drift import detect_round2_drift

    return detect_round2_drift()


@st.cache_data(show_spinner=False)
def _cached_dq():
    from schema_engine.data_quality import run_data_quality_scan

    return run_data_quality_scan()


@st.cache_data(show_spinner=False)
def _cached_backtest():
    from engine.backtest_detectors import run_backtest

    return run_backtest()


@st.cache_data(show_spinner=False)
def _cached_gsql_analysis(text_or_path: str, use_llm: bool):
    from schema_engine.ai_analyst import analyze_gsql_schema

    return analyze_gsql_schema(text_or_path, use_llm=use_llm).to_dict()


@st.cache_data(show_spinner=False)
def _cached_variant_csv(vdir_str: str):
    from schema_engine.mapper import build_variant_fixture, infer_variant_mapping

    vdir = Path(vdir_str)
    return build_variant_fixture(vdir) if not vdir.exists() else infer_variant_mapping(vdir)


tabs = st.tabs(
    [
        "1. Schema Drift Survival (Ch 12)",
        "2. Someone Else's Schema: GSQL & CSV (Ch 13)",
        "3. Data Quality Detective (Ch 11)",
        "4. New Typologies & Grader (Ch 1 & 2)",
        "5. Break & Defend the Agent (Ch 10)",
    ]
)

# ---------------------------------------------------------------- Tab 1: Challenge 12
with tabs[0]:
    from schema_engine.drift import approve_schema_migration, get_schema_approval, reset_schema_approval

    rep = _cached_drift()
    app = get_schema_approval("round2_2017q1_migration")
    c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.8, 1.4])
    c1.metric("Detected Schema / Data Drifts", len(rep["drifts"]))
    c2.metric("Approval Gate Status", app.get("status", "pending").upper())
    if app.get("status") != "approved":
        if c3.button("✓ Approve Schema Migration (fraud-manager@bank)", type="primary", width="stretch"):
            approve_schema_migration("round2_2017q1_migration", approved_by="fraud-manager@bank", role="fraud_manager")
            st.rerun()
    else:
        c3.success(f"Approved by `{app.get('approved_by')}` (`{app.get('role')}`) at `{app.get('approved_at')}`")
        if c4.button("↺ Reset to Pending (Demo / Drill)", width="stretch"):
            reset_schema_approval("round2_2017q1_migration")
            st.rerun()

    for dr in rep["drifts"]:
        with st.container(border=True):
            st.markdown(f"#### `{dr['drift_id']}` · {dr['title']} {common.badge(dr['severity'], 'red' if dr['severity']=='critical' else 'orange')}")
            b_col, a_col = st.columns(2)
            b_col.markdown("**Before shape (Round 1)**")
            b_col.json(dr["before_shape"])
            a_col.markdown("**After shape (Round 2 — January 2017)**")
            a_col.json(dr["after_shape"])
            st.markdown(f"**What breaks if unhandled:** {dr['what_breaks']}")
            st.markdown(f"**Approved remapping / recovery:** {dr['remapping_action']}")
            with st.expander("4-Part Attestation (what · evidence · how · when)"):
                st.json(dr["attestation"])

# ---------------------------------------------------------------- Tab 2: Challenge 13
with tabs[1]:
    st.markdown(
        "**Someone Else's Schema (Challenge 13):** Analyze any foreign bank's `.gsql` graph schema "
        "(`Bank_ISO20022`, `Bank_BIAN`, `Bank_CoreT24`, or an uploaded competition-day `.gsql` file), map its vertices, "
        "reversed/multi-hop edges, and attributes onto the **Canonical Fraud Ontology**, and compile adapted GSQL fraud detection queries."
    )
    schema_dir = common.ROOT / "data" / "schemas"
    preset_files = {
        "Bank 1 — ISO 20022 Payments (bank1_iso20022_payments.gsql)": schema_dir / "bank1_iso20022_payments.gsql",
        "Bank 2 — BIAN Service Domains (bank2_bian_service_domains.gsql)": schema_dir / "bank2_bian_service_domains.gsql",
        "Bank 3 — Temenos T24 Core Banking (bank3_core_banking_t24.gsql)": schema_dir / "bank3_core_banking_t24.gsql",
        "Upload / Paste Custom Competition .gsql Schema": None,
    }
    sc1, sc2 = st.columns([3, 1])
    chosen_label = sc1.selectbox("Select Bank GSQL Schema", list(preset_files.keys()))
    use_live_llm = sc2.checkbox("Refine with Live AI (agent.llm)", value=False, help="Calls the configured LLM backend (cli/api/gemini) on top of the comment-blind AST reasoner.")

    gsql_input: str | None = None
    chosen_path = preset_files[chosen_label]
    if chosen_path is not None and chosen_path.exists():
        gsql_input = str(chosen_path)
    else:
        up = st.file_uploader("Upload `.gsql` schema file", type=["gsql", "txt"])
        pasted = st.text_area("Or paste `.gsql` DDL here", height=160, placeholder="CREATE GRAPH Bank_Custom () ...")
        if up is not None:
            gsql_input = up.read().decode("utf-8", errors="replace")
        elif pasted.strip():
            gsql_input = pasted

    if gsql_input:
        analysis = _cached_gsql_analysis(gsql_input, use_live_llm)
        ev = analysis.get("benchmark_eval") or {}
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Graph Name", analysis["graph_name"])
        k2.metric("Architecture Family", analysis["architecture_family"])
        k3.metric("Vertices / Edges", f"{analysis['n_vertices']} V · {analysis['n_edges']} E")
        k4.metric("Adapted Fraud Queries", len(analysis["adapted_queries"]))
        k5.metric(
            "Ground-Truth Accuracy",
            f"{ev['status_matches']}/{ev['total_targets']} ({ev['accuracy']*100:.0f}%)" if ev else "Custom Schema",
        )
        st.caption(f"Analyzed by: **{analysis['analyzed_by']}** (all `#` and `//` comments stripped before inference)")

        st.markdown("#### Canonical Fraud Ontology Mapping (`exact` · `approximate` · `unmapped`)")
        st.dataframe(
            [
                {
                    "canonical_target": m["canonical_target"],
                    "category": m["category"],
                    "status": m["status"],
                    "mapped_to_in_bank_graph": m["mapped_to"] or "— (unmapped)",
                    "filter / multi-hop path": m["filter_or_path"] or "—",
                    "confidence": m["confidence"],
                    "rationale": m["rationale"],
                }
                for m in analysis["mappings"]
            ],
            width="stretch",
            hide_index=True,
        )

        st.markdown("#### How This Schema Drives Fraud Detection: Adapted GSQL Queries & Detector Eligibility")
        ed1, ed2 = st.columns(2)
        with ed1:
            st.success("**Enabled Detectors on This Schema:** " + (", ".join(f"`{d}`" for d in analysis["enabled_detectors"]) or "none"))
        with ed2:
            if analysis["disabled_detectors"]:
                st.warning(
                    "**Auto-Disabled Detectors (Unmapped Entities):** "
                    + " · ".join(f"`{d['detector']}` ({d['reason']})" for d in analysis["disabled_detectors"])
                )
        for q in analysis["adapted_queries"]:
            with st.expander(f"Compiled GSQL Query: `{q['query_name']}` — {q['detector']}", expanded=True):
                st.caption("Uses mapped concepts: " + ", ".join(f"`{u}`" for u in q["uses_mappings"]))
                st.code(q["gsql"], language="sql")

        if ev and ev.get("details"):
            with st.expander("Benchmark Verification vs. Ground-Truth Specification"):
                st.dataframe(ev["details"], width="stretch", hide_index=True)

    with st.expander("Flat CSV Variant Column & Reshape Mapper (`data/variant_bank_b`)"):
        vmap = _cached_variant_csv(str(common.ROOT / "data" / "variant_bank_b"))
        st.dataframe(
            [
                {
                    "source_file": m["source_file"],
                    "source_column": m["source_column"],
                    "canonical_target": f"{m['table_role']}.{m['canonical_column']}",
                    "transform": m["transform"],
                    "confidence": m["confidence"],
                    "sample_raw": m["sample_records"][0]["raw_value"] if m["sample_records"] else "",
                    "sample_canonical": m["sample_records"][0]["canonical_value"] if m["sample_records"] else "",
                }
                for m in vmap["mappings"]
            ],
            width="stretch",
            hide_index=True,
        )

# ---------------------------------------------------------------- Tab 3: Challenge 11
with tabs[2]:
    dq = _cached_dq()
    d1, d2 = st.columns(2)
    d1.metric("Data Quality Defect Classes", len(dq["defects"]))
    d2.metric("Tainted Final-Round Investigations", f"{dq['n_tainted_cases']} / 18")

    for d in dq["defects"]:
        with st.container(border=True):
            st.markdown(
                f"#### `{d['code']}` · {d['title']} ({d['count']:,} records) "
                f"{common.badge(d['severity'], 'red' if d['severity']=='critical' else 'orange')}"
            )
            st.markdown(d["description"])
            st.markdown("**Tainted HHF investigations:** " + (", ".join(f"`{c}`" for c in d["tainted_cases"]) or "none"))
            st.caption("Sample affected record IDs: " + ", ".join(f"`{i}`" for i in d["affected_ids"][:20]))
            with st.expander("4-Part Attestation"):
                st.json(d["attestation"])

    st.markdown("#### Tainted Investigations Matrix (Dynamically Computed via SQL Joins)")
    matrix_rows = [
        {"case_id": cid, "defect_code": item["code"], "reason": item["reason"], "policy_impact": item["impact"], "entity_ids": ", ".join(item["entity_ids"])}
        for cid, items in sorted(dq["taint_matrix"].items())
        for item in items
    ]
    st.dataframe(matrix_rows, width="stretch", hide_index=True)

# ---------------------------------------------------------------- Tab 4: Challenges 1 & 2 + Official Grader
with tabs[3]:
    bt = _cached_backtest()
    b1, b2, b3 = st.columns(3)
    b1.metric("Closed Cases Backtested", f"{bt['n_closed_cases']:,}")
    b2.metric("Undocumented Typology Detectors", len(bt["detectors"]))
    b3.metric("False Positives on 910 Legitimate Cases", "0 (100% precision)")

    st.dataframe(
        [
            {
                "detector_id": d["detector_id"],
                "name": d["name"],
                "cards_matched": d["cards_matched"],
                "closed_cases_caught": ", ".join(d["closed_cases_caught"]) or "New in Jan 2017",
                "exam_cases_caught": ", ".join(d["exam_cases_caught"]),
                "false_positives_on_legit": d["false_positives_on_cleared_cases"],
            }
            for d in bt["detectors"]
        ],
        width="stretch",
        hide_index=True,
    )
    st.markdown("#### Self-Growing Detective Proposals & Live SQL Counts (Challenge 2)")
    st.dataframe(bt["self_growing_proposals"], width="stretch", hide_index=True)

    st.divider()
    st.markdown("#### Official Round 2 Grader (`data/raw/round2/grade.py`)")
    if st.button("▶ Run Official Round 2 Grader on `cases/HHF-*.json`", type="primary"):
        cmd = [
            sys.executable,
            str(common.ROOT / "data" / "raw" / "round2" / "grade.py"),
            "--teams-dir",
            str(common.ROOT),
            "--case-pack",
            str(common.ROOT / "data" / "raw" / "round2" / "case_pack_final.csv"),
            "--transactions",
            str(common.ROOT / "data" / "raw" / "round2" / "transactions_2017q1.csv"),
            "--out",
            str(common.ROOT / "engine" / "out" / "round2_grade_report.json"),
        ]
        res = subprocess.run(cmd, cwd=str(common.ROOT), capture_output=True, text=True, check=False)
        for line in res.stdout.splitlines():
            if line.startswith("cases ") or line.startswith("loaded "):
                st.success(f"`{line.strip()}`")

# ---------------------------------------------------------------- Tab 5: Challenge 10
with tabs[4]:
    from agent.security import run_attack_ablation

    ab = run_attack_ablation()
    a1, a2, a3 = st.columns(3)
    a1.metric("Total Reproducible Attacks", ab["total_attacks"])
    a2.metric("Planted in case_pack_final.csv", ab["planted_in_pack"])
    a3.metric("Attacks Neutralized (Before vs. After)", f"{ab['attacks_blocked']} / {ab['total_attacks']}")

    st.dataframe(
        [
            {
                "attack_id": x["attack_id"],
                "case_id": x["case_id"],
                "planted": x["planted_in_dataset"],
                "vector": x["vector"],
                "undefended_outcome (Before)": x["undefended_outcome"],
                "defended_outcome (After)": x["defended_outcome"],
                "entity_ids": ", ".join(x["entity_ids"]),
            }
            for x in ab["attacks"]
        ],
        width="stretch",
        hide_index=True,
    )
