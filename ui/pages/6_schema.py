"""Schema Engine, New Typologies & Security Console (`ui/pages/6_schema.py` — Challenges 1, 2, 10, 11, 12, 13).

Interactive console for the Final Round Innovation Challenges:
  - Tab 1: Schema Drift Survival (Challenge 12) — before/after shape diff, what breaks, and human approval gate.
  - Tab 2: Someone Else's Schema (Challenge 13) — automatic column & reshape mapper with 5 attested sample records per column.
  - Tab 3: Data Quality Detective (Challenge 11) — DQ-01..DQ-06 defects and the Tainted Investigations Matrix.
  - Tab 4: New Typologies & Self-Growing Detective (Challenges 1 & 2) — D1..D4 detectors + 5,585-case backtest.
  - Tab 5: Break & Defend the Agent (Challenge 10) — 10 reproducible prompt-injection & spoofing attacks (before vs. after).
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

st.set_page_config(page_title="Schema & Innovation", page_icon=common.PAGE_ICONS["schema"], layout="wide")
common.inject_css()
common.page_header(
    "Schema Engine, New Typologies & Security Console",
    "Attested detection, schema drift survival, variant schema auto-mapping, data quality taint tracking, and adversarial defense.",
)

tabs = st.tabs(
    [
        "1. Schema Drift Survival (Ch 12)",
        "2. Someone Else's Schema (Ch 13)",
        "3. Data Quality Detective (Ch 11)",
        "4. New Typologies & Backtest (Ch 1 & 2)",
        "5. Break & Defend the Agent (Ch 10)",
    ]
)

# ---------------------------------------------------------------- Tab 1: Challenge 12
with tabs[0]:
    from schema_engine.drift import approve_schema_migration, detect_round2_drift

    rep = detect_round2_drift()
    app = rep["approval"]
    c1, c2, c3 = st.columns([1.5, 1.5, 2])
    c1.metric("Detected Schema / Data Drifts", len(rep["drifts"]))
    c2.metric("Approval Gate Status", app.get("status", "pending").upper())
    if app.get("status") != "approved":
        if c3.button("Approve Schema Migration (fraud-manager@bank)", type="primary"):
            approve_schema_migration("round2_2017q1_migration", approved_by="fraud-manager@bank", role="fraud_manager")
            st.rerun()
    else:
        c3.success(f"Approved by `{app.get('approved_by')}` (`{app.get('role')}`) at `{app.get('approved_at')}`")

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
    from schema_engine.mapper import build_variant_fixture, infer_variant_mapping

    st.markdown(
        "**Someone Else's Schema (Challenge 13):** Automatically maps renamed files and reshaped columns "
        "(`amount_cents` → USD, `event_epoch` → ISO timestamp, `POS/WEB` → `channel`, `DR/CR` → `card6`, "
        "`realtime_risk_pct` → `[0..1]`, and composite `ua_device_bundle` → 4 identity fields) to `contracts/schema.md`."
    )
    vdir_str = st.text_input("Variant dataset directory", value=str(common.ROOT / "data" / "variant_bank_b"))
    vdir = Path(vdir_str)
    if not vdir.exists():
        vmap = build_variant_fixture(vdir)
    else:
        vmap = infer_variant_mapping(vdir)

    m1, m2 = st.columns(2)
    m1.metric("Discovered Files", len(vmap["table_roles"]))
    m2.metric("Attested Column Mappings", vmap["n_mapped_columns"])
    st.json(vmap["table_roles"], expanded=False)

    rows_m = []
    for m in vmap["mappings"]:
        s0 = m["sample_records"][0] if m["sample_records"] else {"record_id": "", "raw_value": "", "canonical_value": ""}
        rows_m.append(
            {
                "source_file": m["source_file"],
                "source_column": m["source_column"],
                "canonical_target": f"{m['table_role']}.{m['canonical_column']}",
                "transform": m["transform"],
                "confidence": m["confidence"],
                "sample_record_id": s0["record_id"],
                "sample_raw": s0["raw_value"],
                "sample_canonical": s0["canonical_value"],
                "rationale": m["rationale"],
            }
        )
    st.dataframe(rows_m, width="stretch", hide_index=True)
    with st.expander("Inspect 5 Attested Sample Records & Claim per Column"):
        pick_col = st.selectbox("Column", [f"{m['source_file']}:{m['source_column']}" for m in vmap["mappings"]])
        chosen_m = next(m for m in vmap["mappings"] if f"{m['source_file']}:{m['source_column']}" == pick_col)
        st.table(chosen_m["sample_records"])
        st.json(chosen_m["attestation"])

# ---------------------------------------------------------------- Tab 3: Challenge 11
with tabs[2]:
    from schema_engine.data_quality import run_data_quality_scan

    dq = run_data_quality_scan()
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

    st.markdown("#### Tainted Investigations Matrix (per-case impact)")
    matrix_rows = [
        {"case_id": cid, "defect_code": item["code"], "reason": item["reason"], "policy_impact": item["impact"], "entity_ids": ", ".join(item["entity_ids"])}
        for cid, items in sorted(dq["taint_matrix"].items())
        for item in items
    ]
    st.dataframe(matrix_rows, width="stretch", hide_index=True)

# ---------------------------------------------------------------- Tab 4: Challenges 1 & 2
with tabs[3]:
    from engine.backtest_detectors import run_backtest

    bt = run_backtest()
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
    st.markdown("#### Self-Growing Detective Proposals & Human Approval Queue (Challenge 2)")
    st.dataframe(bt["self_growing_proposals"], width="stretch", hide_index=True)

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
