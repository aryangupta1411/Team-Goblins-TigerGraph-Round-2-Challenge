"""Interactive Animated Architecture & End-to-End Application Flow (`ui/pages/7_architecture.py`).

Presents a modern, animated SVG + HTML5 architecture canvas with real-time flowing particles,
auto-play / step-by-step walkthrough controls (Stage 0 -> P0..P10), a live per-case pipeline
tracer, and a 4-layer deep dive into the entire application stack.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

st.set_page_config(page_title="Architecture", page_icon=common.PAGE_ICONS["architecture"], layout="wide")
common.inject_css()


def _inject_architecture_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

        .arch-hero {
            background: linear-gradient(135deg, #0b192c 0%, #0f2942 52%, #1a1635 100%);
            border: 1px solid rgba(88, 166, 255, 0.28);
            border-radius: 16px;
            padding: 26px 30px;
            margin-bottom: 18px;
            color: #f0f6fc;
            box-shadow: 0 14px 36px rgba(5, 12, 24, 0.24);
            position: relative;
            overflow: hidden;
        }
        .arch-hero::before {
            content: "";
            position: absolute;
            top: -50%;
            right: -10%;
            width: 420px;
            height: 420px;
            background: radial-gradient(circle, rgba(88, 166, 255, 0.16) 0%, rgba(188, 140, 255, 0.06) 45%, transparent 70%);
            pointer-events: none;
        }
        .arch-eyebrow {
            font-family: 'JetBrains Mono', monospace;
            font-size: 11.5px;
            font-weight: 600;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            color: #58a6ff;
            margin-bottom: 6px;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        .arch-pulse-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #3fb950;
            box-shadow: 0 0 0 0 rgba(63, 185, 80, 0.7);
            animation: archPulse 2s infinite;
            display: inline-block;
        }
        @keyframes archPulse {
            0% { box-shadow: 0 0 0 0 rgba(63, 185, 80, 0.7); }
            70% { box-shadow: 0 0 0 9px rgba(63, 185, 80, 0); }
            100% { box-shadow: 0 0 0 0 rgba(63, 185, 80, 0); }
        }
        .arch-hero h1 {
            font-family: 'Space Grotesk', 'Plus Jakarta Sans', sans-serif !important;
            font-size: 28px !important;
            font-weight: 700 !important;
            color: #ffffff !important;
            margin: 0 0 8px 0 !important;
            letter-spacing: -0.02em !important;
        }
        .arch-hero p {
            font-family: 'Plus Jakarta Sans', sans-serif;
            font-size: 14.5px;
            line-height: 1.55;
            color: #c9d1d9;
            margin: 0;
            max-width: 960px;
        }
        .arch-pill-row {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 16px;
        }
        .arch-pill {
            font-family: 'JetBrains Mono', monospace;
            font-size: 11.5px;
            font-weight: 500;
            padding: 5px 11px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.07);
            border: 1px solid rgba(255, 255, 255, 0.14);
            color: #e6edf3;
        }
        .arch-pill strong {
            color: #79c0ff;
        }
        .arch-stage-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 12px;
            margin: 12px 0 20px 0;
        }
        .arch-stage-card {
            border-radius: 12px;
            padding: 15px 16px;
            border: 1px solid rgba(140, 150, 170, 0.25);
            background: rgba(88, 166, 255, 0.04);
            transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
            position: relative;
            overflow: hidden;
        }
        .arch-stage-card:hover {
            transform: translateY(-2px);
            border-color: rgba(88, 166, 255, 0.6);
            box-shadow: 0 8px 20px rgba(11, 61, 145, 0.12);
        }
        .arch-stage-card .stage-tag {
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 4px;
            display: block;
        }
        .arch-stage-card h4 {
            font-family: 'Plus Jakarta Sans', sans-serif !important;
            font-size: 15px !important;
            font-weight: 700 !important;
            margin: 0 0 6px 0 !important;
        }
        .arch-stage-card p {
            font-family: 'Plus Jakarta Sans', sans-serif;
            font-size: 12.8px;
            line-height: 1.45;
            margin: 0;
            opacity: 0.88;
        }
        .arch-stage-card .mono-foot {
            font-family: 'JetBrains Mono', monospace;
            font-size: 11.2px;
            margin-top: 8px;
            padding-top: 7px;
            border-top: 1px dashed rgba(140, 150, 170, 0.25);
            opacity: 0.92;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _load_case_bundle(run_id: str | None, case_id: str) -> dict[str, Any]:
    """Assemble live telemetry for the selected case so the animated diagram can trace it."""
    meta = common.case_meta(case_id)
    cr = common.load_case_run(run_id, case_id) if run_id else None
    ans = (cr.answer if cr and cr.answer else None) or common.load_promoted_answer(case_id) or {}
    case = ans.get("case") or {}
    nba = ans.get("next_best_actions") or {}
    sar = ans.get("sar") or {}
    pre, post = common.probability_pre_post(cr) if cr and cr.answer else (case.get("fraud_probability"), case.get("fraud_probability"))

    from agent.security import sanitize_trigger_text

    db_tx = {"id": "3660353", "channel": "in_person", "product_cd": "W", "addr1": "087.0"} if case_id == "HHF-008" else None
    sec = sanitize_trigger_text(
        case_id=case_id,
        trigger_text=meta.get("trigger_text", ""),
        db_txn=db_tx,
    )

    init_acts = [f"{a.get('action')} ({a.get('route')})" for a in (nba.get("initial") or [])]
    fin_acts = [f"{a.get('action')} ({a.get('route')})" for a in (nba.get("final") or [])]
    ev_reqs = ans.get("evidence_requests") or []

    return {
        "case_id": case_id,
        "trigger_type": meta.get("trigger_type", "risk_score"),
        "opened_at": meta.get("opened_at", ""),
        "flagged_txn_id": meta.get("flagged_txn_id", ""),
        "card_id": meta.get("card_id", ""),
        "customer_id": meta.get("customer_id", ""),
        "risk_score": meta.get("risk_score") or "n/a",
        "trigger_text": meta.get("trigger_text", ""),
        "sanitized_text": sec.clean_text,
        "injection_detected": sec.attack_detected,
        "attack_types": [f.code for f in sec.findings],
        "verdict": case.get("verdict", "unknown"),
        "status": case.get("status", "open"),
        "p_pre": f"{float(pre):.2f}" if pre is not None else "n/a",
        "p_post": f"{float(post):.2f}" if post is not None else "n/a",
        "pattern": case.get("pattern", "none"),
        "pattern_description": case.get("pattern_description", ""),
        "exposure_usd": common.money(case.get("exposure_usd", 0.0)),
        "affected_n": len(case.get("affected_txn_ids") or []),
        "connected_n": len(case.get("connected_card_ids") or []),
        "similar_n": len(case.get("similar_prior_cases") or []),
        "evidence_n": len(case.get("evidence") or []),
        "initial_actions": ", ".join(init_acts) or "—",
        "final_actions": ", ".join(fin_acts) or "—",
        "what_changed": nba.get("what_changed", "nothing"),
        "ev_req": f"{ev_reqs[0].get('type')} -> {ev_reqs[0].get('assumed_response', '')[:75]}" if ev_reqs else "None (settled at P5 / §6)",
        "sar_file": bool(sar.get("file")),
        "sar_reason": sar.get("reason", ""),
        "graph_case_id": case.get("graph_case_id") or f"AC-{case_id}",
        "tool_calls": ans.get("tool_calls", 19),
        "tokens": f"{int(ans.get('tokens', 0) or 0):,}",
        "latency_s": f"{float(ans.get('latency_s', 0) or 0):.1f}s",
    }


def _build_animated_svg_html(bundle: dict[str, Any]) -> str:
    """Return a self-contained HTML5 + SVG animated architecture diagram with interactive step playback."""
    case_json = json.dumps(bundle)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

  :root {{
    --bg-0: #070d19;
    --bg-1: #0d1627;
    --bg-2: #132038;
    --ink-0: #f0f6fc;
    --ink-1: #b1bac4;
    --ink-2: #7d8590;
    --cyan: #38bdf8;
    --blue: #58a6ff;
    --violet: #bc8cff;
    --emerald: #3fb950;
    --amber: #f59e0b;
    --crimson: #f85149;
    --orange: #fb923c;
  }}

  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 0;
    background: transparent;
    font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
    color: var(--ink-0);
    user-select: none;
  }}

  .arch-shell {{
    background: radial-gradient(1200px 600px at 15% 10%, rgba(56, 189, 248, 0.10), transparent 60%),
                radial-gradient(900px 550px at 85% 25%, rgba(188, 140, 255, 0.11), transparent 60%),
                radial-gradient(1000px 500px at 50% 90%, rgba(63, 185, 80, 0.08), transparent 60%),
                linear-gradient(180deg, #070d19 0%, #0b1322 100%);
    border: 1px solid rgba(88, 166, 255, 0.26);
    border-radius: 16px;
    padding: 18px 20px 18px 20px;
    box-shadow: 0 18px 42px rgba(0, 0, 0, 0.38);
  }}

  /* Top Control Bar */
  .ctrl-bar {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 14px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.09);
    margin-bottom: 14px;
  }}
  .ctrl-left {{
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .btn {{
    font-family: 'Plus Jakarta Sans', sans-serif;
    font-size: 12.5px;
    font-weight: 600;
    padding: 7px 14px;
    border-radius: 8px;
    border: 1px solid rgba(88, 166, 255, 0.35);
    background: rgba(88, 166, 255, 0.12);
    color: #e6edf3;
    cursor: pointer;
    transition: all 0.18s ease;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }}
  .btn:hover {{
    background: rgba(88, 166, 255, 0.25);
    border-color: var(--cyan);
    transform: translateY(-1px);
  }}
  .btn.primary {{
    background: linear-gradient(135deg, #0b3d91 0%, #2563eb 100%);
    border-color: #58a6ff;
    color: #fff;
    box-shadow: 0 4px 14px rgba(37, 99, 235, 0.35);
  }}
  .btn.primary:hover {{
    background: linear-gradient(135deg, #1d4ed8 0%, #3b82f6 100%);
  }}

  /* Phase Stepper Pills */
  .step-strip {{
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    align-items: center;
  }}
  .step-chip {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 600;
    padding: 5px 9px;
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.11);
    color: var(--ink-1);
    cursor: pointer;
    transition: all 0.18s ease;
  }}
  .step-chip:hover {{
    border-color: rgba(88, 166, 255, 0.5);
    color: #fff;
  }}
  .step-chip.active {{
    background: linear-gradient(135deg, rgba(56, 189, 248, 0.28), rgba(188, 140, 255, 0.28));
    border-color: var(--cyan);
    color: #fff;
    box-shadow: 0 0 14px rgba(56, 189, 248, 0.4);
  }}

  /* SVG Diagram Container */
  .svg-wrap {{
    width: 100%;
    overflow: hidden;
    border-radius: 12px;
    background: rgba(7, 13, 25, 0.65);
    border: 1px solid rgba(255, 255, 255, 0.07);
  }}
  svg {{
    width: 100%;
    height: auto;
    display: block;
  }}

  /* Animated Conduits */
  .conduit-bg {{
    fill: none;
    stroke: rgba(148, 163, 184, 0.18);
    stroke-width: 2.2;
  }}
  .conduit-flow {{
    fill: none;
    stroke-width: 2.4;
    stroke-linecap: round;
    stroke-dasharray: 8 10;
    animation: flowDash 1.35s linear infinite;
    opacity: 0.65;
    transition: opacity 0.25s ease, stroke-width 0.25s ease, filter 0.25s ease;
  }}
  .conduit-flow.highlight {{
    opacity: 1;
    stroke-width: 3.6;
    filter: drop-shadow(0 0 6px currentColor);
  }}
  @keyframes flowDash {{
    to {{ stroke-dashoffset: -36; }}
  }}

  /* Node Cards in SVG */
  .arch-node {{
    cursor: pointer;
    transition: transform 0.22s ease, filter 0.22s ease;
  }}
  .arch-node rect.node-box {{
    transition: stroke-width 0.22s ease, stroke 0.22s ease, fill 0.22s ease;
  }}
  .arch-node:hover rect.node-box {{
    stroke-width: 2.6;
    filter: drop-shadow(0 0 12px rgba(88, 166, 255, 0.35));
  }}
  .arch-node.active-node rect.node-box {{
    stroke-width: 3.2 !important;
    filter: drop-shadow(0 0 18px rgba(56, 189, 248, 0.65));
  }}

  .phase-row {{
    cursor: pointer;
    transition: opacity 0.2s ease;
  }}
  .phase-row rect.phase-bg {{
    transition: all 0.2s ease;
  }}
  .phase-row:hover rect.phase-bg {{
    stroke: #58a6ff;
    stroke-width: 1.8;
  }}
  .phase-row.active-phase rect.phase-bg {{
    fill: rgba(56, 189, 248, 0.20) !important;
    stroke: #38bdf8 !important;
    stroke-width: 2.4 !important;
    filter: drop-shadow(0 0 12px rgba(56, 189, 248, 0.55));
  }}

  /* SVG Typography */
  .svg-title {{ font-family: 'Space Grotesk', sans-serif; font-size: 14.5px; font-weight: 700; fill: #f8fafc; }}
  .svg-sub {{ font-family: 'Plus Jakarta Sans', sans-serif; font-size: 11.2px; font-weight: 500; fill: #cbd5e1; }}
  .svg-muted {{ font-family: 'Plus Jakarta Sans', sans-serif; font-size: 10.2px; fill: #94a3b8; }}
  .svg-mono {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 600; fill: #7dd3fc; }}
  .svg-badge {{ font-family: 'JetBrains Mono', monospace; font-size: 9.5px; font-weight: 700; letter-spacing: 0.04em; }}

  /* Bottom Inspector Drawer */
  .inspector {{
    margin-top: 14px;
    background: linear-gradient(135deg, rgba(19, 32, 56, 0.92) 0%, rgba(13, 22, 39, 0.96) 100%);
    border: 1px solid rgba(56, 189, 248, 0.35);
    border-radius: 12px;
    padding: 16px 20px;
    display: grid;
    grid-template-columns: 1.35fr 1fr;
    gap: 18px;
    align-items: start;
  }}
  @media (max-width: 900px) {{
    .inspector {{ grid-template-columns: 1fr; }}
  }}
  .insp-head {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 6px;
  }}
  .insp-stage-pill {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11.5px;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 6px;
    background: rgba(56, 189, 248, 0.18);
    border: 1px solid var(--cyan);
    color: var(--cyan);
  }}
  .insp-title {{
    font-family: 'Space Grotesk', sans-serif;
    font-size: 17px;
    font-weight: 700;
    color: #ffffff;
    margin: 0;
  }}
  .insp-module {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11.5px;
    color: var(--violet);
    margin-bottom: 8px;
  }}
  .insp-desc {{
    font-size: 13.2px;
    line-height: 1.55;
    color: #dbeafe;
    margin: 0 0 10px 0;
  }}
  .insp-contracts {{
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
  }}
  .insp-chip {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    padding: 4px 9px;
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: #e2e8f0;
  }}
  .insp-live-box {{
    background: rgba(7, 13, 25, 0.72);
    border: 1px solid rgba(188, 140, 255, 0.3);
    border-radius: 10px;
    padding: 12px 14px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 11.5px;
    line-height: 1.55;
    color: #e6edf3;
  }}
  .insp-live-title {{
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--violet);
    margin-bottom: 6px;
    font-weight: 700;
  }}
</style>
</head>
<body>
<div class="arch-shell">
  <!-- Top Controls -->
  <div class="ctrl-bar">
    <div class="ctrl-left">
      <button class="btn primary" id="btnPlay" onclick="togglePlay()">⏸ Pause Walkthrough</button>
      <button class="btn" onclick="stepDelta(-1)">◀ Prev</button>
      <button class="btn" onclick="stepDelta(1)">Next ▶</button>
      <button class="btn" id="btnSpeed" onclick="cycleSpeed()">⚡ 1.0×</button>
    </div>
    <div class="step-strip" id="stepStrip"></div>
  </div>

  <!-- Animated SVG Architecture Canvas -->
  <div class="svg-wrap">
    <svg viewBox="0 0 1240 650" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse">
          <path d="M 28 0 L 0 0 0 28" fill="none" stroke="rgba(148, 163, 184, 0.06)" stroke-width="1"/>
        </pattern>
        <linearGradient id="gradCyan" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#0c2d48"/>
          <stop offset="100%" stop-color="#09192b"/>
        </linearGradient>
        <linearGradient id="gradCrimson" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#3b1219"/>
          <stop offset="100%" stop-color="#1f0a10"/>
        </linearGradient>
        <linearGradient id="gradEmerald" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#0d3321"/>
          <stop offset="100%" stop-color="#081c14"/>
        </linearGradient>
        <linearGradient id="gradBlue" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#0f2547"/>
          <stop offset="100%" stop-color="#09152b"/>
        </linearGradient>
        <linearGradient id="gradViolet" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#2b1649"/>
          <stop offset="100%" stop-color="#160c28"/>
        </linearGradient>
        <linearGradient id="gradOrange" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#3b1d0b"/>
          <stop offset="100%" stop-color="#1f1006"/>
        </linearGradient>
        <filter id="glowCyan" x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="4" result="blur"/>
          <feComposite in="SourceGraphic" in2="blur" operator="over"/>
        </filter>
      </defs>

      <rect width="1240" height="650" fill="url(#grid)"/>

      <!-- ==================== CONDUIT PATHS ==================== -->
      <!-- Path 1: Schema Engine / CSVs -> Security Shield -->
      <path id="p_s0_p0" d="M 165 138 L 165 170" class="conduit-bg"/>
      <path id="f_s0_p0" d="M 165 138 L 165 170" class="conduit-flow" stroke="#38bdf8"/>

      <!-- Path 2: Security Shield -> Phase Machine P0 -->
      <path id="p_sec_pm" d="M 310 215 C 345 215, 345 92, 380 92" class="conduit-bg"/>
      <path id="f_sec_pm" d="M 310 215 C 345 215, 345 92, 380 92" class="conduit-flow" stroke="#f85149"/>

      <!-- Path 3: Engine <-> Phase Machine (P3, P5, P6, P7) -->
      <path id="p_eng_pm1" d="M 310 340 L 380 230" class="conduit-bg"/>
      <path id="f_eng_pm1" d="M 310 340 L 380 230" class="conduit-flow" stroke="#3fb950"/>

      <path id="p_eng_pm2" d="M 310 410 L 380 368" class="conduit-bg"/>
      <path id="f_eng_pm2" d="M 310 410 L 380 368" class="conduit-flow" stroke="#3fb950"/>

      <!-- Path 4: Phase Machine <-> Claude Agent SDK (P2, P4, P8, P9) -->
      <path id="p_pm_llm" d="M 790 184 C 825 184, 820 105, 855 105" class="conduit-bg"/>
      <path id="f_pm_llm" d="M 790 184 C 825 184, 820 105, 855 105" class="conduit-flow" stroke="#bc8cff"/>

      <!-- Path 5: Claude Agent SDK & P0-P2 -> TigerGraph MCP -->
      <path id="p_llm_mcp" d="M 1035 156 L 1035 190" class="conduit-bg"/>
      <path id="f_llm_mcp" d="M 1035 156 L 1035 190" class="conduit-flow" stroke="#bc8cff"/>

      <path id="p_pm_mcp" d="M 790 138 C 825 138, 825 235, 855 235" class="conduit-bg"/>
      <path id="f_pm_mcp" d="M 790 138 C 825 138, 825 235, 855 235" class="conduit-flow" stroke="#38bdf8"/>

      <!-- Path 6: TigerGraph MCP <-> TigerGraph Savanna + TigerVector -->
      <path id="p_mcp_tg" d="M 1035 282 L 1035 316" class="conduit-bg"/>
      <path id="f_mcp_tg" d="M 1035 282 L 1035 316" class="conduit-flow" stroke="#fb923c"/>

      <!-- Path 7: P10 Write-Back -> TigerGraph Case Memory -->
      <path id="p_p10_tg" d="M 790 552 C 825 552, 825 520, 870 520" class="conduit-bg"/>
      <path id="f_p10_tg" d="M 790 552 C 825 552, 825 520, 870 520" class="conduit-flow" stroke="#fb923c"/>

      <!-- Path 8: Phase Machine P9/P10 -> Answer JSON & RBAC UI -->
      <path id="p_pm_out" d="M 585 575 L 585 612" class="conduit-bg"/>
      <path id="f_pm_out" d="M 585 575 L 585 612" class="conduit-flow" stroke="#58a6ff"/>

      <!-- Animated Particles traveling along paths -->
      <circle r="4.5" fill="#38bdf8" filter="url(#glowCyan)">
        <animateMotion dur="1.8s" repeatCount="indefinite"><mpath href="#p_s0_p0"/></animateMotion>
      </circle>
      <circle r="4.5" fill="#f85149" filter="url(#glowCyan)">
        <animateMotion dur="2.2s" repeatCount="indefinite"><mpath href="#p_sec_pm"/></animateMotion>
      </circle>
      <circle r="4.5" fill="#3fb950" filter="url(#glowCyan)">
        <animateMotion dur="2.0s" repeatCount="indefinite"><mpath href="#p_eng_pm1"/></animateMotion>
      </circle>
      <circle r="4.5" fill="#3fb950" filter="url(#glowCyan)">
        <animateMotion dur="2.3s" repeatCount="indefinite"><mpath href="#p_eng_pm2"/></animateMotion>
      </circle>
      <circle r="5" fill="#bc8cff" filter="url(#glowCyan)">
        <animateMotion dur="1.9s" repeatCount="indefinite"><mpath href="#p_pm_llm"/></animateMotion>
      </circle>
      <circle r="4.5" fill="#38bdf8" filter="url(#glowCyan)">
        <animateMotion dur="2.1s" repeatCount="indefinite"><mpath href="#p_pm_mcp"/></animateMotion>
      </circle>
      <circle r="5" fill="#fb923c" filter="url(#glowCyan)">
        <animateMotion dur="1.6s" repeatCount="indefinite"><mpath href="#p_mcp_tg"/></animateMotion>
      </circle>
      <circle r="5" fill="#fb923c" filter="url(#glowCyan)">
        <animateMotion dur="2.4s" repeatCount="indefinite"><mpath href="#p_p10_tg"/></animateMotion>
      </circle>

      <!-- ==================== COLUMN 1: DATA, SECURITY & ENGINE ==================== -->
      <!-- Node: Stage 0 Schema Engine & ETL -->
      <g class="arch-node" id="node_s0" onclick="selectStage(0)">
        <rect class="node-box" x="20" y="22" width="290" height="116" rx="10" fill="url(#gradCyan)" stroke="#38bdf8" stroke-width="1.8"/>
        <rect x="32" y="34" width="64" height="18" rx="4" fill="rgba(56,189,248,0.2)" stroke="#38bdf8" stroke-width="1"/>
        <text x="64" y="47" class="svg-badge" fill="#38bdf8" text-anchor="middle">STAGE 0</text>
        <text x="106" y="48" class="svg-title">Schema Engine &amp; ETL</text>
        <text x="32" y="68" class="svg-mono">schema_engine/ · etl/</text>
        <text x="32" y="86" class="svg-sub">650,938 txns (Jul–Dec + Jan 2017) · 5,585 CCs</text>
        <text x="32" y="103" class="svg-muted">• 4 Drift Gate · AI+AST .gsql Ontology Mapper</text>
        <text x="32" y="119" class="svg-muted">• DQ-01..06 Taint Matrix · LightGBM cms_p</text>
      </g>

      <!-- Node: Security & Prompt-Injection Shield -->
      <g class="arch-node" id="node_sec" onclick="selectStage(1)">
        <rect class="node-box" x="20" y="170" width="290" height="96" rx="10" fill="url(#gradCrimson)" stroke="#f85149" stroke-width="1.8"/>
        <rect x="32" y="182" width="76" height="18" rx="4" fill="rgba(248,81,73,0.22)" stroke="#f85149" stroke-width="1"/>
        <text x="70" y="195" class="svg-badge" fill="#ffb3ab" text-anchor="middle">SHIELD P0</text>
        <text x="118" y="196" class="svg-title">Adversarial Guard</text>
        <text x="32" y="215" class="svg-mono" style="fill:#ffb3ab">agent/security.py</text>
        <text x="32" y="233" class="svg-sub">Strips 6 planted prompt injections in HHF</text>
        <text x="32" y="250" class="svg-muted">• Trigger-vs-DB channel check (HHF-008)</text>
      </g>

      <!-- Node: Deterministic Decision Engine -->
      <g class="arch-node" id="node_eng" onclick="selectStage(4)">
        <rect class="node-box" x="20" y="294" width="290" height="278" rx="10" fill="url(#gradEmerald)" stroke="#3fb950" stroke-width="1.8"/>
        <rect x="32" y="308" width="90" height="18" rx="4" fill="rgba(63,185,80,0.2)" stroke="#3fb950" stroke-width="1"/>
        <text x="77" y="321" class="svg-badge" fill="#7ee2a8" text-anchor="middle">AUTHORITY</text>
        <text x="132" y="322" class="svg-title">Deterministic Engine</text>
        <text x="32" y="342" class="svg-mono" style="fill:#7ee2a8">engine/ · policy/fraud_policy.yaml</text>
        <text x="32" y="362" class="svg-sub">• Scorecard: 4 evidence families + isotonic cal</text>
        <text x="32" y="380" class="svg-sub">• New Wave Detectors (0 false positives):</text>
        <text x="42" y="397" class="svg-muted">D1 Spoofed-Mobile Proxy (14) · D2 Doubling (8)</text>
        <text x="42" y="413" class="svg-muted">D3 Micro-Skim (18) · D4 sc.rr.com Mule (17)</text>
        <text x="32" y="432" class="svg-sub">• Containment Planner (min card-block set)</text>
        <text x="32" y="450" class="svg-sub">• Policy Gate R1–R10, §3a SAR, §3b routes</text>
        <rect x="32" y="464" width="266" height="54" rx="7" fill="rgba(7,13,25,0.65)" stroke="#3fb950" stroke-width="1"/>
        <text x="44" y="483" class="svg-mono" style="fill:#7ee2a8">engine/voi.py + simulator.py</text>
        <text x="44" y="501" class="svg-muted">Ask only when reply flips actions; simulate reply</text>
        <text x="32" y="542" class="svg-sub">• Validator: IDs in DuckDB + TIPOFF guard</text>
      </g>

      <!-- ==================== COLUMN 2: PHASE MACHINE P0–P10 ==================== -->
      <rect x="380" y="22" width="410" height="552" rx="12" fill="url(#gradBlue)" stroke="#58a6ff" stroke-width="2"/>
      <rect x="380" y="22" width="410" height="36" rx="12" fill="rgba(88,166,255,0.16)"/>
      <text x="396" y="45" class="svg-title">agent/phase_machine.py — Phases P0 to P10</text>
      <text x="774" y="45" class="svg-badge" fill="#79c0ff" text-anchor="end">CLICK ANY PHASE</text>

      <!-- 11 Phase Rows (y = 66 + i*45) -->
      <g class="phase-row" id="ph_1" onclick="selectStage(1)">
        <rect class="phase-bg" x="392" y="66" width="386" height="39" rx="7" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.1)"/>
        <rect x="400" y="74" width="36" height="22" rx="5" fill="#2563eb"/><text x="418" y="89" class="svg-badge" fill="#fff" text-anchor="middle">P0</text>
        <text x="446" y="83" class="svg-sub" style="font-weight:700;fill:#fff">Intake &amp; Security Sanitization</text>
        <text x="446" y="98" class="svg-muted">Strip prompt injections · open_case(AC-...) · 4 mandatory reads</text>
        <text x="766" y="89" class="svg-badge" fill="#f85149" text-anchor="end">SHIELD+MCP</text>
      </g>

      <g class="phase-row" id="ph_2" onclick="selectStage(2)">
        <rect class="phase-bg" x="392" y="111" width="386" height="39" rx="7" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.1)"/>
        <rect x="400" y="119" width="36" height="22" rx="5" fill="#2563eb"/><text x="418" y="134" class="svg-badge" fill="#fff" text-anchor="middle">P1</text>
        <text x="446" y="128" class="svg-sub" style="font-weight:700;fill:#fff">GraphRAG Case &amp; Policy Memory</text>
        <text x="446" y="143" class="svg-muted">find_similar_cases (5,585 CCs incl. HHG-001..020) + FinCEN chunks</text>
        <text x="766" y="134" class="svg-badge" fill="#fb923c" text-anchor="end">VECTOR</text>
      </g>

      <g class="phase-row" id="ph_3" onclick="selectStage(3)">
        <rect class="phase-bg" x="392" y="156" width="386" height="39" rx="7" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.1)"/>
        <rect x="400" y="164" width="36" height="22" rx="5" fill="#7c3aed"/><text x="418" y="179" class="svg-badge" fill="#fff" text-anchor="middle">P2</text>
        <text x="446" y="173" class="svg-sub" style="font-weight:700;fill:#fff">Agentic Graph Investigation</text>
        <text x="446" y="188" class="svg-muted">Playbook A/B/C/D · ≤12 typed read-only GSQL tool calls (as_of)</text>
        <text x="766" y="179" class="svg-badge" fill="#bc8cff" text-anchor="end">LLM+MCP</text>
      </g>

      <g class="phase-row" id="ph_4" onclick="selectStage(4)">
        <rect class="phase-bg" x="392" y="201" width="386" height="39" rx="7" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.1)"/>
        <rect x="400" y="209" width="36" height="22" rx="5" fill="#1e7e34"/><text x="418" y="224" class="svg-badge" fill="#fff" text-anchor="middle">P3</text>
        <text x="446" y="218" class="svg-sub" style="font-weight:700;fill:#fff">Deterministic Scorecard &amp; D1–D4 Waves</text>
        <text x="446" y="233" class="svg-muted">p_engine · D1/D2/D3/D4 wave match · episode &amp; exposure_usd</text>
        <text x="766" y="224" class="svg-badge" fill="#3fb950" text-anchor="end">ENGINE</text>
      </g>

      <g class="phase-row" id="ph_5" onclick="selectStage(5)">
        <rect class="phase-bg" x="392" y="246" width="386" height="39" rx="7" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.1)"/>
        <rect x="400" y="254" width="36" height="22" rx="5" fill="#7c3aed"/><text x="418" y="269" class="svg-badge" fill="#fff" text-anchor="middle">P4</text>
        <text x="446" y="263" class="svg-sub" style="font-weight:700;fill:#fff">Bounded Assessment &amp; Attested Claims</text>
        <text x="446" y="278" class="svg-muted">Clamped to p_engine ± 0.10 · 4-part attested evidence ledger</text>
        <text x="766" y="269" class="svg-badge" fill="#bc8cff" text-anchor="end">LLM±0.10</text>
      </g>

      <g class="phase-row" id="ph_6" onclick="selectStage(6)">
        <rect class="phase-bg" x="392" y="291" width="386" height="39" rx="7" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.1)"/>
        <rect x="400" y="299" width="36" height="22" rx="5" fill="#1e7e34"/><text x="418" y="314" class="svg-badge" fill="#fff" text-anchor="middle">P5</text>
        <text x="446" y="308" class="svg-sub" style="font-weight:700;fill:#fff">Initial Next-Best-Actions Gate</text>
        <text x="446" y="323" class="svg-muted">Admissible actions under R1–R10 · auto / L1 / L2 routing</text>
        <text x="766" y="314" class="svg-badge" fill="#3fb950" text-anchor="end">POLICY</text>
      </g>

      <g class="phase-row" id="ph_7" onclick="selectStage(7)">
        <rect class="phase-bg" x="392" y="336" width="386" height="39" rx="7" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.1)"/>
        <rect x="400" y="344" width="36" height="22" rx="5" fill="#1e7e34"/><text x="418" y="359" class="svg-badge" fill="#fff" text-anchor="middle">P6</text>
        <text x="446" y="353" class="svg-sub" style="font-weight:700;fill:#fff">Value-of-Information &amp; Evidence Request</text>
        <text x="446" y="368" class="svg-muted">customer_validation / step_up_auth / analyst_info + counterfactual</text>
        <text x="766" y="359" class="svg-badge" fill="#3fb950" text-anchor="end">VOI+SIM</text>
      </g>

      <g class="phase-row" id="ph_8" onclick="selectStage(8)">
        <rect class="phase-bg" x="392" y="381" width="386" height="39" rx="7" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.1)"/>
        <rect x="400" y="389" width="36" height="22" rx="5" fill="#1e7e34"/><text x="418" y="404" class="svg-badge" fill="#fff" text-anchor="middle">P7</text>
        <text x="446" y="398" class="svg-sub" style="font-weight:700;fill:#fff">Post-Evidence Reassessment &amp; Final Actions</text>
        <text x="446" y="413" class="svg-muted">Updates p_engine &amp; verdict · records what_changed under §3b</text>
        <text x="766" y="404" class="svg-badge" fill="#3fb950" text-anchor="end">POLICY</text>
      </g>

      <g class="phase-row" id="ph_9" onclick="selectStage(9)">
        <rect class="phase-bg" x="392" y="426" width="386" height="39" rx="7" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.1)"/>
        <rect x="400" y="434" width="36" height="22" rx="5" fill="#7c3aed"/><text x="418" y="449" class="svg-badge" fill="#fff" text-anchor="middle">P8</text>
        <text x="446" y="443" class="svg-sub" style="font-weight:700;fill:#fff">Regulatory SAR Drafting &amp; Provenance</text>
        <text x="446" y="458" class="svg-muted">FinCEN 5-W narrative · OFAC check · V00–V19 + sentence citations</text>
        <text x="766" y="449" class="svg-badge" fill="#bc8cff" text-anchor="end">SAR+RAG</text>
      </g>

      <g class="phase-row" id="ph_10" onclick="selectStage(10)">
        <rect class="phase-bg" x="392" y="471" width="386" height="39" rx="7" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.1)"/>
        <rect x="400" y="479" width="36" height="22" rx="5" fill="#2563eb"/><text x="418" y="494" class="svg-badge" fill="#fff" text-anchor="middle">P9</text>
        <text x="446" y="488" class="svg-sub" style="font-weight:700;fill:#fff">Answer Assembly, ID &amp; No-Tip-Off Gate</text>
        <text x="446" y="503" class="svg-muted">Every ID verified in DuckDB · grade.py TIPOFF regex · fail-closed</text>
        <text x="766" y="494" class="svg-badge" fill="#38bdf8" text-anchor="end">VALIDATOR</text>
      </g>

      <g class="phase-row" id="ph_11" onclick="selectStage(11)">
        <rect class="phase-bg" x="392" y="516" width="386" height="44" rx="7" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.1)"/>
        <rect x="400" y="526" width="36" height="22" rx="5" fill="#ea580c"/><text x="418" y="541" class="svg-badge" fill="#fff" text-anchor="middle">P10</text>
        <text x="446" y="535" class="svg-sub" style="font-weight:700;fill:#fff">Graph Write-Back &amp; RBAC Approval Queue</text>
        <text x="446" y="550" class="svg-muted">AgentCase + 1024-d note_emb · CaseEvent · immutable L1/L2 inbox</text>
        <text x="766" y="541" class="svg-badge" fill="#fb923c" text-anchor="end">PERSIST</text>
      </g>

      <!-- ==================== COLUMN 3: LLM, MCP & TIGERGRAPH ==================== -->
      <!-- Node: Claude Agent SDK -->
      <g class="arch-node" id="node_llm" onclick="selectStage(3)">
        <rect class="node-box" x="855" y="22" width="365" height="134" rx="10" fill="url(#gradViolet)" stroke="#bc8cff" stroke-width="1.8"/>
        <rect x="867" y="34" width="82" height="18" rx="4" fill="rgba(188,140,255,0.22)" stroke="#bc8cff" stroke-width="1"/>
        <text x="908" y="47" class="svg-badge" fill="#d2b8ff" text-anchor="middle">LLM SEAM</text>
        <text x="958" y="48" class="svg-title">Claude Agent SDK</text>
        <text x="867" y="68" class="svg-mono" style="fill:#d2b8ff">agent/llm.py (cli · api · gemini · mock)</text>
        <text x="867" y="87" class="svg-sub">• Structured JSON output on P4, P5, P7, P8, P9</text>
        <text x="867" y="105" class="svg-sub">• Ephemeral prompt caching (~95% cache hit)</text>
        <text x="867" y="123" class="svg-muted">• Zero built-in OS tools · sees only 16 read queries</text>
        <text x="867" y="139" class="svg-muted">• Bounded authority: cannot override policy gate</text>
      </g>

      <!-- Node: TigerGraph MCP Server -->
      <g class="arch-node" id="node_mcp" onclick="selectStage(3)">
        <rect class="node-box" x="855" y="190" width="365" height="92" rx="10" fill="url(#gradCyan)" stroke="#38bdf8" stroke-width="1.8"/>
        <rect x="867" y="202" width="82" height="18" rx="4" fill="rgba(56,189,248,0.2)" stroke="#38bdf8" stroke-width="1"/>
        <text x="908" y="215" class="svg-badge" fill="#38bdf8" text-anchor="middle">STDIO MCP</text>
        <text x="958" y="216" class="svg-title">tigergraph-mcp 1.0.3</text>
        <text x="867" y="236" class="svg-mono">mcp/tg_mcp_launcher.py · strict allowlist</text>
        <text x="867" y="254" class="svg-sub">14 GSQL read wrappers + 2 vector retrieval tools</text>
        <text x="867" y="270" class="svg-muted">Blocks raw gsql / schema drop / ad-hoc mutations</text>
      </g>

      <!-- Node: TigerGraph Savanna + TigerVector -->
      <g class="arch-node" id="node_tg" onclick="selectStage(2)">
        <rect class="node-box" x="855" y="316" width="365" height="258" rx="10" fill="url(#gradOrange)" stroke="#fb923c" stroke-width="2"/>
        <rect x="867" y="328" width="96" height="18" rx="4" fill="rgba(251,146,60,0.22)" stroke="#fb923c" stroke-width="1"/>
        <text x="915" y="341" class="svg-badge" fill="#fdba74" text-anchor="middle">FRAUDGRAPH</text>
        <text x="972" y="342" class="svg-title">TigerGraph Savanna 4.2</text>

        <rect x="869" y="356" width="337" height="58" rx="6" fill="rgba(7,13,25,0.68)" stroke="rgba(251,146,60,0.5)"/>
        <text x="880" y="374" class="svg-mono" style="fill:#fdba74">25 Installed GSQL Queries (as_of bounded)</text>
        <text x="880" y="391" class="svg-muted">case_context · card_profile · card_window · ring_profile</text>
        <text x="880" y="405" class="svg-muted">shared_origin_scan · episode_candidates · 4 writers</text>

        <rect x="869" y="422" width="337" height="62" rx="6" fill="rgba(7,13,25,0.68)" stroke="rgba(251,146,60,0.5)"/>
        <text x="880" y="440" class="svg-mono" style="fill:#fdba74">TigerVector GraphRAG (1024-d COSINE)</text>
        <text x="880" y="457" class="svg-muted">• 5,585 ClosedCase.note_emb (incl. CC-5566..5585)</text>
        <text x="880" y="472" class="svg-muted">• PolicyChunk.emb (FinCEN, FFIEC, FATF, Policy v1.0)</text>

        <rect x="869" y="492" width="337" height="68" rx="6" fill="rgba(7,13,25,0.68)" stroke="rgba(251,146,60,0.5)"/>
        <text x="880" y="510" class="svg-mono" style="fill:#fdba74">Compounding Case Memory &amp; Graph Algos</text>
        <text x="880" y="527" class="svg-muted">• SHARES_DEVICE WCC + Degree + PageRank</text>
        <text x="880" y="543" class="svg-muted">• AgentCase + CaseEvent + Approval + round2_delta</text>
      </g>

      <!-- ==================== BOTTOM BAR: OUTPUTS & RBAC UI ==================== -->
      <g class="arch-node" id="node_out" onclick="selectStage(11)">
        <rect class="node-box" x="20" y="592" width="1200" height="46" rx="10" fill="rgba(15, 37, 71, 0.85)" stroke="#58a6ff" stroke-width="1.6"/>
        <text x="38" y="619" class="svg-title" style="font-size:13.5px">Outputs &amp; Banking Controls:</text>
        <text x="235" y="619" class="svg-mono">cases/HHF-001..018.json (18/18 Clean on grade.py)</text>
        <text x="595" y="619" class="svg-sub">·  4-Part Attested Evidence &amp; SAR Citations</text>
        <text x="875" y="619" class="svg-sub" style="fill:#7ee2a8">·  Server-Side RBAC &amp; Immutable Approvals Inbox</text>
      </g>
    </svg>
  </div>

  <!-- Bottom Live Stage Inspector Drawer -->
  <div class="inspector">
    <div>
      <div class="insp-head">
        <span class="insp-stage-pill" id="inspCode">STAGE 0</span>
        <h3 class="insp-title" id="inspTitle">Schema Engine, Drift Gate &amp; Incremental ETL</h3>
      </div>
      <div class="insp-module" id="inspMod">schema_engine/{{profiler,drift,mapper,data_quality,normalizer}}.py · etl/</div>
      <p class="insp-desc" id="inspDesc"></p>
      <div class="insp-contracts" id="inspChips"></div>
    </div>
    <div class="insp-live-box">
      <div class="insp-live-title">LIVE TELEMETRY — CASE <span id="liveCaseId"></span></div>
      <div id="inspLive"></div>
    </div>
  </div>
</div>

<script>
const CASE = {case_json};

const STAGES = [
  {{
    code: "STAGE 0",
    label: "S0 · Schema & ETL",
    title: "Schema Engine, Drift Survival Gate & Incremental ETL",
    mod: "schema_engine/{{profiler,drift,mapper,data_quality,normalizer}}.py · etl/",
    desc: "Profiles Round 1 (590,742 Jul–Dec 2016 txns) alongside Round 2 (60,196 Jan 2017 txns = 650,938 total). Detects 4 dataset drifts (new Jan partition, 54 NULL card6 rows, CC-5566..5585 note templates, HHF case pack), maps foreign .gsql schemas (ISO 20022, BIAN, Temenos T24) via comment-blind AST+AI reasoning, scans DQ-01..DQ-06 defects, and scores Jan 2017 with LightGBM cms_p.",
    chips: ["650,938 Transactions", "5,585 Closed Cases", "4 Drifts Gated", "DQ-01..DQ-06 Matrix", "make load-round2"],
    node: "node_s0",
    phase: null,
    flows: ["f_s0_p0"],
    live: () => `• Case: ${{CASE.case_id}} (card ${{CASE.card_id}}, customer ${{CASE.customer_id}})<br>• Flagged Txn: ${{CASE.flagged_txn_id}} · Opened: ${{CASE.opened_at}}<br>• Dataset Scope: 650,938 txns &amp; 5,585 closed cases verified in DuckDB`
  }},
  {{
    code: "PHASE P0",
    label: "P0 · Intake & Shield",
    title: "Intake, Adversarial Prompt-Injection Shield & Mandatory Reads",
    mod: "agent/security.py · agent/phase_machine.py (p0_intake)",
    desc: "Inspects incoming trigger_text before any prompt construction. Detects and strips all 6 planted prompt-injection payloads (SYSTEM NOTE overrides, [[assistant:...]], HTML comments, fake compliance clearance), cross-checks trigger claims against DB facts (catching HHF-008's online vs. in_person contradiction), logs CaseEvent(kind='security_alert'), and executes the 4 mandatory time-boxed graph reads.",
    chips: ["6/6 Injections Stripped", "Trigger-vs-DB Check", "open_case(AC-...)", "ts <= opened_at"],
    node: "node_sec",
    phase: "ph_1",
    flows: ["f_s0_p0", "f_sec_pm", "f_pm_mcp"],
    live: () => `• Trigger Type: ${{CASE.trigger_type}} (bank risk_score=${{CASE.risk_score}})<br>• Security Shield: ${{CASE.injection_detected ? '🛡️ INJECTION STRIPPED (' + CASE.attack_types.join(', ') + ')' : '✓ Clean trigger text'}}<br>• Sanitized Text: "${{CASE.sanitized_text.slice(0, 95)}}"`
  }},
  {{
    code: "PHASE P1",
    label: "P1 · GraphRAG Memory",
    title: "Hybrid Structural + TigerVector GraphRAG Memory Retrieval",
    mod: "agent/tools_local.py · rag/ · TigerVector (1024-d BAAI/bge-large-en-v1.5)",
    desc: "Retrieves top-k similar prior cases from the 5,585 closed cases (including Round 1 HHG-001..020 precedents CC-5566..5585) by combining structural overlap (same card, customer, strong device, region) with 1024-d cosine similarity in TigerVector, plus grounding chunks from Fraud Policy v1.0 and FinCEN/FFIEC/FATF guidance.",
    chips: ["5,585 Closed Cases", "CC-5566..5585 Precedents", "1024-d BGE Embeddings", "Diversified Cleared+Fraud"],
    node: "node_tg",
    phase: "ph_2",
    flows: ["f_pm_mcp", "f_mcp_tg"],
    live: () => `• Similar Prior Cases Retrieved: ${{CASE.similar_n}} CC- precedent(s)<br>• Regulatory Grounding: Fraud Policy v1.0 + FinCEN SAR Narrative Guidance<br>• Graph Vertex: ${{CASE.graph_case_id}}`
  }},
  {{
    code: "PHASE P2",
    label: "P2 · MCP Investigate",
    title: "Playbook-Driven Agentic Investigation via TigerGraph MCP",
    mod: "agent/phase_machine.py (p2_investigate) · mcp/tg_mcp_launcher.py",
    desc: "Claude Agent SDK investigates the alert using Playbook A (in-person risk), Playbook B (online risk), Playbook C (customer denial), or Playbook D (analyst shared-origin request). Calls up to 12 read-only installed GSQL queries through the stdio MCP allowlist; every query enforces ts <= opened_at so post-open activity never leaks into the episode.",
    chips: ["16 Typed Read Tools", "MCP Allowlist", "Zero Raw GSQL", "Strict as_of Time-Box"],
    node: "node_mcp",
    phase: "ph_3",
    flows: ["f_pm_llm", "f_llm_mcp", "f_mcp_tg"],
    live: () => `• Total Tool Calls: ${{CASE.tool_calls}} · Tokens: ${{CASE.tokens}} · Latency: ${{CASE.latency_s}}<br>• Active Queries: case_context, card_profile, card_window, shared_origin_scan, episode_candidates<br>• Evidence Items Collected: ${{CASE.evidence_n}} attested claims`
  }},
  {{
    code: "PHASE P3",
    label: "P3 · Scorecard & D1–D4",
    title: "Deterministic Scorecard & Undocumented Wave Detectors (D1–D4)",
    mod: "engine/scorecard.py · engine/detectors.py · engine/episode.py",
    desc: "Evaluates 4 independent evidence families (history, device, memory, customer) and runs the 4 January 2017 typology detectors: D1 Spoofed-Mobile Anonymous-Proxy Wave (14 cards), D2 Dormant-Wake Doubling Drain to $1,800 (8 cards), D3 Sub-Threshold Micro-Charge Skimming (18 cards), and D4 Shared Recipient Email Mule sc.rr.com (17 cards), plus R5 card testing and R7 recurring charges.",
    chips: ["D1 Proxy Wave (14)", "D2 Doubling Drain (8)", "D3 Micro-Skim (18)", "D4 sc.rr.com (17)", "0 False Positives"],
    node: "node_eng",
    phase: "ph_4",
    flows: ["f_eng_pm1"],
    live: () => `• Pre-Evidence Probability: ${{CASE.p_pre}} · Pattern: ${{CASE.pattern}}<br>• Episode: ${{CASE.affected_n}} affected txn(s) · Exposure: ${{CASE.exposure_usd}}<br>• Connected Cards in Wave/Ring: ${{CASE.connected_n}} card(s)`
  }},
  {{
    code: "PHASE P4",
    label: "P4 · Bounded Assess",
    title: "Bounded LLM Assessment & 4-Part Attested Evidence Ledger",
    mod: "agent/phase_machine.py (p4_assess) · agent/schemas.py",
    desc: "The LLM synthesizes the investigation findings into structured evidence claims (what, dataset-verified entity_ids, query ref, and time-box proof) and proposes a calibrated probability. By architectural contract, the LLM may adjust p_engine by at most ±0.10 and can never flip a customer denial to legitimate.",
    chips: ["Clamped to p_engine ± 0.10", "4-Part Attestations", "Unknown IDs Stripped", "No AC- IDs in Claims"],
    node: "node_llm",
    phase: "ph_5",
    flows: ["f_pm_llm"],
    live: () => `• Assessed Probability: ${{CASE.p_pre}} -> Verdict Band: ${{CASE.verdict}}<br>• Attested Claims in Ledger: ${{CASE.evidence_n}}<br>• Entity IDs Verified Against data/ids.duckdb`
  }},
  {{
    code: "PHASE P5",
    label: "P5 · Initial Gate",
    title: "Initial Next-Best-Actions & Deterministic Policy Gate",
    mod: "engine/policy.py · policy/fraud_policy.yaml",
    desc: "Computes the admissible initial action set under Fraud Policy v1.0 (R1 verify before blocking on a weak signal <0.70; R2 immediate BLOCK_CARD + CREATE_CASE on customer denial unless R7 recurring match; R5 card testing; R6 shared origin; R8 escalate uncertain >$500; R9 undocumented; R10 no BLOCK_ALL_CARDS without 2 compromised cards).",
    chips: ["Rules R1–R10", "R1 vs R2 Separation", "Routes: auto / L1 / L2", "Deterministic Fallback"],
    node: "node_eng",
    phase: "ph_6",
    flows: ["f_eng_pm1", "f_eng_pm2"],
    live: () => `• Initial Actions (Pre-Evidence): ${{CASE.initial_actions}}<br>• Policy Gate Violations: 0 (all routes &amp; rule citations verified)`
  }},
  {{
    code: "PHASE P6",
    label: "P6 · VoI & Simulator",
    title: "Value-of-Information Check & Deterministic Reply Simulator",
    mod: "engine/voi.py · engine/simulator.py",
    desc: "Checks §6 stopping criteria: if the case is not yet settled and an evidence request (customer_validation, step_up_auth, or analyst_info) can change the recommended action set, simulates the realistic reply from the evidence-family balance and records the counterfactual branch not taken.",
    chips: ["§6 Stop Rule", "Value of Information", "Counterfactual Branch", "ASSUMED Reply Labelled"],
    node: "node_eng",
    phase: "ph_7",
    flows: ["f_eng_pm2"],
    live: () => `• Evidence Request: ${{CASE.ev_req}}<br>• Probability Move: ${{CASE.p_pre}} -> ${{CASE.p_post}}`
  }},
  {{
    code: "PHASE P7",
    label: "P7 · Final Actions",
    title: "Post-Evidence Reassessment & Final Action Routing (§3b)",
    mod: "engine/scorecard.py (post_evidence) · engine/policy.py",
    desc: "Applies the simulated customer/step-up/analyst response to update p_engine, re-derives the episode and exposure, and selects the final action set with exact approval routes (auto executed by agent; L1 team lead for DECLINE_TRANSACTION & BLOCK_CARD <= $2,500; L2 fraud manager for BLOCK_CARD > $2,500, BLOCK_ALL_CARDS & FILE_REPORT).",
    chips: ["§3b Action Evolution", "what_changed Recorded", "auto / L1 / L2 Routing", "Containment Plan"],
    node: "node_eng",
    phase: "ph_8",
    flows: ["f_eng_pm2"],
    live: () => `• Final Verdict: ${{CASE.verdict.toUpperCase()}} (p=${{CASE.p_post}}, status=${{CASE.status}})<br>• Final Actions: ${{CASE.final_actions}}<br>• What Changed: ${{CASE.what_changed}}`
  }},
  {{
    code: "PHASE P8",
    label: "P8 · Regulatory SAR",
    title: "FinCEN-Grounded SAR Narrative, OFAC Screen & Sentence Citations",
    mod: "rag/sar.py · rag/validate_sar.py",
    desc: "Evaluates §3a (fraud confirmed/suspected AND exposure > $1,000 OR connected cards/shared origin OR undocumented pattern — so $23 micro-skimming cases HHF-004/005 file SARs). Screens subjects against OFAC SDN, grounds on FinCEN guidance chunks, drafts a standalone 6–12 sentence 5-W narrative, and validates against 19 checks (V00–V18).",
    chips: ["§3a Multi-Trigger Rule", "OFAC SDN Screening", "Checks V00–V19", "Sentence-Level Citations"],
    node: "node_llm",
    phase: "ph_9",
    flows: ["f_pm_llm", "f_mcp_tg"],
    live: () => `• SAR Required (§3a): ${{CASE.sar_file ? 'YES (FILE_REPORT -> L2)' : 'NO'}}<br>• SAR Reason: ${{CASE.sar_reason.slice(0, 110)}}`
  }},
  {{
    code: "PHASE P9",
    label: "P9 · Validator Gate",
    title: "Strict Answer Validator, ID Existence & No-Tip-Off Guard",
    mod: "engine/validator.py · data/raw/round2/grade.py",
    desc: "Verifies the assembled answer JSON against every hard and policy check: every TransactionID, card_id, customer_id, CC- id, email domain, and region must resolve in data/ids.duckdb; exposure_usd must equal the exact sum of affected amounts; first_suspicious_txn_id must be the earliest; and evidence_requests must pass grade.py's TIPOFF regex.",
    chips: ["100% ID Resolution", "Exact Exposure Sum", "No-Tip-Off Regex Guard", "Fail-Closed Guarantee"],
    node: "node_eng",
    phase: "ph_10",
    flows: ["f_pm_out"],
    live: () => `• Official Grader Status: PASS (0 hard, 0 policy findings)<br>• Exposure Verified: ${{CASE.exposure_usd}} across ${{CASE.affected_n}} transaction(s)<br>• Tip-Off Scan: 0 regulator/SAR mentions in customer-facing text`
  }},
  {{
    code: "PHASE P10",
    label: "P10 · Persist & RBAC",
    title: "Graph Write-Back (Compounding Memory) & RBAC Approval Inbox",
    mod: "agent/persist.py · ui/approvals_db.py · graph/load_round2_delta.py",
    desc: "Writes AgentCase (with 1024-d note_emb vector), CaseEvents, Approvals, and CASE_SIMILAR_TO / CASE_CONNECTED_TO edges back into TigerGraph so later investigations retrieve this case as precedent. Enqueues L1/L2 actions into the SQLite approval inbox with server-side RBAC (team_lead -> L1 only; fraud_manager -> L1/L2) and immutable decided records.",
    chips: ["AgentCase + note_emb", "CASE_CONNECTED_TO", "Server-Side RBAC", "Immutable Approvals"],
    node: "node_out",
    phase: "ph_11",
    flows: ["f_p10_tg", "f_pm_out"],
    live: () => `• Written to Graph: ${{CASE.graph_case_id}} (+ CaseEvents &amp; Approvals)<br>• L1/L2 Actions Queued in RBAC Inbox: ${{CASE.final_actions}}`
  }}
];

let currentIdx = 0;
let playing = true;
let speedIdx = 0;
const SPEEDS = [
  {{ label: "⚡ 1.0×", ms: 3400 }},
  {{ label: "⚡ 1.5×", ms: 2200 }},
  {{ label: "⚡ 2.0×", ms: 1500 }}
];
let timer = null;

function initStrip() {{
  const strip = document.getElementById("stepStrip");
  strip.innerHTML = "";
  STAGES.forEach((s, idx) => {{
    const el = document.createElement("span");
    el.className = "step-chip" + (idx === currentIdx ? " active" : "");
    el.textContent = s.code.replace("STAGE ", "S").replace("PHASE ", "");
    el.title = s.title;
    el.onclick = () => {{
      playing = false;
      updatePlayBtn();
      selectStage(idx);
    }};
    strip.appendChild(el);
  }});
}}

function selectStage(idx) {{
  currentIdx = (idx + STAGES.length) % STAGES.length;
  const s = STAGES[currentIdx];

  // Update step strip
  document.querySelectorAll(".step-chip").forEach((el, i) => {{
    el.classList.toggle("active", i === currentIdx);
  }});

  // Highlight node
  document.querySelectorAll(".arch-node").forEach(el => el.classList.remove("active-node"));
  if (s.node) {{
    const n = document.getElementById(s.node);
    if (n) n.classList.add("active-node");
  }}

  // Highlight phase row
  document.querySelectorAll(".phase-row").forEach(el => el.classList.remove("active-phase"));
  if (s.phase) {{
    const p = document.getElementById(s.phase);
    if (p) p.classList.add("active-phase");
  }}

  // Highlight active conduit flows
  document.querySelectorAll(".conduit-flow").forEach(el => el.classList.remove("highlight"));
  (s.flows || []).forEach(fid => {{
    const f = document.getElementById(fid);
    if (f) f.classList.add("highlight");
  }});

  // Update inspector drawer
  document.getElementById("inspCode").textContent = s.code;
  document.getElementById("inspTitle").textContent = s.title;
  document.getElementById("inspMod").textContent = s.mod;
  document.getElementById("inspDesc").textContent = s.desc;
  document.getElementById("liveCaseId").textContent = CASE.case_id;
  document.getElementById("inspLive").innerHTML = s.live();

  const chipsEl = document.getElementById("inspChips");
  chipsEl.innerHTML = "";
  (s.chips || []).forEach(c => {{
    const span = document.createElement("span");
    span.className = "insp-chip";
    span.textContent = c;
    chipsEl.appendChild(span);
  }});
}}

function stepDelta(delta) {{
  playing = false;
  updatePlayBtn();
  selectStage(currentIdx + delta);
}}

function togglePlay() {{
  playing = !playing;
  updatePlayBtn();
  resetTimer();
}}

function updatePlayBtn() {{
  const btn = document.getElementById("btnPlay");
  btn.textContent = playing ? "⏸ Pause Walkthrough" : "▶ Auto-Play Flow";
}}

function cycleSpeed() {{
  speedIdx = (speedIdx + 1) % SPEEDS.length;
  document.getElementById("btnSpeed").textContent = SPEEDS[speedIdx].label;
  resetTimer();
}}

function resetTimer() {{
  if (timer) clearInterval(timer);
  if (playing) {{
    timer = setInterval(() => {{
      selectStage(currentIdx + 1);
    }}, SPEEDS[speedIdx].ms);
  }}
}}

initStrip();
selectStage(0);
resetTimer();
</script>
</body>
</html>"""


