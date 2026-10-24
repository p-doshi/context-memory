"""SSH reverse tunnels from the hub to machines that should reach it.

For each host alias in settings.hub_tunnels the daemon keeps

    ssh -N -R 127.0.0.1:<port>:127.0.0.1:<port> <host>

running, so on that host http://127.0.0.1:<port> leads back to the hub. Nothing
is installed on the remote host (it only needs its existing sshd with TCP
forwarding allowed), no third party is involved, and the hub itself listens on
127.0.0.1 only.

ssh runs in BatchMode: it uses the keys/agent already configured in
~/.ssh/config and never prompts. Hosts that require an interactive second
factor (e.g. many HPC clusters) cannot be tunnelled this way.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time

from context_memory.util import NO_WINDOW

log = logging.getLogger("cmh.tunnels")


def ssh_command(host: str, port: int) -> list[str]:
    ssh = shutil.which("ssh") or "ssh"
    return [
        ssh, "-N", "-T",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        # Remote side binds loopback only, never the server's network interfaces.
        "-R", f"127.0.0.1:{port}:127.0.0.1:{port}",
        host,
    ]


class Tunnel(threading.Thread):
    def __init__(self, host: str, port: int, stop: threading.Event):
        super().__init__(name=f"tunnel-{host}", daemon=True)
        self.host, self.port, self.stop_event = host, port, stop
        self.state = "starting"
        self.since = time.time()

    def run(self) -> None:
        backoff = 5
        while not self.stop_event.is_set():
            started = time.time()
            proc = subprocess.Popen(ssh_command(self.host, self.port), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                    creationflags=NO_WINDOW)
            self.state, self.since = "up", time.time()
            log.info("tunnel to %s started (pid %d)", self.host, proc.pid)
            while proc.poll() is None and not self.stop_event.is_set():
                time.sleep(1)
            if proc.poll() is None:
                proc.terminate()
            err = (proc.stderr.read() if proc.stderr else "").strip().splitlines()[-1:] or [""]
            self.state, self.since = "down", time.time()
            log.warning("tunnel to %s exited (%s): %s", self.host, proc.returncode, err[0][:200])
            # Reset backoff after a tunnel that stayed up a while; grow it on fast failures.
            backoff = 5 if time.time() - started > 300 else min(backoff * 2, 600)
            self.stop_event.wait(backoff)


def kill_stale(hosts: list[str], port: int) -> int:
    """Terminate tunnel processes left by a previous daemon. If the daemon dies
    abruptly its ssh child keeps running and holds the remote port, so every new
    tunnel fails ("remote port forwarding failed"). Only one daemon runs at a
    time (lock file), so any existing process with our exact forward is stale."""
    killed = 0
    forward = f"-R 127.0.0.1:{port}:127.0.0.1:{port}"
    for host in hosts:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | Where-Object { "
                  f"$_.CommandLine -like '*{forward} {host}' }} | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                                 text=True, timeout=30, creationflags=NO_WINDOW).stdout
        else:
            out = subprocess.run(["pkill", "-e", "-f", f"ssh .*{forward} {host}$"],
                                 capture_output=True, text=True, timeout=30).stdout
        n = len([l for l in out.splitlines() if l.strip()])
        if n:
            log.warning("killed %d stale tunnel process(es) for %s", n, host)
        killed += n
    return killed


def start(hosts: list[str], port: int, stop: threading.Event) -> list[Tunnel]:
    kill_stale(hosts, port)
    tunnels = [Tunnel(h, port, stop) for h in hosts]
    for t in tunnels:
        t.start()
    return tunnels
