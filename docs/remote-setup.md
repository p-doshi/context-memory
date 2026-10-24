# Using the memory from other machines

The laptop that runs the daemon is the **hub**. Claude Code on any other machine
can read from it (MCP over HTTP: `memory_search`, `graph_neighbors`, `memory_write`,
`memory_stats`) and push its session transcripts into it. Everything lands in the
same store, tagged with the machine and account.

```text
 cluster / server* / laptop2                     hub laptop (always on)
 ┌──────────────────────────┐                ┌──────────────────────────────┐
 │ Claude Code              │  MCP over HTTP │ cmh daemon                   │
 │  ├─ MCP: context-memory ─┼───────────────►│  ├─ hub :8765 (/mcp)         │
 │  └─ hooks → cmh_push.py ─┼── POST ───────►│  ├─ /v1/sessions (uploads)   │
 └──────────────────────────┘ private network│  └─ SQLite + LanceDB + graph │
                                              └──────────────────────────────┘
```

The hub never listens on a public interface (design §24). Remote machines reach it
over a private network: Tailscale where you can install it, an SSH port forward
where you cannot.

## 1. On the hub (once)

Turn the hub on (user environment variables, then restart the daemon):

```powershell
setx CMH_HUB_ENABLED true
setx CMH_HUB_HOST <bind address>   # Tailscale IP of this laptop, or 127.0.0.1 for SSH forwards only
setx CMH_HUB_PORT 8765
```

Register each machine and copy its token (shown once):

```sh
uv run cmh device add cluster --account claude-work
uv run cmh device add server5 --account claude-work
uv run cmh device add laptop2 --account claude-personal
uv run cmh device list
uv run cmh device revoke cluster        # if a machine or token is lost
```

## 2. Network path, per machine type

| Machine | Path | Notes |
|---|---|---|
| Your other laptop | Tailscale | Install the Tailscale client, same tailnet as the hub. Hub URL `http://<hub tailscale name>:8765`. |
| Lab servers (server1/5/6/8) | Tailscale if you have admin rights; otherwise SSH forward | Installing Tailscale needs root (or your lab admin's approval). |
| Clusters with multifactor login | SSH remote forward from whichever machine you connect from | Nothing to install on the cluster. See below. |

### SSH remote forward (clusters)

When you SSH into the cluster from a machine that can reach the hub (the hub itself,
or any machine on your tailnet), forward a port on the login node back to the hub:

```sshconfig
# ~/.ssh/config on the machine you connect FROM
Host cluster
    HostName login.cluster.example.org
    User <your username>
    RemoteForward 127.0.0.1:8765 <hub address>:8765   # <hub address> = 127.0.0.1 on the hub itself, else its Tailscale name
    ExitOnForwardFailure no
```

While that SSH session is open, Claude Code on that login node reaches the hub at
`http://127.0.0.1:8765`. Limits and cautions:

- It works only while your SSH session is open, and only on the login node it landed
  on. Sessions started when the forward is down queue their uploads and send them
  next time (the sweep at session start).
- A login node is shared: other users on it can connect to 127.0.0.1:8765. They
  cannot use it without your device token, so keep the token file `chmod 600`.
  If the port is already taken on that node, pick another and set `CMH_HUB_URL`.
- Check the cluster's policy before relying on this. Some HPC centres' documentation
  mentions dedicated nodes for automated SSH connections under multifactor
  authentication, and earlier revisions of the same page said automation had no
  solution yet; I could not confirm the current rule. An interactive forward that
  lives inside your own login session is the conservative choice.

## 3. On each remote machine

Requirements: Python 3.8+ (standard library only) and Claude Code.

```sh
mkdir -p ~/.config/cmh ~/.local/bin
cp cmh_push.py ~/.local/bin/ && chmod +x ~/.local/bin/cmh_push.py   # from client/ in this repo
printf '%s' '<token from cmh device add>' > ~/.config/cmh/token && chmod 600 ~/.config/cmh/token
cat > ~/.config/cmh/push.json <<'EOF'
{"hub_url": "http://127.0.0.1:8765"}
EOF
~/.local/bin/cmh_push.py status        # should print "hub: 200 {... device ...}"
```

Read memory from Claude Code (user scope = every project on this machine):

```sh
claude mcp add --transport http --scope user context-memory http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $(cat ~/.config/cmh/token)"
```

Push transcripts automatically: add to `~/.claude/settings.json`
(merge with any existing `hooks` block):

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python3 ~/.local/bin/cmh_push.py hook-start", "timeout": 5 } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "python3 ~/.local/bin/cmh_push.py hook-end", "timeout": 5 } ] }
    ]
  }
}
```

`hook-end` only queues the session and starts a detached upload, so it returns well
within SessionEnd's 1.5-second default budget. `hook-start` sweeps for transcripts
that were never sent (SessionEnd is not documented to fire on crashes or dropped SSH
connections) and retries failed uploads. Claude Code holds back hooks until you
accept the workspace trust dialog for a folder.

## What is sent, and where it goes

- The full transcript file of each finished session. On the hub, secrets matching
  the ingress scanner are replaced with `[REDACTED:<rule>]` before anything is
  stored; the raw upload is kept under `<hub home>/inbox/remote/<device>/` so it can
  be re-parsed when Claude Code's transcript format changes.
- Search results returned to remote Claude Code pass the Disclosure Gate as
  `cloud_model`, the same as on the hub.
- Every authentication failure, upload and search is in the hub's audit log
  (`cmh audit`).

## Known gaps

- Subagent transcripts (`projects/<project>/<session>/subagents/`) are not pushed yet.
- Claude Code deletes local transcripts after `cleanupPeriodDays` (default 30); a
  machine that cannot reach the hub for longer loses the unsent ones.
- If the hub laptop is off or asleep, remote sessions work normally but without
  memory; uploads wait in the local queue.
