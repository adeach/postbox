import argparse
import asyncio
import json
import os

import httpx
from httpx_sse import aconnect_sse

from courier.listener.wakeups import build_wakeup


async def run_daemon(url: str, token: str, wakeup) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    last_id = "0"
    async with httpx.AsyncClient(base_url=url, timeout=None) as client:
        while True:
            try:
                async with aconnect_sse(
                    client, "GET", "/events",
                    headers={**headers, "Last-Event-ID": last_id},
                ) as es:
                    async for sse in es.aiter_sse():
                        last_id = sse.id or last_id
                        if sse.event == "message.received":
                            await wakeup.wake(json.loads(sse.data))
            except (httpx.HTTPError, httpx.TransportError):
                await asyncio.sleep(1)  # reconnect with backoff


def main() -> None:
    p = argparse.ArgumentParser(description="Courier listener daemon")
    p.add_argument("--url", default=os.environ.get("COURIER_URL", "http://127.0.0.1:8765"))
    p.add_argument("--token", default=os.environ.get("COURIER_TOKEN"))
    p.add_argument("--wakeup", default="os_notify",
                   choices=["stub", "copilot_cli", "copilot_app", "os_notify"])
    p.add_argument("--repo", default="owner/repo", help="repo for copilot_app deep link")
    args = p.parse_args()
    if not args.token:
        raise SystemExit("COURIER_TOKEN (or --token) is required")
    wakeup = build_wakeup(args.wakeup, repo=args.repo)
    asyncio.run(run_daemon(args.url, args.token, wakeup))


if __name__ == "__main__":
    main()
