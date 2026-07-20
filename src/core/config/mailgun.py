import os

from pydantic import SecretStr, model_validator

from .base import BaseConfig


class MailgunConfig(BaseConfig, env_prefix="MAILGUN_"):
    api_key: SecretStr
    domain: str
    from_email: str = ""
    base_url: str = "https://api.eu.mailgun.net"

    @model_validator(mode="before")
    @classmethod
    def resolve_from_email(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        if data.get("from_email"):
            return data

        for key in ("from_email", "FROM", "MAILGUN_FROM"):
            value = data.get(key) or os.environ.get(key)
            if value:
                data["from_email"] = value
                break

        return data

    @model_validator(mode="after")
    def require_from_email(self) -> "MailgunConfig":
        if not self.from_email.strip():
            raise ValueError("MAILGUN_FROM is required")
        return self
