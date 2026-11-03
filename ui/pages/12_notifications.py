"""Notifications page: optional alerts to a webhook the operator configures. OFF by default.

Recipients registered here are notified only when notifications are enabled AND the deployment sets NOTIFY_SERVICE_URL and
NOTIFY_API_KEY (the endpoint and key are environment settings and cannot be changed from this page). Events: an approval
being decided in the inbox, and the test sends below. The agent runner never sends. Changing anything here needs the
manage_notifications permission (fraud manager or admin); everyone else sees the page read-only.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ops import notify  # noqa: E402

# Reload notify in case streamlit started before new helper functions were added
importlib.reload(notify)

from ui import approvals_db, common  # noqa: E402

ctx = common.page_setup("notifications")
common.page_header(
    "Notifications",
    "Manage recipient phone numbers, verify service health, and audit live fraud alert broadcasts.",
)


def _resolve_db_path() -> Path:
    if hasattr(notify, "get_db_path"):
        return notify.get_db_path()
    p = os.getenv("APPROVALS_DB")
    if p:
        path = Path(p)
        return path if path.is_absolute() else Path(__file__).resolve().parents[2] / path
    return Path(__file__).resolve().parents[2] / "ui" / "approvals.sqlite"


session = common.approver_session()
# fail closed on every deployment: recipients and sends are security-sensitive, so a signed-in session that holds the
# manage_notifications permission is required (not any signed-in role, and not only on "protected" deployments)
can_edit = approvals_db.has_permission(session, "manage_notifications")

if not can_edit:
    st.warning(
        "🔒 **Read-only**: adding or removing recipients and sending messages needs a signed-in fraud manager or admin "
        "(the manage_notifications permission).",
        icon=":material/lock:",
    )

cfg = notify.get_config()
recipients = notify.get_recipient_list()
enabled = notify.is_notify_enabled()
health = notify.health_check()

# --------------------------------------------------------------------------- #
# Top Status Metrics                                                          #
# --------------------------------------------------------------------------- #
db_file = _resolve_db_path()
is_volume = str(db_file).startswith("/data") or (os.getenv("RUNS_DIR") and str(os.getenv("RUNS_DIR")).startswith("/data"))
storage_name = "Railway Volume DB" if is_volume else "Persistent SQLite"

status_label = "Active (Healthy)" if (enabled and health["status"] == "healthy") else ("Active (Degraded)" if enabled else "Disabled")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Status", status_label)
m2.metric("Configured Recipients", len(recipients))
m3.metric("Storage Backend", storage_name)
m4.metric("Filter", "Fraud only" if cfg["fraud_only"] else "All verdicts")

st.info(
    f"💾 **Storage & Persistence:** Recipients added here are saved directly to `{db_file}` "
    + ("(mounted on Railway volume — 100% persistent across redeploys)." if is_volume else "and synced with local storage. On Railway, attach a volume at `/data` so updates survive container rebuilds."),
    icon=":material/storage:",
)
st.divider()

# --------------------------------------------------------------------------- #
# Tabs: Recipients, Broadcast & Test, Audit Log, Configuration                #
# --------------------------------------------------------------------------- #
tab_recipients, tab_broadcast, tab_audit, tab_config = st.tabs([
    "👥 Manage Recipients",
    "🚀 Send Broadcast & Test",
    "📜 Delivery Audit Log",
    "⚙️ Service Configuration",
])

with tab_recipients:
    st.subheader("Add New Recipient")
    st.caption("Enter phone numbers with country code, digits only (for example 91XXXXXXXXXX). Numbers stay in local state, are shown masked, and are never committed.")

    with st.form("add_recipient_form", clear_on_submit=True):
        c_num, c_name, c_btn = st.columns([2, 2, 1])
        new_phone = c_num.text_input("Phone Number", placeholder="e.g. 91XXXXXXXXXX", disabled=not can_edit)
        new_name = c_name.text_input("Name / Role (optional)", placeholder="e.g. On-call Analyst", disabled=not can_edit)
        submit_add = c_btn.form_submit_button("➕ Add Recipient", use_container_width=True, disabled=not can_edit)

        if submit_add:
            is_valid, norm_or_err = notify.validate_phone(new_phone)
            if not is_valid:
                st.error(norm_or_err)
            else:
                clean_num = norm_or_err
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
        selected_choice = r_col1.selectbox("Select recipient to remove", phone_choices, label_visibility="collapsed", disabled=not can_edit)
        if r_col2.button("🗑️ Remove Selected", use_container_width=True, disabled=not can_edit):
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
        disabled=not can_edit,
    )

    specific_number = None
    if msg_target == "Specific Number":
        specific_number = st.selectbox(
            "Select contact",
            [r["phone"] for r in recipients],
            format_func=notify.mask_phone,
            disabled=not can_edit,
        )

    custom_msg = st.text_area(
        "Message Text",
        value="TigerGraph Fraud Agent: Test alert broadcast from analyst dashboard.",
        height=100,
        disabled=not can_edit,
    )

    if st.button("🚀 Send Notification", type="primary", use_container_width=True, disabled=not can_edit):
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

    if sim_col1.button("🚨 Test: fraud verdict message", use_container_width=True, disabled=not can_edit):
        with st.spinner("Broadcasting fraud alert..."):
            res = notify.notify_case_result(
                case_id="HHF-001",
                verdict="fraud",
                fraud_probability=0.92,
            )
        if res.get("ok"):
            st.success(f"Fraud alert broadcasted to {res.get('sent', 0)} number(s)!")
        else:
            st.error(f"Alert failed: {res.get('error') or res.get('reason')}")

    if sim_col2.button("⚠️ Test: uncertain verdict message", use_container_width=True, disabled=not can_edit):
        with st.spinner("Broadcasting escalation alert..."):
            res = notify.notify_case_result(
                case_id="HHF-008",
                verdict="uncertain",
                fraud_probability=0.30,
            )
        if res.get("ok"):
            st.success(f"Escalation alert broadcasted to {res.get('sent', 0)} number(s)!")
        else:
            st.error(f"Alert failed: {res.get('error') or res.get('reason')}")

    if sim_col3.button("🛡️ Test: approval message", use_container_width=True, disabled=not can_edit):
        with st.spinner("Broadcasting approval alert..."):
            res = notify.notify_approval_decision(
                case_id="HHF-001",
                action="",
                route="L2",
                status="approved",
                actor="",
            )
        if res.get("ok"):
            st.success(f"Approval alert dispatched to {len(res.get('recipients', []))} number(s) in the background.")
        else:
            st.error(f"Alert failed: {res.get('error') or res.get('reason')}")

with tab_audit:
    st.subheader("Notification Delivery Audit Trail")
    st.caption("Immutable append-only delivery records from the SQLite database, tracking all automated and manual alert dispatches.")

    audit_logs = notify.get_notification_audit_log(limit=100)

    # Top metrics for audit logs
    total_logs = len(audit_logs)
    success_logs = sum(1 for a in audit_logs if a.get("ok"))
    failed_logs = total_logs - success_logs
    success_rate = (success_logs / total_logs * 100) if total_logs > 0 else 100.0

    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Total Logged Dispatches", total_logs)
    a2.metric("Successful Deliveries", success_logs)
    a3.metric("Failures", failed_logs)
    a4.metric("Success Rate", f"{success_rate:.1f}%")

    # Filter controls
    f_col1, f_col2, f_col3 = st.columns([2, 2, 1])
    trigger_options = ["All Triggers"] + sorted(list({a.get("trigger", "") for a in audit_logs if a.get("trigger")}))
    selected_trigger = f_col1.selectbox("Filter by Trigger", trigger_options)
    status_filter = f_col2.selectbox("Filter by Status", ["All", "Success Only", "Failures Only"])
    if f_col3.button("🔄 Refresh Logs", use_container_width=True):
        st.rerun()

    filtered_logs = audit_logs
    if selected_trigger != "All Triggers":
        filtered_logs = [a for a in filtered_logs if a.get("trigger") == selected_trigger]
    if status_filter == "Success Only":
        filtered_logs = [a for a in filtered_logs if a.get("ok")]
    elif status_filter == "Failures Only":
        filtered_logs = [a for a in filtered_logs if not a.get("ok")]

    if not filtered_logs:
        st.info("No matching notification audit records found.")
    else:
        table_rows = []
        for a in filtered_logs:
            table_rows.append({
                "Timestamp (UTC)": a.get("at", ""),
                "Trigger": a.get("trigger", ""),
                "Case / Run ID": a.get("case_id") or "—",
                "Recipient (last 3 digits)": a.get("phone", ""),
                "HTTP Status": a.get("status_code", 0),
                "Outcome": "✅ Success" if a.get("ok") else "❌ Failed",
                "Message": a.get("msg_snippet", ""),
                "Detail": a.get("detail", ""),
            })
        st.dataframe(pd.DataFrame(table_rows), hide_index=True, width="stretch")


with tab_config:
    st.subheader("Notification Service")
    st.caption("The endpoint and the key are environment settings of the deployment (NOTIFY_SERVICE_URL, NOTIFY_API_KEY). "
               "They are never stored in the database and cannot be changed or revealed here.")
    host = urlparse(str(cfg["url"])).netloc or "not set"

    st.code(
        f"""Endpoint host: {host}
