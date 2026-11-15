#!/usr/bin/env python3
"""Automated Corporate Email & Lead Webhook Dispatcher for Aconcagua Colombia."""

from __future__ import annotations

import argparse
import email
import html
import imaplib
import json
import os
import re
import smtplib
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

_DISPATCH_LOCK = threading.Lock()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

if str(PROJECT_ROOT / "webapp") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "webapp"))

import database  # noqa: E402


def load_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if not clean or clean.startswith("#") or "=" not in clean:
                continue
            k, v = clean.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    for k in (
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASS",
        "SMTP_FROM",
        "SMTP_REPLY_TO",
        "MAX_DAILY_EMAILS",
        "LEAD_WEBHOOK_URL",
    ):
        if os.environ.get(k):
            values[k] = os.environ[k]
    return values


def get_daily_limit(cfg: dict[str, str]) -> int:
    raw = cfg.get("MAX_DAILY_EMAILS")
    if not raw:
        settings = database.get_settings()
        raw = settings.get("max_daily_emails", "25")
    try:
        val = int(raw)
        return max(1, val)
    except ValueError:
        return 25


def get_autopilot_status() -> dict[str, object]:
    cfg = load_env_file()
    smtp_ready = bool(cfg.get("SMTP_HOST") and cfg.get("SMTP_USER") and cfg.get("SMTP_PASS"))
    webhook_ready = bool(cfg.get("LEAD_WEBHOOK_URL"))
    return {
        "smtp_ready": smtp_ready,
        "lead_webhook_ready": webhook_ready,
        "max_daily_emails": get_daily_limit(cfg),
        "sent_today": database.count_sent_emails_today(),
    }


