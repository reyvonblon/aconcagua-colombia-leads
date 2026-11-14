#!/usr/bin/env python3
"""SQLite persistence for Aconcagua Agency Colombia Lead Capture, Diagnostic & CRM."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "aconcagua.db"
AGENCY_PROFILE_PATH = PROJECT_ROOT / "config" / "agency_profile.json"


def get_db_path() -> Path:
    env_path = os.environ.get("ACONCAGUA_DB_PATH")
    return Path(env_path) if env_path else DEFAULT_DB_PATH


def get_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_agency_profile() -> dict[str, Any]:
    if AGENCY_PROFILE_PATH.exists():
        return json.loads(AGENCY_PROFILE_PATH.read_text(encoding="utf-8"))
    return {"agency": {}, "services": [], "qr_routes": {}}


def init_db() -> None:
    """Create tables if missing and seed initial Aconcagua Colombia settings."""
    conn = get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                company TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL UNIQUE,
                whatsapp TEXT DEFAULT '',
                industry TEXT DEFAULT '',
                service_interest TEXT DEFAULT '',
                monthly_budget TEXT DEFAULT '',
                source TEXT NOT NULL DEFAULT 'landing',
                stage TEXT NOT NULL DEFAULT 'nuevo_lead',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS qr_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL,
                channel_ref TEXT NOT NULL DEFAULT '',
                user_agent TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outbox_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                recipient TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                scheduled_for TEXT NOT NULL,
                sent_at TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )

        profile = load_agency_profile()
        ag = profile.get("agency", {})
        defaults = {
            "agency_name": ag.get("name", "Aconcagua Agency"),
            "region_label": ag.get("region_label", "Colombia"),
            "representative_name": ag.get("representative_name", "Representante de Cuentas — Colombia"),
            "corporate_email": ag.get("corporate_email", ""),
            "whatsapp_number": ag.get("whatsapp_number", ""),
            "instagram_handle": ag.get("instagram_handle", "@aconcagua.agency"),
            "instagram_url": ag.get("instagram_url", "https://www.instagram.com/aconcagua.agency/"),
            "website": ag.get("website", "https://aconcagua.agency"),
            "max_daily_emails": str(ag.get("max_daily_emails", 25)),
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
        conn.commit()
    finally:
        conn.close()


def get_settings() -> dict[str, str]:
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}
    finally:
        conn.close()


def update_settings(updates: dict[str, Any]) -> dict[str, str]:
    init_db()
    allowed = {
        "agency_name",
        "region_label",
        "representative_name",
        "corporate_email",
        "whatsapp_number",
        "instagram_handle",
        "instagram_url",
        "website",
        "max_daily_emails",
    }
    conn = get_connection()
    try:
        for key, value in updates.items():
            if key in allowed and value is not None:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value).strip()),
                )
        conn.commit()
    finally:
        conn.close()
    return get_settings()


def _queue_email(conn: sqlite3.Connection, recipient: str, subject: str, body: str, delay_hours: int = 0) -> None:
    scheduled = (datetime.now() + timedelta(hours=delay_hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO outbox_jobs (channel, recipient, subject, body, status, scheduled_for, created_at)
        VALUES ('email', ?, ?, ?, 'pending', ?, ?)
        """,
        (recipient, subject, body, scheduled, now_iso()),
    )


def _queue_inbound_sequence(conn: sqlite3.Connection, name: str, company: str, email: str, service_interest: str) -> None:
    first_name = (name or "Hola").split()[0]
    company_txt = f" para {company}" if company else ""
    service_txt = service_interest or "Estrategia Digital y Performance Ads"

    _queue_email(
        conn,
        recipient=email,
        subject=f"{first_name}, recibimos tu solicitud en Aconcagua Agency Colombia",
        body=(
            f"Hola {first_name},\n\n"
            f"Gracias por contactar a Aconcagua Agency Colombia. Recibimos tu solicitud{company_txt} "
            f"con foco en {service_txt}.\n\n"
            "Nuestro equipo analiza negocios en Colombia y Latinoamérica combinando pauta de resultados "
            "(Meta, Google y TikTok Ads), creadores UGC, contenido y optimización web.\n\n"
            "¿Te funciona una llamada corta de 15 minutos esta semana para revisar oportunidades rápidas "
            "para tu marca? Puedes responder directo a este correo.\n\n"
            "Un saludo,\n"
            "Representante de Cuentas — Colombia\n"
            "Aconcagua Agency (@aconcagua.agency)"
        ),
        delay_hours=0,
    )
    _queue_email(
        conn,
        recipient=email,
        subject=f"Ideas rápidas de crecimiento{company_txt} — Aconcagua Agency",
        body=(
            f"Hola {first_name},\n\n"
            "Te escribo de nuevo por si aún no pudimos conversar.\n\n"
            "Podemos prepararte un mini-diagnóstico sin costo con:\n"
            "1) Oportunidades inmediatas en Meta/Google Ads o contenido con creadores UGC.\n"
            "2) Ajustes clave para convertir más visitas en clientes por WhatsApp o web.\n\n"
            "Responde este correo con tu mejor horario y lo revisamos en 15 minutos.\n\n"
            "Un saludo,\n"
            "Aconcagua Agency Colombia"
        ),
        delay_hours=72,
    )


