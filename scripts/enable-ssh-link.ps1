<#
.SYNOPSIS
  Lets another machine of yours on the same network link to the hub over SSH.
  Run from an elevated PowerShell. -Remove undoes it.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\enable-ssh-link.ps1 -PublicKey "ssh-ed25519 AAAA... cmh-link@laptop2"
  powershell -ExecutionPolicy Bypass -File scripts\enable-ssh-link.ps1 -Remove

.NOTES
  What it changes:
  - Installs the OpenSSH Server Windows feature (downloaded from Windows
    Update, i.e. Microsoft) and sets the sshd service to start automatically.
  - Creates a standard (non-admin) local account, cmhlink by default, with a
    random password nobody knows, hidden from the sign-in screen.
  - sshd_config (backed up first): password login off; only that account
    may log in over SSH; for it, only local forwarding to 127.0.0.1:<HubPort>,
    no terminal, and any command exits at once. Its single authorized key is
    the -PublicKey given here.
  - Firewall: the feature's own allow-all rule is disabled; SSH is allowed only
    on Private networks and only from the local subnet. Mark your home Wi-Fi as
    Private (Settings > Network & internet > Wi-Fi > <network> > Private).
  The other machine then runs client/hub-link.sh with the host key fingerprint
  printed at the end.
#>
param(
    [string]$PublicKey,
    [switch]$Remove,
    [string]$LinkUser = "cmhlink",
    [int]$HubPort = 8765
)
$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "run this from an elevated PowerShell (Run as administrator)"
}
$SshDir = Join-Path $env:ProgramData "ssh"
$Config = Join-Path $SshDir "sshd_config"
$Backup = "$Config.bak-cmh"
$KeyFile = Join-Path $SshDir "$($LinkUser)_authorized_keys"
$RuleName = "Allow context-memory SSH link (LAN)"
$HideKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList"
$Begin = "# cmh:begin"; $End = "# cmh:end"

function Strip-Cmh([string]$text) {
    return [regex]::Replace($text, "(?s)\r?\n?$([regex]::Escape($Begin)).*?$([regex]::Escape($End))\r?\n?", "`r`n")
}

if ($Remove) {
    Stop-Service sshd -ErrorAction SilentlyContinue
    Set-Service sshd -StartupType Disabled -ErrorAction SilentlyContinue
    Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    if (Test-Path $Config) { Set-Content $Config (Strip-Cmh (Get-Content $Config -Raw)) -Encoding ascii }
    Remove-Item $KeyFile -ErrorAction SilentlyContinue
    if (Get-LocalUser $LinkUser -ErrorAction SilentlyContinue) { Remove-LocalUser $LinkUser }
    Remove-ItemProperty $HideKey -Name $LinkUser -ErrorAction SilentlyContinue
    Write-Output "SSH link removed: sshd stopped and disabled, account, key, rule and config block deleted"
    return
}

if ($PublicKey -notmatch '^ssh-ed25519 [A-Za-z0-9+/=]+( \S.*)?$') {
    throw "-PublicKey must be one ssh-ed25519 public key line (from 'hub-link.sh key' on the other machine)"
}

# 1. OpenSSH Server feature and first start (which writes sshd_config and host keys).
$cap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" | Select-Object -First 1
if ($cap.State -ne "Installed") {
    Write-Output "installing OpenSSH Server (from Windows Update)..."
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
}
if (-not (Test-Path $Config)) { Start-Service sshd; Start-Sleep 2 }
Stop-Service sshd -ErrorAction SilentlyContinue

# 2. Link account: standard user, unknown random password, hidden from sign-in.
if (-not (Get-LocalUser $LinkUser -ErrorAction SilentlyContinue)) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $pw = ConvertTo-SecureString ([Convert]::ToBase64String($bytes) + "aA1!") -AsPlainText -Force
    New-LocalUser -Name $LinkUser -Password $pw -PasswordNeverExpires -UserMayNotChangePassword `
        -AccountNeverExpires -Description "context-memory SSH link (port forwarding only)" | Out-Null
}
New-Item -Path $HideKey -Force | Out-Null
New-ItemProperty -Path $HideKey -Name $LinkUser -Value 0 -PropertyType DWord -Force | Out-Null

# 3. Its one key: forwarding to the hub port only; any command exits.
$opts = "restrict,port-forwarding,permitopen=`"127.0.0.1:$HubPort`",command=`"exit`""
Set-Content -Path $KeyFile -Value "$opts $PublicKey" -Encoding ascii
& icacls $KeyFile /inheritance:r /grant "SYSTEM:F" /grant "Administrators:F" /grant "$($LinkUser):R" | Out-Null

# 4. sshd_config: global lines first (before any Match), our Match block last.
if (-not (Test-Path $Backup)) { Copy-Item $Config $Backup }
$text = Strip-Cmh (Get-Content $Config -Raw)
$global = "$Begin`r`nPasswordAuthentication no`r`nPubkeyAuthentication yes`r`nAllowUsers $LinkUser`r`n$End`r`n"
$match = "`r`n$Begin`r`nMatch User $LinkUser`r`n    AuthorizedKeysFile __PROGRAMDATA__/ssh/$($LinkUser)_authorized_keys`r`n" +
         "    AllowTcpForwarding local`r`n    PermitOpen 127.0.0.1:$HubPort`r`n    PermitTTY no`r`n" +
         "    X11Forwarding no`r`n    AllowAgentForwarding no`r`n    GatewayPorts no`r`n$End`r`n"
Set-Content -Path $Config -Value ($global + $text.TrimEnd() + "`r`n" + $match) -Encoding ascii
$sshd = Join-Path $env:WINDIR "System32\OpenSSH\sshd.exe"
& $sshd -t -f $Config
if ($LASTEXITCODE -ne 0) {
    Copy-Item $Backup $Config -Force
    throw "sshd rejected the new config; original restored"
}

# 5. Firewall: LAN only, Private profile only.
Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue | Disable-NetFirewallRule
Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort 22 `
    -Program $sshd -RemoteAddress LocalSubnet -Profile Private -Action Allow | Out-Null

Set-Service sshd -StartupType Automatic
Start-Service sshd

# 6. What the other machine needs.
$fp = (& (Join-Path $env:WINDIR "System32\OpenSSH\ssh-keygen.exe") -lf (Join-Path $SshDir "ssh_host_ed25519_key.pub")).Split(" ")[1]
$private = Get-NetConnectionProfile | Where-Object NetworkCategory -eq "Private"
$ips = $private | ForEach-Object { (Get-NetIPAddress -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress }
Write-Output "sshd running; only '$LinkUser' may log in, key only, forwarding to 127.0.0.1:$HubPort only"
Write-Output "host: $($env:COMPUTERNAME.ToLower()).local  addresses on Private networks: $($ips -join ', ')"
Write-Output "host key fingerprint: $fp"
if (-not $private) { Write-Output "WARNING: no network is marked Private, so the firewall rule applies nowhere yet" }
Write-Output "on the other machine: bash hub-link.sh install <host or address> $fp"
