"""ops.notify and the Notifications page: optional, off by default, and safe when it is on."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from ops import notify

ROOT = Path(__file__).resolve().parents[2]
FAKE_URL = "https://notify.example.invalid/api/notify"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path: Path):
    """Every test starts with no NOTIFY_* settings and its own database and recipients file."""
    for k in ("NOTIFY_SERVICE_URL", "NOTIFY_API_KEY", "NOTIFY_PHONE", "NOTIFY_ENABLED", "NOTIFY_FRAUD_ONLY", "NOTIFY_RECIPIENTS_JSON", "RUNS_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("APPROVALS_DB", str(tmp_path / "approvals.sqlite"))
    monkeypatch.setenv("NOTIFY_RECIPIENTS_FILE", str(tmp_path / "recipients.json"))


@pytest.fixture
def network(monkeypatch):
    """Records every outbound request instead of making it; the fake service always answers 200."""
    sent: list[dict] = []

    class Response:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def urlopen(req, timeout=8.0):
        sent.append({"url": req.full_url, "headers": dict(req.header_items()), "body": json.loads(req.data.decode("utf-8"))})
        return Response()

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    return sent


def enable(monkeypatch):
    monkeypatch.setenv("NOTIFY_ENABLED", "1")
    monkeypatch.setenv("NOTIFY_SERVICE_URL", FAKE_URL)
    monkeypatch.setenv("NOTIFY_API_KEY", "test-key-not-a-secret")


def test_phone_normalization_validation_and_masking():
    assert notify.normalize_phone("+91-900-000-0001") == "919000000001"
    assert notify.normalize_phone(" (91) 9000000001 ") == "919000000001"
    assert notify.normalize_phone("") == ""
    assert notify.validate_phone("+91-900-000-0001") == (True, "919000000001")
    assert not notify.validate_phone("123")[0] and not notify.validate_phone("")[0] and not notify.validate_phone("1" * 20)[0]
    assert notify.mask_phone("919000000001") == "•••••••••001" and notify.mask_phone("") == ""


def test_recipients_start_empty_and_can_be_managed(tmp_path: Path):
    f = tmp_path / "r.json"
    assert notify.get_recipient_list(file_path=f) == []                       # no built-in number
    assert not f.exists()                                                      # and nothing is written just by asking
    assert notify.add_recipient("919000000002", name="Ops Lead", file_path=f)
    assert notify.get_recipient_phones(file_path=f) == ["919000000002"]
    assert not notify.add_recipient("919000000002", file_path=f)               # a duplicate is refused
    assert notify.remove_recipient("919000000002", file_path=f)
    assert notify.get_recipient_phones(file_path=f) == []


def test_env_phone_is_returned_but_never_persisted(monkeypatch, tmp_path: Path):
    f = tmp_path / "does_not_exist.json"
    monkeypatch.setenv("NOTIFY_PHONE", "919000000003, 919000000004")
    assert notify.get_recipient_phones(file_path=f) == ["919000000003", "919000000004"]
    assert not f.exists() and not (tmp_path / "recipients.json").exists()


def test_nothing_is_sent_by_default(network):
    res = notify.send_notification("hello", phone="919000000005")
    assert res["skipped"] and not res["ok"] and network == []


def test_enabled_but_not_configured_sends_nothing(monkeypatch, network):
    monkeypatch.setenv("NOTIFY_ENABLED", "1")
    assert notify.send_notification("hello", phone="919000000005")["skipped"]
    monkeypatch.setenv("NOTIFY_SERVICE_URL", "http://insecure.example.invalid/api")   # not https
    monkeypatch.setenv("NOTIFY_API_KEY", "k")
    assert notify.send_notification("hello", phone="919000000005")["skipped"]
    monkeypatch.setenv("NOTIFY_SERVICE_URL", FAKE_URL)
    monkeypatch.delenv("NOTIFY_API_KEY")
    assert notify.send_notification("hello", phone="919000000005")["skipped"]
    assert network == []


def test_no_recipients_means_no_send_and_no_fallback_number(monkeypatch, network):
    enable(monkeypatch)
    res = notify.send_notification("hello")
    assert not res["ok"] and "recipient" in res["error"].lower() and network == []


def test_send_when_enabled_and_configured(monkeypatch, network):
    enable(monkeypatch)
    res = notify.send_notification("Test broadcast", phone=["919000000006", "919000000007"])
    assert res["ok"] and res["sent"] == 2
    assert {r["body"]["phone"] for r in network} == {"919000000006", "919000000007"}
    assert all(r["url"] == FAKE_URL and r["headers"]["X-api-key"] == "test-key-not-a-secret" for r in network)


def test_a_caller_cannot_redirect_the_message_or_supply_a_key():
    import inspect

    params = inspect.signature(notify.send_notification).parameters
    assert "service_url" not in params and "api_key" not in params


def test_the_database_cannot_hold_the_endpoint_or_key(monkeypatch):
    notify.set_setting("service_url", "https://elsewhere.example.invalid")        # an old row, or a hand edit
    notify.set_setting("api_key", "stored-secret")
    cfg = notify.get_config()
    assert cfg["url"] == "" and cfg["api_key"] == "" and cfg["configured"] is False


def test_defaults_are_off_and_unset():
    cfg = notify.get_config()
    assert cfg == {"url": "", "api_key": "", "enabled": False, "fraud_only": False, "configured": False}


def test_audit_log_masks_the_number_and_keeps_no_message(monkeypatch, network):
    enable(monkeypatch)
    notify.send_notification("a message that must not be kept", phone="919000000008", trigger="manual_broadcast", case_id="CASE-1")
    row = notify.get_notification_audit_log(limit=1)[0]
    assert row["phone"] == "•••••••••008" and "919000000008" not in json.dumps(row)
    assert "must not be kept" not in json.dumps(row) and row["msg_snippet"].endswith("chars")
    assert not hasattr(notify, "clear_notification_audit_log")


def test_messages_carry_only_id_verdict_and_probability(monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(notify, "send_notification", lambda msg, **kw: captured.append(msg) or {"ok": True})
    notify.notify_case_result("HHF-001", "fraud", 0.92, pattern="undocumented", sar=True, actions=["BLOCK_CARD", "FILE_REPORT"])
    notify.notify_case_result("HHF-008", "uncertain", 0.30, actions=["ESCALATE_TO_ANALYST"])
    notify.notify_case_result("HHF-009", "legitimate", 0.0)
    notify.notify_case_failure("HHF-005", "Traceback: card C12975-K2 txn 3660260 failed")
    notify.notify_run_summary("run-1", 18, 18, 8, 7, 0)
    notify.notify_approval_decision("HHF-001", "FILE_REPORT", "L2", "approved", "manager@demo", role="fraud_manager")
    text = "\n".join(captured)
    for banned in ("SAR", "FILE_REPORT", "BLOCK_CARD", "undocumented", "C12975", "3660260", "manager@demo", "Traceback", "report"):
        assert banned.lower() not in text.lower(), banned
    assert "HHF-001" in text and "p=0.92" in text and "approval was approved" in text


def test_fraud_only_skips_other_verdicts(monkeypatch, network):
    enable(monkeypatch)
    monkeypatch.setenv("NOTIFY_FRAUD_ONLY", "1")
    assert notify.notify_case_result("HHF-009", "legitimate", 0.0, phone="919000000009")["skipped"] and network == []
    assert notify.notify_case_result("HHF-001", "fraud", 0.92, phone="919000000009")["ok"] and len(network) == 1


def test_approval_decisions_never_wait_for_the_network(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(notify, "send_notification", lambda msg, **kw: seen.update(kw) or {"ok": True})
    notify.notify_approval_decision("HHF-001", "", "L1", "approved", "")
    assert seen["background"] is True


def test_the_agent_runner_makes_no_notification_calls():
    assert "notify" not in (ROOT / "agent" / "bench.py").read_text(encoding="utf-8")


def test_no_endpoint_key_or_number_is_committed():
    for rel in ("ops/notify.py", "ui/pages/12_notifications.py", ".env.example", "docs/RESILIENCE.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"jrsy|PUBLIC_SECRET|otp-service", text), rel
        assert not re.search(r"(?<!\d)91\d{10}(?!\d)", text), rel
    assert "data/notification_recipients.json" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert not (ROOT / "data" / "notification_recipients.json").exists() or "data/notification_recipients.json" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_only_a_fraud_manager_or_admin_manages_notifications():
    from ui import approvals_db as adb

    assert adb.roles_with("manage_notifications") == ["admin", "fraud_manager"]


def test_audit_table_is_append_only_in_the_approvals_database(tmp_path: Path):
    from ui import approvals_db as adb

    db = tmp_path / "a.sqlite"
    adb.connect(db).close()
    notify.log_notification_audit("t", "C-1", "919000000010", 200, True, "OK", "x")
    with sqlite3.connect(str(db)) as con:
        con.execute("INSERT INTO notification_audit_log(at, trigger, phone, status_code, ok) VALUES ('now', 't', 'p', 200, 1)")
        with pytest.raises(sqlite3.DatabaseError):
            con.execute("DELETE FROM notification_audit_log")


def _render_page():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "ui" / "pages" / "12_notifications.py"), default_timeout=60)
    at.run()
    return at


def test_page_renders_read_only_for_a_visitor():
    at = _render_page()
    assert not at.exception
    assert any("Read-only" in w.value for w in at.warning)
    assert all(b.disabled for b in at.button if "Send" in str(b.label) or "Test" in str(b.label))


def test_page_stays_read_only_on_a_protected_deployment(monkeypatch):
    monkeypatch.setenv("RUNS_NEED_APPROVER", "1")
    at = _render_page()
    assert not at.exception and any("Read-only" in w.value for w in at.warning)
