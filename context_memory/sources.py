"""Sources config: what the daemon indexes, and the account registry.

The file is owner-authored and lives outside the repository (default
<data_dir>/../config/sources.toml). Paths listed in it are added to the
ingest allowlist for the daemon. It holds no secrets: API keys go in the OS
keychain through `cmh secret set`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from context_memory.util import validate_project

PROVIDERS = {"claude", "perplexity", "chatgpt", "openrouter", "cursor"}


@dataclass
class Account:
    label: str
    provider: str
    email: str | None = None
    plan: str | None = None
    claude_config_dir: Path | None = None
    notes: str | None = None


@dataclass
class PathSource:
    path: Path
    project: str
    max_files: int = 2000


@dataclass
class Sources:
    accounts: list[Account] = field(default_factory=list)
    codebases: list[PathSource] = field(default_factory=list)
    docs: list[PathSource] = field(default_factory=list)
    claude_project_map: dict[str, str] = field(default_factory=dict)

    def roots(self) -> list[Path]:
        return [s.path for s in self.codebases + self.docs]


def load_sources(path: Path) -> Sources:
    if not path.exists():
        return Sources()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    accounts = []
    for a in data.get("accounts", []):
        if a.get("provider") not in PROVIDERS:
            raise ValueError(f"account {a.get('label')!r}: unknown provider {a.get('provider')!r}")
        accounts.append(Account(
            label=a["label"], provider=a["provider"], email=a.get("email"), plan=a.get("plan"),
            claude_config_dir=Path(a["claude_config_dir"]).expanduser()
            if a.get("claude_config_dir") else None,
            notes=a.get("notes"),
        ))
    labels = [a.label for a in accounts]
    if len(labels) != len(set(labels)):
        raise ValueError("account labels must be unique")

    def paths(key: str) -> list[PathSource]:
        out = []
        for e in data.get(key, []):
            out.append(PathSource(Path(e["path"]).expanduser(), validate_project(e["project"]),
                                  int(e.get("max_files", 2000))))
        return out

    project_map = data.get("claude_code", {}).get("project_map", {})
    for v in project_map.values():
        validate_project(v)
    return Sources(accounts, paths("codebase"), paths("docs"), project_map)
