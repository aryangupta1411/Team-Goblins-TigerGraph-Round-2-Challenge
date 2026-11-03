"""Notification helper: optional alerts to a webhook that the operator configures. OFF by default.

Nothing is sent unless ALL of these hold: NOTIFY_ENABLED is on, NOTIFY_SERVICE_URL (https) and NOTIFY_API_KEY are set in the
environment, and at least one recipient is registered. There is no built-in endpoint, key or phone number, and neither
agent.bench nor the investigation path calls this module: it runs only from the approval inbox (a decided approval) and
from the Notifications page, which needs the manage_notifications permission.

Environment (all optional; see .env.example):
  NOTIFY_ENABLED          1/true/yes/on to turn it on (default: off)
  NOTIFY_SERVICE_URL      https endpoint that accepts POST {"phone": ..., "msg": ...} (no default)
  NOTIFY_API_KEY          sent as the X-API-Key header (no default; never stored in the database)
  NOTIFY_FRAUD_ONLY       1 to notify only on a fraud verdict (default: 0)
  NOTIFY_RECIPIENTS_FILE  recipients file (default: data/notification_recipients.json, git-ignored)

What a message carries: the case id, the verdict word and the probability, or the fact that an approval was decided. Never
an action name, a pattern, a report/SAR flag, a customer or card id, or an error text. The audit log keeps the time, the
trigger, the case id, the HTTP status and the LAST THREE digits of the number; it never keeps the message.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("ops.notify")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECIPIENTS_FILE = ROOT / "data" / "notification_recipients.json"


def normalize_phone(phone: str) -> str:
    """Normalize phone number to digits only (stripping +, spaces, dashes, etc.)."""
    return re.sub(r"[^\d]", "", str(phone or "").strip())


def mask_phone(phone: str) -> str:
    """Last three digits only, for logs and screens."""
    digits = normalize_phone(phone)
    return ("•" * max(len(digits) - 3, 0) + digits[-3:]) if digits else ""


def validate_phone(phone: str) -> tuple[bool, str]:
    """Validate phone according to ITU-T E.164 (8 to 15 digits). Returns (is_valid, normalized_or_reason)."""
    norm = normalize_phone(phone)
    if not norm:
        return False, "Phone number cannot be empty"
    if len(norm) < 8:
        return False, f"Phone number {norm} is too short (minimum 8 digits with country code)"
    if len(norm) > 15:
        return False, f"Phone number {norm} is too long (maximum 15 digits according to E.164)"
    return True, norm


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
    con.execute("""
    CREATE TABLE IF NOT EXISTS notification_audit_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        at          TEXT NOT NULL,
        trigger     TEXT NOT NULL,
        case_id     TEXT NOT NULL DEFAULT '',
        phone       TEXT NOT NULL,
        status_code INTEGER NOT NULL,
        ok          INTEGER NOT NULL,
        detail      TEXT NOT NULL DEFAULT '',
        msg_snippet TEXT NOT NULL DEFAULT ''
    );
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS notification_settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    con.commit()


def get_setting(key: str, default: str = "") -> str:
    """Retrieve a persistent setting from SQLite (survives container redeployments)."""
    db_path = get_db_path()
    try:
        if db_path.is_file():
            with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=3) as con:
                cur = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='notification_settings'")
                if not cur.fetchone()[0]:
                    return default
                cur = con.execute("SELECT value FROM notification_settings WHERE key = ?", (key,))
                row = cur.fetchone()
                if row and row[0] is not None:
                    return str(row[0]).strip()
    except Exception:
        pass
    return default


def set_setting(key: str, value: str) -> None:
    """Persist a setting into SQLite database."""
    db_path = get_db_path()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path), timeout=3) as con:
            init_recipients_table(con)
            con.execute("INSERT OR REPLACE INTO notification_settings(key, value) VALUES (?, ?)", (key, str(value).strip()))
            con.commit()
    except Exception as exc:
        logger.warning("Could not persist setting '%s' to SQLite: %s", key, exc)


