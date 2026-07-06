"""Live end-to-end proof of Postbox federation between two real servers.

Run from the repo root:
  python -m scripts.federation_e2e
"""
import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx


TOKEN = "s3cr3t"
OK = "✓"
FAIL = "✗"


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def check(label: str, condition: bool) -> None:
    print(f"{OK if condition else FAIL} {label}")
    if not condition:
        raise AssertionError(label)


async def wait_for_ready(base: str, proc: subprocess.Popen, log_path: Path,
                         timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"{base} exited early with code {proc.returncode}\n"
                    f"{tail(log_path)}"
                )
            try:
                resp = await client.get(f"{base}/agents")
                if resp.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError(f"{base} did not become ready\n{tail(log_path)}")


def launch_server(root: Path, data_dir: Path, instance: str, port: int):
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "server.log"
    log_file = log_path.open("w")
    env = os.environ.copy()
    env.update({
        "POSTBOX_HOST": "127.0.0.1",
        "POSTBOX_PORT": str(port),
        "POSTBOX_INSTANCE": instance,
        "POSTBOX_DATA_DIR": str(data_dir),
    })
    env.pop("POSTBOX_OBSERVER_TOKEN", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "postbox.main"],
        cwd=root,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc, log_file, log_path, f"http://127.0.0.1:{port}"


def terminate(proc: subprocess.Popen | None, log_file) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
    if log_file is not None:
        log_file.close()


def tail(path: Path, lines: int = 80) -> str:
    if not path.exists():
        return "(no server log)"
    content = path.read_text(errors="replace").splitlines()
    return "\n".join(content[-lines:]) or "(empty server log)"


async def post_json(client: httpx.AsyncClient, url: str, body: dict,
                    headers: dict | None = None) -> dict:
    resp = await client.post(url, json=body, headers=headers)
    if resp.status_code >= 400:
        raise AssertionError(f"POST {url} failed {resp.status_code}: {resp.text}")
    return resp.json() if resp.content else {}


async def get_json(client: httpx.AsyncClient, url: str) -> dict | list:
    resp = await client.get(url)
    if resp.status_code >= 400:
        raise AssertionError(f"GET {url} failed {resp.status_code}: {resp.text}")
    return resp.json()


async def find_message(client: httpx.AsyncClient, base: str, address: str,
                       sender: str, body: str, thread_id: str | None = None):
    threads = await get_json(client, f"{base}/observer/threads?address={address}")
    for summary in threads:
        tid = summary["thread_id"]
        if thread_id is not None and tid != thread_id:
            continue
        detail = await get_json(client, f"{base}/observer/threads/{tid}")
        for message in detail["messages"]:
            if message["from"] == sender and message["body"] == body:
                return detail, message
    return None, None


async def poll_message(client: httpx.AsyncClient, base: str, address: str,
                       sender: str, body: str, thread_id: str | None = None,
                       timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail, message = await find_message(
            client, base, address, sender, body, thread_id)
        if message is not None:
            return detail, message
        await asyncio.sleep(0.1)
    raise TimeoutError(
        f"timed out waiting for {sender!r} -> {address!r}: {body!r}"
    )


async def count_thread_messages(client: httpx.AsyncClient, base: str,
                                thread_id: str) -> int:
    detail = await get_json(client, f"{base}/observer/threads/{thread_id}")
    return len(detail["messages"])


async def run() -> None:
    root = Path.cwd()
    run_dir = root / ".postbox-federation-e2e" / f"run-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    data1 = run_dir / "postbox1"
    data2 = run_dir / "postbox2"
    proc1 = proc2 = None
    log1_file = log2_file = None
    log1_path = log2_path = None

    try:
        p1, p2 = free_port(), free_port()
        proc1, log1_file, log1_path, base1 = launch_server(root, data1, "postbox1", p1)
        proc2, log2_file, log2_path, base2 = launch_server(root, data2, "postbox2", p2)
        await asyncio.gather(
            wait_for_ready(base1, proc1, log1_path),
            wait_for_ready(base2, proc2, log2_path),
        )
        check(f"both servers ready ({base1}, {base2})", True)

        async with httpx.AsyncClient(timeout=5.0) as client:
            await post_json(client, f"{base1}/peers", {
                "name": "postbox2", "url": base2, "token": TOKEN,
            })
            await post_json(client, f"{base2}/peers", {
                "name": "postbox1", "url": base1, "token": TOKEN,
            })
            check("peered both ways", True)

            await post_json(client, f"{base1}/agents", {"name": "alice"})
            await post_json(client, f"{base2}/agents", {"name": "bob"})
            check("registered alice on postbox1 and bob on postbox2", True)

            forward = await post_json(client, f"{base1}/observer/send", {
                "from": "alice", "to": "bob@postbox2", "body": "hello bob",
            })
            p2_detail, p2_msg = await poll_message(
                client, base2, "bob", "alice@postbox1", "hello bob")
            check("forward relay arrived on postbox2", p2_msg is not None)

            p1_detail, _ = await poll_message(
                client, base1, "alice", "alice", "hello bob")
            check(
                "shared thread id propagated",
                p1_detail["thread_id"] == p2_detail["thread_id"] == forward["thread_id"],
            )

            reply = await post_json(client, f"{base2}/observer/send", {
                "from": "bob",
                "to": "alice@postbox1",
                "body": "hi alice",
                "in_reply_to": p2_msg["id"],
            })
            p1_reply_detail, p1_reply = await poll_message(
                client, base1, "alice", "bob@postbox2", "hi alice",
                thread_id=forward["thread_id"])
            check(
                "reply relay arrived in the same shared thread",
                p1_reply is not None
                and p1_reply_detail["thread_id"] == forward["thread_id"]
                and reply["thread_id"] == forward["thread_id"],
            )

            before = await count_thread_messages(client, base2, p2_detail["thread_id"])
            inbound_payload = {
                "from": "alice@postbox1",
                "to": "bob",
                "subject": forward["subject"],
                "body": "hello bob",
                "content_type": forward["content_type"],
                "fed_thread_id": forward["thread_id"],
                "origin_msg_id": forward["id"],
                "created_at": forward["created_at"],
            }
            headers = {"X-Postbox-Peer-Token": TOKEN}
            await post_json(client, f"{base2}/federation/inbound",
                            inbound_payload, headers=headers)
            await post_json(client, f"{base2}/federation/inbound",
                            inbound_payload, headers=headers)
            after = await count_thread_messages(client, base2, p2_detail["thread_id"])
            check("idempotent re-relay did not duplicate bob's message", after == before)

        print("\nPASS: two real peered Postbox servers relayed messages both ways.")
    except Exception:
        if log1_path:
            print("\npostbox1 log tail:\n" + tail(log1_path))
        if log2_path:
            print("\npostbox2 log tail:\n" + tail(log2_path))
        raise
    finally:
        terminate(proc1, log1_file)
        terminate(proc2, log2_file)
        shutil.rmtree(run_dir, ignore_errors=True)
        try:
            run_dir.parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception as exc:
        print(f"\n{FAIL} federation e2e failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
