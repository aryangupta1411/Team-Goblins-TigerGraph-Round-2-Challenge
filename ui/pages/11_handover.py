"""Hand-over: take a case over cold.

For the case picked in the context bar: its state, what changed, what it is waiting on (pending approvals, requested
customer replies), what was decided and by whom, the next step, and every state change with its time, actor and
evidence ids (agent/lifecycle.py, stored next to the approvals). A signed-in approver can record a customer reply or
hand the case to a colleague; the final approval (Approvals page) or a reply with nothing pending closes the case and
calls close_case. A read-only deployment shows the record and changes nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent import lifecycle  # noqa: E402
from ui import common  # noqa: E402

ctx = common.page_setup("handover")
common.page_header("Hand-over", "Where this case stands and what it needs next, for whoever picks it up.")
case_id = ctx.case_id
if not case_id:
    st.info("Pick a case in the context bar.")
    st.stop()

db = common.approvals_db_path()
answer = None
if ctx.source is not None:
    p = common.runs_root() / str(ctx.source_id) / case_id / "answer.json"
    if p.exists():
        import json
        answer = json.loads(p.read_text())
h = lifecycle.handover(case_id, db, answer)

c1, c2, c3 = st.columns(3)
c1.metric("State", h["state"].replace("_", " "))
c2.metric("Verdict", f"{h['verdict'] or '—'}" + (f" · p={h['fraud_probability']:.2f}" if isinstance(h["fraud_probability"], (int, float)) else ""))
c3.metric("Waiting on", f"{len(h['waiting_on']['approvals'])} approvals · {len(h['waiting_on']['replies'])} replies")
st.markdown(f"**Next step.** {h['next_step']}")
if h["summary"]:
    st.markdown(f"**Summary.** {h['summary']}")
if h["what_changed"]:
    st.markdown(f"**What changed during the run.** {h['what_changed']}")

st.subheader("Waiting on")
if h["waiting_on"]["approvals"]:
    st.dataframe(pd.DataFrame([{k: r[k] for k in ("id", "action", "route", "reason")} for r in h["waiting_on"]["approvals"]]), hide_index=True, width="stretch")
for r in h["waiting_on"]["replies"]:
    st.markdown(f"- Customer reply to **{r.get('type', 'request')}**: {r.get('question') or r.get('why') or ''}")
if not h["waiting_on"]["approvals"] and not h["waiting_on"]["replies"]:
    st.caption("Nothing pending.")

st.subheader("Decided")
if h["decided"]:
    st.dataframe(pd.DataFrame(h["decided"]), hide_index=True, width="stretch")
else:
    st.caption("No decision yet.")

st.subheader("State changes")
if h["events"]:
    st.dataframe(pd.DataFrame([{"seq": e["seq"], "at": e["at"], "change": e["kind"], "state": e["state"], "by": e["actor"],
                                "evidence": ", ".join(e["evidence_ids"]), "detail": e["detail"],
                                "graph": ("written" if e["graph_written"] else "not written") + (f" · {e['graph_note']}" if e["graph_note"] else "")}
                               for e in h["events"]]), hide_index=True, width="stretch")
else:
    st.caption("No state change recorded since the agent's run: the first decision, reply or hand-over starts the record.")

if h["state"] != "closed" and not ctx.read_only:
    who = ctx.session
    if who is None:
        st.caption("Sign in as an approver (top-right sign-in badge) to record a reply, hand the case over, or decide pending approvals to close the case.")
    else:
        a, b, c = st.columns(3)
        with a.form("ho_reply"):
            text = st.text_area("Customer reply", max_chars=1000, placeholder="e.g. Customer confirmed they did not make the charges; card destroyed.")
            if st.form_submit_button("Record reply & advance") and text.strip():
                out = lifecycle.on_customer_reply(case_id, text.strip(), who.principal, db, answer)
                st.success("Recorded" + (f"; case closed ({out['closed']['outcome']})" if "closed" in out else ""))
                st.rerun()
        with b.form("ho_handover"):
            to = st.text_input("Hand to", max_chars=120, value="manager@demo")
            note = st.text_input("Note", max_chars=500, value="Cold shift hand-over: reviewed graph evidence, ready for final sign-off.")
            if st.form_submit_button("Hand over"):
                if to.strip():
                    lifecycle.hand_over(case_id, who.principal, to.strip(), note.strip(), db)
                    st.rerun()
        with c.form("ho_close_approvals"):
            st.markdown("**Approve pending & close case**")
            pend_list = h["waiting_on"]["approvals"]
            st.caption(f"{len(pend_list)} pending approval(s) on {case_id}" if pend_list else "No pending approvals on this case.")
            close_note = st.text_input("Decision reason", value="Approved after cold hand-over review", max_chars=300)
            if st.form_submit_button("Approve all & close case", type="primary", disabled=not pend_list):
                from ui import approvals_db
                for ap in pend_list:
                    approvals_db.decide(int(ap["id"]), "approved", who, close_note, path=db)
                    lifecycle.on_approval_decided(case_id, str(ap["action"]), "approved", who.principal, path=db, answer=answer)
                st.rerun()
