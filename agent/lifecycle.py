"""Case lifecycle after the agent's run (Challenge 14): open -> escalated -> added to -> handed over -> closed.

The agent's run opens the case (open_case at P0) and leaves CaseEvents and pending Approvals (persist). What happens
after that is people: an approver decides the L1/L2 rows, a customer replies, an analyst hands the case to another.
Every one of those state changes lands here as one append-only row (case_id, seq, kind, state after, time, actor,
evidence ids, detail) in the approvals SQLite file, next to the approvals and their audit log.

When the last pending approval on a case is decided, or a customer reply arrives with nothing left pending, the case
is closed: `close_case` writes the outcome onto the AgentCase vertex and `append_case_event` writes the matching
CaseEvent. Graph writes go through agent.mcp_client.open_session(read_only=False):
  - RUN_MODE=mock: the FakeSession answers (nothing leaves the process);
  - live: only when LIFECYCLE_GRAPH_WRITES=1, so a unit test or a stray UI click never writes the shared graph.
Either way the local row is written first and records whether the graph write landed (graph_written, graph_note).

`handover(case_id)` is what an analyst taking the case over cold reads: state, what changed, what it waits on,
what was decided and by whom, and the next step.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

STATE_AFTER = {
    "opened": "open", "escalated": "escalated", "evidence_added": "added_to", "reply_received": "added_to",
    "approval_decided": None, "handed_over": "handed_over", "closed": "closed",
}
# graph CaseEvent seq for lifecycle rows starts here so it never collides with persist's 1..N
GRAPH_SEQ_BASE = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS case_lifecycle (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    state        TEXT NOT NULL,
    at           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    detail       TEXT NOT NULL DEFAULT '',
    graph_written INTEGER NOT NULL DEFAULT 0,
    graph_note   TEXT NOT NULL DEFAULT '',
    UNIQUE (case_id, seq)
);
CREATE TRIGGER IF NOT EXISTS case_lifecycle_no_delete BEFORE DELETE ON case_lifecycle
BEGIN SELECT RAISE(ABORT, 'case lifecycle rows are append-only'); END;
CREATE TRIGGER IF NOT EXISTS case_lifecycle_no_update BEFORE UPDATE OF case_id, seq, kind, state, at, actor, evidence_ids, detail ON case_lifecycle
BEGIN SELECT RAISE(ABORT, 'case lifecycle rows are append-only'); END;
"""


class CaseClosed(RuntimeError):
    pass


def _db_path(path: str | os.PathLike | None) -> Path:
    if path:
        return Path(path)
    from ui import approvals_db
    return approvals_db.default_path()


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    p = _db_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


def events(case_id: str, path: str | os.PathLike | None = None) -> list[dict]:
    con = connect(path)
    try:
        rows = con.execute("SELECT * FROM case_lifecycle WHERE case_id = ? ORDER BY seq", (case_id,)).fetchall()
    finally:
        con.close()
    return [dict(r, evidence_ids=json.loads(r["evidence_ids"] or "[]")) for r in rows]


def state(case_id: str, path: str | os.PathLike | None = None) -> str | None:
    ev = events(case_id, path)
    return ev[-1]["state"] if ev else None


