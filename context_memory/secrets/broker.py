"""Secret broker backed by the OS keychain (Windows Credential Manager,
macOS Keychain, Secret Service on Linux) via `keyring`.

Secrets never enter SQLite, LanceDB, logs, or model context. Values are set
interactively by the owner and read only by harness code that needs them;
there is deliberately no MCP tool that returns a secret value yet.
"""

from __future__ import annotations

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "context-memory"
INDEX_KEY = "__index__"


def _index() -> list[str]:
    raw = keyring.get_password(SERVICE, INDEX_KEY) or ""
    return [n for n in raw.split(",") if n]


def set_secret(name: str, value: str) -> None:
    if not name or "," in name or name == INDEX_KEY:
        raise ValueError(f"invalid secret name {name!r}")
    keyring.set_password(SERVICE, name, value)
    names = _index()
    if name not in names:
        keyring.set_password(SERVICE, INDEX_KEY, ",".join(sorted(names + [name])))


def get_secret(name: str) -> str | None:
    return keyring.get_password(SERVICE, name)


def delete_secret(name: str) -> bool:
    try:
        keyring.delete_password(SERVICE, name)
    except PasswordDeleteError:
        return False
    keyring.set_password(SERVICE, INDEX_KEY, ",".join(n for n in _index() if n != name))
    return True


def list_secret_names() -> list[str]:
    return _index()
