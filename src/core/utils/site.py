from src.core.constants import DOMAIN_REGEX


def normalize_domain(value: str) -> str:
    domain = (
        value.strip()
        .lower()
        .removeprefix("https://")
        .removeprefix("http://")
        .split("/")[0]
    )
    return domain


def normalize_subdomain_host(value: str, base_domain: str | None = None) -> str:
    host = normalize_domain(value)
    if base_domain and host.count(".") < base_domain.count("."):
        if not host.endswith(base_domain):
            host = f"{host}.{base_domain}"
    return host


def is_valid_domain(value: str) -> bool:
    return bool(DOMAIN_REGEX.match(value))
