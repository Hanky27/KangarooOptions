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
Prepare a Windows host to run the Kangaroo Options agent.

.DESCRIPTION
Everything the host needs, from the repos and pinned upstream releases -
nothing copied by hand, so the host's state is reproducible.

  1. Python and Git are located (or reported missing, with the winget line).
  2. The repo is cloned or fast-forwarded from GitHub.
  3. The Alpaca CLI is downloaded at a PINNED version and its SHA-256 is
     checked against the release's own checksums.txt before it is unpacked.
  4. config.yaml is GENERATED with this host's paths. It is not in the repo
     (it names local paths and the credentials file), so generating it here
     is the only way the host gets one that matches its own layout.

WHY THE VERSION IS PINNED: the Alpaca CLI README says, verbatim, that it is
an alpha preview whose "output formats may change or be removed without
notice between releases". Every measurement behind this bot was taken
against 0.0.13. Letting a host pull "latest" means the agent parses output
from a build nobody verified, and a changed field name surfaces as a
trading error, not as a version error.

NOT done here, on purpose: the credentials file. Secrets never travel
through a repo. Create .env.hackathon on the host afterwards (two lines,
ALPACA_API_KEY / ALPACA_SECRET_KEY) - see -Verify output.

.PARAMETER Root
Parent folder for the checkout. Default C:\Trading.

.PARAMETER Verify
Only report what is present; change nothing.

.EXAMPLE
.\setup_host.ps1
.\setup_host.ps1 -Verify
#>

[CmdletBinding()]
param(
   [string]$Root = 'C:\Trading',
   [switch]$Verify
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$REPO = 'https://github.com/Hanky27/KangarooOptions.git'
$CLI_VERSION = '0.0.13'
$CLI_SHA256 = 'e5a71a9eea3de90ae54d8ac9e1c0100db87d1e7b33f431003b3f904d34d95f48'
$CLI_URL = "https://github.com/alpacahq/cli/releases/download/v$CLI_VERSION/cli_${CLI_VERSION}_windows_amd64.zip"

$repoDir = Join-Path $Root 'KangarooOptions'
$cliDir = Join-Path $Root 'AlpacaTools\cli'
$cliExe = Join-Path $cliDir 'alpaca.exe'
$envFile = Join-Path $repoDir '.env.hackathon'

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m) { Write-Host "    $m" -ForegroundColor Green }
function Bad($m) { Write-Host "    $m" -ForegroundColor Yellow }

# --- 1. prerequisites ----------------------------------------------------
Step 'Prerequisites'
$py = $null
foreach ($c in @('C:\Program Files\Python313\python.exe',
      "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
      'C:\Python313\python.exe')) {
   if (Test-Path $c) { $py = $c; break }
}
if (-not $py) {
   $cmd = Get-Command python -ErrorAction SilentlyContinue
   if ($cmd) { $py = $cmd.Source }
}
if ($py) {
   $v = & $py --version 2>&1
   Ok "Python: $py ($v)"
   # The agent uses `X | None` annotations and zoneinfo - 3.10 is the floor.
   $mm = [version](( & $py -c "import sys;print('%d.%d'%sys.version_info[:2])") 2>&1)
   if ($mm -lt [version]'3.10') { Bad "  too old - 3.10 is the minimum" ; $py = $null }
}
if (-not $py) { Bad 'Python MISSING -> winget install -e --id Python.Python.3.13' }

$git = (Get-Command git -ErrorAction SilentlyContinue)
if ($git) { Ok "Git: $($git.Source) ($(git --version))" }
else { Bad 'Git MISSING -> winget install -e --id Git.Git' }

if ($Verify) {
   Step 'Verify only - state of this host'
   Write-Host ("    repo      : " + $(if (Test-Path $repoDir) { (git -C $repoDir rev-parse --short HEAD 2>$null) } else { 'MISSING' }))
   Write-Host ("    alpaca cli: " + $(if (Test-Path $cliExe) { (& $cliExe version -q 2>&1) } else { 'MISSING' }))
   Write-Host ("    config    : " + $(if (Test-Path (Join-Path $repoDir 'config.yaml')) { 'present' } else { 'MISSING' }))
   Write-Host ("    creds     : " + $(if (Test-Path $envFile) { 'present' } else { 'MISSING - create it by hand, never via the repo' }))
   return
}
if (-not $py -or -not $git) { throw 'Install the missing prerequisites above, then re-run.' }