def _sync_to_db(recipients: list[dict]) -> None:
    """Mirror recipients into the SQLite database ONLY if the database file already exists."""
    db_path = get_db_path()
    if not db_path.is_file():
        return
    try:
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
            if db_path.is_file():
                with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5) as con:
                    cur = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='notification_recipients'")
                    if cur.fetchone()[0]:
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

    # 4. NOTIFY_PHONE (one number or a comma-separated list), if the operator set it. It is returned as is and never written
    #    to a file or the database, and there is no built-in number.
    init_data = []
    seen = set()
    for raw in os.getenv("NOTIFY_PHONE", "").split(","):
        clean = normalize_phone(raw)
        if clean and clean not in seen:
            seen.add(clean)
            init_data.append({"phone": clean, "name": "Environment Config", "added_at": "Env Var"})
    return init_data


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
    except (PermissionError, OSError) as exc:
        logger.warning("Could not save the recipients file %s: %s", p, exc)


def add_recipient(phone: str, name: str = "", file_path: Path | None = None) -> bool:
    """Add a new phone number to the recipient list. Returns True if added, False if duplicate or invalid."""
    is_valid, norm_or_err = validate_phone(phone)
    if not is_valid:
        logger.warning("Rejecting invalid recipient phone '%s': %s", phone, norm_or_err)
        return False
    clean_phone = norm_or_err
    current = get_recipient_list(file_path)
    if any(r["phone"] == clean_phone for r in current):
        return False
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current.append({
        "phone": clean_phone,
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
                (clean_phone, name.strip(), now_str),
            )
            con.commit()
    except Exception as exc:
        logger.warning("Could not mirror recipient to SQLite: %s", exc)

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
        except Exception as exc:
            logger.warning("Could not remove recipient from SQLite: %s", exc)
    return removed


def get_config() -> dict[str, str | bool]:
    """Effective notification configuration. The endpoint and key come from the environment only: a signed-in user cannot
    redirect messages or read a stored secret. `enabled` is the environment, else the toggle on the Notifications page,
    else off. `configured` is true only for an https endpoint with a key."""
    url = (os.getenv("NOTIFY_SERVICE_URL") or "").strip()
    api_key = (os.getenv("NOTIFY_API_KEY") or "").strip()

    env_enabled = os.getenv("NOTIFY_ENABLED")
    if env_enabled is not None and env_enabled.strip():
        enabled = env_enabled.strip().lower() in ("1", "true", "yes", "on")
    else:
        enabled = get_setting("enabled").lower() in ("1", "true", "yes", "on")

    env_fraud = os.getenv("NOTIFY_FRAUD_ONLY")
    if env_fraud is not None and env_fraud.strip():
        fraud_only = env_fraud.strip().lower() in ("1", "true", "yes", "on")
    else:
        fraud_only = get_setting("fraud_only").lower() in ("1", "true", "yes", "on")

    return {
        "url": url,
        "api_key": api_key,
        "enabled": enabled,
        "fraud_only": fraud_only,
        "configured": url.lower().startswith("https://") and bool(api_key),
    }


def is_notify_enabled() -> bool:
    return bool(get_config()["enabled"])



def log_notification_audit(
    trigger: str,
    case_id: str,
    phone: str,
    status_code: int,
    ok: bool,
    detail: str = "",
    msg: str = "",
) -> None:
    """Record an outbound notification in the append-only SQLite audit log: the number is masked to its last three digits
    and the message is not kept (only its length)."""
    db_path = get_db_path()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path), timeout=5) as con:
            init_recipients_table(con)
            con.execute(
                """INSERT INTO notification_audit_log(at, trigger, case_id, phone, status_code, ok, detail, msg_snippet)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S"),
                    trigger,
                    case_id,
                    mask_phone(phone),
                    int(status_code),
                    1 if ok else 0,
                    str(detail)[:250],
                    f"{len(str(msg))} chars",
                ),
            )
            con.commit()
    except Exception as exc:
        logger.debug("Could not record notification audit log: %s", exc)


def get_notification_audit_log(limit: int = 50) -> list[dict]:
    """Retrieve recent notification audit records, newest first."""
    db_path = get_db_path()
    try:
        if not db_path.is_file():
            return []
        with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5) as con:
            con.row_factory = sqlite3.Row
            cur = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='notification_audit_log'")
            if not cur.fetchone()[0]:
                return []
            cur = con.execute(
                "SELECT * FROM notification_audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        logger.debug("Could not fetch notification audit log: %s", exc)
        return []


def health_check(timeout: float = 3.0, check_network: bool = False) -> dict:
    """Evaluate operational health of the notification subsystem for production readiness."""
    cfg = get_config()
    issues: list[str] = []
    db_path = get_db_path()
    db_ok = False

    if db_path.is_file():
        try:
            with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=3.0) as con:
                cur = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='notification_recipients'")
                db_ok = bool(cur.fetchone()[0])
        except Exception as exc:
            issues.append(f"Database read error ({db_path}): {exc}")
    else:
        # DB not initialized yet - completely normal for fresh container/mock mode before first write
        db_ok = True

    recs = get_recipient_list()
    if not recs:
        issues.append("No recipients configured")

    if not cfg["configured"]:
        issues.append("Service not configured (set an https NOTIFY_SERVICE_URL and a NOTIFY_API_KEY)")
    if not cfg["enabled"]:
        issues.append("Notifications disabled (NOTIFY_ENABLED is off)")

    network_ok = None
    if check_network and cfg["enabled"] and cfg["configured"]:
        try:
            req = urllib.request.Request(
                str(cfg["url"]),
                headers={"User-Agent": "TigerGraph-Fraud-Agent/1.0"},
                method="HEAD",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                network_ok = 200 <= resp.status < 400
        except Exception as exc:
            network_ok = False
            issues.append(f"Service endpoint unreachable: {exc}")

    if not issues:
        status = "healthy"
    elif not cfg["enabled"]:
        status = "disabled"
    elif db_ok and recs and cfg["configured"]:
        status = "degraded"
    else:
        status = "unhealthy"

    return {
        "status": status,
        "enabled": cfg["enabled"],
        "url": str(cfg["url"]),
        "recipients_count": len(recs),
        "db_path": str(db_path),
        "db_ok": db_ok,
        "fraud_only": cfg["fraud_only"],
        "network_ok": network_ok,
        "issues": issues,
    }


def _send_single_recipient(
    phone: str,
    msg: str,
    target_url: str,
    target_key: str,
    timeout: float,
    trigger: str,
    case_id: str,
    quiet: bool,
) -> dict:
    """Send to a single phone with one retry on transient network errors."""
    payload_data = json.dumps({"phone": phone, "msg": msg}).encode("utf-8")
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

    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.status
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body)
                except Exception:
                    parsed = body
                if not quiet:
                    logger.info("Sent notification to %s (HTTP %d)", mask_phone(phone), code)
                    print(f"[notify] Sent notification to {mask_phone(phone)} (HTTP {code})")
                log_notification_audit(trigger, case_id, phone, code, 200 <= code < 300, detail="Delivered", msg=msg)
                return {"phone": phone, "ok": 200 <= code < 300, "status_code": code, "response": parsed}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
            if not quiet:
                logger.error("HTTP %d error for %s", e.code, mask_phone(phone))
                print(f"[notify] HTTP {e.code} error for {mask_phone(phone)}", file=sys.stderr)
            log_notification_audit(trigger, case_id, phone, e.code, False, detail=f"HTTP {e.code}", msg=msg)
            return {"phone": phone, "ok": False, "status_code": e.code, "error": err_body}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == 0:
                time.sleep(0.4)
                continue
            if not quiet:
                logger.error("Delivery network error for %s: %s", mask_phone(phone), e)
                print(f"[notify] Delivery error for {mask_phone(phone)}", file=sys.stderr)
            log_notification_audit(trigger, case_id, phone, 0, False, detail=str(e)[:100], msg=msg)
            return {"phone": phone, "ok": False, "status_code": 0, "error": str(e)}
        except Exception as e:
            if not quiet:
                logger.error("Unexpected delivery exception for %s", mask_phone(phone))
                print(f"[notify] Delivery error for {mask_phone(phone)}", file=sys.stderr)
            log_notification_audit(trigger, case_id, phone, 0, False, detail=str(e)[:100], msg=msg)
            return {"phone": phone, "ok": False, "status_code": 0, "error": str(e)}

    return {"phone": phone, "ok": False, "status_code": 0, "error": "Unknown failure"}


def send_notification(
    msg: str,
    phone: str | list[str] | None = None,
    timeout: float = 6.0,
    trigger: str = "manual_broadcast",
    case_id: str = "",
    background: bool = False,
    quiet: bool = False,
) -> dict:
    """Send a notification message to phone(s) or to ALL registered recipients in parallel.

    Nothing is sent unless notifications are enabled and configured (see the module docstring). background=True dispatches on a
    daemon thread, so the caller is never delayed by network I/O and a delivery failure never reaches it.
    """
    cfg = get_config()
    if not cfg["enabled"]:
        return {"ok": False, "skipped": True, "reason": "notifications are off (NOTIFY_ENABLED)"}
    if not cfg["configured"]:
        return {"ok": False, "skipped": True, "reason": "no https NOTIFY_SERVICE_URL and NOTIFY_API_KEY configured"}

    target_url = str(cfg["url"])
    target_key = str(cfg["api_key"])

    if phone is None:
        phones = get_recipient_phones()
    elif isinstance(phone, str):
        phones = [normalize_phone(p) for p in phone.split(",") if normalize_phone(p)]
    else:
        phones = [normalize_phone(p) for p in phone if normalize_phone(p)]

    if not phones:
        return {"ok": False, "error": "No recipient phone numbers configured"}

    # Sanitize message length (max 1000 chars)
    msg_cleaned = str(msg).strip()[:1000]

    def _execute() -> dict:
        results: list[dict] = []
        sent_count = 0
        failed_count = 0

        # Concurrent parallel delivery across all recipients
        max_workers = max(1, min(10, len(phones)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_phone = {
                executor.submit(
                    _send_single_recipient,
                    p,
                    msg_cleaned,
                    target_url,
                    target_key,
                    timeout,
                    trigger,
                    case_id,
                    quiet,
                ): p
                for p in phones
            }
            for fut in concurrent.futures.as_completed(future_to_phone):
                try:
                    res = fut.result()
                    results.append(res)
                    if res.get("ok"):
                        sent_count += 1
                    else:
                        failed_count += 1
                except Exception as exc:
                    p = future_to_phone[fut]
                    results.append({"phone": p, "ok": False, "status_code": 0, "error": str(exc)})
                    failed_count += 1

        return {
            "ok": sent_count > 0,
            "sent": sent_count,
            "failed": failed_count,
            "recipients": phones,
            "results": results,
        }

    if background:
        t = threading.Thread(target=_execute, daemon=True)
        t.start()
        return {"ok": True, "dispatched_async": True, "recipients": phones}

    return _execute()


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
    """Notify a case verdict: the case id, the verdict word and the probability. `pattern`, `sar` and `actions` are
    accepted for callers' convenience and are never put in the message."""
    cfg = get_config()
    verdict_l = str(verdict).lower()
    if cfg["fraud_only"] and verdict_l != "fraud":
        return {"ok": False, "skipped": True, "reason": "NOTIFY_FRAUD_ONLY is active and the case is not fraud"}
    p_str = f"{fraud_probability:.2f}"
    if verdict_l == "fraud":
        msg = f"[FRAUD] Case {case_id}, p={p_str}. Open the dashboard for the case file."
    elif verdict_l == "uncertain":
        msg = f"[UNCERTAIN] Case {case_id}, p={p_str}. Handed to an analyst."
    else:
        msg = f"[CLEARED] Case {case_id}, p={p_str}."
    return send_notification(msg, phone=phone, trigger="case_verdict", case_id=case_id, quiet=quiet)


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
    """Notify that a run finished: counts only (`sar_count` is accepted and not sent)."""
    msg = f"Run {run_id}: {completed}/{total_cases} cases, {fraud_count} fraud, {failures} failed."
    return send_notification(msg, phone=phone, trigger="run_summary", case_id=run_id, quiet=quiet)


def notify_case_failure(case_id: str, error: str, phone: str | list[str] | None = None, quiet: bool = False) -> dict:
    """Notify that a case failed. The error text is not sent: it can quote case data. Read it in the run log."""
    msg = f"[CASE FAILED] Case {case_id}. See the run log."
    return send_notification(msg, phone=phone, trigger="case_failure", case_id=case_id, quiet=quiet)


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
    """Notify that an approval was decided: the case id, the decision and the route. The action name and the approver are
    not sent. Dispatched in the background, so deciding an approval never waits for the network."""
    msg = f"Case {case_id}: an approval was {str(status).lower()} (route {route})."
    return send_notification(msg, phone=phone, trigger=f"approval_{status}", case_id=case_id, background=True, quiet=quiet)


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
    parser.add_argument("--health", action="store_true", help="Run health check and report status")
    parser.add_argument("--network-check", action="store_true", help="Include network reachability check in health assessment")
    args = parser.parse_args(argv)

    if args.health:
        report = health_check(check_network=args.network_check)
        print(json.dumps(report, indent=2))
        return 0 if report["status"] in ("healthy", "disabled") else 1

    if args.list_recipients:
        recs = get_recipient_list()
        print(f"Registered recipients ({len(recs)}):")
        for r in recs:
            print(f"  - {mask_phone(r['phone'])} ({r.get('name') or 'Unnamed'}, added {r.get('added_at') or 'N/A'})")
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