def _queue_outbound_sequence(
    conn: sqlite3.Connection,
    name: str,
    company: str,
    email: str,
    industry: str,
    service_interest: str,
) -> None:
    first_name = (name or "").split()[0] if name else ""
    greeting = f"Hola {first_name}," if first_name else "Hola,"
    company_txt = company or "tu empresa"
    industry_txt = f" en el sector {industry}" if industry else " en Colombia"
    service_txt = service_interest or "Meta/Google Ads y contenido con creadores UGC"

    _queue_email(
        conn,
        recipient=email,
        subject=f"Idea de crecimiento para {company_txt} — Aconcagua Agency Colombia",
        body=(
            f"{greeting}\n\n"
            f"Te saludo desde el equipo de Aconcagua Agency en Colombia (@aconcagua.agency). "
            f"Estuvimos revisando marcas{industry_txt} y vimos potencial claro para escalar ventas "
            f"de {company_txt} con {service_txt}.\n\n"
            "Trabajamos con marcas en Latinoamérica combinando campañas de performance medibles, "
            "creadores de contenido (UGC) y optimización de conversión.\n\n"
            "¿Te gustaría que te enviemos un diagnóstico corto de 3 puntos sin compromiso para "
            f"{company_txt}? Solo respóndeme 'Sí' a este correo.\n\n"
            "Un saludo,\n"
            "Representante de Cuentas — Colombia\n"
            "Aconcagua Agency"
        ),
        delay_hours=0,
    )
    _queue_email(
        conn,
        recipient=email,
        subject=f"Re: Idea de crecimiento para {company_txt}",
        body=(
            f"{greeting}\n\n"
            f"Te comparto un breve seguimiento sobre {company_txt}. Muchos negocios{industry_txt} "
            "pierden presupuesto en anuncios sin una estructura clara de creativos UGC y embudo a WhatsApp.\n\n"
            "Si te sirve, agendamos 15 minutos esta semana y te muestro cómo lo estructuramos en Aconcagua.\n\n"
            "Un saludo,\n"
            "Aconcagua Agency Colombia"
        ),
        delay_hours=72,
    )
    _queue_email(
        conn,
        recipient=email,
        subject=f"Cierro seguimiento por ahora — {company_txt}",
        body=(
            f"{greeting}\n\n"
            "No quiero saturar tu bandeja, así que este es mi último mensaje por ahora.\n"
            f"Cuando {company_txt} quiera escalar adquisición de clientes en Colombia con pauta o creadores, "
            "aquí tienes mi contacto directo.\n\n"
            "Mucho éxito,\n"
            "Representante de Cuentas — Aconcagua Agency Colombia"
        ),
        delay_hours=168,
    )


