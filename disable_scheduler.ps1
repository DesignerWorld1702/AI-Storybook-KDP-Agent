$ErrorActionPreference = "Stop"
$TaskName = "AI Storybook Daily Generator"
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Task) {
    Write-Host "No existing AI Storybook Daily Generator task was found."
    exit 0
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "The AI Storybook Daily Generator scheduled task has been disabled and removed." -ForegroundColor Green