Key set      : {'yes' if cfg['api_key'] else 'no'}
Configured   : {cfg['configured']}
Enabled      : {cfg['enabled']}
Fraud only   : {cfg['fraud_only']}
Recipients   : {len(recipients)} registered
Health       : {health['status'].upper()}""",
        language="yaml",
    )

    if health.get("issues"):
        for issue in health["issues"]:
            st.warning(f"⚠️ Health Notice: {issue}")

    if can_edit:
        st.markdown("#### Switches")
        st.caption("Turn sending on or off and restrict it to fraud verdicts. Sending still needs the environment settings above.")
        with st.form("notify_settings_form"):
            col_s1, col_s2 = st.columns(2)
            toggle_enabled = col_s1.toggle("Send notifications", value=bool(cfg["enabled"]))
            toggle_fraud = col_s2.toggle("Fraud verdicts only", value=bool(cfg["fraud_only"]))

            if st.form_submit_button("💾 Save switches", use_container_width=True):
                notify.set_setting("enabled", "1" if toggle_enabled else "0")
                notify.set_setting("fraud_only", "1" if toggle_fraud else "0")
                st.success("Saved.")
                st.rerun()

    with st.expander("Subsystem Diagnostic Health Report", expanded=False):
        st.json(health if can_edit else {k: v for k, v in health.items() if k not in ("url", "db_path")})
        if st.button("🌐 Test Endpoint Network Reachability", disabled=not can_edit):
            with st.spinner("Pinging endpoint..."):
                diag = notify.health_check(check_network=True)
            if diag.get("network_ok"):
                st.success("Service endpoint is reachable over HTTPS!")
            else:
                st.error("Service endpoint reachability check failed or timed out.")
            st.json(diag)
