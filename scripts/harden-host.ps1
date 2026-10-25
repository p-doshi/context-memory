<#
.SYNOPSIS
  Host firewall hardening for the Context Memory hub. Run once from an
  elevated PowerShell. Every change is listed below and undone by -Remove.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\harden-host.ps1
  powershell -ExecutionPolicy Bypass -File scripts\harden-host.ps1 -Remove

.NOTES
  Rule 1 "Block context-memory hub inbound": blocks inbound TCP to the hub
  port. The hub binds 127.0.0.1 only; this rule still holds if that setting
  is ever changed by mistake.

  Rule 2 "Block context-memory interpreter outbound": blocks the harness's
  Python interpreter from connecting to any non-loopback address, so nothing
  the harness runs can send data off the machine. The remote ranges are
  listed explicitly (everything except 127.0.0.0/8 and ::1), so the rule does
  not depend on how Windows treats loopback. Unaffected: the hub itself,
  SSH tunnels (ssh.exe), browser syncs (chrome.exe), git and uv.
  The rule names the base interpreter from .venv\pyvenv.cfg, because that is
  the process that opens sockets; any other project using the same
  uv-managed Python build is blocked too. Remove the rule before running
  anything that needs the network from that interpreter (for example a
  model download with CMH_ALLOW_MODEL_DOWNLOAD=1), then run this script again.

  The script also reports disk encryption status (read-only) and ends with a
  self-test: loopback to the hub port must work and a connection to the LAN
  default gateway must be refused by the firewall (WinError 10013). No
  outside host is contacted.
#>
param(
    [switch]$Remove,
    [int]$HubPort = 8765
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$InboundName = "Block context-memory hub inbound"
$OutboundName = "Block context-memory interpreter outbound"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "run this from an elevated PowerShell (Run as administrator)"
}

foreach ($name in $InboundName, $OutboundName) {
    Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}
if ($Remove) {
    Write-Output "removed: $InboundName; $OutboundName"
    return
}

# The interpreters: venv launchers plus the base build they start.
$cfg = Get-Content (Join-Path $Repo ".venv\pyvenv.cfg")
$base = (($cfg | Where-Object { $_ -match '^\s*home\s*=' }) -replace '^\s*home\s*=\s*', '').Trim()
$programs = @(
    (Join-Path $Repo ".venv\Scripts\python.exe"), (Join-Path $Repo ".venv\Scripts\pythonw.exe"),
    (Join-Path $base "python.exe"), (Join-Path $base "pythonw.exe")
) | Where-Object { Test-Path $_ }

New-NetFirewallRule -DisplayName $InboundName -Direction Inbound -Protocol TCP -LocalPort $HubPort `
    -Action Block -Profile Any | Out-Null
Write-Output "added: $InboundName (TCP $HubPort)"

$nonLoopback = @("0.0.0.0-126.255.255.255", "128.0.0.0-255.255.255.255",
                 "::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff")
foreach ($p in $programs) {
    New-NetFirewallRule -DisplayName $OutboundName -Direction Outbound -Program $p `
        -RemoteAddress $nonLoopback -Action Block -Profile Any | Out-Null
    Write-Output "added: $OutboundName ($p)"
}

# Disk encryption: report only.
$bde = & manage-bde -status C: 2>&1 | Select-String -Pattern "Protection Status|Conversion Status|Encryption Method"
if ($bde) { $bde | ForEach-Object { Write-Output ("C: " + $_.Line.Trim()) } }
else { Write-Output "C: encryption status unavailable (manage-bde missing; check Settings > Privacy & security > Device encryption)" }

# Self-test with the base interpreter.
$gateway = (Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
            Sort-Object RouteMetric | Select-Object -First 1).NextHop
$probe = @"
import socket, sys
def try_(host, port):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host, port)); return "connected"
    except OSError as e:
        return f"error {getattr(e, 'winerror', None) or e.errno}"
    finally:
        s.close()
print("loopback", try_("127.0.0.1", int(sys.argv[1])))
if sys.argv[2]:
    print("gateway", try_(sys.argv[2], 80))
"@
$py = Join-Path $base "python.exe"
$out = & $py -I -c $probe $HubPort "$gateway"
$out | ForEach-Object { Write-Output "self-test: $_" }
if ($out -match "gateway error 10013") { Write-Output "self-test: PASS (outbound blocked by firewall)" }
elseif (-not $gateway) { Write-Output "self-test: no default gateway (offline?); rerun when connected" }
else { Write-Output "self-test: CHECK (expected 'gateway error 10013')" }
