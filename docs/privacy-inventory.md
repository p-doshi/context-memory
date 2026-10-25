# Data-flow and third-party inventory

Last checked: 2026-10-24, by code inspection and live network measurement on the
dev laptop. Re-check after dependency upgrades (`uv lock` changes).

## Where the memory lives

| Data | Location | Leaves the machine? |
|---|---|---|
| SQLite metadata, chunk text, graph, audit log | `<home>/data/metadata.sqlite3` | No |
| LanceDB vectors | `<home>/data/lance/` | No |
| Raw uploads from other machines | `<home>/inbox/remote/<device>/` | No |
| Daily snapshots of all of the above (7 kept) | `<home>/backup/snapshot-*/` | No; browser profiles and device tokens are excluded |
| Browser profiles (session cookies) | `<home>/browser/<account>/` | See Google, below |
| Device tokens | SHA-256 only, in SQLite | No |
| API keys | OS keychain (`cmh secret set`) | No |

Data at rest is protected only by the OS account and disk encryption, if enabled.
Application-level encryption is roadmap Week 6.

## Parties that receive content

| Party | What they receive | Why | Control |
|---|---|---|---|
| **Anthropic** | Everything a Claude Code session sees, on every machine, including `memory_search` results returned to it. claude.ai chats are already stored there; the sync only reads them back. | This is how Claude Code and claude.ai work. | Disclosure Gate withholds contact, precise location, financial and credential attributes from MCP results. It does **not** withhold research content. `CMH_CLOUD_DENIED_PROJECTS` withholds whole projects from MCP results. A local model (roadmap) is the only way to keep a project entirely off the cloud. |
| **Perplexity** | Its threads are already stored there; the sync reads them back. | Source of the data. | None needed. |
| **GitHub (Microsoft)** | This repository: code, design documents, benchmark results. | Version control (private repository). | A pre-commit guard (`scripts/check_private.py`) blocks commits containing indexed document titles or account emails. Result files record query ids and metrics only. |

## Parties that receive metadata only

| Party | What | Status |
|---|---|---|
| Cloudflare | Browser fingerprint when claude.ai shows its access challenge | Allowed: it is claude.ai's own gate. The sync does not try to pass challenges. |
| Perplexity's vendors: Datadog, Cloudflare Insights, Singular, Eppo, Cloudflare Images | Page telemetry from the site's own scripts | **Blocked** in the sync browser (verified: library still loads). Still apply when you browse Perplexity yourself. |
| Google | Chrome is Google software: update checks, Safe Browsing. A harness profile signed in to Chrome can sync profile data to the Google account. | Harness profiles should stay signed out of Chrome (sign-in to claude.ai/Perplexity with Google does not require Chrome sign-in). |
| Hugging Face | IP address, model name, library versions on each model load | **Off by default** (`HF_HUB_OFFLINE=1`, telemetry disabled). Measured: zero network calls during model load and search. `CMH_ALLOW_MODEL_DOWNLOAD=1` only to fetch a model. |
| PyPI, download.pytorch.org, GitHub releases (Python builds via uv) | Package downloads at install time | No user data. |

## Checked and not sending anything

| Component | Evidence |
|---|---|
| LanceDB | OpenTelemetry bridge is opt-in and no OpenTelemetry SDK/exporter is installed. Its compiled core contains a LanceDB Cloud endpoint, used only for `db://` URIs; this project opens a local folder. OS-level check: no non-loopback connections during searches. |
| mcp, starlette | OpenTelemetry API hooks only; no exporter installed, so spans go nowhere. |
| sentence-transformers, transformers, huggingface_hub | No call to `send_telemetry` from installed code; network calls only for model fetches, disabled by offline mode. |
| keyring, uvicorn, pydantic-settings, httpx | No telemetry code found. |

Not verified: Playwright's Node driver (no telemetry known to me, not inspected),
connections opened and closed between the OS-level snapshots (firewall logging
would settle this), and OS-level software on the host (antivirus, vendor support
agents), which is outside this project.

## Other machines

Remote machines reach the hub only through SSH (see [remote-setup.md](remote-setup.md)).
No third party is in the path. On a shared server, other users of that server can
connect to the forwarded loopback port but cannot use it without the device token;
that server's administrators can read anything on it regardless.

## Not used

Tailscale or any other relay or VPN service. Cloud vector databases. Hosted
embedding APIs.
