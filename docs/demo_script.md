# Demo script: Final Round, 5 minutes, judge picks the case

Every number spoken below is shown on screen from a file. Each one is named next to the shot that shows it, so nothing
is quoted from memory.

## Before the session (15 min)

1. Wake the workspace and keep it awake: `make demo-warm` (`make awake` + `make keepalive`).
2. Start the dashboard: `make ui`, at http://localhost:8501. It opens on the canonical run `runs/hhf-final-3`, the
   source of `cases/HHF-*.json`. The same app is live at
   https://team-goblins-tigergraph-round-2-challenge-production.up.railway.app, connected to FraudGraph: open it in a
   second tab as the fallback if the laptop's workspace connection drops, and to show that it runs outside this machine.
3. Approvals needs an approver directory (`ui/principals.json`, git-ignored). Without one, nobody can approve; the page says
   "missing → nobody can approve (fail closed)". To show a real sign-in, create one team lead and one fraud manager:
   `uv run python -m ui.approvals_db add-principal lead@bank --role team_lead` and the same with `--role fraud_manager` (each prompts for a passphrase). The wrong-role drill itself needs no directory, because it runs in a temp store.
4. Screen at 1920×1080, browser zoom 110 %, notifications off. A terminal is open on the repo for the fallback.
5. Know the live re-run's state. A live January case calls the installed `typology_scan` in the graph, and the
   submitted run did so on all 18 cases (`runs/hhf-final-3/*/calls.jsonl`). Confirm the query is still there before the
   demo with `graph/check_typology_parity.py --installed`, which is read-only. If it is missing, a live re-run stops
   with `CaseDataMissing` and writes no answer, which is the fail-closed drill below.
6. Know the run's history, in case a judge asks. The submitted run `runs/hhf-final-3` (commit `05bc21d`, clean
   detached worktree, `gemini-3.6-flash`) completed 18 / 18 in one invocation with no failure. It replaced
   `runs/hhf-final-2` (`0010621`): same verdicts, but HHF-008 no longer carries `DECLINE_TRANSACTION` (the R4 decline
   waits on a real reply) and `sar.md` is now the cited filing. `hhf-final-2` had replaced the earliest run,
   `runs/hhf-final` (`6945275`, `gemini-2.5-flash`), after an engine change moved four verdicts (HHF-008 → uncertain,
   HHF-010 and HHF-014 → legitimate, HHF-013 → fraud with a SAR). That earliest run's one incident is the fail-closed
   example: Gemini returned degenerate P5 output for HHF-005 three times, the case failed with no answer, and
   `--resume` re-ran it (`runs/hhf-final/provenance.json`, `failed_attempt`; `docs/RESILIENCE.md`).

## The 5 minutes

| Time | Page | What is on screen | Say (short) | Source of what is shown |
|---|---|---|---|---|
| 0:00–0:30 | **Overview** | The decision board: 18 HHF rows with trigger, verdict, final actions, SAR, evidence request, approvals and run state; a click on a case opens its Case file. Ask the judge to pick any case. | "Eighteen alerts, one live run on Gemini 3.6 Flash. Eight fraud, one uncertain, nine legitimate. Seven SARs, all on the multi-card rings. Pick any row." Once the judge picks, start the live re-run of that case straight away (see below): it finishes while we walk through the file. | `runs/hhf-final-3/HHF-*/answer.json` |
| 0:30–1:00 | **Case file**: decision card | Verdict, probability, pattern, status, final actions with routes (auto / L1 / L2), SAR yes or no, tool calls, tokens, latency for the picked case | "This is the answer file as submitted, byte for byte. The routes come from the written policy: auto runs, L1 goes to a team lead, L2 to a fraud manager." | `cases/<case>.json` (byte-identical to `runs/hhf-final-3/<case>/answer.json`) |
| 1:00–1:45 | **Case file** → tab **Decision path P0–P10** | Each phase in order: P0 harness facts, P2 the model's own query choices, P3 the scorecard and its score moves, P4 the model's ±0.10 adjustment, P5/P7 actions inside the admissible set, P8 SAR, P9 summary. Point at any clamp or `policy_override` note. | "The engine decides; the model explains, picks extra queries and may move p by at most 0.10. Any action it proposes outside the policy's admissible set is replaced." | `runs/hhf-final-3/<case>/phases.jsonl` |
| 1:45–2:30 | **Case file** → tab **Evidence ledger** | One row per claim, with what / evidence / how / when checkboxes, the matched call and its timestamp, and simulated replies marked `ASSUMED` | "Every claim names what it says, the ids it rests on, the query that returned them and when that query ran. All of it is time-boxed to the moment the alert opened." | `answer.json` `case.evidence` joined with `calls.jsonl` |
| 2:30–3:15 | **Graph** | The case's neighbourhood in FraudGraph, and the write-back rows (`AgentCase AC-<case>`, `CaseEvent`s, `Approval`s, `CASE_*` edges). For a ring case (HHF-001 … 006, HHF-013), the connected cards that share no device. | "The waves share no device, so a device traversal finds nothing. The detectors scan the transactions themselves. The case is written back, so later cases retrieve it." | `runs/hhf-final-3/<case>/calls.jsonl` (`caller=persist`); in live mode, `case_subgraph` on the workspace |
| 3:15–4:00 | One **Proof** page: **Detectors & containment** (if the judge prefers, **Runs & consistency**) | The D1-D4 backtest over 5,585 closed cases: 0 cleared cases hit by D1-D3, 1 by D4. The containment table: 0 legitimate customers touched by the targeted plans, against 55 / 3 / 91 / 26 extra customers for a broad rule. | "We backtest every detector on the bank's closed cases and name the legitimate customers it would act against. The targeted block set touches none." | `engine/out/detector_backtest.json`, `engine/out/containment_summary.json` |
| 4:00–4:45 | **Approvals** → **Wrong-role drill** → "Run wrong-role drill in a sandbox" | 4 / 4 blocked by the server: a team lead deciding an L2 `FILE_REPORT`, a role claimed by the client, a session re-labelled outside `authenticate()`, and an overwrite of a decided approval. Then the audit log tab. | "The role comes from the server's directory, never from the request. Decided approvals are immutable: SQLite triggers refuse the update. The drill runs in a throwaway store, so the real inbox is untouched." | `ui.approvals_db.run_rbac_drills()`; the same four drills are in `agent.security.run_attack_ablation()` (ATK-08, 22, 23, 09) |
| 4:45–5:00 | Back to **Overview** | The board again | "Every number you saw is computed from a file in this repo. The README names each file." | `README.md` (sources line under each table) |

