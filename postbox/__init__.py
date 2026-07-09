"""Postbox — local mailbox for AI agents.

Single source of truth for the running server version. Bump this when you cut a
new version; mutagen syncs this file to the VM, so a restart on each host makes
`GET /health` report the same version — that's how you check laptop/VM are in sync.
"""

__version__ = "0.2.0"
