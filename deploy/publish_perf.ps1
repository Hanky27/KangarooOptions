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
Measure the account and publish the snapshot the live sheet reads.

.DESCRIPTION
The published page at docs/index.html ships with the snapshot it was built
from and re-fetches the newest one when a visitor opens it. This script
produces that newest one.

WHY THE DATA LIVES ON ITS OWN BRANCH, AS A SINGLE AMENDED COMMIT
The repository IS the hackathon submission and the judges read its
history. A snapshot pushed every few minutes over four trading days would
bury the twenty commits that matter under hundreds that say nothing. So
the snapshot goes to an orphan branch that holds exactly one commit,
amended and force-pushed on every run: main's history never sees it, and
raw.githubusercontent.com/<owner>/<repo>/<branch>/snapshot.json always
resolves to the current one. Force-pushing is safe here precisely because
nothing but this script ever writes that branch.

WHY raw.githubusercontent.com AND NOT THE PAGES PATH
Measured 2026-08-31: Pages answers `Cache-Control: max-age=600` and
ignores the query string entirely - a fresh `?v=<random>` returned the
same `Age: 117` twice, so the usual cache-busting trick buys nothing
there. raw answers the same file with `max-age=300` and
`Access-Control-Allow-Origin: *`, which is why the page fetches from raw.

.PARAMETER RebuildPage
Also re-render docs/index.html from the template and commit it to the
current branch. Needed when the template or the built-in fallback
snapshot should move - not on the periodic run, which only publishes data.

.PARAMETER Check
Measure and render, report, push nothing.

.EXAMPLE
.\publish_perf.ps1
.\publish_perf.ps1 -Check
.\publish_perf.ps1 -RebuildPage
#>

[CmdletBinding()]
param(
   [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
   [string]$DataBranch = 'data',
   [string]$Python = 'python',
   [switch]$RebuildPage,
   [switch]$Check
)

$ErrorActionPreference = 'Stop'

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m) { Write-Host "    $m" -ForegroundColor Green }

# git reports progress on STDERR even when it succeeds ("To github.com:...",
# "Switched to a new branch"). Called bare while the whole script's output
# is redirected to a file, those lines arrive as ErrorRecords, and with
# ErrorActionPreference = Stop they terminate a run that had already done
# its work - measured: the scheduled task pushed the commit and still
# exited 1, with the log cut off mid-step. Capturing the streams here keeps
# stderr as text and leaves the exit code as the only verdict.
#
# The name matters and git.exe is spelled out: PowerShell resolves command
# names case-insensitively and puts FUNCTIONS ahead of executables, so a
# helper called "Git" that runs "& git" calls ITSELF - measured, it dies
# with CallDepthOverflow after recursing. Invoke-Git cannot shadow git.exe,
# and naming the executable in full removes the question altogether.
function Invoke-Git {
   param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
   # ErrorActionPreference is lowered for the native call ONLY. Windows
   # PowerShell 5.1 - which is what the scheduled task starts - raises a
   # terminating NativeCommandError for anything a native program writes to
   # stderr while the preference is Stop, and it does so BEFORE the 2>&1
   # can merge the streams. Measured on a push that had already succeeded:
   #   git.exe : To github.com:Hanky27/KangarooOptions.git
   #   FullyQualifiedErrorId : NativeCommandError
   # This is not a softened check: the exit code below is still the only
   # verdict, and it still throws.
   # SilentlyContinue, not Continue, and 2>&1 rather than a file: both
   # variants were measured under Windows PowerShell 5.1 on a push that
   # SUCCEEDED, and both still produce the NativeCommandError record - a
   # file redirect even logs it twice. The preference is the only thing
   # that keeps it out of the log. Nothing is lost: 2>&1 puts git's own
   # words into $out, which is printed line by line below, and the exit
   # code is still the only verdict.
   $previous = $ErrorActionPreference
   $ErrorActionPreference = 'SilentlyContinue'
   try {
      $out = & git.exe @Arguments 2>&1
      $code = $LASTEXITCODE
   } finally {
      $ErrorActionPreference = $previous
   }
   foreach ($line in $out) { if ("$line".Trim()) { Write-Host "    $line" } }
   if ($code -ne 0) { throw "git $($Arguments -join ' ') failed ($code)" }
   return $out
}

# --- 0. everything this needs must be there, or stop here ---------------

foreach ($rel in @('config.yaml', 'perf_page.tpl.html',
                   'tools_perf_snapshot.py', 'tools_perf_page.py')) {
   $p = Join-Path $RepoDir $rel
   if (-not (Test-Path $p)) { throw "missing: $p" }
}