Start the live re-run within the first minute, as soon as the judge picks a case: on the **Case file**, the re-run dialog lists its side effects. It
writes `AC-<case>` and its events to the shared graph, adds inbox rows, spends tokens, and writes `runs/<run_id>/`. It
starts only after the checkbox is ticked. On the public deployment, sign in first (a team lead, fraud manager or admin;
anyone else is refused), and the run writes `AC-web-<case>`, so the submitted write-backs stay as they are. A live re-run takes about as long as the decision card's latency (median
62.7 s, p95 84.2 s in `runs/hhf-final-3`), so started by 1:00 it is done well before the Graph shot.

## If the live re-run fails

Say what failed and show the failure. Do not switch data silently.

1. **The workspace is unreachable.** `GraphUnavailable` arrives after up to `TG_RESUME_MAX_WAIT_S` (240 s). No answer is
   written and the run table marks the case failed. Show that row, then carry on from the recorded run: every page
   reads `runs/hhf-final-3` through the run picker.
2. **The case stops with `CaseDataMissing`** (expected only if `typology_scan` has been removed from the workspace). Open the failed run's
   `phases.jsonl` (`case failed`, "typology_scan failed … no answer written"). That stop is the fail-closed rule: a
   January case is never scored without its detector evidence.
3. **The model backend fails** (quota, timeout, unparseable output after 3 attempts). The case fails and no partial answer is promoted.
   Show the `kind=llm` error row in `calls.jsonl`.
4. **Offline proof that it runs from a clean clone.** In the terminal, run the one-command drill. It needs no keys and no graph,
   and it is the same step CI runs:

   ```bash
   RUN_MODE=mock uv run python -m agent.bench --cases HHF-004 --run-id demo-drill --mock && uv run python -m agent.validator --db data/ids.duckdb runs/demo-drill/HHF-004/answer.json
   ```

   Then, if asked, show that the deterministic engine reproduces all 18 verdicts, SAR decisions and final action sets:
   `uv run python -m engine.run_cases --cases HHF-001 … HHF-018 --out <dir> --sheet <dir>/sheet.md`.
5. **The UI itself will not start.** Fall back to `RUN_MODE=mock make ui`, which uses the same pages over the recorded
   run, or to the files: `cases/MANIFEST.md` (per-case table), then `runs/hhf-final-3/<case>/answer.json` and `sar.md`.

## What not to say

- No accuracy against the answer key: the organisers hold it.
- The submitted run did complete in one pass, but the earliest run did not: HHF-005 failed closed once in `hhf-final` and was resumed (see step 6 above).
- The three candidate detectors were written by us, not invented by the agent (`docs/INNOVATION.md`, "What is not claimed").

## If asked for the weakest point

- **Challenge 12:** the January loader refuses to run until the schema migration is signed, and that approval still
  awaits the human signature.
- **Challenge 2:** the discovery miner (`engine/discovery.py`) found a detector proposal, and it is not yet approved.
- **Accuracy:** unknown against the answer key. The organisers hold it; what we show is self-consistency (the offline
  engine matches the live run on all 18) and a clean grader report.
