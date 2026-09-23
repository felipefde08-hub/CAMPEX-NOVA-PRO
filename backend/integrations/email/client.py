from __future__ import annotations

import logging
import smtplib
import socket
import time
from email.message import EmailMessage
from typing import Any

import httpx

from backend.config import Settings


logger = logging.getLogger("campex.email")


class EmailError(RuntimeError):
    pass


class EmailClient:
    def __init__(self, settings: Settings, timeout_seconds: float = 10.0) -> None:
        self.resend_api_key = settings.resend_api_key
        self.resend_from_email = settings.resend_from_email
        self.resend_from_name = settings.resend_from_name
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.username = settings.smtp_username
        self.password = settings.smtp_password
        self.from_email = settings.smtp_from_email
        self.from_name = settings.smtp_from_name
        self.use_tls = settings.smtp_use_tls
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(
            (self.resend_api_key and self.resend_from_email)
            or (self.host and self.port and self.from_email)
        )

    def test_connection(self) -> dict[str, Any]:
        if not self.configured:
            raise EmailError("Email provider is not configured.")
        if self.resend_api_key and self.resend_from_email:
            return {"status": "ok", "provider": "resend", "latency_ms": 0}
        started = time.perf_counter()
        with self._connect() as smtp:
            smtp.noop()
        return {
            "status": "ok",
            "provider": "smtp",
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    def send_email(
        self,
        *,
        recipients: list[str],
        subject: str,
        text: str,
        html: str | None = None,
    ) -> dict[str, Any]:
        if not self.configured:
            raise EmailError("Email provider is not configured.")
        clean_recipients = [item.strip() for item in recipients if item.strip()]
        if not clean_recipients:
            raise EmailError("At least one email recipient is required.")
        if self.resend_api_key and self.resend_from_email:
            return self._send_resend(
                recipients=clean_recipients,
                subject=subject,
                text=text,
                html=html,
            )
        return self._send_smtp(
            recipients=clean_recipients,
            subject=subject,
            text=text,
            html=html,
        )

    def _send_resend(
        self,
        *,
        recipients: list[str],
        subject: str,
        text: str,
        html: str | None,
    ) -> dict[str, Any]:
        logger.info("[CAMPEX][EMAIL] sending via Resend")
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "from": f"{self.resend_from_name} <{self.resend_from_email}>",
            "to": recipients,
            "subject": subject,
            "text": text,
        }
        if html:
            payload["html"] = html
        try:
            response = httpx.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {self.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise EmailError("Resend request timed out.") from exc
        except httpx.HTTPError as exc:
            raise EmailError("Resend request failed.") from exc
        data = _resend_json(response)
        logger.info("[CAMPEX][EMAIL] sent via Resend")
        return {
            "status": "sent",
            "provider": "resend",
            "message_id": data.get("id"),
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "recipients": recipients,
        }

    def _send_smtp(
        self,
        *,
        recipients: list[str],
        subject: str,
        text: str,
        html: str | None,
    ) -> dict[str, Any]:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{self.from_name} <{self.from_email}>"
        message["To"] = ", ".join(recipients)
        message.set_content(text)
        if html:
            message.add_alternative(html, subtype="html")

        logger.info("[CAMPEX][EMAIL] sending report")
        started = time.perf_counter()
        try:
            with self._connect() as smtp:
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailError("SMTP authentication failed.") from exc
        except (smtplib.SMTPException, OSError, socket.timeout) as exc:
            raise EmailError("SMTP send failed.") from exc
        logger.info("[CAMPEX][EMAIL] sent")
        return {
            "status": "sent",
            "provider": "smtp",
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "recipients": recipients,
        }

    def _connect(self):
        if self.use_tls:
            smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds)
            smtp.starttls()
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds)
        if self.username:
            smtp.login(self.username, self.password or "")
        return smtp


def _resend_json(response: httpx.Response) -> dict[str, Any]:
    if response.status_code in {401, 403}:
        raise EmailError("Resend API key is invalid or not authorized.")
    if response.status_code == 422:
        raise EmailError("Resend rejected the email payload.")
    if response.status_code == 429:
        raise EmailError("Resend rate limit reached.")
    if response.status_code >= 400:
        raise EmailError("Resend service unavailable.")
    try:
        data = response.json()
    except ValueError as exc:
        raise EmailError("Resend returned invalid JSON.") from exc
    if not data.get("id"):
        raise EmailError("Resend did not return a message id.")
    return data
