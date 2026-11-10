# Run this PowerShell script once as the Windows user who will own the scheduled task.
$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = (Get-Command python.exe -ErrorAction Stop).Source
$Runner = Join-Path $Project "daily_runner.py"
$Action = New-ScheduledTaskAction -Execute $Python -Argument ('"' + $Runner + '"') -WorkingDirectory $Project
$Daily = New-ScheduledTaskTrigger -Daily -At 8:00PM
$Logon = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 18) -MultipleInstances IgnoreNew -WakeToRun
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "AI Storybook Daily Generator" -Action $Action -Trigger @($Daily, $Logon) -Settings $Settings -Principal $Principal -Description "Generates one resumable AI storybook daily at 20:00 Pakistan time and catches up missed dates sequentially." -Force
Write-Host "Scheduled task installed. It runs daily at 8:00 PM and when you log into Windows (and can wake the PC from sleep)." -ForegroundColor Green
