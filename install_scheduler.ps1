param(
    [switch]$EnableScheduling
)

$ErrorActionPreference = "Stop"
if (-not $EnableScheduling) {
    Write-Host "Scheduling is intentionally disabled while the storybook agent is being finalized." -ForegroundColor Yellow
    Write-Host "No Windows scheduled task was installed or changed."
    Write-Host "After the agent is approved, install with: .\\install_scheduler.ps1 -EnableScheduling"
    exit 0
}

# Run only after the agent has been finalized and scheduling is explicitly enabled.
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = (Get-Command python.exe -ErrorAction Stop).Source
$Runner = Join-Path $Project "daily_runner.py"
$Action = New-ScheduledTaskAction -Execute $Python -Argument ('"' + $Runner + '"') -WorkingDirectory $Project
$Daily = New-ScheduledTaskTrigger -Daily -At 8:00PM
$Logon = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 18) -MultipleInstances IgnoreNew -WakeToRun
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "AI Storybook Daily Generator" -Action $Action -Trigger @($Daily, $Logon) -Settings $Settings -Principal $Principal -Description "Generates one resumable AI storybook daily at 20:00 Pakistan time." -Force
Write-Host "Scheduled task installed. Scheduling is now enabled." -ForegroundColor Green
