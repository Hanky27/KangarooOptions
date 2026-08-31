# MIT License
# Copyright (c) 2026 Heinrich Munz
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

<#
.SYNOPSIS
Register the agent as a scheduled task plus a watchdog that revives it.

.DESCRIPTION
The run has to survive four days unattended, including a reboot and a
crash. Two tasks do that:

  KangarooAgent    - the agent itself. Runs as SYSTEM, at startup and on
                     demand. MultipleInstances = IgnoreNew, so a second
                     start request while it is running is DISCARDED by the
                     scheduler.
  KangarooWatchdog - every WatchdogMinutes it simply asks the scheduler to
                     start the agent task. If the agent is alive, IgnoreNew
                     makes that a no-op; if it died, this is the restart.

That is deliberately two INDEPENDENT guards against the same accident -
two agents on one set of state files, each overwriting the other's cluster:

  1. the scheduler's IgnoreNew policy, and
  2. the agent's own OS-level instance lock (agent.py SingleInstance),
     which the kernel releases however the process dies.

A watchdog that shells out to "is a python process running?" would need to
tell OUR python from any other on the box; asking the scheduler avoids that
question entirely.

WHY SYSTEM: it needs no stored password, survives a logoff, and is granted
on the credentials file (its ACL is SYSTEM + Administrators). A task tied
to an interactive account stops the moment nobody is logged on - which on
a VPS is most of the time.

.PARAMETER RepoDir
The checkout. Default C:\Trading\KangarooOptions.

.PARAMETER WatchdogMinutes
How often the watchdog pokes the agent task. Default 2.

.PARAMETER Remove
Unregister both tasks and stop the agent.

.EXAMPLE
.\install_task.ps1
.\install_task.ps1 -Remove
#>

[CmdletBinding()]
param(
   [string]$RepoDir = 'C:\Trading\KangarooOptions',
   [string]$Python = 'C:\Program Files\Python313\python.exe',
   [int]$WatchdogMinutes = 2,
   [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$AGENT_TASK = 'KangarooAgent'
$WATCH_TASK = 'KangarooWatchdog'
$wrapper = Join-Path $RepoDir 'deploy\run_agent_service.ps1'

function Step($m) { Write-Output "==> $m" }

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
   throw 'Run this elevated - registering a SYSTEM task needs it.'
}

if ($Remove) {
   foreach ($t in @($WATCH_TASK, $AGENT_TASK)) {
      if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
         Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
         Unregister-ScheduledTask -TaskName $t -Confirm:$false
         Step "removed $t"
      }
      else { Step "$t was not registered" }
   }
   return
}

if (-not (Test-Path $wrapper)) { throw "wrapper not found: $wrapper" }
if (-not (Test-Path $Python)) { throw "python not found: $Python" }

# --- the agent ----------------------------------------------------------
Step "Registering $AGENT_TASK"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
   '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $wrapper +
   '" -RepoDir "' + $RepoDir + '" -Python "' + $Python + '"')
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount `
   -RunLevel Highest
# ExecutionTimeLimit 0 = never killed for running long; this process is
# MEANT to run for days. RestartOnIdle/StopIfGoingOnBatteries off: a VPS
# has no battery and no idle policy should touch a trading process.
$settings = New-ScheduledTaskSettingsSet `
   -MultipleInstances IgnoreNew `
   -ExecutionTimeLimit ([TimeSpan]::Zero) `
   -DontStopOnIdleEnd `
   -AllowStartIfOnBatteries `
   -DontStopIfGoingOnBatteries `
   -StartWhenAvailable `
   -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$trigger = New-ScheduledTaskTrigger -AtStartup
if (Get-ScheduledTask -TaskName $AGENT_TASK -ErrorAction SilentlyContinue) {
   Unregister-ScheduledTask -TaskName $AGENT_TASK -Confirm:$false
}
Register-ScheduledTask -TaskName $AGENT_TASK -Action $action -Principal $principal `
   -Settings $settings -Trigger $trigger `
   -Description 'Kangaroo Options agent - Alpaca AI Trading Agents Hackathon' | Out-Null
Write-Output "    action: powershell -File $wrapper"

# --- the watchdog -------------------------------------------------------
Step "Registering $WATCH_TASK (every $WatchdogMinutes min)"
$wAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
   '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ' +
   '"Start-ScheduledTask -TaskName ' + $AGENT_TASK + '"')
$wTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
   -RepetitionInterval (New-TimeSpan -Minutes $WatchdogMinutes)
$wSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
   -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable `
   -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
if (Get-ScheduledTask -TaskName $WATCH_TASK -ErrorAction SilentlyContinue) {
   Unregister-ScheduledTask -TaskName $WATCH_TASK -Confirm:$false
}
Register-ScheduledTask -TaskName $WATCH_TASK -Action $wAction -Principal $principal `
   -Settings $wSettings -Trigger $wTrigger `
   -Description 'Starts KangarooAgent if it is not running (IgnoreNew makes it a no-op when it is)' | Out-Null

Write-Output ''
foreach ($t in @($AGENT_TASK, $WATCH_TASK)) {
   $st = Get-ScheduledTask -TaskName $t
   Write-Output ("    {0,-18} {1}" -f $t, $st.State)
}
Write-Output ''
Write-Output "Start it with:  Start-ScheduledTask -TaskName $AGENT_TASK"
Write-Output "Log:            $RepoDir\logs\agent_<date>.log"