def send_smtp_email(cfg: dict[str, str], to_addr: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = cfg.get("SMTP_FROM") or cfg["SMTP_USER"]
    if cfg.get("SMTP_REPLY_TO"):
        msg["Reply-To"] = cfg["SMTP_REPLY_TO"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    port = int(cfg.get("SMTP_PORT") or "587")
    host = cfg["SMTP_HOST"]
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
            smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
            smtp.send_message(msg)


def send_lead_webhook(webhook_url: str, subject: str, body: str) -> None:
    payload = json.dumps(
        {
            "agency": "Aconcagua Agency Colombia",
            "title": subject,
            "details": body,
            "timestamp": datetime.now().isoformat(),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"Webhook HTTP {resp.status}")


def dispatch_due_jobs() -> dict:
    with _DISPATCH_LOCK:
        database.init_db()
        cfg = load_env_file()
        status = get_autopilot_status()
        daily_limit = int(status["max_daily_emails"])
        sent_today = database.count_sent_emails_today()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pending = database.list_outbox_jobs(status="pending", limit=200)

        sent_count = 0
        skipped_no_creds = 0
        skipped_daily_cap = 0
        failed_count = 0
        future_count = 0

        for job in pending:
            if job["scheduled_for"] > now_str:
                future_count += 1
                continue
            ch = job["channel"]
            if ch == "email":
                if not status["smtp_ready"]:
                    skipped_no_creds += 1
                    continue
                if sent_today >= daily_limit:
                    skipped_daily_cap += 1
                    continue
                try:
                    send_smtp_email(cfg, job["recipient"], job["subject"], job["body"])
                    database.update_outbox_job(job["id"], "sent")
                    sent_count += 1
                    sent_today += 1
                except Exception as exc:
                    database.update_outbox_job(job["id"], "failed", str(exc))
                    failed_count += 1
            elif ch == "lead_webhook":
                if not status["lead_webhook_ready"]:
                    skipped_no_creds += 1
                    continue
                try:
                    send_lead_webhook(cfg["LEAD_WEBHOOK_URL"], job["subject"], job["body"])
                    database.update_outbox_job(job["id"], "sent")
                    sent_count += 1
                except Exception as exc:
                    database.update_outbox_job(job["id"], "failed", str(exc))
                    failed_count += 1

        status["sent_today"] = sent_today
        return {
            "config_status": status,
            "sent": sent_count,
            "skipped_missing_credentials": skipped_no_creds,
            "skipped_daily_cap": skipped_daily_cap,
            "failed": failed_count,
            "scheduled_for_later": future_count,
        }


def _decode_mime_header(raw_val: str) -> str:
    if not raw_val:
        return ""
    parts = decode_header(raw_val)
    decoded: list[str] = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(str(chunk))
    return "".join(decoded).strip()


def _html_to_text(raw_html: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw_html)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _strip_quoted_reply(text: str) -> str:
    """Remove quoted original email blocks so our own subject/body words don't trigger false positives."""
    lines: list[str] = []
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith(">"):
            continue
        if re.match(r"^(el|on)\s+.+(escribió|wrote):$", low):
            break
        if low.startswith("-----original message-----") or low.startswith("-----mensaje original-----"):
            break
        lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _extract_text_snippet(msg: email.message.Message) -> str:
    plain_text = ""
    html_text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if ctype == "text/plain" and not plain_text:
                plain_text = decoded
            elif ctype == "text/html" and not html_text:
                html_text = _html_to_text(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_text = _html_to_text(decoded)
            else:
                plain_text = decoded
    raw = plain_text or html_text
    return _strip_quoted_reply(raw)[:400]


def notify_mac_desktop(title: str, message: str) -> bool:
    """Show a native macOS notification with sound when an interested lead is detected."""
    if sys.platform != "darwin" or os.environ.get("ACONCAGUA_QUIET") == "1":
        return False
    safe_title = title.replace('"', '\\"')[:80]
    safe_msg = message.replace('"', '\\"')[:180]
    script = (
        f'display notification "{safe_msg}" '
        f'with title "Aconcagua Colombia" subtitle "{safe_title}" sound name "Glass"'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
        return True
    except Exception:
        return False


def classify_reply_intent(subject: str, body: str, headers: dict[str, str] | None = None) -> str:
    """Classify an inbound lead reply as respuesta_automatica, interesado_cita, no_interesado, or respuesta_recibida."""
    hdrs = {k.lower(): (v or "").lower() for k, v in (headers or {}).items()}
    if (
        (hdrs.get("auto-submitted") and hdrs.get("auto-submitted") != "no")
        or hdrs.get("x-auto-response-suppress")
        or hdrs.get("precedence") in ("bulk", "auto_reply", "junk")
        or hdrs.get("x-zendesk-ticket-id")
        or hdrs.get("x-freshdesk-ticket-id")
    ):
        return "respuesta_automatica"

    clean_body = _strip_quoted_reply(body).lower()
    clean_subj = re.sub(r"^(re|fwd|rv):\s*", "", (subject or "").strip(), flags=re.IGNORECASE).lower()
    combined = re.sub(r"\s+", " ", f"{clean_subj} {clean_body}").strip()

    auto_reply_kw = (
        "ticket",
        "solicitud #",
        "caso #",
        "número de caso",
        "numero de caso",
        "número de ticket",
        "numero de ticket",
        "hemos recibido tu",
        "hemos recibido su",
        "recibimos tu mensaje",
        "recibimos tu correo",
        "recibimos tu solicitud",
        "respuesta automática",
        "respuesta automatica",
        "auto-reply",
        "autoreply",
        "automatic reply",
        "out of office",
        "fuera de oficina",
        "nuestro equipo se pondrá en contacto",
        "nuestro equipo se pondra en contacto",
        "nuestro equipo te responderá",
        "nuestro equipo te respondera",
        "gracias por contactarnos",
        "gracias por escribirnos",
        "gracias por comunicarte",
        "your request has been received",
        "we have received your",
        "request received",
        "no-reply",
        "noreply",
        "do not reply",
    )
    if any(kw in combined for kw in auto_reply_kw):
        return "respuesta_automatica"

    negative_kw = (
        "no gracias",
        "no estamos interesados",
        "no nos interesa",
        "no interesa",
        "dar de baja",
        "remover",
        "unsubscribe",
        "dejar de escribir",
        "no me escriban",
    )
    if any(kw in clean_body for kw in negative_kw):
        return "no_interesado"

    positive_kw = (
        "cita",
        "reunión",
        "reunion",
        "agendar",
        "llamada",
        "auditoría",
        "auditoria",
        "disponibilidad",
        "cuándo",
        "cuando",
        "hora",
        "interesado",
        "interesada",
        "interesa",
        "con gusto",
        "cotización",
        "cotizacion",
        "propuesta",
        "hablemos",
        "conversemos",
        "zoom",
        "meet",
        "calendario",
        "whatsapp",
        "teléfono",
        "telefono",
        "sí claro",
        "si claro",
    )
    # Match positive keywords ONLY on the reply body (never on echoed email subject!)
    if any(kw in clean_body for kw in positive_kw):
        return "interesado_cita"
    return "respuesta_recibida"


def sync_imap_replies(max_messages: int = 40) -> dict[str, object]:
    """Scan Gmail/IMAP inbox for replies from tracked leads and flag interested leads for direct contact."""
    with _DISPATCH_LOCK:
        database.init_db()
        cfg = load_env_file()
        user = cfg.get("SMTP_USER", "")
        password = cfg.get("SMTP_PASS", "")
        imap_host = cfg.get("IMAP_HOST") or ("imap.gmail.com" if "gmail" in (cfg.get("SMTP_HOST") or "").lower() else "")
        if not (user and password and imap_host):
            return {"imap_ready": False, "checked": 0, "matched": 0, "interested_citas": []}

        matched = 0
        interested_citas: list[dict[str, str]] = []
        checked = 0
        try:
            with imaplib.IMAP4_SSL(imap_host, 993, timeout=15) as imap:
                imap.login(user, password)
                imap.select("INBOX")
                status_code, data = imap.search(None, "ALL")
                if status_code != "OK" or not data or not data[0]:
                    return {"imap_ready": True, "checked": 0, "matched": 0, "interested_citas": []}
                msg_ids = data[0].split()[-max_messages:]
                for msg_num in reversed(msg_ids):
                    checked += 1
                    st, msg_data = imap.fetch(msg_num, "(RFC822)")
                    if st != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                        continue
                    msg = email.message_from_bytes(msg_data[0][1])
                    msg_id = (msg.get("Message-ID") or "").strip() or f"imap-{msg_num.decode(errors='ignore')}"
                    if database.has_processed_reply(msg_id):
                        continue
                    _, sender_email = parseaddr(msg.get("From") or "")
                    sender_email = sender_email.strip().lower()
                    if not sender_email or sender_email == user.lower():
                        continue
                    lead = database.get_lead_by_email(sender_email)
                    if not lead:
                        continue

                    subj = _decode_mime_header(msg.get("Subject") or "")
                    snippet = _extract_text_snippet(msg)
                    hdrs = {
                        "Auto-Submitted": msg.get("Auto-Submitted") or "",
                        "X-Auto-Response-Suppress": msg.get("X-Auto-Response-Suppress") or "",
                        "Precedence": msg.get("Precedence") or "",
                        "X-Zendesk-Ticket-Id": msg.get("X-Zendesk-Ticket-Id") or "",
                        "X-Freshdesk-Ticket-Id": msg.get("X-Freshdesk-Ticket-Id") or "",
                    }
                    intent = classify_reply_intent(subj, snippet, headers=hdrs)
                    database.record_processed_reply(msg_id, sender_email, intent, snippet)
                    matched += 1

                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                    prev_notes = (lead.get("notes") or "").strip()
                    new_note = f"[Respuesta {stamp} | {intent}] Asunto: {subj} — {snippet[:220]}"
                    merged_notes = f"{new_note}\n{prev_notes}".strip() if prev_notes else new_note
                    if intent in ("interesado_cita", "no_interesado", "respuesta_automatica"):
                        next_stage = intent
                    else:
                        next_stage = lead["stage"]
                    database.update_lead_stage(lead["id"], next_stage, notes=merged_notes)
                    if intent != "respuesta_automatica":
                        database.cancel_pending_outbox_for_recipient(sender_email)

                    if intent == "interesado_cita":
                        interested_citas.append(
                            {
                                "name": lead["name"],
                                "company": lead["company"],
                                "email": sender_email,
                                "snippet": snippet[:180],
                            }
                        )
                        notify_mac_desktop(
                            f"Cita interesada: {lead['company'] or lead['name']}",
                            f"{sender_email} respondió interesado en agendar cita.",
                        )
                        notify_to = cfg.get("SMTP_REPLY_TO") or ""
                        if notify_to and cfg.get("SMTP_HOST"):
                            try:
                                send_smtp_email(
                                    cfg,
                                    notify_to,
                                    f"Lead listo para agendar cita: {lead['company'] or lead['name']}",
                                    (
                                        f"Hola Raymond,\n\n"
                                        f"El siguiente lead respondió interesado en agendar cita y ya quedó "
                                        f"separado en tu lista 'Leads para Agendar Cita':\n\n"
                                        f"- Nombre: {lead['name']}\n"
                                        f"- Empresa: {lead['company'] or 'N/A'}\n"
                                        f"- Correo: {sender_email}\n"
                                        f"- WhatsApp: {lead['whatsapp'] or 'No registrado'}\n"
                                        f"- Respuesta: {snippet[:300]}\n\n"
                                        "Puedes contactarlo directamente desde tu correo corporativo.\n"
                                    ),
                                )
                            except Exception:
                                pass
        except Exception as exc:
            return {
                "imap_ready": True,
                "error": str(exc),
                "checked": checked,
                "matched": matched,
                "interested_citas": interested_citas,
            }

        return {
            "imap_ready": True,
            "checked": checked,
            "matched": matched,
            "interested_citas": interested_citas,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch due Aconcagua Colombia corporate emails.")
    parser.add_argument("--status-only", action="store_true", help="Only inspect credentials and pending count.")
    parser.add_argument("--sync-replies", action="store_true", help="Scan IMAP inbox for interested replies.")
    args = parser.parse_args()

    database.init_db()
    if args.status_only:
        print(
            json.dumps(
                {
                    "config_status": get_autopilot_status(),
                    "pending_jobs": len(database.list_outbox_jobs(status="pending")),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.sync_replies:
        print(json.dumps(sync_imap_replies(), ensure_ascii=False, indent=2))
        return
    print(json.dumps(dispatch_due_jobs(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