def record_event(case_id: str, kind: str, actor: str, evidence_ids: list[str] | tuple = (), detail: str = "",
                 path: str | os.PathLike | None = None, at: str | None = None) -> dict:
    """Append one state change. A closed case takes no further rows."""
    if kind not in STATE_AFTER:
        raise ValueError(kind)
    con = connect(path)
    try:
        with con:
            last = con.execute("SELECT seq, state FROM case_lifecycle WHERE case_id = ? ORDER BY seq DESC LIMIT 1", (case_id,)).fetchone()
            if last and last["state"] == "closed":
                raise CaseClosed(f"{case_id} is closed; no further state changes")
            seq = (last["seq"] if last else 0) + 1
            new_state = STATE_AFTER[kind] or (last["state"] if last else "open")
            con.execute("INSERT INTO case_lifecycle(case_id, seq, kind, state, at, actor, evidence_ids, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (case_id, seq, kind, new_state, at or _now(), str(actor or "system")[:120], json.dumps(list(evidence_ids)), str(detail)[:2000]))
        row = con.execute("SELECT * FROM case_lifecycle WHERE case_id = ? AND seq = ?", (case_id, seq)).fetchone()
        return dict(row, evidence_ids=list(evidence_ids))
    finally:
        con.close()


def _mark_graph(case_id: str, seq: int, ok: bool, note: str, path) -> None:
    con = connect(path)
    try:
        with con:
            con.execute("UPDATE case_lifecycle SET graph_written = ?, graph_note = ? WHERE case_id = ? AND seq = ?",
                        (1 if ok else 0, note[:500], case_id, seq))
    finally:
        con.close()


# ---------------------------------------------------------------- the agent's answer for a case

def find_answer(case_id: str, runs_root: Path | None = None) -> dict | None:
    """The newest runs/*/<case_id>/answer.json (LIFECYCLE_RUN=<folder> pins one run)."""
    root = runs_root or ROOT / "runs"
    pinned = os.getenv("LIFECYCLE_RUN", "").strip()
    cands = [root / pinned / case_id / "answer.json"] if pinned else list(root.glob(f"*/{case_id}/answer.json"))
    cands = [p for p in cands if p.exists()]
    if not cands:
        return None
    return json.loads(max(cands, key=lambda p: p.stat().st_mtime).read_text())


def _evidence_ids(answer: dict | None) -> list[str]:
    out: list[str] = []
    for e in ((answer or {}).get("case") or {}).get("evidence") or []:
        if isinstance(e, dict):
            ref = e.get("id") or e.get("ref") or e.get("source_ref")
            if ref and ref not in out:
                out.append(str(ref))
    return out


def sync_from_answer(case_id: str, answer: dict | None, approvals: list[dict], path=None) -> list[dict]:
    """Seed 'opened' (and 'escalated' when approvals were queued) from the agent's run, once per case."""
    if events(case_id, path):
        return []
    a = answer or {}
    c = a.get("case") or {}
    out = [record_event(case_id, "opened", "agent", _evidence_ids(a)[:20],
                        f"Agent run opened the case: verdict {c.get('verdict', '?')}, p={c.get('fraud_probability', '?')}, "
                        f"status {c.get('status', '?')}.", path)]
    if approvals:
        out.append(record_event(case_id, "escalated", "agent", [f"approval:{r['id']}" for r in approvals],
                                "Queued for human approval: " + ", ".join(f"{r['action']} ({r['route']})" for r in approvals), path))
    return out


# ---------------------------------------------------------------- graph writes (close_case + append_case_event)

def _settings():
    from agent.config import SETTINGS
    return SETTINGS


def graph_mode() -> str:
    """'mock' (FakeSession), 'live' (LIFECYCLE_GRAPH_WRITES=1) or 'off'."""
    if _settings().mock:
        return "mock"
    return "live" if os.getenv("LIFECYCLE_GRAPH_WRITES", "").strip() == "1" else "off"


def close_params(case_id: str, answer: dict | None, outcome: str, approver: str, at: str, what_changed: str) -> dict:
    """close_case parameters from the agent's answer and the human outcome."""
    from agent.case_ids import agent_case_id
    a = answer or {}
    c = a.get("case") or {}
    nba = a.get("next_best_actions") or {}
    sar = a.get("sar") or {}
    sims = [str(x) for x in c.get("similar_prior_cases") or []]
    return {
        "ac": {"id": c.get("graph_case_id") or agent_case_id(case_id)},
        "status": outcome, "verdict": c.get("verdict", ""), "fraud_probability": float(c.get("fraud_probability") or 0.0),
        "pattern": c.get("pattern", "") or "", "pattern_description": c.get("pattern_description", "") or "",
        "exposure_usd": float(c.get("exposure_usd") or 0.0), "summary": c.get("summary", "") or "",
        "sar_filed": bool(sar.get("file")), "sar_narrative": sar.get("narrative", "") or "",
        "initial_actions": json.dumps(nba.get("initial") or [], sort_keys=True),
        "final_actions": json.dumps(nba.get("final") or [], sort_keys=True),
        "what_changed": what_changed, "stop_reason": f"closed by {approver}: {a.get('stop_reason', '')}"[:2000], "updated_at": at,
        "affected_txn_ids": [str(x) for x in c.get("affected_txn_ids") or []],
        "connected_card_ids": [str(x) for x in c.get("connected_card_ids") or []][:100],
        "connected_device_profiles": [str(x) for x in c.get("connected_device_profiles") or []],
        "similar_closed_cases": {x: 1.0 for x in sims if x.startswith("CC-")},
        "similar_agent_cases": {x: 1.0 for x in sims if x.startswith("AC-")},
        "cited_chunk_ids": [],
    }


async def _write_graph(case_id: str, ev: dict, params: dict | None) -> tuple[bool, str, list[str]]:
    from agent.case_ids import agent_case_id
    from agent.mcp_client import open_session, run_query
    gid = agent_case_id(case_id)
    calls: list[str] = []
    async with open_session(read_only=False, settings=_settings()) as (session, _tools):
        if params is not None:
            await run_query(session, "close_case", params)
            calls.append("close_case")
        await run_query(session, "append_case_event", {
            "case_id": gid, "seq": GRAPH_SEQ_BASE + ev["seq"], "kind": f"lifecycle_{ev['kind']}", "event_at": ev["at"],
            "payload": json.dumps({"state": ev["state"], "actor": ev["actor"], "evidence_ids": ev["evidence_ids"], "detail": ev["detail"]}, sort_keys=True)})
        calls.append("append_case_event")
        seen = [n for n, _ in getattr(session, "calls", [])]
    return True, f"{graph_mode()}: {', '.join(calls)} -> {gid}-{GRAPH_SEQ_BASE + ev['seq']:03d}", seen


def push_to_graph(case_id: str, ev: dict, params: dict | None = None, path=None) -> dict:
    """Write the CaseEvent (and close_case when params are given); record the outcome on the local row."""
    mode = graph_mode()
    if mode == "off":
        note = "graph write off (live without LIFECYCLE_GRAPH_WRITES=1); local record only"
        _mark_graph(case_id, ev["seq"], False, note, path)
        return {"ok": False, "note": note, "mode": mode}
    try:
        ok, note, seen = asyncio.run(_write_graph(case_id, ev, params))
    except Exception as e:  # noqa: BLE001 - the local record stands; the hand-over view shows the failure
        ok, note, seen = False, f"{mode}: graph write failed: {type(e).__name__}: {str(e)[:300]}", []
    _mark_graph(case_id, ev["seq"], ok, note, path)
    return {"ok": ok, "note": note, "mode": mode, "tool_calls": seen}


# ---------------------------------------------------------------- triggers

def _approvals(case_id: str, path) -> list[dict]:
    from ui import approvals_db
    return [r for r in approvals_db.rows(path=path) if r["case_id"] == case_id]


def _outcome(answer: dict | None, approvals: list[dict]) -> tuple[str, str]:
    verdict = ((answer or {}).get("case") or {}).get("verdict", "")
    rejected = [r for r in approvals if r["status"] == "rejected"]
    base = {"fraud": "closed_fraud", "legitimate": "closed_legitimate"}.get(verdict, "closed_reviewed")
    if rejected:
        return base + "_overridden", "Approver rejected " + ", ".join(f"{r['action']} ({r['decided_by']})" for r in rejected)
    return base, "All recommended actions approved" if approvals else "Closed on the customer reply"


def close(case_id: str, approver: str, reason: str, path=None, answer: dict | None = None, at: str | None = None) -> dict:
    """Close the case: local 'closed' row + close_case + CaseEvent. Raises CaseClosed when already closed."""
    answer = answer if answer is not None else find_answer(case_id)
    aps = _approvals(case_id, path)
    sync_from_answer(case_id, answer, aps, path)
    outcome, what = _outcome(answer, aps)
    when = at or _now()
    evid = _evidence_ids(answer)[:20] + [f"approval:{r['id']}" for r in aps if r["status"] != "pending"]
    ev = record_event(case_id, "closed", approver, evid, f"{outcome}. {what}. {reason}".strip(), path, when)
    g = push_to_graph(case_id, ev, close_params(case_id, answer, outcome, approver, when, f"{what}. {reason}".strip()), path)
    return {"event": ev, "outcome": outcome, "graph": g}


def on_approval_decided(row: dict, path=None, answer: dict | None = None) -> dict:
    """Called by approvals_db.decide after a decision commits. Closes the case when nothing is left pending."""
    case_id = row["case_id"]
    answer = answer if answer is not None else find_answer(case_id)
    aps = _approvals(case_id, path)
    sync_from_answer(case_id, answer, aps, path)
    ev = record_event(case_id, "approval_decided", row["decided_by"], [f"approval:{row['id']}"],
                      f"{row['action']} ({row['route']}) {row['status']} by {row['decided_by']}", path, row.get("decided_at"))
    out: dict[str, Any] = {"event": ev, "graph": push_to_graph(case_id, ev, None, path)}
    if not any(r["status"] == "pending" for r in aps):
        out["closed"] = close(case_id, row["decided_by"], f"Final approval #{row['id']} decided.", path, answer, row.get("decided_at"))
    return out


def on_customer_reply(case_id: str, text: str, recorded_by: str, path=None, answer: dict | None = None, at: str | None = None) -> dict:
    """A customer reply adds to the case; with no approval pending it closes it."""
    answer = answer if answer is not None else find_answer(case_id)
    aps = _approvals(case_id, path)
    sync_from_answer(case_id, answer, aps, path)
    ev = record_event(case_id, "reply_received", recorded_by, [f"reply:{case_id}"], f"Customer reply: {text}", path, at)
    out: dict[str, Any] = {"event": ev, "graph": push_to_graph(case_id, ev, None, path)}
    if not any(r["status"] == "pending" for r in aps):
        out["closed"] = close(case_id, recorded_by, "Customer reply received with nothing pending.", path, answer, at)
    return out


def hand_over(case_id: str, from_actor: str, to_actor: str, note: str = "", path=None, at: str | None = None) -> dict:
    sync_from_answer(case_id, find_answer(case_id), _approvals(case_id, path), path)
    ev = record_event(case_id, "handed_over", from_actor, [], f"Handed to {to_actor}. {note}".strip(), path, at)
    return {"event": ev, "graph": push_to_graph(case_id, ev, None, path)}


# ---------------------------------------------------------------- the hand-over view

def handover(case_id: str, path=None, answer: dict | None = None) -> dict:
    """Everything an analyst needs to take the case over cold."""
    answer = answer if answer is not None else find_answer(case_id)
    a = answer or {}
    c = a.get("case") or {}
    aps = _approvals(case_id, path)
    ev = events(case_id, path)
    pending = [r for r in aps if r["status"] == "pending"]
    decided = [r for r in aps if r["status"] != "pending"]
    requests = [r for r in a.get("evidence_requests") or [] if isinstance(r, dict)]
    replied = any(e["kind"] == "reply_received" for e in ev)
    st = ev[-1]["state"] if ev else ("escalated" if pending else "open" if a else "unknown")
    if st == "closed":
        nxt = "Nothing: the case is closed. Reopen only with new evidence (a new alert opens a new case)."
    elif pending:
        nxt = "Decide " + ", ".join(f"#{r['id']} {r['action']} ({r['route']})" for r in pending) + " on the Approvals page."
    elif requests and not replied:
        nxt = "Wait for the customer reply to " + ", ".join(str(r.get("type", "request")) for r in requests) + ", then record it here."
    else:
        nxt = "Close the case: every approval is decided."
    return {
        "case_id": case_id, "state": st, "verdict": c.get("verdict"), "fraud_probability": c.get("fraud_probability"),
        "summary": c.get("summary", ""),
        "what_changed": (a.get("next_best_actions") or {}).get("what_changed", ""),
        "waiting_on": {"approvals": pending, "replies": [] if replied else requests},
        "decided": [{"what": f"{r['action']} ({r['route']})", "status": r["status"], "by": r["decided_by"], "at": r["decided_at"]} for r in decided],
        "events": ev, "next_step": nxt,
    }
