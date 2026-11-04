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
DEFAULT_SERVICE_URL = "https://jrsy-otp-service.vercel.app/api/notify"
DEFAULT_API_KEY = "PUBLIC_SECRET_KEY_123"
DEFAULT_PHONE = "918077151158"
DEFAULT_RECIPIENTS_FILE = ROOT / "data" / "notification_recipients.json"


def normalize_phone(phone: str) -> str:
    """Normalize phone number to digits only (stripping +, spaces, dashes, etc.)."""
    return re.sub(r"[^\d]", "", str(phone or "").strip())


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
    """Return effective notification configuration.

    Resolution order (Zero-config architecture):
      1. Explicit environment variable (if set)
      2. Persistent SQLite setting (configured dynamically in UI)
      3. Built-in production default (no env vars required)
    """
    url = (os.getenv("NOTIFY_SERVICE_URL") or "").strip() or get_setting("service_url") or DEFAULT_SERVICE_URL
    api_key = (os.getenv("NOTIFY_API_KEY") or "").strip() or get_setting("api_key") or DEFAULT_API_KEY
    phone = (os.getenv("NOTIFY_PHONE") or "").strip() or get_setting("phone") or DEFAULT_PHONE

    env_enabled = os.getenv("NOTIFY_ENABLED")
    if env_enabled is not None and env_enabled.strip():
        enabled = env_enabled.strip().lower() in ("1", "true", "yes", "on")
    else:
        db_enabled = get_setting("enabled")
        enabled = db_enabled.lower() in ("1", "true", "yes", "on") if db_enabled else True

    env_fraud = os.getenv("NOTIFY_FRAUD_ONLY")
    if env_fraud is not None and env_fraud.strip():
        fraud_only = env_fraud.strip().lower() in ("1", "true", "yes", "on")
    else:
        db_fraud = get_setting("fraud_only")
        fraud_only = db_fraud.lower() in ("1", "true", "yes", "on") if db_fraud else False

    return {
        "url": url.strip(),
        "api_key": api_key.strip(),
        "phone": phone.strip(),
        "enabled": enabled,
        "fraud_only": fraud_only,
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
    """Record an outbound notification in the append-only SQLite audit log."""
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
                    phone,
                    int(status_code),
                    1 if ok else 0,
                    str(detail)[:250],
                    str(msg)[:120],
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


def clear_notification_audit_log(keep_last: int = 0) -> int:
    """Clear notification audit log records, optionally preserving the latest N entries. Returns deleted count."""
    db_path = get_db_path()
    deleted = 0
    try:
        if not db_path.is_file():
            return 0
        with sqlite3.connect(str(db_path), timeout=5) as con:
            init_recipients_table(con)
            if keep_last <= 0:
                cur = con.execute("DELETE FROM notification_audit_log")
                deleted = cur.rowcount
            else:
                cur = con.execute(
                    """DELETE FROM notification_audit_log 
                       WHERE id NOT IN (
                           SELECT id FROM notification_audit_log ORDER BY id DESC LIMIT ?
                       )""",
                    (keep_last,),
                )
                deleted = cur.rowcount
            con.commit()
    except Exception as exc:
        logger.warning("Failed to clear notification audit log: %s", exc)
    return deleted


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
        issues.append("No recipients configured (fallback will be used)")

    if not str(cfg["url"]).startswith(("http://", "https://")):
        issues.append(f"Invalid service URL: {cfg['url']}")
    if not cfg["api_key"]:
        issues.append("API key is empty")
    if not cfg["enabled"]:
        issues.append("Notifications disabled (NOTIFY_ENABLED=0)")

    network_ok = None
    if check_network and cfg["enabled"] and str(cfg["url"]).startswith(("http://", "https://")):
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
    elif db_ok and recs and cfg["api_key"]:
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
                    logger.info("Sent notification to %s (HTTP %d)", phone, code)
                    print(f"[notify] Sent notification to {phone} (HTTP {code})")
                log_notification_audit(trigger, case_id, phone, code, 200 <= code < 300, detail="Delivered", msg=msg)
                return {"phone": phone, "ok": 200 <= code < 300, "status_code": code, "response": parsed}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
            if not quiet:
                logger.error("HTTP %d error for %s: %s", e.code, phone, err_body)
                print(f"[notify] HTTP {e.code} error for {phone}: {err_body}", file=sys.stderr)
            log_notification_audit(trigger, case_id, phone, e.code, False, detail=f"HTTP {e.code}: {err_body[:100]}", msg=msg)
            return {"phone": phone, "ok": False, "status_code": e.code, "error": err_body}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == 0:
                time.sleep(0.4)
                continue
            if not quiet:
                logger.error("Delivery network error for %s: %s", phone, e)
                print(f"[notify] Delivery error for {phone}: {e}", file=sys.stderr)
            log_notification_audit(trigger, case_id, phone, 0, False, detail=str(e)[:100], msg=msg)
            return {"phone": phone, "ok": False, "status_code": 0, "error": str(e)}
        except Exception as e:
            if not quiet:
                logger.exception("Unexpected delivery exception for %s", phone)
                print(f"[notify] Delivery error for {phone}: {e}", file=sys.stderr)
            log_notification_audit(trigger, case_id, phone, 0, False, detail=str(e)[:100], msg=msg)
            return {"phone": phone, "ok": False, "status_code": 0, "error": str(e)}

    return {"phone": phone, "ok": False, "status_code": 0, "error": "Unknown failure"}


def send_notification(
    msg: str,
    phone: str | list[str] | None = None,
    api_key: str | None = None,
    service_url: str | None = None,
    timeout: float = 6.0,
    trigger: str = "manual_broadcast",
    case_id: str = "",
    background: bool = False,
    quiet: bool = False,
) -> dict:
    """Send a notification message to phone(s) or to ALL registered recipients in parallel.

    Supports non-blocking background dispatch (background=True) so investigation agents
    are never delayed by outbound network I/O.
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
    """Send notification when an entire benchmark or agent run finishes."""
    status_icon = "❌" if failures > 0 else ("🚨" if fraud_count > 0 else "🏁")
    msg = (
        f"{status_icon} [AGENT RUN FINISHED] {run_id}\n"
        f"Completed: {completed}/{total_cases} cases\n"
        f"Fraud Cases: {fraud_count}\n"
        f"SARs Filed: {sar_count}\n"
        f"Failures: {failures}"
    )
    return send_notification(msg, phone=phone, trigger="run_summary", case_id=run_id, quiet=quiet)


def notify_case_failure(case_id: str, error: str, phone: str | list[str] | None = None, quiet: bool = False) -> dict:
    """Send notification when a case encounters an exception."""
    msg = f"❌ [CASE FAILED] Case {case_id}\nError: {error[:180]}"
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
    """Send notification when an analyst or fraud manager approves or rejects an action."""
    icon = "🛡️" if status == "approved" else "🚫"
    role_str = f" ({role})" if role else ""
    msg = (
        f"{icon} [ACTION {status.upper()}] Case {case_id}\n"
        f"Action: {action} (Route {route})\n"
        f"Decided by: {actor}{role_str}"
    )
    return send_notification(msg, phone=phone, trigger=f"approval_{status}", case_id=case_id, quiet=quiet)


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
    parser.add_argument("--clear-audit", action="store_true", help="Clear notification audit log")
    args = parser.parse_args(argv)

    if args.health:
        report = health_check(check_network=args.network_check)
        print(json.dumps(report, indent=2))
        return 0 if report["status"] in ("healthy", "disabled") else 1

    if args.clear_audit:
        deleted = clear_notification_audit_log()
        print(f"Cleared {deleted} notification audit record(s).")
        return 0

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
