"""Mail observability (2026-10-23): emailed codes (signup OTP +
password reset) failed silently in production — the app's INFO logs
went only to a rotating log FILE on the server, which the host's log
stream (Render) never shows, and nothing outside could tell whether
SMTP was even configured on the live instance.

Two fixes, pinned here:
  * the "powerhouse" logger mirrors every record to stderr as well as
    the file (same formatter, same redaction, message text unchanged),
    so "… email sent … via SMTP" / "… (SMTP not configured …)" /
    "… FAILED via SMTP" lines appear in the process output;
  * GET /api/auth/config also reports "mail_configured" — a boolean
    computed from _smtp_config() at request time. It must never carry
    any value, host, or fragment of the SMTP settings.
"""
import logging

import main

SMTP_VARS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM")


def _smtp_off(monkeypatch):
    for var in SMTP_VARS:
        monkeypatch.delenv(var, raising=False)


def _smtp_on(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "mailer@example.test")
    monkeypatch.setenv("SMTP_PASS", "not-a-real-password")
    monkeypatch.setenv("SMTP_FROM", "Do Gym <mailer@example.test>")


def test_auth_config_mail_not_configured_without_smtp(client, monkeypatch):
    _smtp_off(monkeypatch)
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["mail_configured"] is False
    assert "google_enabled" in body  # pre-existing key untouched


def test_auth_config_mail_configured_with_full_smtp(client, monkeypatch):
    _smtp_on(monkeypatch)
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["mail_configured"] is True
    # Boolean only: exactly the two known keys, and no setting leaks.
    assert set(body) == {"google_enabled", "mail_configured"}
    assert "smtp.example.test" not in r.text
    assert "not-a-real-password" not in r.text


def test_auth_config_mail_not_configured_when_smtp_partial(client, monkeypatch):
    _smtp_on(monkeypatch)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    r = client.get("/api/auth/config")
    assert r.json()["mail_configured"] is False


def test_app_logger_streams_info_too():
    # Alongside the RotatingFileHandler there must be a plain console
    # StreamHandler, and the logger level must let INFO through.
    assert main.logger.level == logging.INFO
    stream_handlers = [
        h for h in main.logger.handlers if type(h) is logging.StreamHandler
    ]
    assert stream_handlers, "no console stream handler on the app logger"
