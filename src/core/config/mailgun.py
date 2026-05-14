from pydantic import Field, SecretStr

from .base import BaseConfig


class MailgunConfig(BaseConfig, env_prefix="MAILGUN_"):
    api_key: SecretStr
    domain: str
    from_email: str = Field(alias="MAILGUN_FROM")
    base_url: str = "https://api.eu.mailgun.net"
