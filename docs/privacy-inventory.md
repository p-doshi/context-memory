# Data-flow and third-party inventory

Last checked: 2026-10-25, by code inspection and live network measurement on the
dev laptop. Re-check after dependency upgrades (`uv lock` changes).

## Where the memory lives

| Data | Location | Leaves the machine? |
|---|---|---|
| SQLite metadata, chunk text, graph, audit log | `<home>/data/metadata.sqlite3` | No |
| LanceDB vectors | `<home>/data/lance/` | No |
| Report digests (short notes per conversation, written by the local model on loopback) | `digests` table in the same SQLite file | No |
| Raw uploads from other machines | `<home>/inbox/remote/<device>/` | No |
| Source code from other machines' repositories (tracked source and docs only, changed files, sent by the session-end hook over the SSH link; data folders, key/env files, notebook outputs and files over 500 KB are excluded on the sending machine) | `<home>/inbox/remote/<device>/_code/` until indexed, then the store | No |
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
| Tailscale (client 1.104.2 on the laptop only; Funnel for the claude.ai connector) | Connection metadata: that Anthropic's servers connect to the laptop, when, and how many bytes. TLS ends on the laptop, so Tailscale's relays carry ciphertext only (Tailscale docs: relays "do not decrypt the traffic"). Client diagnostic logging is turned off (TS_NO_LOGS_NO_SUPPORT=true). | Active since 2026-10-26. The connector accepts only Anthropic's outbound range 160.79.104.0/21 (address taken from Tailscale's X-Forwarded-For, which replaces any client-supplied value: tested), a read-only token, and the Disclosure Gate. Off switch: `cmh connector off` or `tailscale funnel off`. |
| Cloudflare | Browser fingerprint when claude.ai shows its access challenge | Allowed: it is claude.ai's own gate. The sync does not try to pass challenges. |
| Perplexity's vendors: Datadog, Cloudflare Insights, Singular, Eppo, Cloudflare Images | Page telemetry from the site's own scripts | **Blocked** in the sync browser (verified: library still loads). Still apply when you browse Perplexity yourself. |
| Google | Chrome is Google software: update checks, Safe Browsing. A harness profile signed in to Chrome can sync profile data to the Google account. | Harness profiles should stay signed out of Chrome (sign-in to claude.ai/Perplexity with Google does not require Chrome sign-in). |
| Hugging Face | IP address, model name, library versions on each model load | **Off by default** (`HF_HUB_OFFLINE=1`, telemetry disabled). Measured: zero network calls during model load and search. `CMH_ALLOW_MODEL_DOWNLOAD=1` only to fetch a model. |
| PyPI, download.pytorch.org, GitHub releases (Python builds via uv) | Package downloads at install time (includes pypdf 6.21.2, added 2026-10-25 to read PDFs in claude.ai Project exports) | No user data. PDFs are read locally, inside the zip. |
| GitHub releases (llama.cpp build b11875), Hugging Face (Qwen/Qwen3-1.7B-GGUF at commit 90862c4b) | One-time downloads of the visitor answer model and its server; SHA-256 checked against the published digests | No user data. The model runs offline on loopback. |
| Cloudflare (quick tunnel, cloudflared 2026.10.3, SHA-256 checked) | Visitor requests and responses for the public node pass through Cloudflare's edge in plain text there: public material and visitors' own questions only. No Cloudflare account; random trycloudflare.com address, no uptime guarantee, 200 concurrent requests, no server-sent events (Cloudflare's Quick Tunnels page). Cloudflare's edge also refuses known AI training crawlers (seen: ClaudeBot, GPTBot get 403; Claude-User, curl, httpx pass). | Active since 2026-10-25. Only port 8787 (the public node) is behind it; the private hub is never behind the tunnel. |
| GitHub (site repository, private) | The static site files and the current tunnel address, pushed by the daemon when either changes | Active. No memory content. |
| Vercel | Hosts the static site files from that repository; visitors' browsers then call the tunnel address directly | Active. No memory content. |
| Figma | Console design mock-ups, sent from the owner's Claude session (Figma MCP connector) to a file in the owner's Figma team | Design only. The mock-ups use invented sample data generated by a script; no memory content, titles, project names or counts are sent. |
| jsDelivr (npm @ibm/plex-sans 1.1.0, plex-mono 2.5.0, plex-serif 2.0.0) | One-time download of the IBM Plex font files, now committed under `web/fonts/` | No user data. The site and console serve the fonts themselves, so visitors' browsers contact no font CDN. |

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

## Surfaces served by the hub

The hub listens on 127.0.0.1 only. Every route except the static pages needs a device
token; tokens are stored as SHA-256 hashes and carry scopes.

| Surface | Scope | What it can return |
|---|---|---|
| MCP, `/v1/context`, `/v1/sessions` | `read`, `context`, `upload` | Memory through the Disclosure Gate as `cloud_model`; uploads are queued (202) and indexed by the daemon |
| `/console` + `/v1/console/*` | `admin` | Owner view: counts, graph projection, audit trail |
| `/visitor` + `/v1/public/*` | `public` only | Projects listed under `[public]`, kinds in `public.kinds` (chats excluded by default), gated as `public`, no local paths; 60 requests per minute per token. A public token cannot call any other route, and a normal token cannot call the public routes. |

Nothing is public until projects are listed under `[public]`. The visitor answerer is
extractive, so no language-model provider sees visitor questions.

## Host firewall

`scripts/harden-host.ps1` (run as administrator) adds an inbound block on the hub port and
an outbound block on the harness interpreter for every non-loopback address. With the
outbound rule in place, an indexing bug or a malicious dependency in the Python process
cannot send data off the machine; ssh.exe (tunnels) and chrome.exe (browser syncs) are
separate programs and unaffected. `cmh security-check` reports whether both rules exist.

## Other machines

Remote machines reach the hub only through SSH (see [remote-setup.md](remote-setup.md)).
No third party is in the path. On a shared server, other users of that server can
connect to the forwarded loopback port but cannot use it without the device token;
that server's administrators can read anything on it regardless.

## Not used

Any relay or VPN service for the private store's own links (other machines connect over
SSH only); Tailscale is used solely as the claude.ai connector's Funnel, above. Cloud
vector databases. Hosted embedding APIs.
