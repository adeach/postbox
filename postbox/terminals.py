import asyncio
import os
import re
import time

# tmux session names can't contain '.' or ':'; keep it tight and also safe to reuse
# verbatim as the POSTBOX_NAME. Validated at the trust boundary (this spawns processes).
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")
SESSION = "postbox"     # ONE tmux session per host; each spawned agent is a WINDOW in it, so a
                        # whole team lives under `tmux attach -t postbox` instead of N sessions.


class TerminalService:
    """Spawn INTERACTIVE copilot sessions from the UI/API, in detached tmux sessions
    the human attaches to. A web server has no TTY of its own, so tmux is the bridge:
    the pane's pty lets copilot run, and you `tmux attach` to interact. Running inside
    tmux also gives the agent its real-time mail poke + a resumable session id for free.

    The launched program and the subprocess runner are injectable seams for tests, so
    the tmux plumbing can be exercised without a real `copilot`.
    """

    def __init__(self, settings, agents, runner=None, program=None):
        self.s = settings
        self.agents = agents
        self._run = runner or self._run_tmux
        # ponytail: --allow-all = spawned workers run FULLY unattended — tools + file paths + urls.
        # --allow-all-tools alone still stalls on copilot's "Allow directory access" prompt the first
        # time a worker touches a path outside its cwd (e.g. /tmp, another repo), which is invisible
        # to the parent and kills long-running tasks. Scope down via terminal_cmd config if you want
        # narrower. --allow-all-mcp-server-instructions delivers the postbox collab instructions.
        self.program = list(program) if program else [
            "copilot", "--allow-all", "--allow-all-mcp-server-instructions"]
        self.spawn_wait = 25.0        # seconds to wait for a spawned agent to register

    async def _run_tmux(self, argv: list[str]) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        return proc.returncode, out.decode(errors="replace")

    def _attach_cmd(self, window: str) -> str:
        # attach to the shared session and focus this agent's window
        return f"tmux attach -t {SESSION} \\; select-window -t {window}"

    async def _session_exists(self) -> bool:
        return (await self._run(["tmux", "has-session", "-t", SESSION]))[0] == 0

    async def spawn(self, name: str, cwd: str | None = None,
                    model: str | None = None) -> dict:
        if not NAME_RE.match(name or ""):
            raise ValueError("name must be 1–40 chars: letters, digits, '_' or '-'")
        if cwd is not None and not os.path.isdir(cwd):
            raise ValueError(f"cwd is not a directory: {cwd}")
        if model is not None and not MODEL_RE.match(model):
            raise ValueError("model must be a plain model id (letters, digits, '.', '_', '-')")
        # a row by this name (even a 'forgotten'/offline one) still holds the address,
        # so the spawned copilot's registration would 409. Reject up front, clearly.
        existing = await self.agents.get_by_address(name)
        if existing is not None:
            raise ValueError(
                f"'{name}' is already taken — pick another name "
                "(a forgotten or offline agent still reserves its name)")

        session = SESSION
        # point the spawned copilot's MCP back at THIS server (so it registers here
        # regardless of the global mcp-config), and pre-name it. `env` sets the vars as
        # an arg-list (no shell) → injection-safe and portable across tmux versions.
        url = f"http://127.0.0.1:{self.s.port}"
        # per-agent model: insert `--model X` right after the launcher binary so it
        # overrides the model for THIS worker (e.g. give the reviewer a different model).
        program = list(self.program)
        if model:
            program = [program[0], "--model", model, *program[1:]]
        # one shared session per host: the FIRST agent creates it, the rest are added as
        # windows — so a whole team is `tmux attach -t postbox` + switch windows, not N sessions.
        if await self._session_exists():
            argv = ["tmux", "new-window", "-t", session, "-n", name]
        else:
            argv = ["tmux", "new-session", "-d", "-s", session, "-n", name]
        if cwd:
            argv += ["-c", cwd]
        argv += ["env", f"POSTBOX_NAME={name}", f"POSTBOX_URL={url}", *program]
        rc, out = await self._run(argv)
        if rc != 0:
            raise RuntimeError(f"tmux failed to start the agent window: {out.strip() or rc}")
        return {"name": name, "session": session, "window": name,
                "attach": self._attach_cmd(name)}

    async def list_terminals(self) -> list[dict]:
        # windows in the shared session ARE the agents (the session is the namespace)
        rc, out = await self._run(
            ["tmux", "list-windows", "-t", SESSION, "-F", "#{window_name}"])
        if rc != 0:      # the shared session doesn't exist yet → none
            return []
        names = [ln for ln in out.splitlines() if ln.strip()]
        return [{"name": n, "session": SESSION, "window": n,
                 "attach": self._attach_cmd(n)} for n in sorted(names)]

    async def kill(self, name: str) -> None:
        if not NAME_RE.match(name or ""):
            raise ValueError("bad terminal name")
        rc, out = await self._run(["tmux", "kill-window", "-t", f"{SESSION}:{name}"])
        if rc != 0:
            raise RuntimeError(f"tmux kill-window failed: {out.strip() or rc}")

    async def wait_registered(self, name: str, timeout: float | None = None) -> bool:
        """Poll until the freshly-spawned copilot has registered its identity (so the
        caller can message it immediately instead of hitting 'unknown recipient').
        The spawn pre-check guarantees no row existed, so any appearance is the new one."""
        deadline = time.monotonic() + (self.spawn_wait if timeout is None else timeout)
        while time.monotonic() < deadline:
            if await self.agents.get_by_address(name) is not None:
                return True
            await asyncio.sleep(0.5)
        return False
