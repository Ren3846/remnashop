from pydantic import AliasChoices, Field, SecretStr

from .base import BaseConfig


class MailgunConfig(BaseConfig, env_prefix="MAILGUN_"):
    api_key: SecretStr
    domain: str
    from_email: str = Field(validation_alias=AliasChoices("FROM", "FROM_EMAIL"))
    base_url: str = "https://api.eu.mailgun.net"
