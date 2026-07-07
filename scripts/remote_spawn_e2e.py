"""Live proof of CROSS-INSTANCE (federated) terminal spawn.

Boots two real peered servers — "laptop" and "vm" — and has an agent on laptop
ask the vm to spin up a terminal agent. The vm is configured to launch a harmless
`sleep` instead of a real copilot (POSTBOX_TERMINAL_CMD), so this proves the relay +
remote-spawn plumbing without needing copilot installed.

Run from the repo root:
  python -m scripts.remote_spawn_e2e
"""
import asyncio
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

TOKEN = "shared-peer-secret"
NAME = "helper"
OK, FAIL = "✓", "✗"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def check(label: str, cond: bool) -> None:
    print(f"{OK if cond else FAIL} {label}")
    if not cond:
        raise AssertionError(label)


def launch(root: Path, data: Path, instance: str, port: int, extra: dict) -> tuple:
    data.mkdir(parents=True, exist_ok=True)
    log = data / "server.log"
    lf = log.open("w")
    env = os.environ.copy()
    env.update({"POSTBOX_HOST": "127.0.0.1", "POSTBOX_PORT": str(port),
                "POSTBOX_INSTANCE": instance, "POSTBOX_DATA_DIR": str(data)})
    env.pop("POSTBOX_OBSERVER_TOKEN", None)
    env.update(extra)
    proc = subprocess.Popen([sys.executable, "-m", "postbox.main"], cwd=root, env=env,
                            stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    return proc, lf, log, f"http://127.0.0.1:{port}"


def terminate(proc, lf) -> None:
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    if lf:
        lf.close()


async def ready(base: str, proc, log: Path, timeout: float = 12.0) -> None:
    end = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=1.0) as c:
        while time.monotonic() < end:
            if proc.poll() is not None:
                raise RuntimeError(f"{base} died:\n{log.read_text()[-2000:]}")
            try:
                if (await c.get(f"{base}/agents")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError(f"{base} not ready:\n{log.read_text()[-2000:]}")


async def main() -> int:
    root = Path(__file__).resolve().parent.parent
    tmp = Path(tempfile.mkdtemp(prefix="postbox_remote_spawn_"))
    p_lap = p_vm = lf_lap = lf_vm = None
    try:
        lap_port, vm_port = free_port(), free_port()
        # the vm launches `sleep` instead of copilot, and waits only briefly for "registration"
        p_lap, lf_lap, log_lap, lap = launch(root, tmp / "laptop", "laptop", lap_port, {})
        p_vm, lf_vm, log_vm, vm = launch(root, tmp / "vm", "vm", vm_port, {
            "POSTBOX_TERMINAL_CMD": "sleep 30", "POSTBOX_SPAWN_WAIT": "2"})
        await ready(lap, p_lap, log_lap)
        await ready(vm, p_vm, log_vm)

        async with httpx.AsyncClient(timeout=30.0) as c:
            # peer the two instances (shared token both ways)
            await c.post(f"{lap}/peers", json={"name": "vm", "url": vm, "token": TOKEN})
            await c.post(f"{vm}/peers", json={"name": "laptop", "url": lap, "token": TOKEN})
            # an agent on the laptop asks the vm to spawn `helper`
            caller = (await c.post(f"{lap}/agents", json={"name": "caller"})).json()
            h = {"Authorization": f"Bearer {caller['token']}"}
            r = await c.post(f"{lap}/spawn", headers=h,
                             json={"name": NAME, "instance": "vm"})
            check("laptop /spawn(instance=vm) returned 201", r.status_code == 201)
            res = r.json()
            check("addressable as helper@vm", res.get("address") == f"{NAME}@vm")
            check("reported instance == vm", res.get("instance") == "vm")
            check("session name is postbox_helper", res.get("session") == f"postbox_{NAME}")

            # the tmux session was really created ON THE VM host (same machine here)
            ls = subprocess.run(["tmux", "ls"], capture_output=True, text=True)
            check("vm actually created tmux session postbox_helper",
                  f"postbox_{NAME}:" in ls.stdout)
            # and the vm lists it via its own /terminals
            vm_terms = (await c.get(f"{vm}/terminals")).json()
            check("vm /terminals lists helper", any(t["name"] == NAME for t in vm_terms))

            # unknown instance is a clean 404 (not a silent success)
            r404 = await c.post(f"{lap}/spawn", headers=h,
                                json={"name": "x", "instance": "ghost"})
            check("unknown instance → 404", r404.status_code == 404)

        print("\nALL CHECKS PASSED — cross-instance remote spawn works.")
        return 0
    finally:
        subprocess.run(["tmux", "kill-session", "-t", f"postbox_{NAME}"],
                       capture_output=True)
        terminate(p_lap, lf_lap)
        terminate(p_vm, lf_vm)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
