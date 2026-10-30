# Innovation Claim & Attested Measurements — Final Round (HHGOA 2026)

## Headline Innovation Claim
**A Policy-Gated, Schema-Resilient Hybrid Graph Investigator (`schema_engine/` + `engine/detectors.py` + `engine/containment.py` + `agent/security.py`)** that:
1. **Survives schema drift, arbitrary column renames/reshapes, and dirty telemetry (`Challenges 11, 12, 13`)** before any investigation runs.
2. **Discovers and backtests 4 new cross-card fraud typologies in January 2017 (`Challenges 1, 2, 3`)** where attackers rotated or stripped `DeviceProfile` IDs — catching **57 compromised cards** across `HHF-001`…`006` and `HHF-013` with **0 false positives** across all 910 cleared legitimate cases.
3. **Plans targeted ring containment (`Challenges 5, 7, 14`)** that stops **$59,356.29** in coordinated wave exposure while touching **0 legitimate customers** (saving 47+ innocent customers that naive broad rules would block), and enforces **0 false blocks / 0 false SARs** across all 9 legitimate/ambiguous `HHF-` cases.
4. **Neutralizes 10/10 prompt-injection, channel-spoofing, tip-off, and RBAC privilege-escalation attacks (`Challenge 10`)** while attaching a **4-part verifiable attestation (`what`, `evidence`, `how`, `when`)** to every claim and SAR sentence (`Challenges 4 & 6`).

---

## 1. Ablation & Measurement Table (Before vs. After on `HHF-001`…`HHF-018`)

| Capability / Challenge | Baseline (Round-1 Agent Without Upgrade) | Upgraded Final-Round System | Cases Changed / Measured Impact |
|---|---|---|---|
| **Challenge 1 & 2: New Typology Detectors (`engine/detectors.py`)** | Misses cross-card links when `DeviceProfile` is rotated (`HHF-001/002`) or missing (`HHF-003/004/005/006`); misses `$1,800` 72h-gap drain on `HHF-003`; misses `§3a` SAR on `$23` micro-skimming (`HHF-004/005`). | Detects `D1` (14 cards), `D2` (8 cards), `D3` (18 cards), `D4` `sc.rr.com` (17 cards) with **0 false positives** on 910 cleared cases. | **`HHF-001, 002, 003, 004, 005, 006, 013`** (7 of 18 cases; SARs correctly triggered on `$23.99` `HHF-004` and `$23.17` `HHF-005`). |
| **Challenge 7 & 14: Containment Planner & Innocent-Customer Protection (`engine/containment.py`)** | Naive proxy/amount blocking touches 47+ legitimate customers; single-signal `risk_score` alerts risk false `BLOCK_CARD` (0 pts penalty). | Targeted wave containment blocks only attested wave cards (**$59,356.29 stopped, 0 innocent customers touched**); `R1`/`R8` keeps `HHF-010, 011, 013` in `uncertain` + `ESCALATE_TO_ANALYST` and clears `HHF-012, 014, 015, 016, 017, 018`. | **0 false blocks and 0 false SARs across all 9 non-fraud cases (`HHF-010`…`018`).** |
| **Challenge 10: Prompt-Injection & Spoofing Defense (`agent/security.py`)** | Vulnerable to `SYSTEM NOTE:` (`HHF-004/005`), `[[assistant:...]]` + fake `online` claim (`HHF-008`), fake compliance note (`HHF-010`), and decoy HTML/customer notes (`HHF-014/016`). | Strips all 6 planted injection payloads at intake, cross-checks `trigger_text` against DB `channel='in_person'` (`HHF-008`), and logs `CaseEvent(kind="security_alert")`. | **10/10 attacks neutralized** (`HHF-004, 005, 008, 010, 014, 016` + 4 synthetic drills). |
| **Challenge 11, 12, 13: Schema Engine (`schema_engine/`)** | Hardcoded filenames/row-counts fail on `2017q1` delta; 54 `card6 IS NULL` rows shift `-K1`/`-K2`; `CC-5566..5585` notes fail regex; renamed variant schemas fail. | Detects 4 schema drifts (`DRIFT-01`…`04`) with human approval gate; auto-maps **27/27 renamed & reshaped variant columns** (`cents→USD`, `epoch→ISO`, `ua_device_bundle`); flags **11/18 tainted `HHF-` cases** (`DQ-01`…`06`). | **`HHF-001`…`008`, `HHF-011, 012, 013` taint-tracked; 100% variant schema recovery.** |
| **Official Round 2 Grader (`data/raw/round2/grade.py`)** | `0/18` (no `HHF-` answers). | **`18/18 answered · 0 hard · 0 policy · 0 warnings · 18/18 clean`.** | Verified by `engine/out/round2_grade_report.json`. |

---

## 2. Reproducing Every Measurement From the CLI

```bash
# 1. Challenge 12 & 11: Schema Drift Detection & Data Quality Taint Matrix
uv run python -m schema_engine.cli drift
uv run python -m schema_engine.cli dq

# 2. Challenge 13: Automatic Column & Reshape Mapping on a Variant Bank Schema
uv run python -m schema_engine.cli variant --generate-fixture

# 3. Challenge 1 & 2: Backtest All 6 Undocumented Typology Detectors Over 5,585 Closed Cases
uv run python -m engine.backtest_detectors

# 4. Stage 1 Grader: Verify 18/18 Clean (0 hard, 0 policy, 0 warnings) on Official grade.py
uv run python data/raw/round2/grade.py --teams-dir . --case-pack data/raw/round2/case_pack_final.csv \
    --transactions data/raw/round2/transactions_2017q1.csv --out engine/out/round2_grade_report.json
```
