#!/usr/bin/env python3
"""Integration tests for Aconcagua Agency Colombia Lead Capture, Diagnostic & CRM."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "webapp"))

import server  # noqa: E402


class TestAconcaguaWebApp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_aconcagua.db"
        os.environ["ACONCAGUA_DB_PATH"] = str(self.db_path)
        os.environ["ACONCAGUA_QUIET"] = "1"
        self.orig_env_file = server.autopilot_dispatcher.ENV_FILE
        server.autopilot_dispatcher.ENV_FILE = Path(self.tmp_dir.name) / ".env.missing"
        self.httpd = server.create_server(host="127.0.0.1", port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.autopilot_dispatcher.ENV_FILE = self.orig_env_file
        os.environ.pop("ACONCAGUA_DB_PATH", None)
        self.tmp_dir.cleanup()

    def _req(self, path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, str]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_full_agency_lead_diagnostic_csv_outreach_and_qr(self) -> None:
        # 1. Health & config
        status, body = self._req("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

        status, cfg_body = self._req("/api/config")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(json.loads(cfg_body)["services"]), 5)

        # 2. Landing lead capture
        status, lead_body = self._req(
            "/api/leads",
            method="POST",
            payload={
                "name": "Camila Restrepo",
                "company": "Dermovital Bogotá",
                "email": "camila@dermovital.co",
                "whatsapp": "+573001112233",
                "service_interest": "Meta Ads y UGC",
                "monthly_budget": "$4M - $10M COP / mes",
            },
        )
        self.assertEqual(status, 201)
        lead = json.loads(lead_body)["lead"]
        self.assertEqual(lead["company"], "Dermovital Bogotá")

        # 3. Interactive diagnostic
        status, diag_body = self._req(
            "/api/diagnostico/submit",
            method="POST",
            payload={
                "name": "Andrés Mejía",
                "company": "Café Altura Medellín",
                "email": "andres@cafealtura.co",
                "whatsapp": "+573109998877",
                "industry": "E-commerce alimentos",
                "goal": "Escalar ventas y leads con pauta (ROAS)",
                "current_channel": "Instagram y WhatsApp",
                "challenge": "Costo por lead alto o pocas ventas en ads",
                "monthly_budget": "$4M - $10M COP / mes",
            },
        )
        self.assertEqual(status, 201)
        diag = json.loads(diag_body)
        self.assertIn("Performance", diag["evaluation"]["recommended_primary"])

        # 4. CSV prospect import (3 emails queued per prospect: Day 0, Day 3, Day 7)
        csv_sample = (
            "nombre,empresa,correo,whatsapp,industria,servicio\n"
            "Sofía Valencia,Estética Luz Cali,sofia@esteticaluz.co,+573152223344,Salud y Belleza,Meta Ads y UGC\n"
            ",Sin Correo,,,,\n"
        )
        status, imp_body = self._req(
            "/api/admin/prospects/import-csv",
            method="POST",
            payload={"csv_text": csv_sample},
        )
        self.assertEqual(status, 201)
        imp = json.loads(imp_body)
        self.assertEqual(imp["imported"], 1)
        self.assertEqual(imp["skipped"], 1)

        # 5. QR route logging & dashboard
        status, _ = self._req("/qr/ig")
        self.assertEqual(status, 200)
        _, dash_body = self._req("/api/dashboard")
        metrics = json.loads(dash_body)["metrics"]
        self.assertEqual(metrics["total_leads"], 3)
        self.assertEqual(metrics["qr_scans"], 1)

        # 6. Outbox dispatch without credentials skips cleanly without failing
        status, disp_body = self._req("/api/admin/outbox/dispatch", method="POST", payload={})
        self.assertEqual(status, 200)
        disp = json.loads(disp_body)
        # 3 Day-0 emails due (Camila, Andrés, Sofía), remaining Day 3/7 scheduled for later
        self.assertEqual(disp["skipped_missing_credentials"], 3)
        self.assertEqual(disp["scheduled_for_later"], 4)

    def test_static_path_traversal_blocked(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._req("/static/../../config/agency_profile.json")
        self.assertEqual(ctx.exception.code, 404)

    def test_appointment_handoff_and_reply_classification(self) -> None:
        import autopilot_dispatcher  # noqa: E402

        self.assertEqual(
            autopilot_dispatcher.classify_reply_intent(
                "Re: Propuesta de Auditoría Digital",
                "Hola Raymond, sí nos interesa agendar una cita esta semana.",
            ),
            "interesado_cita",
        )
        self.assertEqual(
            autopilot_dispatcher.classify_reply_intent(
                "Re: Propuesta",
                "No gracias, por favor no estamos interesados.",
            ),
            "no_interesado",
        )
        self.assertEqual(
            autopilot_dispatcher.classify_reply_intent(
                "Re: Propuesta de Auditoría Digital y Comercial – Blush-Bar",
                "Gracias por escribirnos. Hemos recibido tu solicitud #12345 y nuestro equipo se pondrá en contacto.",
            ),
            "respuesta_automatica",
        )

        # Lead requesting a meeting goes straight to /api/admin/citas and cancels pending follow-ups
        status, lead_body = self._req(
            "/api/leads",
            method="POST",
            payload={
                "name": "Laura Gómez",
                "company": "Glow Skincare CO",
                "email": "laura@glowskincare.co",
                "whatsapp": "+573001112233",
                "wants_meeting": True,
            },
        )
        self.assertEqual(status, 201)
        lead_id = json.loads(lead_body)["lead"]["id"]

        # Also test moving an outbound prospect to interesado_cita cancels its pending outbox jobs
        self._req(
            "/api/admin/prospects/import-csv",
            method="POST",
            payload={
                "csv_text": "nombre,empresa,correo,industria,servicio\nCarlos,Marca X,carlos@marcax.co,Moda,TikTok Ads\n"
            },
        )
        _, leads_data = self._req("/api/leads?search=carlos@marcax.co")
        carlos_id = json.loads(leads_data)["leads"][0]["id"]
        self._req(f"/api/leads/{carlos_id}", method="PATCH", payload={"stage": "interesado_cita"})

        _, citas_body = self._req("/api/admin/citas")
        citas = json.loads(citas_body)["citas"]
        emails = {c["email"] for c in citas}
        self.assertIn("laura@glowskincare.co", emails)
        self.assertIn("carlos@marcax.co", emails)
        self.assertTrue(any(c["id"] == lead_id and c["direct_mailto_url"].startswith("mailto:") for c in citas))

        _, csv_body = self._req("/api/admin/citas.csv")
        self.assertIn("laura@glowskincare.co", csv_body)
        self.assertIn("carlos@marcax.co", csv_body)

        _, outbox_body = self._req("/api/admin/outbox")
        carlos_jobs = [j for j in json.loads(outbox_body)["jobs"] if j["recipient"] == "carlos@marcax.co"]
        self.assertTrue(carlos_jobs)
        self.assertTrue(all(j["status"] == "cancelled" for j in carlos_jobs))


if __name__ == "__main__":
    unittest.main()
