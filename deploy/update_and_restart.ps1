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
Ship new code to a RUNNING agent and prove the new code is what took over.

.DESCRIPTION
This is a hackathon: the agent keeps being developed while it trades. A
'git pull' alone changes NOTHING about the live process - python read the
whole file at startup and keeps running the old code. Restarting is what
applies the change, and restarting mid-week is safe by design: the agent
reloads every cluster from state/kangaroo_<sym>_<side>.json and reconciles
it against the account's real positions before it trades again.

Every step waits for the OBSERVED change, never for a fixed number of
seconds:
  1. record the old commit and the old pid
  2. fast-forward the checkout
  3. stop the task, then WAIT UNTIL THE PROCESS IS ACTUALLY GONE - a task
     reporting 'Ready' only means the scheduler let go, not that python
     exited, and starting the new one while the old still holds the
     instance lock makes the new one refuse to start
  4. start the task, then WAIT FOR A NEW PID
  5. read back the banner the new process wrote and check the commit in it

`-Check` reports without changing anything.

.PARAMETER MaxWaitSeconds
Upper bound for each wait. Only a bound, not a delay: the loops exit the
moment the condition holds.
#>

[CmdletBinding()]
param(
   [string]$RepoDir = 'C:\Trading\KangarooOptions',
   [string]$TaskName = 'KangarooAgent',
   [int]$MaxWaitSeconds = 120,
   [switch]$Check
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Agents {
   @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
      Where-Object { $_.CommandLine -like '*agent.py*' })
}
function Say($m) { Write-Output $m }

$oldCommit = (& git -C $RepoDir rev-parse --short HEAD 2>$null)
$oldProcs = Agents
Say ("checkout : $oldCommit")
Say ("running  : " + $oldProcs.Count + " agent process(es) " +
   (($oldProcs | ForEach-Object { 'pid ' + $_.ProcessId }) -join ', '))

if ($Check) {
   $log = Get-ChildItem (Join-Path $RepoDir 'logs\agent_*.log') -ErrorAction SilentlyContinue |
   Sort-Object LastWriteTime -Descending | Select-Object -First 1
   if ($log) {
      Say '--- last log lines ---'
      Get-Content $log.FullName -Tail 8 | ForEach-Object { Say ('  ' + $_) }
   }
   return
}

# --- 1. fetch -----------------------------------------------------------
& git -C $RepoDir fetch --quiet origin
$newCommit = (& git -C $RepoDir rev-parse --short origin/main)
if ($newCommit -eq $oldCommit) {
   Say "origin/main is the same commit - nothing to ship, not restarting."
   return
}
Say ("origin   : $newCommit")
Say '--- what changes ---'
& git -C $RepoDir log --oneline "$oldCommit..origin/main" | ForEach-Object { Say ('  ' + $_) }
& git -C $RepoDir merge --ff-only --quiet origin/main
Say ("checkout now at " + (& git -C $RepoDir rev-parse --short HEAD))

# --- 2. stop, and wait for the PROCESS, not the task --------------------
Say 'stopping the agent'
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
while ((Agents).Count -gt 0 -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
if ((Agents).Count -gt 0) {
   throw ("the agent is still running after $MaxWaitSeconds s - not starting a " +
      "second one on top of it. Investigate before retrying.")
}
Say 'process gone, instance lock released'

# --- 3. start, and wait for a NEW pid -----------------------------------
Say 'starting the agent'
Start-ScheduledTask -TaskName $TaskName
$deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
while ((Agents).Count -eq 0 -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
$new = Agents
if ($new.Count -eq 0) { throw "the agent did not come back within $MaxWaitSeconds s" }
if ($new.Count -gt 1) { throw ("TWO agent processes are running: " +
      (($new | ForEach-Object { $_.ProcessId }) -join ', ')) }
Say ('running  : pid ' + $new[0].ProcessId + ' since ' + $new[0].CreationDate)

# --- 4. prove the NEW code is what took over ----------------------------
$log = Get-ChildItem (Join-Path $RepoDir 'logs\agent_*.log') |
Sort-Object LastWriteTime -Descending | Select-Object -First 1
$deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
$banner = $null
while (-not $banner -and (Get-Date) -lt $deadline) {
   $banner = (Get-Content $log.FullName -Tail 60 |
      Where-Object { $_ -match '=== agent start' } | Select-Object -Last 1)
   if (-not $banner) { Start-Sleep -Milliseconds 500 }
}
if (-not $banner) { throw 'the new process wrote no start banner' }
Say ('banner   : ' + $banner.Trim())
if ($banner -notmatch [regex]::Escape($newCommit)) {
   throw ("the running process reports a different commit than the one just " +
      "shipped ($newCommit). Restart did not pick up the new code.")
}
Say ''
Say '--- first lines of the new run ---'
Get-Content $log.FullName -Tail 40 |
Select-Object -Last 12 | ForEach-Object { Say ('  ' + $_) }
Say ''
Say "shipped $oldCommit -> $newCommit and verified it is the code now running."
