#!/usr/bin/env python3
"""24/7 HTTP Server for Aconcagua Agency Colombia Lead Capture, Diagnostic & CRM."""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import os
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
STATIC_DIR = Path(__file__).resolve().parent / "static"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import autopilot_dispatcher  # noqa: E402
import database  # noqa: E402


def build_whatsapp_url(raw_number: str, message: str) -> str:
    digits = re.sub(r"[^\d]", "", raw_number or "")
    encoded = quote(message)
    if digits:
        return f"https://wa.me/{digits}?text={encoded}"
    return f"https://wa.me/?text={encoded}"


class AconcaguaHandler(BaseHTTPRequestHandler):
    server_version = "AconcaguaColombiaStudio/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("ACONCAGUA_QUIET") == "1":
            return
        super().log_message(format, *args)

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str, filename: str | None = None) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _serve_static_file(self, relative_path: str) -> None:
        clean_rel = relative_path.lstrip("/")
        if ".." in Path(clean_rel).parts:
            self._send_json({"error": "Ruta no permitida"}, status=HTTPStatus.NOT_FOUND)
            return
        target = (STATIC_DIR / clean_rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._send_json({"error": "Archivo no encontrado"}, status=HTTPStatus.NOT_FOUND)
            return
        ctype, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        elif target.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif target.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        self._send_bytes(target.read_bytes(), ctype or "application/octet-stream")

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            self._serve_static_file("index.html")
            return
        if path in ("/diagnostico", "/diagnostico/"):
            self._serve_static_file("diagnostico.html")
            return
        if path in ("/admin", "/admin/"):
            self._serve_static_file("admin.html")
            return
        if path.startswith("/qr/"):
            slug = path[len("/qr/"):].strip("/") or "ig"
            ctx = database.get_qr_context(slug)
            database.log_qr_scan(
                slug=slug,
                channel_ref=ctx.get("channel_ref", ""),
                user_agent=self.headers.get("User-Agent", ""),
            )
            self._serve_static_file("qr.html")
            return
        if path.startswith("/static/"):
            self._serve_static_file(path[len("/static/"):])
            return

        if path == "/api/health":
            self._send_json({"status": "ok", "service": "Aconcagua Agency Colombia"})
            return

        if path == "/api/config":
            settings = database.get_settings()
            profile = database.load_agency_profile()
            self._send_json(
                {
                    "settings": settings,
                    "services": profile.get("services", []),
                    "whatsapp_direct_url": build_whatsapp_url(
                        settings.get("whatsapp_number", ""),
                        "Hola, quiero información sobre los servicios de Aconcagua Agency en Colombia.",
                    ),
                }
            )
            return

        if path.startswith("/api/qr/"):
            slug = path[len("/api/qr/"):].strip("/") or "ig"
            ctx = database.get_qr_context(slug)
            settings = database.get_settings()
            msg = (
                f"Hola! Vengo desde '{ctx['channel_ref']}' de Aconcagua Agency Colombia "
                "y quiero solicitar un diagnóstico para mi marca."
            )
            self._send_json(
                {
                    "qr": ctx,
                    "whatsapp_url": build_whatsapp_url(settings.get("whatsapp_number", ""), msg),
                    "settings": settings,
                }
            )
            return

        if path == "/api/dashboard":
            metrics = database.get_dashboard_metrics()
            settings = database.get_settings()
            self._send_json(
                {
                    "metrics": metrics,
                    "settings": settings,
                    "autopilot": autopilot_dispatcher.get_autopilot_status(),
                }
            )
            return

        if path == "/api/leads":
            search = qs.get("search", [""])[0]
            stage = qs.get("stage", [""])[0]
            leads = database.list_leads(search=search, stage=stage)
            settings = database.get_settings()
            wa_num = settings.get("whatsapp_number", "")
            for item in leads:
                target_num = item.get("whatsapp") or ""
                item["whatsapp_chat_url"] = (
                    build_whatsapp_url(
                        target_num,
                        f"Hola {item['name']}, te saludo desde Aconcagua Agency Colombia (@aconcagua.agency). "
                        f"¿Cómo vas con el crecimiento digital de {item['company'] or 'tu marca'}?",
                    )
                    if target_num
                    else ""
                )
                item["rep_whatsapp_configured"] = bool(re.sub(r"[^\d]", "", wa_num))
            self._send_json({"leads": leads})
            return

        if path == "/api/leads/export.csv":
            leads = database.list_leads()
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(
                [
                    "ID",
                    "Nombre",
                    "Empresa",
                    "Correo",
                    "WhatsApp",
                    "Industria",
                    "Servicio",
                    "Presupuesto",
                    "Origen",
                    "Etapa",
                    "Notas",
                    "Creado",
                ]
            )
            for l in leads:
                writer.writerow(
                    [
                        l["id"],
                        l["name"],
                        l["company"],
                        l["email"],
                        l["whatsapp"],
                        l["industry"],
                        l["service_interest"],
                        l["monthly_budget"],
                        l["source"],
                        l["stage"],
                        l["notes"],
                        l["created_at"],
                    ]
                )
            data = ("\ufeff" + buf.getvalue()).encode("utf-8")
            self._send_bytes(data, "text/csv; charset=utf-8", filename="leads_aconcagua_colombia.csv")
            return

        if path == "/api/admin/outbox":
            status_filter = qs.get("status", [""])[0]
            self._send_json(
                {
                    "config_status": autopilot_dispatcher.get_autopilot_status(),
                    "jobs": database.list_outbox_jobs(status=status_filter),
                }
            )
            return

        self._send_json({"error": "Ruta GET no encontrada"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json({"error": "JSON inválido"}, status=HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/leads":
            try:
                lead = database.upsert_lead(
                    name=data.get("name", ""),
                    company=data.get("company", ""),
                    email=data.get("email", ""),
                    whatsapp=data.get("whatsapp", ""),
                    industry=data.get("industry", ""),
                    service_interest=data.get("service_interest", ""),
                    monthly_budget=data.get("monthly_budget", ""),
                    source=data.get("source", "landing"),
                    stage=data.get("stage", "nuevo_lead"),
                    notes=data.get("notes", ""),
                    queue_type="inbound",
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"lead": lead}, status=HTTPStatus.CREATED)
            return

        if path == "/api/diagnostico/submit":
            evaluation = database.evaluate_diagnostic(data)
            try:
                lead = database.upsert_lead(
                    name=data.get("name", ""),
                    company=data.get("company", ""),
                    email=data.get("email", ""),
                    whatsapp=data.get("whatsapp", ""),
                    industry=data.get("industry", ""),
                    service_interest=evaluation["recommended_primary"],
                    monthly_budget=data.get("monthly_budget", ""),
                    source="diagnostico",
                    stage="diagnostico_enviado",
                    notes=evaluation["summary"],
                    queue_type="inbound",
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            settings = database.get_settings()
            wa_msg = (
                f"Hola! Acabo de hacer el diagnóstico de Aconcagua Colombia para {lead['company'] or lead['name']}. "
                f"Me recomendó: {evaluation['recommended_primary']}."
            )
            self._send_json(
                {
                    "lead": lead,
                    "evaluation": evaluation,
                    "whatsapp_url": build_whatsapp_url(settings.get("whatsapp_number", ""), wa_msg),
                },
                status=HTTPStatus.CREATED,
            )
            return

        if path == "/api/admin/prospects/import-csv":
            csv_text = data.get("csv_text", "")
            if not csv_text.strip():
                self._send_json({"error": "Pega el contenido CSV de prospectos."}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                result = database.import_prospects_csv(csv_text)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(result, status=HTTPStatus.CREATED)
            return

        if path == "/api/admin/outbox/dispatch":
            self._send_json(autopilot_dispatcher.dispatch_due_jobs())
            return

        self._send_json({"error": "Ruta POST no encontrada"}, status=HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        try:
            data = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json({"error": "JSON inválido"}, status=HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/settings":
            updated = database.update_settings(data)
            self._send_json({"settings": updated})
            return

        m = re.match(r"^/api/leads/(\d+)$", path)
        if m:
            lead_id = int(m.group(1))
            stage = data.get("stage", "nuevo_lead")
            notes = data.get("notes")
            updated = database.update_lead_stage(lead_id, stage, notes=notes)
            if not updated:
                self._send_json({"error": "Lead no encontrado"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json({"lead": updated})
            return

        self._send_json({"error": "Ruta PATCH no encontrada"}, status=HTTPStatus.NOT_FOUND)


def create_server(host: str = "127.0.0.1", port: int = 8796) -> ThreadingHTTPServer:
    database.init_db()
    return ThreadingHTTPServer((host, port), AconcaguaHandler)


def _start_autopilot_worker(interval_seconds: int) -> threading.Event:
    stop_event = threading.Event()

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                status = autopilot_dispatcher.get_autopilot_status()
                if status.get("smtp_ready") or status.get("lead_webhook_ready"):
                    autopilot_dispatcher.dispatch_due_jobs()
            except Exception:
                pass

    thread = threading.Thread(target=_loop, name="aconcagua-autopilot-worker", daemon=True)
    thread.start()
    return stop_event


def main() -> None:
    host = os.environ.get("ACONCAGUA_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT") or os.environ.get("ACONCAGUA_PORT", "8796"))
    server = create_server(host=host, port=port)
    dispatch_interval = int(os.environ.get("ACONCAGUA_DISPATCH_INTERVAL", "60"))
    stop_event = _start_autopilot_worker(dispatch_interval) if dispatch_interval > 0 else None
    print(f"Aconcagua Agency Colombia escuchando en http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if stop_event is not None:
            stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
