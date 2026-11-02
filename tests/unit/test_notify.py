"""Unit tests for ops.notify and the UI Notifications page."""
from __future__ import annotations

import json
from pathlib import Path

from ops import notify


def test_phone_normalization():
    assert notify.normalize_phone("+91-807-715-1158") == "918077151158"
    assert notify.normalize_phone(" (91) 8077151158 ") == "918077151158"
    assert notify.normalize_phone("918077151158") == "918077151158"
    assert notify.normalize_phone("") == ""


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


def test_ui_notifications_page_renders():
    from streamlit.testing.v1 import AppTest
    page_path = Path(__file__).resolve().parents[2] / "ui" / "pages" / "12_notifications.py"
    at = AppTest.from_file(str(page_path))
    at.run()
    assert not at.exception
