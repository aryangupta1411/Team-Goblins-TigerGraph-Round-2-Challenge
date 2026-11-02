# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Three audiences, all first-class (confirmed by the owner, 2026-10-31). The console must be presentable, smooth and good to use for each of them; none is a secondary afterthought.

- **Hackathon judges** (Hacker House Goa 2026, Final Round). A five-minute judged demo on a 1920×1080 screen at 110 % zoom. The judge picks any of the 18 cases (HHF-001 … HHF-018) and follows it: Overview → Case file → Graph → one Proof page → Approvals → back to Overview (`docs/demo_script.md`).
- **Fraud analysts and approvers.** Triage the alerts, read the evidence behind each decision, and decide gated actions in an authenticated approvals inbox: L1 is a team lead, L2 a fraud manager.
- **The team's engineers.** Check runs and their consistency, data quality and schema mapping, detectors, security drills and the architecture, and keep working on the system with the console.

## Product Purpose

An agent takes an alert from the case pack and investigates it through TigerGraph: a GSQL query library and graph algorithms exposed as TigerGraph MCP tools. It grounds itself with GraphRAG over closed cases and FinCEN/FFIEC/FATF guidance (TigerVector) and decides under the written Fraud Policy with a deterministic policy gate. It asks for evidence when the policy says so, explains itself, and writes its case back into the graph so later investigations retrieve it.

The console shows every decision, why it was made, and the proof behind each claim and number. Success means anyone can pick a case and see its verdict and the reason for it on the first screen, then trace any number or claim to the file, query and time it came from.

## Positioning

Taken from the repository (`docs/INNOVATION.md`, the team's stated claim). The owner expressed no preference on which claim the console foregrounds.

- **Act only where the graph shows coordination; everywhere else, verify instead of block.** Cross-card detectors D1–D4 find January's undocumented waves that share no device. Only a detector match or a customer denial lets the agent block. Every detector is backtested over all 5,585 closed cases, naming the legitimate customers it would act against.
- **The engine decides; the model explains.** The model picks extra queries and may move the fraud probability by at most ±0.10. Any action it proposes outside the policy's admissible set is replaced.
- **Every claim is evidenced.** Each claim names the ids it rests on, the query that returned them and when that query ran, time-boxed to the moment the alert opened.

## Operating Context

- The canonical run is `runs/hhf-final-3`: a live run on Gemini 3.6 Flash from commit `05bc21d` (it supersedes `hhf-final-2` and the earliest run, `hhf-final`). `cases/HHF-*.json` are byte-identical copies of its answers.
- Per case, the files are `runs/<run>/<case>/answer.json`, `phases.jsonl`, `calls.jsonl` and `sar.md`, plus `run_manifest.json` and `provenance.json` per run. Proof artifacts are `engine/out/*.json`, facts are in `data/*.duckdb`, and approvals are in a SQLite store.
- The public deployment is Railway, read-only (`DEPLOY_READONLY=1`): it never spawns the agent and never writes to the graph. When TigerGraph is unreachable, the console reads the recorded files (files-only / mock mode) and says so.
- In the demo the console is served by `make ui` at localhost:8501. If a live re-run fails, the presenter shows the failure and carries on from the recorded run; data is never switched silently.

## Capabilities and Constraints

The owner marked all of these as binding ("everything stays").

- **Stack.** Streamlit, with `ui/app.py` as the router, shared helpers in `ui/common.py` and pages in `ui/pages/*.py`, deployed through `Dockerfile` / `railway.json`. Custom HTML and CSS inside Streamlit are allowed; replacing the framework is not.
- **Pages and URLs.** Every page registered in `common.PAGES` keeps its file, URL path and place in the top navigation: Overview, Case file, Graph and Approvals; a Proof group; a System group. That includes "Live architecture", which the team added on 2026-10-31. The query parameters `?run=` and `?case=` stay. The layouts inside the pages may change completely.
- **Provenance.** Every headline number keeps its source file and as-of time (`common.measured`). The UI never recomputes a proof artifact and never claims what a file does not show.
- **Untrusted text.** It is shown verbatim and never followed.
- **Themes.** Light and dark both stay, switchable from the app menu, and both must look designed.
- **Terminology the console uses.**
  - Verdicts: fraud, legitimate, uncertain.
  - Routes: auto, L1 (team lead), L2 (fraud manager).
  - Phases P0–P10. Detectors D1–D4. Data-quality codes DQ-xx. SAR.
  - Hard actions: `BLOCK_CARD`, `BLOCK_ALL_CARDS`, `FILE_REPORT` and the other impact actions.
  - Write-back: an `AgentCase AC-<case>` vertex with `CaseEvent` and `Approval` rows.

## Brand Commitments

- **Name.** The app's own name is **HHGOA Fraud Investigator** (`ui/assets/mark.svg`, set by the team on 2026-10-31).
- **Platform credit.** TigerGraph is the platform. It is credited separately ("Built on TigerGraph Savanna") and never presented as the product's brand.
- **Colour.** An earlier theme comment named a navy (#0b3d91) as the colour of the demo deck and video. The owner has not confirmed it as binding.

## Evidence on Hand

- **Final Round answers:** 18 (8 fraud, 9 legitimate, 1 uncertain; 7 SARs, all on the multi-card rings), with full traces in `runs/hhf-final-3/`. Per case: latency median 62.7 s (p95 84.2 s), cost p50 $0.10 (p95 $0.18) (`engine/out/cost_latency.json`); official grader 18 / 18 clean.
- **Round 1 / Round 2 archives:** HHG cases and runs under `runs/`.
- **Detector backtest:** over 5,585 closed cases (`engine/out/detector_backtest.json`).
- **Containment summary:** 0 legitimate customers touched by the targeted plans (`engine/out/containment_summary.json`).
- **Attack ablation:** 23 / 23 attacks blocked (`agent.security.run_attack_ablation()`), including 4 / 4 approval RBAC drills.
- **Architecture diagram:** `docs/architecture.svg`.
- **Not on hand, never to be fabricated:** accuracy against the answer key (the organisers hold it), customers, testimonials, production-deployment claims.

## Product Principles

1. **Answer first.** The verdict and the reason for it are on the first screen of every case; the evidence follows in the order an investigator reads it.
2. **Prove, don't claim.** Every number and claim traces to a file, a query and a time, one step away.
3. **Honest states.** Failures, mocks, missing files and read-only limits are shown plainly and never dressed up or hidden.
4. **One console, three audiences.** Presentable for a judge, efficient for an analyst, inspectable for an engineer, without separate modes.
5. **Safety stays legible.** Gated actions, approval roles, untrusted input and clamps are always visible where they apply.