$remote = (& git -C $RepoDir remote get-url origin).Trim()
if (-not $remote) { throw "no origin remote in $RepoDir" }

# owner/repo out of either remote form, because the raw URL is built from it
# and a wrong guess would point the page at a file that does not exist.
if ($remote -match '[:/]([^/:]+)/([^/]+?)(\.git)?$') {
   $owner = $Matches[1]
   $repo = $Matches[2]
} else {
   throw "cannot read owner/repo out of origin remote: $remote"
}
$rawUrl = "https://raw.githubusercontent.com/$owner/$repo/$DataBranch/snapshot.json"

# --- 1. measure ---------------------------------------------------------

Step 'Measuring the account through the Alpaca CLI'
$docs = Join-Path $RepoDir 'docs'
if (-not (Test-Path $docs)) { New-Item -ItemType Directory $docs | Out-Null }
$snapshot = Join-Path $docs 'snapshot.json'

& $Python (Join-Path $RepoDir 'tools_perf_snapshot.py') `
   --config (Join-Path $RepoDir 'config.yaml') --out $snapshot
if ($LASTEXITCODE -ne 0) { throw "tools_perf_snapshot.py failed ($LASTEXITCODE)" }

# The snapshot writes itself only when its terms reconcile, so reaching
# this line already means the numbers close. What is checked here is that
# the file on disk is the run that just happened, not a leftover.
$asOf = (Get-Content $snapshot -Raw | ConvertFrom-Json).as_of
Ok "snapshot as of $asOf"

# --- 2. render the page, when asked -------------------------------------

if ($RebuildPage) {
   Step 'Rendering docs/index.html'
   & $Python (Join-Path $RepoDir 'tools_perf_page.py') `
      --snapshot $snapshot `
      --template (Join-Path $RepoDir 'perf_page.tpl.html') `
      --out (Join-Path $docs 'index.html') `
      --live-url $rawUrl `
      --repo-url "https://github.com/$owner/$repo"
   if ($LASTEXITCODE -ne 0) { throw "tools_perf_page.py failed ($LASTEXITCODE)" }
}

if ($Check) {
   Step 'Check only - nothing pushed'
   Ok "would publish to $rawUrl"
   return
}

# --- 3. publish the data ------------------------------------------------
#
# A separate worktree, so the branch the agent is developed on is never
# checked out, stashed or otherwise disturbed by a job that runs every few
# minutes.

$work = Join-Path ([IO.Path]::GetTempPath()) "kangaroo-perf-$DataBranch"

if (-not (Test-Path (Join-Path $work '.git'))) {
   Step "Preparing the $DataBranch worktree"
   if (Test-Path $work) { Remove-Item $work -Recurse -Force }

   $exists = (& git -C $RepoDir ls-remote --heads origin $DataBranch 2>&1 |
              Where-Object { $_ -match "refs/heads/$DataBranch" })
   if ($exists) {
      Invoke-Git -C $RepoDir fetch origin $DataBranch
      Invoke-Git -C $RepoDir worktree add --force -B $DataBranch $work "origin/$DataBranch"
   } else {
      # An ORPHAN branch: the data shares no history with the code, which
      # is the whole point - amending it can never rewrite a code commit.
      Invoke-Git -C $RepoDir worktree add --force --detach $work
      Invoke-Git -C $work checkout --orphan $DataBranch
      Invoke-Git -C $work reset --hard
   }
   Ok $work
}

Copy-Item $snapshot (Join-Path $work 'snapshot.json') -Force
Invoke-Git -C $work add snapshot.json

$head = (& git -C $work rev-parse --verify --quiet HEAD)
$message = "Live snapshot as of $asOf"
if ($head) {
   Invoke-Git -C $work commit --amend -m $message --quiet
} else {
   Invoke-Git -C $work commit -m $message --quiet
}

Step "Pushing $DataBranch"
Invoke-Git -C $work push --force origin "${DataBranch}:${DataBranch}"

# The push having returned is not proof it landed - ask the remote what it
# now holds and compare it with what was just committed.
$local = (& git -C $work rev-parse HEAD).Trim()
$remoteHead = ((Invoke-Git -C $work ls-remote origin $DataBranch) -split '\s+')[0]
if ($local -ne $remoteHead) {
   throw "push reported success but origin/$DataBranch is $remoteHead, not $local"
}

$commits = (& git -C $work rev-list --count HEAD)
Ok "published: $rawUrl  ($commits commit on $DataBranch, $($local.Substring(0,7)))"

if ($RebuildPage) {
   Write-Host ''
   Write-Host 'docs/index.html was re-rendered but NOT committed - review it,' -ForegroundColor Yellow
   Write-Host 'then commit it on the code branch yourself.' -ForegroundColor Yellow
}
