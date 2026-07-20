import asyncio
import smtplib
from email.message import EmailMessage

from httpx import AsyncClient
from loguru import logger

from src.application.common.email_sender import EmailSender
from src.core.config import AppConfig
from src.core.config.mailgun import MailgunConfig
from src.core.exceptions import EmailDeliveryError


class SmtpEmailSender(EmailSender):
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    @property
    def is_enabled(self) -> bool:
        email = self._config.email
        return bool(
            email.enabled
            and email.host
            and email.from_email
            and email.username.get_secret_value()
            and email.password.get_secret_value()
        )

    async def send(self, *, to: str, subject: str, body: str) -> None:
        try:
            await asyncio.to_thread(self._send_sync, to=to, subject=subject, body=body)
        except Exception as e:
            logger.error(f"Failed to send email to '{to}': {e}")
            raise EmailDeliveryError(
                "Failed to send verification email. Please try again later."
            ) from e

    def _send_sync(self, *, to: str, subject: str, body: str) -> None:
        email = self._config.email
        message = EmailMessage()
        message["Subject"] = subject
        from_name = email.from_name.strip()
        from_email = email.from_email.strip()
        message["From"] = f"{from_name} <{from_email}>" if from_name else from_email
        message["To"] = to
        message.set_content(body)

        smtp_user = email.username.get_secret_value()
        smtp_password = email.password.get_secret_value()

        if email.use_ssl:
            with smtplib.SMTP_SSL(email.host, email.port, timeout=20) as client:
                client.login(smtp_user, smtp_password)
                client.send_message(message)
            return

        with smtplib.SMTP(email.host, email.port, timeout=20) as client:
            client.ehlo()
            if email.use_tls:
                client.starttls()
                client.ehlo()
            client.login(smtp_user, smtp_password)
            client.send_message(message)


class MailgunEmailSender(EmailSender):
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def _get_mailgun_config(self) -> MailgunConfig | None:
        if self._config.mailgun is not None:
            return self._config.mailgun

        try:
            return MailgunConfig()
        except Exception as exc:
            logger.warning(f"Mailgun is not configured: {exc}")
            return None

    @property
    def is_enabled(self) -> bool:
        return self._get_mailgun_config() is not None

    async def send(self, *, to: str, subject: str, body: str) -> None:
        mailgun = self._get_mailgun_config()
        if mailgun is None:
            raise EmailDeliveryError("Mailgun is not configured")

        try:
            async with AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{mailgun.base_url}/v3/{mailgun.domain}/messages",
                    auth=("api", mailgun.api_key.get_secret_value()),
                    data={
                        "from": mailgun.from_email,
                        "to": to,
                        "subject": subject,
                        "text": body,
                    },
                )
                response.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send email to '{to}' via Mailgun: {e}")
            raise EmailDeliveryError(
                "Failed to send verification email. Please try again later."
            ) from e


class CompositeEmailSender(EmailSender):
    def __init__(self, config: AppConfig) -> None:
        self._smtp = SmtpEmailSender(config)
        self._mailgun = MailgunEmailSender(config)

    @property
    def is_enabled(self) -> bool:
        return self._smtp.is_enabled or self._mailgun.is_enabled

    async def send(self, *, to: str, subject: str, body: str) -> None:
        if self._mailgun.is_enabled:
            await self._mailgun.send(to=to, subject=subject, body=body)
            return
        if self._smtp.is_enabled:
            await self._smtp.send(to=to, subject=subject, body=body)
            return
        raise EmailDeliveryError("Email delivery is not configured")