# --- 2. repo -------------------------------------------------------------
Step "Repo -> $repoDir"
if (-not (Test-Path $Root)) { New-Item -ItemType Directory -Path $Root -Force | Out-Null }
if (Test-Path (Join-Path $repoDir '.git')) {
   git -C $repoDir fetch --quiet origin
   git -C $repoDir merge --ff-only --quiet origin/main
   Ok "fast-forwarded to $(git -C $repoDir rev-parse --short HEAD)"
}
else {
   git clone --quiet $REPO $repoDir
   Ok "cloned at $(git -C $repoDir rev-parse --short HEAD)"
}

# --- 3. Alpaca CLI, pinned and checksum-verified -------------------------
Step "Alpaca CLI v$CLI_VERSION"
$needed = $true
if (Test-Path $cliExe) {
   $have = (& $cliExe version -q 2>&1 | Out-String).Trim()
   if ($have -eq $CLI_VERSION) { Ok "already at $have"; $needed = $false }
   else { Bad "found $have, replacing with $CLI_VERSION" }
}
if ($needed) {
   $zip = Join-Path $env:TEMP "alpaca_$CLI_VERSION.zip"
   Invoke-WebRequest -Uri $CLI_URL -OutFile $zip
   $sha = (Get-FileHash -Path $zip -Algorithm SHA256).Hash.ToLower()
   if ($sha -ne $CLI_SHA256) {
      Remove-Item $zip -Force
      throw ("SHA-256 mismatch for the Alpaca CLI download.`n" +
         "  expected $CLI_SHA256`n  got      $sha`n" +
         'Refusing to unpack an unverified binary.')
   }
   Ok "SHA-256 matches the release checksum"
   if (-not (Test-Path $cliDir)) { New-Item -ItemType Directory -Path $cliDir -Force | Out-Null }
   Expand-Archive -Path $zip -DestinationPath $cliDir -Force
   Remove-Item $zip -Force
   Ok "unpacked -> $cliExe ($(& $cliExe version -q 2>&1))"
}

# --- 3b. Python dependencies --------------------------------------------
# PyYAML is the ONLY third-party import in the agent's chain (agent.py,
# alpaca_cli.py, kangaroo_core.py - the rest is stdlib or this repo).
Step 'Python dependencies'
$req = Join-Path $repoDir 'requirements.txt'
if (Test-Path $req) {
   $pipOut = & $py -m pip install --quiet --disable-pip-version-check -r $req 2>&1
   if ($LASTEXITCODE -ne 0) { throw ("pip failed:`n" + ($pipOut -join "`n")) }
   Ok ((& $py -c "import yaml;print('PyYAML ' + yaml.__version__)") 2>&1)
}
else { Bad "no requirements.txt in the checkout" }

# --- 4. config.yaml for THIS host ---------------------------------------
Step 'config.yaml (generated - it names local paths, so it is not in the repo)'
$cfgPath = Join-Path $repoDir 'config.yaml'
$example = Join-Path $repoDir 'config.example.yaml'
if (-not (Test-Path $example)) { throw "missing $example - is the checkout complete?" }
$cfg = Get-Content $example -Raw
$cfg = $cfg -replace '(?m)^cli_path:.*$', ("cli_path: " + ($cliExe -replace '\\', '/'))
$cfg = $cfg -replace '(?m)^env_file:.*$', ("env_file: " + ($envFile -replace '\\', '/'))
if ($cfg -notmatch '(?m)^cli_path:') { $cfg += "`ncli_path: " + ($cliExe -replace '\\', '/') }
if ($cfg -notmatch '(?m)^env_file:') { $cfg += "`nenv_file: " + ($envFile -replace '\\', '/') }
if (Test-Path $cfgPath) {
   Copy-Item $cfgPath "$cfgPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)" -Force
   Bad 'existing config.yaml backed up before overwrite'
}
[IO.File]::WriteAllText($cfgPath, $cfg, (New-Object Text.UTF8Encoding($false)))
Ok "written: $cfgPath"

# --- 5. what is still missing -------------------------------------------
Write-Host ''
if (Test-Path $envFile) {
   Ok "credentials present: $envFile"
   Write-Host ''
   Write-Host 'Ready. Smoke test (sends no order):' -ForegroundColor Green
   Write-Host "    cd $repoDir; & '$py' agent.py --dry-run --once"
}
else {
   Bad "credentials MISSING: $envFile"
   Write-Host ''
   Write-Host 'Create that file with two lines (never through the repo):' -ForegroundColor Yellow
   Write-Host '    ALPACA_API_KEY=...'
   Write-Host '    ALPACA_SECRET_KEY=...'
   Write-Host 'then re-run with -Verify.'
}
