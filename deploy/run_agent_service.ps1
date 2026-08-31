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
Run the agent as a service-style process, logging every line to disk.

.DESCRIPTION
The scheduled task points at this wrapper rather than at python directly,
for three reasons:

  1. The agent's whole story is in its stdout - every order, every fill,
     every halt. A task that runs python with no redirection throws all of
     that away, and after an unattended night there is nothing to read.
  2. The log is TEE'd, not redirected: run the wrapper by hand and you see
     the same lines the file gets. A plain '>' would make a manual run
     silent and indistinguishable from a hung one.
  3. It records the commit it started from. Over a multi-day run the
     checkout can move; without this line a log cannot be matched to the
     code that wrote it.

One log file per calendar day, appended - a restart continues the day's
file instead of truncating what the previous process wrote.
#>

[CmdletBinding()]
param(
   [string]$RepoDir = 'C:\Trading\KangarooOptions',
   [string]$Python = 'C:\Program Files\Python313\python.exe'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if (-not (Test-Path $Python)) { throw "python not found: $Python" }
if (-not (Test-Path $RepoDir)) { throw "checkout not found: $RepoDir" }

$logDir = Join-Path $RepoDir 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$log = Join-Path $logDir ('agent_' + (Get-Date -Format 'yyyy-MM-dd') + '.log')

Set-Location $RepoDir
$commit = (& git -C $RepoDir rev-parse --short HEAD 2>$null)
# $PID is THIS wrapper, not python - python has not been started yet when
# the banner is written. Labelled as such so a log line can never be
# mistaken for the agent's own pid; the agent logs its own pid itself when
# it takes the instance lock.
$banner = ("=== agent start " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') +
   " | commit " + $commit + " | wrapper pid " + $PID +
   " | user " + $env:USERNAME + " ===")
Add-Content -Path $log -Value $banner -Encoding UTF8
Write-Host $banner

# -u so python does not buffer: a buffered agent writes nothing to the log
# until it exits, which for a process meant to run for days means nothing
# at all. 2>&1 folds stderr in so a traceback lands in the same file.
& $Python -u agent.py 2>&1 | Tee-Object -FilePath $log -Append

$tail = ("=== agent exit " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') +
   " | exitcode " + $LASTEXITCODE + " ===")
Add-Content -Path $log -Value $tail -Encoding UTF8
Write-Host $tail
exit $LASTEXITCODE
