<#
.SYNOPSIS
  Run Volt at boot on Windows as a Scheduled Task. No extra software: Task Scheduler ships with Windows.

.DESCRIPTION
  Registers one task, "Volt", that runs `python -m volt run --env-file <DataDir>\volt.env` when the machine
  starts, with no time limit, and that Task Scheduler restarts (every minute, up to 999 times) if the
  supervisor itself ever exits with a failure. `volt run` already restarts a crashed serve, runner or clock.

  Creates <DataDir> with logs\, out\, proxy\ and publish\ beside it, and writes volt.env from the template
  if one is not there yet. The file is restricted to SYSTEM and Administrators because it holds the token.

  -DryRun prints what would be done as JSON and changes nothing (no folders, no ACLs, no task). Use it to
  review, and it is what the tests run. -Uninstall removes the task and leaves the data alone.

  Needs an elevated PowerShell for a real install. Not exercised against a live Task Scheduler in CI.

.EXAMPLE
  .\install-volt-task.ps1 -Python C:\Volt\venv\Scripts\python.exe -DryRun
  .\install-volt-task.ps1 -Python C:\Volt\venv\Scripts\python.exe
  Start-ScheduledTask -TaskName Volt
#>
[CmdletBinding()]
param(
  [string]$Python = '',
  [string]$DataDir = (Join-Path $env:ProgramData 'Volt'),
  [string]$TaskName = 'Volt',
  [string]$RunAs = 'SYSTEM',
  [switch]$DryRun,
  [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

if ($Uninstall) {
  if ($DryRun) { Write-Output (@{ action = 'unregister'; taskName = $TaskName } | ConvertTo-Json -Compress); return }
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Output "Removed task $TaskName. Data in $DataDir was left alone."
  return
}

if (-not $Python) {
  $found = Get-Command python.exe -ErrorAction SilentlyContinue
  if (-not $found) { throw 'python.exe not found. Pass -Python <path to the python.exe that has volt installed>.' }
  $Python = $found.Source
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python not found at $Python" }
$Python = (Resolve-Path -LiteralPath $Python).Path

$envFile  = Join-Path $DataDir 'volt.env'
$template = Join-Path $PSScriptRoot 'volt.env.example'
$arguments = "-m volt run --env-file `"$envFile`""
$isSystem = $RunAs -in @('SYSTEM', 'NT AUTHORITY\SYSTEM')

$plan = [ordered]@{
  action           = 'register'
  taskName         = $TaskName
  execute          = $Python
  arguments        = $arguments
  workingDirectory = $DataDir
  runAs            = $RunAs
  logonType        = $(if ($isSystem) { 'ServiceAccount' } else { 'S4U' })
  trigger          = 'AtStartup'
  restartCount     = 999
  restartIntervalMinutes = 1
  executionTimeLimit     = 'none'
  multipleInstances      = 'IgnoreNew'
  envFile          = $envFile
  createsEnvFile   = (-not (Test-Path -LiteralPath $envFile))
  directories      = @('logs', 'out', 'proxy', 'publish') | ForEach-Object { Join-Path $DataDir $_ }
}

if ($DryRun) { Write-Output ($plan | ConvertTo-Json -Compress); return }

# Preflight with the interpreter the task will use: refuse to register a task that cannot start.
& $Python -c "import volt" 2>$null
if ($LASTEXITCODE -ne 0) { throw "$Python cannot import volt. Install it first: $Python -m pip install -e <checkout>\control" }

foreach ($dir in @($DataDir) + $plan.directories) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }

if ($plan.createsEnvFile) {
  (Get-Content -LiteralPath $template -Raw) -replace '@DATA_DIR@', $DataDir | Set-Content -LiteralPath $envFile -Encoding UTF8
  # Only SYSTEM and Administrators (and the service account, if different) may read the token.
  & icacls.exe $envFile /inheritance:r /grant:r '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
  if (-not $isSystem) { & icacls.exe $envFile /grant:r "${RunAs}:(R)" | Out-Null }
  Write-Output "Wrote $envFile. Edit it (token, database, room URL), then run: volt check --env-file `"$envFile`""
}

$action    = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $DataDir
$trigger   = New-ScheduledTaskTrigger -AtStartup
$settings  = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
               -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
               -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = if ($isSystem) { New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest }
             else { New-ScheduledTaskPrincipal -UserId $RunAs -LogonType S4U -RunLevel Highest }

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
  -Description 'Volt control plane (serve, runner, clock) supervised by volt run' -Force | Out-Null
Write-Output "Registered task $TaskName. Start it with: Start-ScheduledTask -TaskName $TaskName"
Write-Output "Stop it with: Stop-ScheduledTask -TaskName $TaskName   (a hard stop; see docs/operations.md)"
