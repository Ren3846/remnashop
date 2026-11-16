"""Step 2 of the uuid -> id map: join the 2.7.4 export with the upgraded 3.x panel.

Run inside the NEW (remnapy 3.x) remnashop image, after the panel upgrade and before the
bot migration a026:

    python -m src.cli.remna_id_map <panel_v2.csv> <panel_v2.csv sha256> <out.csv>

The join key is shortUuid, which Remnawave keeps unchanged across the 2.x -> 3.x upgrade.
username is compared as a guard only; any mismatch, duplicate, or missing row aborts.
Only GET requests are sent to the panel. Prints counts and the sha256 of <out.csv>, which
is what REMNA_ID_MAP_SHA256 must be set to for the migration.
"""

import asyncio
import csv
import hashlib
import sys
from pathlib import Path

from httpx import AsyncClient, Timeout
from remnapy import RemnawaveSDK

from src.core.config import AppConfig

_PAGE = 500


class MapBuildError(RuntimeError):
    pass


def _read_v2(path: Path, expected_sha: str) -> list[dict[str, str]]:
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha.strip().lower():
        raise MapBuildError(f"v2 export checksum mismatch: {actual}")
    rows = list(csv.DictReader(raw.decode("utf-8").splitlines()))
    if not rows or list(rows[0]) != ["uuid", "short_uuid", "username"]:
        raise MapBuildError("Unexpected v2 export header")
    for key in ("uuid", "short_uuid"):
        values = [r[key] for r in rows]
        if len(set(values)) != len(values):
            raise MapBuildError(f"Duplicate {key} in v2 export")
    return rows


async def _fetch_v3() -> list[tuple[int, str, str]]:
    c = AppConfig.get().remnawave
    headers = {
        "Authorization": f"Bearer {c.token.get_secret_value()}",
        "X-Api-Key": c.caddy_token.get_secret_value(),
        "CF-Access-Client-Id": c.cf_client_id.get_secret_value(),
        "CF-Access-Client-Secret": c.cf_client_secret.get_secret_value(),
    }
    if not c.is_external:
        headers["x-forwarded-proto"] = "https"
        headers["x-forwarded-for"] = "127.0.0.1"

    async with AsyncClient(
        base_url=f"{c.url.get_secret_value()}/api",
        headers=headers,
        cookies=c.cookies,
        timeout=Timeout(30.0),
    ) as client:
        sdk = RemnawaveSDK(client)
        result: list[tuple[int, str, str]] = []
        start = 0
        while True:
            page = await sdk.users.get_all_users(start=start, size=_PAGE)
            result.extend((u.id, u.short_uuid, u.username) for u in page.users)
            start += _PAGE
            if not page.users or start >= page.total:
                return result


def build(
    v2_rows: list[dict[str, str]], v3_users: list[tuple[int, str, str]]
) -> list[tuple[str, int]]:
    by_short: dict[str, tuple[int, str]] = {}
    for remna_id, short_uuid, username in v3_users:
        if short_uuid in by_short:
            raise MapBuildError(f"Duplicate shortUuid on the 3.x panel: {short_uuid}")
        by_short[short_uuid] = (remna_id, username)

    out: list[tuple[str, int]] = []
    missing, renamed = [], []
    for row in v2_rows:
        hit = by_short.get(row["short_uuid"])
        if hit is None:
            missing.append(row["uuid"])
            continue
        remna_id, username = hit
        if username != row["username"]:
            renamed.append(row["uuid"])
            continue
        out.append((row["uuid"], remna_id))

    if missing or renamed:
        raise MapBuildError(
            f"{len(missing)} v2 user(s) missing on the 3.x panel, "
            f"{len(renamed)} with a different username; refusing to build a partial map"
        )
    ids = [i for _, i in out]
    if len(set(ids)) != len(ids):
        raise MapBuildError("Two v2 users resolved to the same 3.x id")
    return sorted(out)


async def main(v2_path: str, v2_sha: str, out_path: str) -> None:
    v2_rows = _read_v2(Path(v2_path), v2_sha)
    v3_users = await _fetch_v3()
    pairs = build(v2_rows, v3_users)

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["uuid", "remna_id"])
        writer.writerows(pairs)

    digest = hashlib.sha256(Path(out_path).read_bytes()).hexdigest()
    print(  # noqa: T201 - CLI output
        f"v2_users={len(v2_rows)} v3_users={len(v3_users)} matched={len(pairs)} "
        f"unresolved=0 collisions=0 sha256={digest}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    asyncio.run(main(*sys.argv[1:]))
