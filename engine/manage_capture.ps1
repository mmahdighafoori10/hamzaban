param(
    [ValidateSet('start','stop','status')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'auto_capture.py'
$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $candidate = 'C:\Program Files\Python310\pythonw.exe'
    if (Test-Path -LiteralPath $candidate) { $python = $candidate }
}
$reg = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$key = 'ChatOnSteroidsInfiniteContext'
$taskName = 'ChatOnSteroidsInfiniteContext'

function Get-OurTask {
    $item = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $item) { return $null }
    $matchesPath = @($item.Actions | Where-Object { $_.Arguments -like ('*' + $scriptPath + '*') })
    if ($matchesPath.Count -eq 0) { throw 'Task name already in use by another command.' }
    return $item
}

if ($Action -eq 'start') {
    if (-not $python) { throw 'Pythonw executable not found.' }
    # Remove the earlier watcher-at-login setting once the scheduled task is ready.
    $existing = Get-OurTask
    if (-not $existing) {
        $taskAction = New-ScheduledTaskAction -Execute $python -Argument ('"' + $scriptPath + '" once')
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -StartWhenAvailable
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Settings $settings -Description 'Store local Chat On Steroids user messages and context checkpoints in the Obsidian Vault' -Force | Out-Null
    }
    Remove-ItemProperty -LiteralPath $reg -Name $key -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $taskName
}

if ($Action -eq 'stop') {
    Remove-ItemProperty -LiteralPath $reg -Name $key -ErrorAction SilentlyContinue
    $existing = Get-OurTask
    if ($null -ne $existing) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
}

$task = Get-OurTask
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $taskName } else { $null }
[pscustomobject]@{
    Scheduled = ($null -ne $task)
    State = if ($task) { [string]$task.State } else { 'Not installed' }
    LastRun = if ($taskInfo) { $taskInfo.LastRunTime } else { $null }
    LastResult = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
    RunsEveryMinute = ($null -ne $task)
    ObsidianData = Join-Path (Split-Path -Parent $PSScriptRoot) '40-Conversation-Capture'
} | Format-List