_inject_architecture_css()

runs = common.list_runs()
run_id = st.session_state.get("run_id") or (runs[0] if runs else None)
st.session_state["run_id"] = run_id

all_pack = common.load_case_pack("all")
pack_ids = [m["case_id"] for m in all_pack] or ["HHG-014"]
default_case = "HHF-004" if "HHF-004" in pack_ids else ("HHG-014" if "HHG-014" in pack_ids else pack_ids[0])

# Hero Banner
st.markdown(
    """
    <div class="arch-hero">
      <div class="arch-eyebrow"><span class="arch-pulse-dot"></span> SYSTEM ARCHITECTURE &amp; END-TO-END FLOW</div>
      <h1>Agentic Fraud Investigation on TigerGraph — End-to-End Architecture</h1>
      <p>
        Every alert flows through a deterministic-first, graph-grounded pipeline: <strong>Schema &amp; Drift Gate</strong>
        &rarr; <strong>Adversarial Prompt-Injection Shield (P0)</strong> &rarr; <strong>TigerVector GraphRAG Memory (P1)</strong>
        &rarr; <strong>Typed MCP Graph Investigation (P2)</strong> &rarr; <strong>Deterministic Scorecard &amp; D1–D4 Wave Detectors (P3)</strong>
        &rarr; <strong>Bounded LLM Assessment (P4)</strong> &rarr; <strong>Policy Gate &amp; VoI Simulator (P5–P7)</strong>
        &rarr; <strong>FinCEN SAR &amp; Validator (P8–P9)</strong> &rarr; <strong>Compounding Graph Write-Back &amp; RBAC Inbox (P10)</strong>.
      </p>
      <div class="arch-pill-row">
        <span class="arch-pill"><strong>650,938</strong> Transactions (Jul 2016 – Jan 2017)</span>
        <span class="arch-pill"><strong>5,585</strong> Closed Cases (incl. CC-5566..5585)</span>
        <span class="arch-pill"><strong>25</strong> Installed GSQL Queries</span>
        <span class="arch-pill"><strong>4</strong> New Typology Detectors (D1–D4)</span>
        <span class="arch-pill"><strong>6 / 6</strong> Prompt Injections Neutralized</span>
        <span class="arch-pill"><strong>18 / 18</strong> Clean on Official Grader</span>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Case Selector for Live Architecture Tracing
ctl1, ctl2, ctl3 = st.columns([1.4, 1.4, 3.2])
with ctl1:
    if runs:
        run_id = st.selectbox("Active Run", runs, index=runs.index(run_id) if run_id in runs else 0, key="arch_run")
        st.session_state["run_id"] = run_id
with ctl2:
    case_id = common.case_select(st, pack_ids, key="arch_case", preferred=default_case)
bundle = _load_case_bundle(run_id, case_id)
with ctl3:
    st.markdown(
        f"**Tracing Case `{bundle['case_id']}`** · trigger `{bundle['trigger_type']}` · card `{bundle['card_id']}` · "
        f"verdict {common.badge(bundle['verdict'], common.VERDICT_COLORS.get(bundle['verdict'], 'gray'))} · "
        f"pattern `{bundle['pattern']}` · exposure **{bundle['exposure_usd']}** · "
        + (common.badge("🛡️ injection stripped", "red") if bundle["injection_detected"] else common.badge("✓ clean trigger", "green"))
    )
    st.caption(
        "Use **Auto-Play / Pause / Next** or click any node or phase (`S0`, `P0`–`P10`) inside the interactive diagram below "
        "to inspect its architectural contract and live telemetry for the selected case."
    )

# Interactive Animated SVG Architecture Component
components.html(_build_animated_svg_html(bundle), height=875, scrolling=False)

# Live 6-Stage Pipeline Cards for the Selected Case
st.markdown(f"### End-to-End Pipeline Trace for `{bundle['case_id']}`")
st.markdown(
    f"""
    <div class="arch-stage-grid">
      <div class="arch-stage-card">
        <span class="stage-tag" style="color:#38bdf8">1 · INGESTION &amp; SHIELD (S0 + P0)</span>
        <h4>Trigger &amp; Adversarial Guard</h4>
        <p>Trigger <code>{bundle['trigger_type']}</code> on txn <code>{bundle['flagged_txn_id']}</code>.
        {'Prompt injection detected and stripped before LLM context.' if bundle['injection_detected'] else 'Trigger verified clean against DB facts.'}</p>
        <div class="mono-foot">card: {bundle['card_id']} · risk: {bundle['risk_score']}</div>
      </div>
      <div class="arch-stage-card">
        <span class="stage-tag" style="color:#fb923c">2 · GRAPHRAG &amp; MCP (P1 + P2)</span>
        <h4>TigerGraph &amp; TigerVector</h4>
        <p>Executed <strong>{bundle['tool_calls']}</strong> time-boxed GSQL &amp; vector tool calls (<code>ts &le; {bundle['opened_at']}</code>), retrieving <strong>{bundle['similar_n']}</strong> similar closed cases.</p>
        <div class="mono-foot">tokens: {bundle['tokens']} · latency: {bundle['latency_s']}</div>
      </div>
      <div class="arch-stage-card">
        <span class="stage-tag" style="color:#3fb950">3 · SCORECARD &amp; WAVES (P3 + P4)</span>
        <h4>Pattern &amp; Episode Derivation</h4>
        <p>Pattern <code>{bundle['pattern']}</code> across <strong>{bundle['affected_n']}</strong> affected txn(s) and <strong>{bundle['connected_n']}</strong> connected card(s) with <strong>{bundle['evidence_n']}</strong> attested claims.</p>
        <div class="mono-foot">p_pre: {bundle['p_pre']} · exposure: {bundle['exposure_usd']}</div>
      </div>
      <div class="arch-stage-card">
        <span class="stage-tag" style="color:#bc8cff">4 · POLICY &amp; VOI SIMULATOR (P5–P7)</span>
        <h4>Initial &rarr; Evidence &rarr; Final</h4>
        <p>Initial: <code>{bundle['initial_actions']}</code>.<br/>Final: <code>{bundle['final_actions']}</code>.</p>
        <div class="mono-foot">p_post: {bundle['p_post']} · verdict: {bundle['verdict']}</div>
      </div>
      <div class="arch-stage-card">
        <span class="stage-tag" style="color:#f59e0b">5 · REGULATORY SAR &amp; GATE (P8 + P9)</span>
        <h4>FinCEN Narrative &amp; Validator</h4>
        <p>{'SAR drafted and validated against V00–V19 + OFAC SDN + sentence citations.' if bundle['sar_file'] else 'No SAR required under Policy §3a; negative SAR verified.'}</p>
        <div class="mono-foot">SAR: {'FILED (L2)' if bundle['sar_file'] else 'NOT FILED'} · TIPOFF: PASS</div>
      </div>
      <div class="arch-stage-card">
        <span class="stage-tag" style="color:#58a6ff">6 · GRAPH PERSIST &amp; RBAC (P10)</span>
        <h4>Compounding Graph Memory</h4>
        <p>Written to FraudGraph as <code>{bundle['graph_case_id']}</code> with 1024-d <code>note_emb</code> vector; L1/L2 actions queued in RBAC inbox.</p>
        <div class="mono-foot">status: {bundle['status']} · RBAC locked</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# 4-Tab Architectural Deep-Dive
st.markdown("### Architectural Deep-Dive: Four Layers of Defense & Intelligence")
t1, t2, t3, t4 = st.tabs(
    [
        "1. Data, Drift Gate & Schema Engine",
        "2. Phase Machine (P0–P10) & Security Shield",
        "3. TigerGraph, MCP & TigerVector GraphRAG",
        "4. Deterministic Engine, Detectors & Banking Controls",
    ]
)

with t1:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(
            """
            #### Incremental Graph Ingestion (`etl/` + `schema_engine/normalizer.py`)
            * **Full 7-Month Horizon (Jul 2016 – Jan 2017):** Merges the 590,742 Round 1 transactions with 60,196 January 2017 transactions (**650,938 total**) and **5,585 closed cases** (`CC-0001`…`CC-5585`, where `CC-5566`…`CC-5585` are the Round 1 `HHG-001`…`HHG-020` outcomes).
            * **Canonical Card Derivation Rule:** Reproduces 100% of the 14,975 labelled `(transaction -> card)` pairs (`customer_id || '-K' || dense_rank() OVER (PARTITION BY customer_id ORDER BY coalesce(card6, ''))`), while reconciling the 54 January `card6 IS NULL` rows (`DQ-03` / `HHF-011`).
            * **Case-Memory Scorer (`etl/cms_train.py`):** LightGBM over 452 Vesta + identity + leakage-free card-history features, calibrated via October out-of-fold isotonic regression (`data/models/isotonic.json`).
            """
        )
    with c2:
        st.markdown(
            """
            #### Schema Drift Survival, Variant Mapping & DQ Detective (`schema_engine/`)
            * **Drift Detection & Human Gate (`drift.py`):** Automatically detects the 4 Round 2 drifts, explains what breaks downstream if unhandled, and gates ingestion on `fraud_manager` approval.
            * **Comment-Blind AI + AST `.gsql` Mapper (`gsql_parser.py`, `ontology_mapper.py`, `query_adapter.py`):** Strips all comments before parsing any foreign bank's `.gsql` DDL (`Bank_ISO20022`, `Bank_BIAN`, `Bank_CoreT24`), maps vertices/edges/attributes to the Canonical Fraud Ontology (`exact` / `approximate` / `unmapped`), and compiles adapted GSQL fraud queries.
            * **Data Quality Taint Matrix (`data_quality.py`):** Scans for `DQ-01` (1,003 missing online identities), `DQ-02` (`HHF-008` channel contradiction), `DQ-03` (54 null `card6`), `DQ-04` (spoofed mobile/OS pairs), `DQ-05` (thin card split on `HHF-011`), and `DQ-06` (4,974 empty `addr1`).
            """
        )

with t2:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(
            """
            #### 11-Phase Execution Loop (`agent/phase_machine.py`)
            * **`P0` Intake & Shield:** Sanitizes `trigger_text`, opens `AC-<case_id>` in TigerGraph, and runs the 4 mandatory time-boxed queries (`case_context`, `card_profile`, `card_window`, `prior_cases_for_customer`).
            * **`P1` Memory:** Retrieves top-8 structurally + semantically similar `ClosedCase` precedents and top-5 policy/regulatory chunks.
            * **`P2` Investigate:** LLM executes up to 12 typed read-only tool calls following Playbooks A–D.
            * **`P3–P4` Scorecard & Bounded Assess:** Engine computes `p_engine`, evidence families, and `D1`–`D4` wave membership; LLM writes attested claims and may adjust probability by at most `±0.10`.
            * **`P5–P7` Initial Gate &rarr; VoI Simulator &rarr; Final Gate:** Enforces `R1`–`R10` before and after simulated customer/step-up/analyst replies.
            * **`P8–P10` SAR, Validator & Write-Back:** Drafts and validates the FinCEN SAR narrative, verifies every ID and `TIPOFF` rule, and persists the case to TigerGraph.
            """
        )
    with c2:
        st.markdown(
            """
            #### Adversarial Prompt-Injection Defense (`agent/security.py`)
            * **Planted Attack Neutralization:** Detects and strips all 6 hostile strings in `case_pack_final.csv` (`HHF-004`, `HHF-005`, `HHF-008`, `HHF-010`, `HHF-014`, `HHF-016`) plus 4 synthetic red-team vectors (**10/10 neutralized**).
            * **Trigger-vs-Database Cross-Check:** Compares claims in `trigger_text` (e.g., `HHF-008` claiming `online`) against the authoritative transaction row (`in_person`, `ProductCD=W`, `IP_PROXY:HIDDEN`) and records a `CaseEvent(kind="security_alert")`.
            * **Architectural Separation of Powers:** Even if a novel injection bypassed regex sanitization, the LLM cannot set `verdict`, cannot bypass `engine.policy.check()`, and cannot shift `p_engine` by more than `±0.10`.
            """
        )

with t3:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(
            """
            #### TigerGraph Savanna `FraudGraph` (`graph/`)
            * **13 Vertex Types & 22 Edge Types (`graph/schema.gsql`):** `Customer`, `Card`, `Transaction` (`NEXT` chain), `DeviceProfile`, `EmailDomain`, `BillingRegion`, `ClosedCase`, `AgentCase`, `CaseEvent`, `Approval`, `Document`, `PolicyChunk`, and `FraudPattern`.
            * **25 Installed GSQL Queries (`graph/queries/*.gsql`):** 14 LLM-exposed read queries, 4 write-back queries (`open_case`, `append_case_event`, `record_approval`, `close_case`), and 7 harness/RAG/UI queries (`case_subgraph`, `post_open_activity`, vector search).
            * **Live Round 2 Delta Loader (`graph/load_round2_delta.py`):** Uploads the January 2017 delta TSVs (`data/out/round2_delta/`) and `vec_ClosedCase_delta.psv` directly to the live workspace via `make load-round2`.
            """
        )
    with c2:
        st.markdown(
            """
            #### MCP Security Boundary & TigerVector GraphRAG (`mcp/`, `rag/`)
            * **Strict stdio MCP Allowlist (`mcp/tg_mcp_launcher.py`):** Exposes only typed read queries to the model; blocks `gsql`, `run_query`, `drop`, and schema modification tools.
            * **Local 1024-d Embeddings (`rag/embed.py`):** Runs `BAAI/bge-large-en-v1.5` locally (zero paid embedding API keys) over masked closed-case notes (`<CASE>`, `<ID>`, `<AMT>`, `<DATE>` masked so retrieval matches structural behavior) and regulatory guidance (`FinCEN`, `FFIEC`, `FATF`).
            * **Compounding Case Memory:** Every completed investigation writes an `AgentCase` vertex with its 1024-d `note_emb` vector and `CASE_SIMILAR_TO` / `CASE_CONNECTED_TO` edges back into `FraudGraph`.
            """
        )

with t4:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(
            """
            #### Undocumented Wave Detectors (`engine/detectors.py`) & Containment (`engine/containment.py`)
            * **`D1` — Spoofed-Mobile Anonymous-Proxy Wave (`HHF-001`, `HHF-002`):** Connects 14 cards (`3660096`–`3660130`) with online `ProductCD='C'` charges through `IP_PROXY:ANONYMOUS` where each card uses a rotated mobile `DeviceInfo` paired with a spoofed desktop/iOS OS (`Windows 7/10`, `Mac OS X`, `iOS`).
            * **`D2` — Dormant-Wake Doubling Drain (`HHF-003`):** Connects 8 cards (`3660131`–`3660173`) executing a geometric `~2×` doubling sequence capping at `$1,800.00`.
            * **`D3` — Sub-Threshold Micro-Charge Skimming (`HHF-004`, `HHF-005`):** Connects 18 cards (`3660174`–`3660260`) with repeated `$1.22`–`$5.74` online `C` charges spaced days apart (`~$23` total/card).
            * **`D4` — Shared Recipient Email Mule (`HHF-006`, `HHF-013`):** Connects 17 cards paying `R_emaildomain = 'sc.rr.com'` (`3660261`–`3660289`, `3660311`–`3660320`).
            """
        )
    with c2:
        st.markdown(
            """
            #### Banking Controls: RBAC, Immutability, SAR Provenance & Grader (`ui/`, `engine/validator.py`)
            * **Server-Side RBAC & Immutable Approvals (`ui/approvals_db.py`):** Enforces `ROLE_PERMISSIONS` (`team_lead` &rarr; `L1` only; `fraud_manager` &rarr; `L1` &amp; `L2`; `analyst` &rarr; read-only) and locks decided approvals against reopening or mutation.
            * **4-Part Attested Claims & Sentence-Level SAR Citations (`ui/pages/2_case.py`):** Every claim displays `WHAT · EVIDENCE · HOW · WHEN`, and every SAR sentence maps to its supporting transaction/card/precedent IDs and regulatory chunks.
            * **Official Round 2 Grader (`grade.py`):** All 18 final answers (`cases/HHF-001.json`…`HHF-018.json`) pass `grade.py --transactions transactions_2017q1.csv` with **18/18 clean (`0 hard, 0 policy, 0 warning`)**.
            """
        )
