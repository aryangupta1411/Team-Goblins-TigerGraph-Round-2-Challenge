"""Notifications page: manage recipient phone numbers and send / test alert broadcasts.

Recipients entered here receive notifications whenever:
- An agent investigation flags a transaction or case as fraud.
- An analyst escalates an uncertain case or files a SAR.
- An L1/L2 action is approved or rejected in the approval inbox.
- A batch agent benchmark run completes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ops import notify  # noqa: E402
from ui import common  # noqa: E402

ctx = common.page_setup("notifications")
common.page_header(
    "Notifications",
    "Manage recipient phone numbers and dispatch live fraud alert broadcasts via the notification service.",
)

cfg = notify.get_config()
recipients = notify.get_recipient_list()
enabled = notify.is_notify_enabled()

# --------------------------------------------------------------------------- #
# Top Status Metrics                                                          #
# --------------------------------------------------------------------------- #
db_file = notify.get_db_path()
is_volume = str(db_file).startswith("/data") or (os.getenv("RUNS_DIR") and str(os.getenv("RUNS_DIR")).startswith("/data"))
storage_name = "Railway Volume DB" if is_volume else "Persistent SQLite"

m1, m2, m3, m4 = st.columns(4)
m1.metric("Status", "Active" if enabled else "Disabled")
m2.metric("Configured Recipients", len(recipients))
m3.metric("Storage Backend", storage_name)
m4.metric("Filter", "Fraud & SAR Only" if cfg["fraud_only"] else "All Verdicts")

st.info(
    f"💾 **Storage & Persistence:** Recipients added here are saved directly to `{db_file}` "
    + ("(mounted on Railway volume — 100% persistent across redeploys)." if is_volume else "and synced with local storage. On Railway, attach a volume at `/data` so updates survive container rebuilds."),
    icon=":material/storage:",
)
st.divider()

# --------------------------------------------------------------------------- #
# Tabs: Recipients, Broadcast & Test, Configuration                           #
# --------------------------------------------------------------------------- #
tab_recipients, tab_broadcast, tab_config = st.tabs([
    "👥 Manage Recipients",
    "🚀 Send Broadcast & Test",
    "⚙️ Service Configuration",
])

with tab_recipients:
    st.subheader("Add New Recipient")
    st.caption("Enter phone numbers with country code (e.g., 918077151158 for India). Digits only, no spaces or '+' symbol.")

    with st.form("add_recipient_form", clear_on_submit=True):
        c_num, c_name, c_btn = st.columns([2, 2, 1])
        new_phone = c_num.text_input("Phone Number", placeholder="e.g. 918077151158")
        new_name = c_name.text_input("Name / Role (optional)", placeholder="e.g. On-call Analyst")
        submit_add = c_btn.form_submit_button("➕ Add Recipient", use_container_width=True)

        if submit_add:
            clean_num = notify.normalize_phone(new_phone)
            if not clean_num or len(clean_num) < 8:
                st.error("Please enter a valid phone number with country code (minimum 8 digits).")
            else:
                added = notify.add_recipient(clean_num, new_name)
                if added:
                    st.success(f"Added recipient {clean_num} ({new_name or 'Unnamed'}) successfully!")
                    st.rerun()
                else:
                    st.warning(f"Phone number {clean_num} is already in the recipient list.")

    st.subheader(f"Current Recipients ({len(recipients)})")
    if not recipients:
        st.info("No recipient numbers registered. Notifications will fall back to the default contact.")
    else:
        df_rows = []
        for r in recipients:
            df_rows.append({
                "Phone": r["phone"],
                "Name / Label": r.get("name") or "—",
                "Added At": r.get("added_at") or "—",
            })
        st.dataframe(pd.DataFrame(df_rows), hide_index=True, width="stretch")

        st.markdown("#### Remove a Recipient")
        r_col1, r_col2 = st.columns([3, 1])
        phone_choices = [f"{r['phone']} ({r.get('name') or 'Unnamed'})" for r in recipients]
        selected_choice = r_col1.selectbox("Select recipient to remove", phone_choices, label_visibility="collapsed")
        if r_col2.button("🗑️ Remove Selected", use_container_width=True):
            target_phone = selected_choice.split(" ")[0].strip()
            if notify.remove_recipient(target_phone):
                st.success(f"Removed {target_phone} from recipients.")
                st.rerun()
            else:
                st.error("Failed to remove recipient.")

with tab_broadcast:
    st.subheader("Broadcast Custom Message")
    st.caption("Compose and send an immediate message to all registered phone numbers or a selected contact.")

    msg_target = st.radio(
        "Recipients",
        ["All Registered Recipients", "Specific Number"],
        horizontal=True,
    )

    specific_number = None
    if msg_target == "Specific Number":
        specific_number = st.selectbox(
            "Select contact",
            [r["phone"] for r in recipients] if recipients else [str(cfg["phone"])],
        )

    custom_msg = st.text_area(
        "Message Text",
        value="TigerGraph Fraud Agent: Test alert broadcast from analyst dashboard.",
        height=100,
    )

    if st.button("🚀 Send Notification", type="primary", use_container_width=True):
        if not custom_msg.strip():
            st.error("Please enter a message to send.")
        else:
            with st.spinner("Dispatching notification..."):
                target_arg = specific_number if msg_target == "Specific Number" else None
                res = notify.send_notification(custom_msg.strip(), phone=target_arg)

            if res.get("ok"):
                st.success(f"Notification dispatched successfully to {res.get('sent', 0)} recipient(s)!")
            else:
                st.error(f"Failed to dispatch notification: {res.get('error') or res.get('reason')}")

            with st.expander("Delivery Log Details", expanded=True):
                for r in res.get("results", []):
                    icon = "✅" if r.get("ok") else "❌"
                    status = r.get("status_code", 0)
                    phone = r.get("phone", "")
                    st.write(f"{icon} **{phone}**: HTTP {status} — {r.get('response') or r.get('error')}")

    st.divider()
    st.subheader("Simulate System Triggers")
    st.caption("Test how automated events format and deliver alerts to all registered recipients.")

    sim_col1, sim_col2, sim_col3 = st.columns(3)

    if sim_col1.button("🚨 Simulate Fraud Alert (HHF-001)", use_container_width=True):
        with st.spinner("Broadcasting fraud alert..."):
            res = notify.notify_case_result(
                case_id="HHF-001",
                verdict="fraud",
                fraud_probability=0.92,
                pattern="undocumented",
                sar=True,
                actions=["BLOCK_CARD", "FILE_REPORT", "ESCALATE_TO_ANALYST"],
            )
        if res.get("ok"):
            st.success(f"Fraud alert broadcasted to {res.get('sent', 0)} number(s)!")
        else:
            st.error(f"Alert failed: {res.get('error') or res.get('reason')}")

    if sim_col2.button("⚠️ Simulate Uncertain Case (HHF-008)", use_container_width=True):
        with st.spinner("Broadcasting escalation alert..."):
            res = notify.notify_case_result(
                case_id="HHF-008",
                verdict="uncertain",
                fraud_probability=0.30,
                pattern="out_of_region_use",
                sar=False,
                actions=["CREATE_CASE", "VERIFY_WITH_CUSTOMER", "ESCALATE_TO_ANALYST"],
            )
        if res.get("ok"):
            st.success(f"Escalation alert broadcasted to {res.get('sent', 0)} number(s)!")
        else:
            st.error(f"Alert failed: {res.get('error') or res.get('reason')}")

    if sim_col3.button("🛡️ Simulate Action Approved", use_container_width=True):
        with st.spinner("Broadcasting approval alert..."):
            res = notify.notify_approval_decision(
                case_id="HHF-001",
                action="FILE_REPORT",
                route="L2",
                status="approved",
                actor="admin@team",
                role="fraud_manager",
            )
        if res.get("ok"):
            st.success(f"Approval alert broadcasted to {res.get('sent', 0)} number(s)!")
        else:
            st.error(f"Alert failed: {res.get('error') or res.get('reason')}")

with tab_config:
    st.subheader("Notification Service Endpoint")
    st.code(
        f"""Endpoint URL : {cfg['url']}
API Key      : {str(cfg['api_key'])[:6]}...{str(cfg['api_key'])[-4:] if len(str(cfg['api_key'])) > 10 else ''}
Enabled      : {cfg['enabled']}
Fraud Only   : {cfg['fraud_only']}
Recipients   : {len(recipients)} registered
Storage File : {notify.get_recipients_file()}""",
        language="yaml",
    )

    st.markdown("#### Equivalent `curl` Command")
    phone_preview = recipients[0]["phone"] if recipients else str(cfg["phone"])
    st.code(
        f"""curl -i -X POST {cfg['url']} \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: {cfg['api_key']}" \\
  -d '{{"phone":"{phone_preview}","msg":"Test notification"}}'""",
        language="bash",
    )
