"""Step 1 of the uuid -> id map: read-only export from the Remnawave 2.7.4 panel.

Run inside the CURRENT (remnapy 2.x) remnashop container, before the panel upgrade:
    docker cp export_v2_map.py remnashop:/tmp/ && \
    docker exec -w /opt/remnashop remnashop python /tmp/export_v2_map.py /tmp/panel_v2.csv
Only GET requests. Output columns: uuid, short_uuid, username.
"""
import asyncio, csv, hashlib, sys
from httpx import AsyncClient, Timeout
from remnapy import RemnawaveSDK
from src.core.config import AppConfig

async def main(out_path: str) -> None:
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
    async with AsyncClient(base_url=f"{c.url.get_secret_value()}/api", headers=headers,
                           cookies=c.cookies, timeout=Timeout(30.0)) as client:
        sdk = RemnawaveSDK(client)
        rows, start, size = [], 0, 500
        while True:
            resp = await sdk.users.get_all_users(start=start, size=size)
            for u in resp.users:
                rows.append((str(u.uuid), u.short_uuid, u.username))
            total = resp.total
            start += size
            if start >= total or not resp.users:
                break
    rows.sort()
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["uuid", "short_uuid", "username"])
        w.writerows(rows)
    uu = {r[0] for r in rows}; su = {r[1] for r in rows}
    data = open(out_path, "rb").read()
    print(f"panel_total={total} exported={len(rows)} unique_uuid={len(uu)} unique_short_uuid={len(su)} "
          f"null_short_uuid={sum(1 for r in rows if not r[1])} sha256={hashlib.sha256(data).hexdigest()}")

asyncio.run(main(sys.argv[1]))
