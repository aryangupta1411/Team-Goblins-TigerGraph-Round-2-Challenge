"""Notification helper: dispatch alerts to external notification / OTP service.

Service endpoint: POST https://jrsy-otp-service.vercel.app/api/notify
Headers:
  Content-Type: application/json
  X-API-Key: <NOTIFY_API_KEY>
Payload:
  {"phone": "<PHONE>", "msg": "<MESSAGE>"}

Can be configured via .env or environment variables:
  NOTIFY_SERVICE_URL      (default: https://jrsy-otp-service.vercel.app/api/notify)
  NOTIFY_API_KEY          (default: PUBLIC_SECRET_KEY_123)
  NOTIFY_PHONE            (default fallback: 918077151158)
  NOTIFY_ENABLED          (default: 1; set 0 to disable)
  NOTIFY_FRAUD_ONLY       (default: 0; set 1 to notify only on confirmed fraud/SAR)
  NOTIFY_RECIPIENTS_FILE  (default: data/notification_recipients.json)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import sqlite3

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVICE_URL = "https://jrsy-otp-service.vercel.app/api/notify"
DEFAULT_API_KEY = "PUBLIC_SECRET_KEY_123"
DEFAULT_PHONE = "918077151158"
DEFAULT_RECIPIENTS_FILE = ROOT / "data" / "notification_recipients.json"


def normalize_phone(phone: str) -> str:
    """Normalize phone number to digits only (stripping +, spaces, dashes, etc.)."""
    return re.sub(r"[^\d]", "", str(phone or "").strip())


def get_db_path() -> Path:
    """Path to the SQLite database. On Railway, this is typically /data/approvals.sqlite on a persistent volume."""
    p = os.getenv("APPROVALS_DB")
    if p:
        path = Path(p)
        return path if path.is_absolute() else ROOT / path
    return ROOT / "ui" / "approvals.sqlite"


def init_recipients_table(con: sqlite3.Connection) -> None:
    con.execute("""
    CREATE TABLE IF NOT EXISTS notification_recipients (
        phone    TEXT PRIMARY KEY,
        name     TEXT NOT NULL DEFAULT '',
        added_at TEXT NOT NULL
    );
    """)
    con.commit()


def _sync_to_db(recipients: list[dict]) -> None:
    """Mirror recipients into the SQLite database."""
    db_path = get_db_path()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path), timeout=5) as con:
            init_recipients_table(con)
            for r in recipients:
                con.execute(
                    "INSERT OR REPLACE INTO notification_recipients(phone, name, added_at) VALUES (?, ?, ?)",
                    (r["phone"], r.get("name", ""), r.get("added_at", "")),
                )
            con.commit()
    except Exception:
        pass


def get_recipients_file() -> Path:
    p = os.getenv("NOTIFY_RECIPIENTS_FILE")
    if p:
        path = Path(p)
        return path if path.is_absolute() else ROOT / path

    # If RUNS_DIR points to a persistent volume (common in Railway/Docker deployments)
    runs_dir = os.getenv("RUNS_DIR")
    if runs_dir and Path(runs_dir).is_dir() and Path(runs_dir).resolve() != (ROOT / "runs").resolve():
        return Path(runs_dir) / "notification_recipients.json"

    return DEFAULT_RECIPIENTS_FILE


def get_recipient_list(file_path: Path | None = None) -> list[dict]:
    """Return the list of recipient records [{'phone': ..., 'name': ..., 'added_at': ...}]."""
    # 1. First priority: SQLite table in APPROVALS_DB (survives redeploys when APPROVALS_DB is on a volume)
    if file_path is None:
        db_path = get_db_path()
        try:
            if db_path.exists():
                with sqlite3.connect(str(db_path), timeout=5) as con:
                    init_recipients_table(con)
                    rows = con.execute("SELECT phone, name, added_at FROM notification_recipients ORDER BY added_at ASC").fetchall()
                    if rows:
                        return [{"phone": r[0], "name": r[1], "added_at": r[2]} for r in rows]
        except Exception:
            pass

    # 2. Check explicit JSON in environment variable (e.g. on Railway/Vercel)
    env_json = os.getenv("NOTIFY_RECIPIENTS_JSON", "").strip()
    if env_json:
        try:
            data = json.loads(env_json)
            if isinstance(data, list):
                valid = []
                for item in data:
                    if isinstance(item, dict) and "phone" in item:
                        p = normalize_phone(item["phone"])
                        if p:
                            valid.append({
                                "phone": p,
                                "name": str(item.get("name", "")).strip() or "Environment Config",
                                "added_at": str(item.get("added_at", "")).strip() or "Env Var",
                            })
                    elif isinstance(item, str):
                        p = normalize_phone(item)
                        if p:
                            valid.append({"phone": p, "name": "Environment Config", "added_at": "Env Var"})
                if valid:
                    _sync_to_db(valid)
                    return valid
        except Exception:
            pass

    # 3. Check persistent recipients JSON file
    p = file_path or get_recipients_file()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                valid = []
                for item in data:
                    if isinstance(item, dict) and "phone" in item:
                        phone = normalize_phone(item["phone"])
                        if phone:
                            valid.append({
                                "phone": phone,
                                "name": str(item.get("name", "")).strip(),
                                "added_at": str(item.get("added_at", "")).strip(),
                            })
                    elif isinstance(item, str):
                        phone = normalize_phone(item)
                        if phone:
                            valid.append({"phone": phone, "name": "", "added_at": ""})
                if valid:
                    _sync_to_db(valid)
                    return valid
        except Exception:
            pass

    # 4. Fallback to NOTIFY_PHONE (supports single or comma-separated list!)
    raw_phones = os.getenv("NOTIFY_PHONE", DEFAULT_PHONE)
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    init_data = []
    seen = set()
    for raw in raw_phones.split(","):
        clean = normalize_phone(raw)
        if clean and clean not in seen:
            seen.add(clean)
            init_data.append({
                "phone": clean,
                "name": "Default Recipient",
                "added_at": now_str,
            })
    if init_data:
        try:
            save_recipients(init_data, file_path=p)
            _sync_to_db(init_data)
        except Exception:
            pass
        return init_data
    return []


def get_recipient_phones(file_path: Path | None = None) -> list[str]:
    """Return unique phone strings for all configured recipients."""
    return [r["phone"] for r in get_recipient_list(file_path)]


def save_recipients(recipients: list[dict], file_path: Path | None = None) -> None:
    """Persist the recipient list to JSON file (with fallback to /tmp on permission error)."""
    p = file_path or get_recipients_file()
    clean: list[dict] = []
    seen: set[str] = set()
    for r in recipients:
        phone = normalize_phone(r.get("phone", ""))
        if phone and phone not in seen:
            seen.add(phone)
            clean.append({
                "phone": phone,
                "name": str(r.get("name", "")).strip(),
                "added_at": r.get("added_at") or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

    content = json.dumps(clean, indent=2)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except (PermissionError, OSError):
        fallback = Path("/tmp/notification_recipients.json")
        fallback.write_text(content, encoding="utf-8")


def add_recipient(phone: str, name: str = "", file_path: Path | None = None) -> bool:
    """Add a new phone number to the recipient list. Returns True if added, False if duplicate or invalid."""
    phone = normalize_phone(phone)
    if not phone or len(phone) < 8:
        return False
    current = get_recipient_list(file_path)
    if any(r["phone"] == phone for r in current):
        return False
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current.append({
        "phone": phone,
        "name": name.strip(),
        "added_at": now_str,
    })
    save_recipients(current, file_path)

    # Persist directly into SQLite
    db_path = get_db_path()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path), timeout=5) as con:
            init_recipients_table(con)
            con.execute(
                "INSERT OR REPLACE INTO notification_recipients(phone, name, added_at) VALUES (?, ?, ?)",
                (phone, name.strip(), now_str),
            )
            con.commit()
    except Exception:
        pass

    return True


def remove_recipient(phone: str, file_path: Path | None = None) -> bool:
    """Remove a phone number from the recipient list. Returns True if removed."""
    phone = normalize_phone(phone)
    current = get_recipient_list(file_path)
    filtered = [r for r in current if r["phone"] != phone]
    removed = len(filtered) != len(current)
    if removed:
        save_recipients(filtered, file_path)
        # Delete from SQLite
        db_path = get_db_path()
        try:
            if db_path.exists():
                with sqlite3.connect(str(db_path), timeout=5) as con:
                    init_recipients_table(con)
                    con.execute("DELETE FROM notification_recipients WHERE phone = ?", (phone,))
                    con.commit()
        except Exception:
            pass
    return removed


def get_config() -> dict[str, str | bool]:
    return {
        "url": os.getenv("NOTIFY_SERVICE_URL", DEFAULT_SERVICE_URL).strip() or DEFAULT_SERVICE_URL,
        "api_key": os.getenv("NOTIFY_API_KEY", DEFAULT_API_KEY).strip() or DEFAULT_API_KEY,
        "phone": os.getenv("NOTIFY_PHONE", DEFAULT_PHONE).strip() or DEFAULT_PHONE,
        "enabled": os.getenv("NOTIFY_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on"),
        "fraud_only": os.getenv("NOTIFY_FRAUD_ONLY", "0").strip().lower() in ("1", "true", "yes", "on"),
    }


def is_notify_enabled() -> bool:
    return bool(get_config()["enabled"])


def send_notification(
    msg: str,
    phone: str | list[str] | None = None,
    api_key: str | None = None,
    service_url: str | None = None,
    timeout: float = 8.0,
    quiet: bool = False,
) -> dict:
    """Send a notification message to specified phone(s) or to ALL registered recipients if phone is None.

    Never raises: fails gracefully with error details in dict.
    """
    cfg = get_config()
    if not cfg["enabled"]:
        return {"ok": False, "skipped": True, "reason": "NOTIFY_ENABLED is false"}

    target_url = str(service_url or cfg["url"])
    target_key = str(api_key or cfg["api_key"])

    if phone is None:
        phones = get_recipient_phones()
        if not phones:
            phones = [str(cfg["phone"])]
    elif isinstance(phone, str):
        phones = [normalize_phone(p) for p in phone.split(",") if normalize_phone(p)]
    else:
        phones = [normalize_phone(p) for p in phone if normalize_phone(p)]

    if not phones:
        return {"ok": False, "error": "No recipient phone numbers configured"}

    results: list[dict] = []
    sent_count = 0
    failed_count = 0

    for p in phones:
        payload_data = json.dumps({"phone": p, "msg": msg}).encode("utf-8")
        req = urllib.request.Request(
            target_url,
            data=payload_data,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": target_key,
                "User-Agent": "TigerGraph-Fraud-Agent/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.status
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body)
                except Exception:
                    parsed = body
                if not quiet:
                    print(f"[notify] Sent notification to {p} (HTTP {code})")
                results.append({"phone": p, "ok": 200 <= code < 300, "status_code": code, "response": parsed})
                if 200 <= code < 300:
                    sent_count += 1
                else:
                    failed_count += 1
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
            if not quiet:
                print(f"[notify] HTTP {e.code} error for {p}: {err_body}", file=sys.stderr)
            results.append({"phone": p, "ok": False, "status_code": e.code, "error": err_body})
            failed_count += 1
        except Exception as e:
            if not quiet:
                print(f"[notify] Delivery error for {p}: {e}", file=sys.stderr)
            results.append({"phone": p, "ok": False, "status_code": 0, "error": str(e)})
            failed_count += 1

    return {
        "ok": sent_count > 0,
        "sent": sent_count,
        "failed": failed_count,
        "recipients": phones,
        "results": results,
    }


def notify_case_result(
    case_id: str,
    verdict: str,
    fraud_probability: float,
    pattern: str | None = None,
    sar: bool = False,
    actions: list[str] | None = None,
    phone: str | list[str] | None = None,
    quiet: bool = False,
) -> dict:
    """Send a formatted notification when a case is completed to all registered phones."""
    cfg = get_config()
    is_fraud = str(verdict).lower() == "fraud"
    is_uncertain = str(verdict).lower() == "uncertain"

    if cfg["fraud_only"] and not (is_fraud or sar):
        return {"ok": False, "skipped": True, "reason": "NOTIFY_FRAUD_ONLY is active and case is not fraud/SAR"}

    p_str = f"{fraud_probability:.2f}"
    act_str = ", ".join(actions) if actions else "None"
    pattern_str = pattern or "none"

    if is_fraud:
        msg = (
            f"🚨 [FRAUD DETECTED] Case {case_id}\n"
            f"Verdict: FRAUD (p={p_str})\n"
            f"Pattern: {pattern_str}\n"
            f"Actions: {act_str}\n"
            f"SAR Filed: {'YES' if sar else 'No'}"
        )
    elif is_uncertain:
        msg = (
            f"⚠️ [UNCERTAIN CASE] Case {case_id}\n"
            f"Verdict: UNCERTAIN (p={p_str})\n"
            f"Actions: {act_str}\n"
            f"Escalated to analyst for manual review."
        )
    else:
        msg = (
            f"✅ [CASE CLEARED] Case {case_id}\n"
            f"Verdict: LEGITIMATE (p={p_str})\n"
            f"Pattern: {pattern_str}\n"
            f"Actions: {act_str}"
        )

    return send_notification(msg, phone=phone, quiet=quiet)


def notify_run_summary(
    run_id: str,
    total_cases: int,
    completed: int,
    fraud_count: int,
    sar_count: int,
    failures: int,
    phone: str | list[str] | None = None,
    quiet: bool = False,
) -> dict:
    """Send notification when an entire benchmark or agent run finishes."""
    status_icon = "❌" if failures > 0 else ("🚨" if fraud_count > 0 else "🏁")
    msg = (
        f"{status_icon} [AGENT RUN FINISHED] {run_id}\n"
        f"Completed: {completed}/{total_cases} cases\n"
        f"Fraud Cases: {fraud_count}\n"
        f"SARs Filed: {sar_count}\n"
        f"Failures: {failures}"
    )
    return send_notification(msg, phone=phone, quiet=quiet)


def notify_case_failure(case_id: str, error: str, phone: str | list[str] | None = None, quiet: bool = False) -> dict:
    """Send notification when a case encounters an exception."""
    msg = f"❌ [CASE FAILED] Case {case_id}\nError: {error[:180]}"
    return send_notification(msg, phone=phone, quiet=quiet)


def notify_approval_decision(
    case_id: str,
    action: str,
    route: str,
    status: str,
    actor: str,
    role: str | None = None,
    phone: str | list[str] | None = None,
    quiet: bool = False,
) -> dict:
    """Send notification when an analyst or fraud manager approves or rejects an action."""
    icon = "🛡️" if status == "approved" else "🚫"
    role_str = f" ({role})" if role else ""
    msg = (
        f"{icon} [ACTION {status.upper()}] Case {case_id}\n"
        f"Action: {action} (Route {route})\n"
        f"Decided by: {actor}{role_str}"
    )
    return send_notification(msg, phone=phone, quiet=quiet)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send notifications to notification service")
    parser.add_argument("message", nargs="?", default="", help="Message text to send")
    parser.add_argument("--phone", default=None, help="Target phone number (or comma-separated list; default: all registered)")
    parser.add_argument("--add-recipient", default=None, help="Add a recipient phone number")
    parser.add_argument("--name", default="", help="Name for the new recipient")
    parser.add_argument("--remove-recipient", default=None, help="Remove a recipient phone number")
    parser.add_argument("--list-recipients", action="store_true", help="List registered recipient numbers")
    parser.add_argument("--case", default=None, help="Simulate case result for case ID")
    parser.add_argument("--verdict", default="fraud", choices=["fraud", "uncertain", "legitimate"])
    parser.add_argument("--prob", type=float, default=0.92)
    parser.add_argument("--sar", action="store_true")
    parser.add_argument("--test", action="store_true", help="Send a quick test message to all recipients")
    args = parser.parse_args(argv)

    if args.list_recipients:
        recs = get_recipient_list()
        print(f"Registered recipients ({len(recs)}):")
        for r in recs:
            print(f"  - {r['phone']} ({r.get('name') or 'Unnamed'}, added {r.get('added_at') or 'N/A'})")
        return 0

    if args.add_recipient:
        ok = add_recipient(args.add_recipient, args.name)
        if ok:
            print(f"Added recipient {args.add_recipient} ({args.name})")
        else:
            print(f"Failed to add {args.add_recipient}: already exists or invalid number", file=sys.stderr)
        return 0 if ok else 1

    if args.remove_recipient:
        ok = remove_recipient(args.remove_recipient)
        if ok:
            print(f"Removed recipient {args.remove_recipient}")
        else:
            print(f"Failed to remove {args.remove_recipient}: not found", file=sys.stderr)
        return 0 if ok else 1

    if args.test or (not args.message and not args.case):
        test_msg = "TigerGraph Agent notification test: Broadcast to all registered recipients successful!"
        res = send_notification(test_msg, phone=args.phone)
        print(f"Result: {res}")
        return 0 if res.get("ok") else 1

    if args.case:
        res = notify_case_result(
            case_id=args.case,
            verdict=args.verdict,
            fraud_probability=args.prob,
            pattern="undocumented" if args.verdict == "fraud" else "none",
            sar=args.sar,
            actions=["BLOCK_CARD", "FILE_REPORT"] if args.verdict == "fraud" else ["CREATE_CASE"],
            phone=args.phone,
        )
        print(f"Result: {res}")
        return 0 if res.get("ok") else 1

    res = send_notification(args.message, phone=args.phone)
    print(f"Result: {res}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
