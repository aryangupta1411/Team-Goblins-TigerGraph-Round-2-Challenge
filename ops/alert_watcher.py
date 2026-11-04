"""Alert watcher (Challenge 9): every new alert starts an investigation, so the case is ready before anyone opens it.

    python -m ops.alert_watcher --inbox alerts/inbox --run-id watch --mock          # poll forever
    python -m ops.alert_watcher --inbox alerts/inbox --run-id watch --mock --once   # one sweep, then exit

The inbox holds CSV files in case-pack columns (the Run console's upload format: case_id, opened_at, trigger_type,
flagged_txn_id, card_id, customer_id, risk_score, trigger_text). Each row whose case_id has not been seen starts one
`python -m agent.bench --run-id <run-id> --cases <case_id> --case-pack <one-row csv>` (with --mock when asked), one at a
time. runs/<run-id>/watcher.jsonl gets one line per alert: when the file was seen, when the run started and ended,
seconds from alert to ready answer, the exit code. A row already in watcher.jsonl is never started twice, so a restart
picks up where it left off. Without --mock the bench runs live against the .env workspace; that is the deployment's call.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
COLUMNS = ("case_id", "opened_at", "trigger_type", "flagged_txn_id", "card_id", "customer_id", "risk_score", "trigger_text")
REQUIRED = ("case_id", "opened_at", "trigger_type", "flagged_txn_id", "card_id", "customer_id")
CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")   # it names a folder under runs/


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def read_alerts(inbox: Path) -> list[tuple[Path, dict]]:
    """(file, row) for every valid row of every *.csv in the inbox, oldest file first; bad rows are skipped."""
    out = []
    for f in sorted(inbox.glob("*.csv"), key=lambda p: (p.stat().st_mtime, p.name)):
        with f.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                r = {k: (r.get(k) or "").strip() for k in COLUMNS}
                if all(r[k] for k in REQUIRED) and CASE_ID_RE.match(r["case_id"]):
                    out.append((f, r))
    return out


def seen_ids(log: Path) -> set[str]:
    if not log.exists():
        return set()
    return {json.loads(line)["case_id"] for line in log.read_text().splitlines() if line.strip()}


def start_case(row: dict, run_id: str, mock: bool, work: Path, python: str = sys.executable) -> dict:
    """Run agent.bench for one alert and wait for it; returns the watcher.jsonl record."""
    work.mkdir(parents=True, exist_ok=True)
    pack = work / f"{row['case_id']}.csv"
    with pack.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerow(row)
    cmd = [python, "-m", "agent.bench", "--run-id", run_id, "--cases", row["case_id"], "--case-pack", str(pack)]
    if mock:
        cmd.extend(["--mock", "--llm-backend", "mock"])
    t0, started = time.monotonic(), _now()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    answer = ROOT / "runs" / run_id / row["case_id"] / "answer.json"
    return {"case_id": row["case_id"], "started_at": started, "finished_at": _now(), "run_s": round(time.monotonic() - t0, 2),
            "exit_code": proc.returncode, "answer_ready": answer.exists(), "answer": str(answer.relative_to(ROOT)),
            "mock": mock, "stderr_tail": proc.stderr[-400:] if proc.returncode else ""}


def open_lifecycle(case_id: str, answer_path: Path) -> str:
    """Record 'opened' (and 'escalated' when approvals were queued) the moment the answer is ready (agent/lifecycle.py)."""
    try:
        from agent import lifecycle
        from ui import approvals_db
        aps = [r for r in approvals_db.rows("pending") if r["case_id"] == case_id]
        return ",".join(e["kind"] for e in lifecycle.sync_from_answer(case_id, json.loads(answer_path.read_text()), aps)) or "already recorded"
    except Exception as e:  # noqa: BLE001 - the case is ready either way
        return f"not recorded: {type(e).__name__}: {e}"[:200]


def sweep(inbox: Path, run_id: str, mock: bool, log: Path, first_seen: dict[str, tuple[str, float]]) -> list[dict]:
    done = seen_ids(log)
    out = []
    for f, row in read_alerts(inbox):
        cid = row["case_id"]
        if cid in done:
            continue
        seen_at, seen_t = first_seen.setdefault(cid, (_now(), time.monotonic()))
        rec = {"alert_file": f.name, "alert_seen_at": seen_at, **start_case(row, run_id, mock, log.parent / "watcher_alerts")}
        rec["alert_to_ready_s"] = round(time.monotonic() - seen_t, 2)
        if rec["answer_ready"]:
            rec["lifecycle"] = open_lifecycle(cid, ROOT / rec["answer"])
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        done.add(cid)
        out.append(rec)
        print(f"{cid}: exit {rec['exit_code']} · ready={rec['answer_ready']} · alert->ready {rec['alert_to_ready_s']} s", flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--inbox", type=Path, required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args(argv)
    if not re.match(r"^[A-Za-z0-9_-]{1,60}$", args.run_id):
        ap.error("--run-id: letters, digits, '-' or '_'")
    args.inbox.mkdir(parents=True, exist_ok=True)
    log = ROOT / "runs" / args.run_id / "watcher.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    first_seen: dict[str, tuple[str, float]] = {}
    while True:
        sweep(args.inbox, args.run_id, args.mock, log, first_seen)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
