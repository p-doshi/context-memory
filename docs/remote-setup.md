# Using the memory from other machines (SSH only)

The always-on laptop is the **hub**. Claude Code on other machines reads from it
(MCP over HTTP: `memory_search`, `graph_neighbors`, `memory_write`, `memory_stats`)
and pushes finished session transcripts into it. All of it travels inside SSH
connections you already use. No relay service, VPN or third party is involved, and
nothing is installed on the remote servers: they need only their existing sshd,
Python 3.8+ (standard library) and Claude Code.

```text
hub laptop                                   lab server (server*)
 cmh daemon                                   Claude Code
  ├─ hub on 127.0.0.1:8765 only                ├─ MCP → http://127.0.0.1:8765/mcp
  └─ ssh -N -R 127.0.0.1:8765:127.0.0.1:8765 ──►└─ hooks → cmh_push.py → same port
     (one tunnel per lab server, kept alive by the daemon)

other laptop:  ssh -L 8765:127.0.0.1:8765 <lab server>   then use 127.0.0.1:8765 locally
Cluster (MFA):     ssh with RemoteForward 127.0.0.1:8765 127.0.0.1:8765 from a machine
               that already has the port (the hub, or the other laptop with its -L)
```

The hub never listens on a network interface. Each lab server exposes the port on
its own loopback only.

## 1. On the hub

```powershell
setx CMH_HUB_ENABLED true
setx CMH_HUB_TUNNELS "[\"server5.example.org\"]"   # JSON list of ~/.ssh/config hosts
```

Tunnels run with `BatchMode=yes`: key-based login must work without a prompt
(an unencrypted key, or a key loaded in the Windows ssh-agent service). Restart the
daemon after changing these. `cmh status` shows each tunnel's state.

Register machines (token shown once; copy it to that machine):

```sh
uv run cmh device add server5 --account claude-personal
uv run cmh device add cluster --account claude-personal
uv run cmh device add laptop2 --account claude-personal
uv run cmh device revoke <name>    # lost machine or leaked token
```

One tunnel to one lab server is enough if the other lab servers can reach it; a
second tunnel adds redundancy. To reach the hub from server1/6/8 through the server
that has the tunnel: `ssh -N -L 8765:127.0.0.1:8765 server5`.

## 2. Other laptop

```sh
ssh -N -L 8765:127.0.0.1:8765 server5.example.org   # keep open while working
```

## 3. Clusters with multifactor login

Unattended tunnels cannot pass multifactor authentication, so the hub does not
connect to the cluster. Instead, whenever you SSH into it from a machine that has the hub
port (the hub itself, or the other laptop with the `-L` above):

```sshconfig
Host cluster
    HostName login.cluster.example.org
    RemoteForward 127.0.0.1:8765 127.0.0.1:8765
    ExitOnForwardFailure no
```

The port exists on that login node while your SSH session is open. Sessions run
while it is closed queue their uploads and send them next time. Check the cluster's
policy on port forwarding before relying on it.

## 4. On each machine that runs Claude Code

```sh
mkdir -p ~/.config/cmh ~/.local/bin
cp cmh_push.py ~/.local/bin/          # client/cmh_push.py from this repository
printf '%s' '<device token>' > ~/.config/cmh/token && chmod 600 ~/.config/cmh/token
echo '{"hub_url": "http://127.0.0.1:8765"}' > ~/.config/cmh/push.json
python3 ~/.local/bin/cmh_push.py status        # expect: hub: 200 {... "device": ...}

claude mcp add --transport http --scope user context-memory http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $(cat ~/.config/cmh/token)"
```

Hooks in `~/.claude/settings.json` (merge with existing `hooks`):

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

`hook-end` queues the session and starts a detached upload, returning well within
SessionEnd's 1.5-second default budget. `hook-start` sends anything missed
(SessionEnd is not documented to fire on crashes or dropped connections). Claude
Code withholds hooks until you accept the folder's workspace trust dialog.

## Security notes

- Transport: SSH end to end. The HTTP inside the tunnel is unencrypted only on the
  two loopback interfaces.
- On a shared server, other users can connect to the loopback port but get 401
  without your token; keep the token file `chmod 600`. Root on that server can read
  anything there, including your Claude Code transcripts, regardless of this setup.
- Uploads are redacted by the hub's secret scanner before indexing; the raw file is
  kept on the hub only.
- What each party sees: [privacy-inventory.md](privacy-inventory.md).

## Known gaps

- Subagent transcripts are not pushed yet.
- Claude Code deletes local transcripts after `cleanupPeriodDays` (default 30).
- If the hub is off, sessions elsewhere run without memory; uploads wait in the queue.
