import asyncio
import os
import re

# tmux session names can't contain '.' or ':'; keep it tight and also safe to reuse
# verbatim as the POSTBOX_NAME. Validated at the trust boundary (this spawns processes).
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PREFIX = "postbox_"


class TerminalService:
    """Spawn INTERACTIVE copilot sessions from the UI/API, in detached tmux sessions
    the human attaches to. A web server has no TTY of its own, so tmux is the bridge:
    the pane's pty lets copilot run, and you `tmux attach` to interact. Running inside
    tmux also gives the agent its real-time mail poke + a resumable session id for free.

    The launched program and the subprocess runner are injectable seams for tests, so
    the tmux plumbing can be exercised without a real `copilot`.
    """

    def __init__(self, settings, agents, runner=None, program=("copilot", "--allow-tool=postbox")):
        self.s = settings
        self.agents = agents
        self._run = runner or self._run_tmux
        self.program = list(program)

    async def _run_tmux(self, argv: list[str]) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        return proc.returncode, out.decode(errors="replace")

    def _attach_cmd(self, session: str) -> str:
        return f"tmux attach -t {session}"

    async def spawn(self, name: str, cwd: str | None = None) -> dict:
        if not NAME_RE.match(name or ""):
            raise ValueError("name must be 1–40 chars: letters, digits, '_' or '-'")
        if cwd is not None and not os.path.isdir(cwd):
            raise ValueError(f"cwd is not a directory: {cwd}")
        # a live identity by this name would make the spawned agent's registration 409;
        # reject up front with a clear message (a 'forgotten' row is fine to reuse).
        existing = await self.agents.get_by_address(name)
        if existing is not None and existing.status != "deregistered":
            raise ValueError(
                f"'{name}' is already a registered agent — pick another name or forget it")

        session = PREFIX + name
        # point the spawned copilot's MCP back at THIS server (so it registers here
        # regardless of the global mcp-config), and pre-name it. `env` sets the vars as
        # an arg-list (no shell) → injection-safe and portable across tmux versions.
        url = f"http://127.0.0.1:{self.s.port}"
        argv = ["tmux", "new-session", "-d", "-s", session]
        if cwd:
            argv += ["-c", cwd]
        argv += ["env", f"POSTBOX_NAME={name}", f"POSTBOX_URL={url}", *self.program]
        rc, out = await self._run(argv)
        if rc != 0:
            raise RuntimeError(f"tmux failed to start the session: {out.strip() or rc}")
        return {"name": name, "session": session, "attach": self._attach_cmd(session)}

    async def list_terminals(self) -> list[dict]:
        rc, out = await self._run(
            ["tmux", "list-sessions", "-F", "#{session_name}"])
        if rc != 0:      # no tmux server running yet, or no sessions → none
            return []
        names = [ln[len(PREFIX):] for ln in out.splitlines() if ln.startswith(PREFIX)]
        return [{"name": n, "session": PREFIX + n,
                 "attach": self._attach_cmd(PREFIX + n)} for n in sorted(names)]

    async def kill(self, name: str) -> None:
        if not NAME_RE.match(name or ""):
            raise ValueError("bad terminal name")
        rc, out = await self._run(["tmux", "kill-session", "-t", PREFIX + name])
        if rc != 0:
            raise RuntimeError(f"tmux kill-session failed: {out.strip() or rc}")