def upsert_lead(
    name: str,
    email: str,
    company: str = "",
    whatsapp: str = "",
    industry: str = "",
    service_interest: str = "",
    monthly_budget: str = "",
    source: str = "landing",
    stage: str = "nuevo_lead",
    notes: str = "",
    queue_type: str = "inbound",
) -> dict[str, Any]:
    init_db()
    clean_email = email.strip().lower()
    clean_name = name.strip() or (company.strip() or clean_email.split("@")[0])
    if not clean_email or "@" not in clean_email:
        raise ValueError("Correo electrónico válido obligatorio.")

    ts = now_iso()
    conn = get_connection()
    try:
        existing = conn.execute("SELECT id FROM leads WHERE email = ?", (clean_email,)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE leads
                SET name = COALESCE(NULLIF(?, ''), name),
                    company = COALESCE(NULLIF(?, ''), company),
                    whatsapp = COALESCE(NULLIF(?, ''), whatsapp),
                    industry = COALESCE(NULLIF(?, ''), industry),
                    service_interest = COALESCE(NULLIF(?, ''), service_interest),
                    monthly_budget = COALESCE(NULLIF(?, ''), monthly_budget),
                    notes = CASE WHEN ? != '' THEN ? ELSE notes END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_name,
                    company.strip(),
                    whatsapp.strip(),
                    industry.strip(),
                    service_interest.strip(),
                    monthly_budget.strip(),
                    notes.strip(),
                    notes.strip(),
                    ts,
                    existing["id"],
                ),
            )
            lead_id = existing["id"]
        else:
            cur = conn.execute(
                """
                INSERT INTO leads (
                    name, company, email, whatsapp, industry, service_interest,
                    monthly_budget, source, stage, notes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_name,
                    company.strip(),
                    clean_email,
                    whatsapp.strip(),
                    industry.strip(),
                    service_interest.strip(),
                    monthly_budget.strip(),
                    source.strip() or "landing",
                    stage.strip() or "nuevo_lead",
                    notes.strip(),
                    ts,
                    ts,
                ),
            )
            lead_id = cur.lastrowid
            if queue_type == "outbound":
                _queue_outbound_sequence(
                    conn,
                    clean_name,
                    company.strip(),
                    clean_email,
                    industry.strip(),
                    service_interest.strip(),
                )
            elif queue_type == "inbound":
                _queue_inbound_sequence(
                    conn,
                    clean_name,
                    company.strip(),
                    clean_email,
                    service_interest.strip(),
                )
        conn.commit()
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def evaluate_diagnostic(payload: dict[str, Any]) -> dict[str, Any]:
    """Recommend the best Aconcagua service roadmap based on client answers."""
    goal = (payload.get("goal") or "").strip().lower()
    channel = (payload.get("current_channel") or "").strip().lower()
    budget = (payload.get("monthly_budget") or "").strip()
    challenge = (payload.get("challenge") or "").strip().lower()

    recommendations: list[str] = []
    if "venta" in goal or "roas" in goal or "ads" in challenge or "leads" in goal:
        recommendations.append("Performance & Paid Media (Meta Ads, Google Ads y TikTok Ads)")
    if "contenido" in challenge or "marca" in goal or "ugc" in goal or "instagram" in channel:
        recommendations.append("Influencer & UGC Marketing + Social Media")
    if "web" in challenge or "conversion" in challenge or "seo" in goal or "google" in goal:
        recommendations.append("SEO / AI Search (GEO) & Optimización Web de Conversión")

    if not recommendations:
        recommendations = [
            "Performance & Paid Media (Meta Ads y Google Ads)",
            "Influencer & UGC Marketing para acelerar confianza",
        ]

    priority = recommendations[0]
    summary = (
        f"Plan recomendado para tu etapa ({budget or 'por definir'}): priorizar {priority} "
        f"con medición semanal de costo por lead y retorno sobre inversión."
    )
    return {
        "recommended_primary": priority,
        "recommended_services": recommendations,
        "summary": summary,
    }


def import_prospects_csv(csv_text: str) -> dict[str, Any]:
    """Parse a B2B prospect CSV and queue outbound corporate email sequences."""
    init_db()
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    if not reader.fieldnames:
        raise ValueError("El CSV debe incluir encabezados (ej: nombre,empresa,correo,whatsapp,industria,servicio).")

    imported = 0
    skipped = 0
    errors: list[str] = []

    for idx, row in enumerate(reader, start=2):
        norm = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k is not None}
        email = norm.get("correo") or norm.get("email") or ""
        name = norm.get("nombre") or norm.get("name") or norm.get("contacto") or ""
        company = norm.get("empresa") or norm.get("company") or norm.get("marca") or ""
        whatsapp = norm.get("whatsapp") or norm.get("telefono") or norm.get("phone") or ""
        industry = norm.get("industria") or norm.get("sector") or norm.get("nicho") or ""
        service = norm.get("servicio") or norm.get("service") or "Performance Ads y Contenido UGC"

        if not email or "@" not in email:
            skipped += 1
            errors.append(f"Fila {idx}: correo inválido o vacío")
            continue

        upsert_lead(
            name=name or company or email.split("@")[0],
            company=company,
            email=email,
            whatsapp=whatsapp,
            industry=industry,
            service_interest=service,
            source="prospeccion_csv",
            stage="nuevo_lead",
            notes="Importado desde lista CSV de prospección Colombia",
            queue_type="outbound",
        )
        imported += 1

    return {"imported": imported, "skipped": skipped, "errors": errors[:10]}


def list_leads(search: str = "", stage: str = "") -> list[dict[str, Any]]:
    init_db()
    conn = get_connection()
    try:
        sql = "SELECT * FROM leads WHERE 1=1"
        params: list[Any] = []
        if search.strip():
            like = f"%{search.strip()}%"
            sql += " AND (name LIKE ? OR company LIKE ? OR email LIKE ? OR industry LIKE ? OR whatsapp LIKE ?)"
            params.extend([like, like, like, like, like])
        if stage.strip():
            sql += " AND stage = ?"
            params.append(stage.strip())
        sql += " ORDER BY updated_at DESC"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def update_lead_stage(lead_id: int, stage: str, notes: str | None = None) -> dict[str, Any] | None:
    init_db()
    conn = get_connection()
    try:
        if notes is None:
            conn.execute(
                "UPDATE leads SET stage = ?, updated_at = ? WHERE id = ?",
                (stage, now_iso(), lead_id),
            )
        else:
            conn.execute(
                "UPDATE leads SET stage = ?, notes = ?, updated_at = ? WHERE id = ?",
                (stage, notes, now_iso(), lead_id),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_qr_context(slug: str) -> dict[str, Any]:
    profile = load_agency_profile()
    routes = profile.get("qr_routes", {})
    clean = (slug or "ig").strip().lower()
    info = routes.get(clean)
    if not info:
        info = {
            "title": f"Contacto Directo Aconcagua ({clean})",
            "channel_ref": f"Enlace {clean}",
            "cta_label": "Hablar por WhatsApp con Colombia",
        }
    return {"slug": clean, **info}


def log_qr_scan(slug: str, channel_ref: str = "", user_agent: str = "") -> None:
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO qr_events (slug, channel_ref, user_agent, created_at) VALUES (?, ?, ?, ?)",
            (slug, channel_ref, user_agent[:240], now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def list_outbox_jobs(status: str = "", limit: int = 80) -> list[dict[str, Any]]:
    init_db()
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM outbox_jobs WHERE status = ? ORDER BY scheduled_for ASC, id ASC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM outbox_jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_outbox_job(job_id: int, status: str, error: str = "") -> None:
    init_db()
    conn = get_connection()
    try:
        sent_at = now_iso() if status == "sent" else ""
        conn.execute(
            "UPDATE outbox_jobs SET status = ?, sent_at = ?, error = ? WHERE id = ?",
            (status, sent_at, error[:500], job_id),
        )
        conn.commit()
    finally:
        conn.close()


def count_sent_emails_today() -> int:
    init_db()
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM outbox_jobs WHERE channel = 'email' AND status = 'sent' AND sent_at LIKE ?",
            (f"{today_prefix}%",),
        ).fetchone()
        return int(row["c"])
    finally:
        conn.close()


def get_dashboard_metrics() -> dict[str, Any]:
    init_db()
    conn = get_connection()
    try:
        total_leads = conn.execute("SELECT COUNT(*) AS c FROM leads").fetchone()["c"]
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        leads_today = conn.execute(
            "SELECT COUNT(*) AS c FROM leads WHERE created_at LIKE ?",
            (f"{today_prefix}%",),
        ).fetchone()["c"]
        qualified_count = conn.execute(
            "SELECT COUNT(*) AS c FROM leads WHERE stage IN ('diagnostico_enviado', 'reunion_agendada', 'propuesta', 'cerrado_ganado')"
        ).fetchone()["c"]
        meetings_count = conn.execute(
            "SELECT COUNT(*) AS c FROM leads WHERE stage IN ('reunion_agendada', 'propuesta', 'cerrado_ganado')"
        ).fetchone()["c"]
        qr_scans = conn.execute("SELECT COUNT(*) AS c FROM qr_events").fetchone()["c"]
        qr_by_slug = [
            dict(r)
            for r in conn.execute(
                "SELECT slug, COUNT(*) AS scans FROM qr_events GROUP BY slug ORDER BY scans DESC"
            ).fetchall()
        ]
        pending_emails = conn.execute(
            "SELECT COUNT(*) AS c FROM outbox_jobs WHERE channel = 'email' AND status = 'pending'"
        ).fetchone()["c"]
        sent_today = count_sent_emails_today()

        return {
            "total_leads": total_leads,
            "leads_today": leads_today,
            "qualified_count": qualified_count,
            "meetings_count": meetings_count,
            "qr_scans": qr_scans,
            "qr_by_slug": qr_by_slug,
            "pending_emails": pending_emails,
            "sent_today": sent_today,
        }
    finally:
        conn.close()
