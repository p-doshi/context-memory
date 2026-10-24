<#
.SYNOPSIS
  Registers the Context Memory daemon as a per-user scheduled task that starts
  at logon and restarts on failure. Optionally keeps the laptop awake on AC.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-daemon.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install-daemon.ps1 -KeepAwakeOnAC
  powershell -ExecutionPolicy Bypass -File scripts\install-daemon.ps1 -Uninstall

.NOTES
  The task runs only while the user is logged in (a locked screen is fine).
  -KeepAwakeOnAC sets sleep and hibernate to "never" on AC power and the
  lid-close action on AC to "do nothing". Battery behaviour is unchanged.
  Revert with: powercfg /restoredefaultschemes
#>
param(
    [switch]$KeepAwakeOnAC,
    [switch]$Uninstall,
    [string]$TaskName = "ContextMemoryDaemon"
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Repo ".venv\Scripts\pythonw.exe"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "removed scheduled task $TaskName"
    return
}

if (-not (Test-Path $Pythonw)) {
    throw "missing $Pythonw; run 'uv sync --locked --extra embed' in $Repo first"
}

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m context_memory daemon" -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "registered and started $TaskName"

if ($KeepAwakeOnAC) {
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
    powercfg /setactive SCHEME_CURRENT
    Write-Output "AC power: sleep=never, hibernate=never, lid close=do nothing"
}
