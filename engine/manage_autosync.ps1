<#
Memory Suite AutoSync - register / unregister / status for the per-user scheduled task "MemorySuiteAutoSync".
The task runs   pythonw autosync.py once   every 1 minute as the current user. No admin rights, no stored password.
Usage (from this folder):   powershell -ExecutionPolicy Bypass -File manage_autosync.ps1 -Action register
#>
param(
    [ValidateSet('register','unregister','status')]
    [string]$Action = 'status',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'autosync.py'
$taskName = 'MemorySuiteAutoSync'

function Test-RealPython([string]$exe) {
    # The Microsoft Store "python.exe" alias in WindowsApps is only a stub; ask the interpreter itself.
    if (-not $exe) { return $null }
    try {
        $out = & $exe -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ([string]($out | Select-Object -First 1)).Trim() }
    } catch { }
    return $null
}

function Find-PythonW {
    $real = $null
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $out = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { $real = ([string]($out | Select-Object -First 1)).Trim() }
        } catch { }
    }
    if (-not $real) {
        $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($cmd) { $real = Test-RealPython $cmd.Source }
    }
    if ($real) {
        $candidate = Join-Path (Split-Path -Parent $real) 'pythonw.exe'
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    $w = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($w -and $w.Source -notlike '*\WindowsApps\*') { return $w.Source }
    return $null
}

function Get-OurTask {
    $item = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $item) { return $null }
    $matchesPath = @($item.Actions | Where-Object { $_.Arguments -like ('*' + $scriptPath + '*') })
    if ($matchesPath.Count -eq 0) { throw "A task named $taskName already exists but does not run this autosync.py. Leaving it alone." }
    return $item
}

function Show-Status {
    $task = Get-OurTask
    $taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $taskName } else { $null }
    [pscustomobject]@{
        Scheduled  = ($null -ne $task)
        State      = if ($task) { [string]$task.State } else { 'Not installed' }
        LastRun    = if ($taskInfo) { $taskInfo.LastRunTime } else { $null }
        LastResult = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
        NextRun    = if ($taskInfo) { $taskInfo.NextRunTime } else { $null }
        Heartbeat  = Join-Path $PSScriptRoot 'autosync-status.json'
    } | Format-List
    $hb = Join-Path $PSScriptRoot 'autosync-status.json'
    if (Test-Path -LiteralPath $hb) {
        try {
            $j = Get-Content -LiteralPath $hb -Raw -Encoding UTF8 | ConvertFrom-Json
            Write-Host ("Last autosync pass: {0}  ok={1}  skills={2}  propagated={3}  queue={4}  conflicts={5}" -f $j.last_run, $j.ok, $j.counts.skills_seen, $j.counts.propagated, $j.queue_len, @($j.conflicts).Count)
        } catch {
            Write-Host 'autosync-status.json exists but could not be read.'
        }
    } else {
        Write-Host 'autosync has not run yet (no autosync-status.json). It starts about one minute after registering.'
    }
}

if (-not (Test-Path -LiteralPath $scriptPath)) { throw "autosync.py not found next to this script: $scriptPath" }

if ($Action -eq 'register') {
    $pythonw = Find-PythonW
    if (-not $pythonw) { throw 'Could not find a real Python 3 (pythonw.exe). Install Python 3 from python.org, tick "Add to PATH", then run this again.' }
    $user = "$env:USERDOMAIN\$env:USERNAME"
    Write-Host 'About to register a scheduled task:'
    Write-Host "  Name    : $taskName"
    Write-Host "  Runs    : `"$pythonw`" `"$scriptPath`" once"
    Write-Host '  Every   : 1 minute, starting in 1 minute (no console window)'
    Write-Host "  As user : $user (only while you are logged in; no admin rights, no password stored)"
    Write-Host '  Effect  : new/changed skills are copied between Codex, Claude, Chat On Steroids and skills-src; nothing is ever deleted.'
    if ($DryRun) { Write-Host 'DryRun: nothing was changed.'; return }
    $existing = Get-OurTask
    $taskAction = New-ScheduledTaskAction -Execute $pythonw -Argument ('"' + $scriptPath + '" once') -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Settings $settings -Principal $principal -Description 'Memory Suite: keep skills and project instructions in step between Codex, Claude and Chat On Steroids (never deletes)' -Force | Out-Null
    if ($existing) { Write-Host 'Task already existed: it was replaced with the settings above.' } else { Write-Host 'Task registered.' }
}

if ($Action -eq 'unregister') {
    $existing = Get-OurTask
    if ($null -eq $existing) {
        Write-Host 'Task is not installed; nothing to do.'
    } elseif ($DryRun) {
        Write-Host "DryRun: would remove the scheduled task $taskName (your skills and files are not touched)."
    } else {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Task $taskName removed. Your skills, notes and backups are untouched."
    }
}

Show-Status
