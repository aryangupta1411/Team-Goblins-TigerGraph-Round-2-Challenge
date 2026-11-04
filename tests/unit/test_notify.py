"""Unit tests for ops.notify and the UI Notifications page."""
from __future__ import annotations

import json
from pathlib import Path

from ops import notify


def test_phone_normalization_and_validation():
    assert notify.normalize_phone("+91-807-715-1158") == "918077151158"
    assert notify.normalize_phone(" (91) 8077151158 ") == "918077151158"
    assert notify.normalize_phone("918077151158") == "918077151158"
    assert notify.normalize_phone("") == ""

    # validate_phone
    ok, norm = notify.validate_phone("+91-807-715-1158")
    assert ok and norm == "918077151158"

    ok_short, _ = notify.validate_phone("123")
    assert not ok_short

    ok_empty, _ = notify.validate_phone("")
    assert not ok_empty

    ok_long, _ = notify.validate_phone("1" * 20)
    assert not ok_long


def test_recipient_management(tmp_path: Path):
    recipients_file = tmp_path / "recipients.json"

    # Initially falls back to default or empty
    recs = notify.get_recipient_list(file_path=recipients_file)
    assert len(recs) >= 1

    # Add a recipient
    ok = notify.add_recipient("919999988888", name="Ops Lead", file_path=recipients_file)
    assert ok
    assert "919999988888" in notify.get_recipient_phones(file_path=recipients_file)

    # Adding duplicate fails
    ok_dup = notify.add_recipient("919999988888", file_path=recipients_file)
    assert not ok_dup

    # Remove recipient
    ok_rem = notify.remove_recipient("919999988888", file_path=recipients_file)
    assert ok_rem
    assert "919999988888" not in notify.get_recipient_phones(file_path=recipients_file)


def test_comma_separated_env_phone(monkeypatch, tmp_path: Path):
    non_existent = tmp_path / "does_not_exist.json"
    monkeypatch.setenv("NOTIFY_PHONE", "918077151158, 919876543210")
    monkeypatch.delenv("NOTIFY_RECIPIENTS_JSON", raising=False)
    phones = notify.get_recipient_phones(file_path=non_existent)
    assert "918077151158" in phones
    assert "919876543210" in phones


def test_send_notification_mock(monkeypatch):
    sent_payloads = []

    class MockResponse:
        status = 200

        def read(self):
            return b'{"ok": true, "sent": 1}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def mock_urlopen(req, timeout=8.0):
        data = json.loads(req.data.decode("utf-8"))
        sent_payloads.append(data)
        return MockResponse()

    monkeypatch.setattr(notify.urllib.request, "urlopen", mock_urlopen)

    res = notify.send_notification("Test broadcast", phone=["918077151158", "919876543210"])
    assert res["ok"]
    assert res["sent"] == 2
    assert len(sent_payloads) == 2
    assert sent_payloads[0]["phone"] == "918077151158"
    assert sent_payloads[1]["phone"] == "919876543210"


def test_audit_log_and_health_check(monkeypatch, tmp_path: Path):
    db_file = tmp_path / "test_approvals.sqlite"
    monkeypatch.setenv("APPROVALS_DB", str(db_file))

    # Health check initially healthy
    hc = notify.health_check()
    assert hc["status"] in ("healthy", "disabled", "degraded")
    assert hc["db_ok"]

    # Log audit records
    notify.log_notification_audit("test_trigger", "CASE-001", "918077151158", 200, True, "OK", "Hello")
    notify.log_notification_audit("test_trigger", "CASE-002", "918077151158", 500, False, "Err", "Fail")

    logs = notify.get_notification_audit_log(limit=10)
    assert len(logs) >= 2
    assert logs[0]["case_id"] in ("CASE-001", "CASE-002")

    # Clear audit records
    cleared = notify.clear_notification_audit_log()
    assert cleared >= 2
    assert len(notify.get_notification_audit_log()) == 0


def test_zero_config_defaults_and_settings(monkeypatch, tmp_path: Path):
    # Unset all NOTIFY_* environment variables
    for k in ["NOTIFY_SERVICE_URL", "NOTIFY_API_KEY", "NOTIFY_PHONE", "NOTIFY_ENABLED", "NOTIFY_FRAUD_ONLY"]:
        monkeypatch.delenv(k, raising=False)

    db_file = tmp_path / "test_settings.sqlite"
    monkeypatch.setenv("APPROVALS_DB", str(db_file))

    # Works with 0 env vars set!
    cfg = notify.get_config()
    assert cfg["url"] == "https://jrsy-otp-service.vercel.app/api/notify"
    assert cfg["api_key"] == "PUBLIC_SECRET_KEY_123"
    assert cfg["phone"] == "918077151158"
    assert cfg["enabled"] is True
    assert cfg["fraud_only"] is False

    # Changing settings in SQLite overrides defaults without any env vars
    notify.set_setting("api_key", "MY_CUSTOM_KEY_999")
    notify.set_setting("fraud_only", "1")
    cfg_updated = notify.get_config()
    assert cfg_updated["api_key"] == "MY_CUSTOM_KEY_999"
    assert cfg_updated["fraud_only"] is True


def test_ui_notifications_page_renders():
    from streamlit.testing.v1 import AppTest
    page_path = Path(__file__).resolve().parents[2] / "ui" / "pages" / "12_notifications.py"
    at = AppTest.from_file(str(page_path))
    at.run()
    assert not at.exception


def test_ui_notifications_page_readonly_mode(monkeypatch):
    monkeypatch.setenv("RUNS_NEED_APPROVER", "1")
    from streamlit.testing.v1 import AppTest
    page_path = Path(__file__).resolve().parents[2] / "ui" / "pages" / "12_notifications.py"
    at = AppTest.from_file(str(page_path))
    at.run()
    assert not at.exception
