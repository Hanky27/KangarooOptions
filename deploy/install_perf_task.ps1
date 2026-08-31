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
Register the task that keeps the published performance sheet current.

.DESCRIPTION
KangarooPerfPublish runs deploy/publish_perf.ps1 every EveryMinutes and
appends its output to logs/publish_perf.log.

WHY NOT SYSTEM, UNLIKE THE AGENT TASK
The agent task runs as SYSTEM because it needs no credentials beyond a
file whose ACL grants SYSTEM. This one PUSHES TO GITHUB, and the ssh key
that authorises that push lives in the calling user's profile. As SYSTEM
there is no such key and every run would fail at the last step. So it runs
as the user who installs it.

.PARAMETER Unattended
Register with LogonType S4U, which fires whether or not the user is logged
on. It needs an ELEVATED session; without -Unattended the task is
registered Interactive, which any session can do but which only runs while
that user is logged on.

WHAT IT DOES NOT DO
It does not re-render docs/index.html. The page on GitHub Pages carries a
built-in snapshot as its fallback and fetches the live one from the data
branch on open, so keeping the DATA current is enough - and it keeps the
code branch untouched between runs. Re-render deliberately, with
publish_perf.ps1 -RebuildPage, when the template changes.

WHICH MACHINE
Any machine that has the Alpaca CLI, the credentials file and push rights
on the repository. It reads the ACCOUNT over the network, so it does not
have to be the machine the agent runs on.

.PARAMETER EveryMinutes
Repetition interval. Default 5. The published file answers with
Cache-Control max-age=300 (measured 2026-08-31), so a faster task would
produce pushes no visitor can see.

.PARAMETER Remove
Unregister the task.

.EXAMPLE
.\install_perf_task.ps1
.\install_perf_task.ps1 -EveryMinutes 10
.\install_perf_task.ps1 -Remove
#>

[CmdletBinding()]
param(
   [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
   [int]$EveryMinutes = 5,
   [switch]$Unattended,
   [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TASK = 'KangarooPerfPublish'

function Step($m) { Write-Output "==> $m" }

if ($Remove) {
   if (Get-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue) {
      Stop-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue
      Unregister-ScheduledTask -TaskName $TASK -Confirm:$false
      Step "removed $TASK"
   }
   else { Step "$TASK was not registered" }
   return
}

$script = Join-Path $RepoDir 'deploy\publish_perf.ps1'
if (-not (Test-Path $script)) { throw "not found: $script" }

$logDir = Join-Path $RepoDir 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory $logDir | Out-Null }
$log = Join-Path $logDir 'publish_perf.log'

# -Command rather than -File: the run has to leave a trace, and only a
# command line can redirect every stream into the log. A task that fails
# silently every five minutes for four days is worse than no task.
$command = "& '$script' -RepoDir '$RepoDir' *>> '$log'"

Step "Registering $TASK (every $EveryMinutes minutes)"

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
   '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + $command + '"')

$me = "$env:USERDOMAIN\$env:USERNAME"

# Interactive by default, S4U on request. S4U is the better one - it fires
# whether or not anybody is logged on - but registering it needs an
# elevated session: unelevated, the service answers "Zugriff verweigert"
# (measured 2026-08-31), while the same registration as Interactive
# succeeds. That is checked here rather than discovered at the first run.
$logon = if ($Unattended) { 'S4U' } else { 'Interactive' }
if ($Unattended) {
   $id = [Security.Principal.WindowsIdentity]::GetCurrent()
   $pr = New-Object Security.Principal.WindowsPrincipal($id)
   if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
      throw 'Registering an S4U task needs an elevated session. Run this ' +
            'elevated, or drop -Unattended to install it for the logged-on user.'
   }
}
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType $logon -RunLevel Limited

# StartWhenAvailable so a run missed while the machine slept is caught up
# rather than skipped; ExecutionTimeLimit bounds a hung run so the next
# one is not blocked by it.
$settings = New-ScheduledTaskSettingsSet `
   -MultipleInstances IgnoreNew `
   -StartWhenAvailable `
   -AllowStartIfOnBatteries `
   -DontStopIfGoingOnBatteries `
   -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# A repetition that outlives the contest: OMIT the duration. Both spellings
# that look like "forever" are rejected by the service - [TimeSpan]::MaxValue
# serialises to P99999999DT23H59M59S and [TimeSpan]::Zero to PT0S, and each
# comes back "value out of range" (both measured 2026-08-31). Leaving the
# parameter off produces Duration='' in the task XML, which IS indefinite.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
   -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)

if (Get-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue) {
   Unregister-ScheduledTask -TaskName $TASK -Confirm:$false
}

Register-ScheduledTask -TaskName $TASK -Action $action -Principal $principal `
   -Settings $settings -Trigger $trigger `
   -Description 'Publishes the Kangaroo Options performance snapshot to the data branch' | Out-Null

# Register-ScheduledTask reports a rejected XML as a NON-terminating error,
# so the script would sail past it and announce a task that does not exist.
# Ask the scheduler instead of trusting the call.
$t = Get-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue
if (-not $t) { throw "$TASK was not registered - see the error above" }
Step "registered as $me, state $($t.State), log $log"
Write-Output ''
Write-Output 'Prove it works before walking away:'
Write-Output "    Start-ScheduledTask -TaskName $TASK"
Write-Output "    Get-Content '$log' -Tail 20"
