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
Install Python and Git on a Windows host that has no package manager.

.DESCRIPTION
Windows Server 2022 ships without winget (verified absent on FOREX12GB-1,
2026-08-31), so the official installers are fetched and run silently.

Both are installed MACHINE-WIDE (Python: InstallAllUsers=1, Git: default
program-files location). A per-user install would be invisible to a
scheduled task that runs as SYSTEM or as another account, which is exactly
how the agent will be started later.

Versions are pinned, not "latest": a host that silently drifts to another
Python minor is a host whose behaviour no longer matches what was tested
here.

Run elevated. Idempotent: an already-present matching version is skipped.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$PY_VERSION = '3.13.6'
$PY_URL = "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-amd64.exe"
$PY_EXE = 'C:\Program Files\Python313\python.exe'

$GIT_VERSION = '2.55.0.5'
$GIT_URL = "https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.5/Git-$GIT_VERSION-64-bit.exe"
$GIT_EXE = 'C:\Program Files\Git\cmd\git.exe'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
   throw 'Run this elevated - a machine-wide install needs it.'
}

# NOTE: the parameter is NOT called $args - that is a PowerShell
# AUTOMATIC variable, and binding it as a parameter makes the caller's
# array arrive EMPTY. Measured 2026-08-31 on FOREX12GB-1: Start-Process
# then failed with "ArgumentList ... ist NULL oder leer" after the
# installer had already been downloaded.
function Install-Silent($name, $url, $switches, $probe, $want) {
   if (Test-Path $probe) {
      $have = (& $probe --version 2>&1 | Out-String).Trim()
      if ($have -match [regex]::Escape($want)) {
         Write-Output "$name : already $have - skipped"
         return
      }
      Write-Output "$name : found '$have', installing $want over it"
   }
   $exe = Join-Path $env:TEMP ([IO.Path]::GetFileName($url))
   Write-Output "$name : downloading $url"
   Invoke-WebRequest -Uri $url -OutFile $exe
   Write-Output ("$name : " + [math]::Round((Get-Item $exe).Length / 1MB, 1) + ' MB, installing silently')
   $p = Start-Process -FilePath $exe -ArgumentList $switches -Wait -PassThru
   Remove-Item $exe -Force -ErrorAction SilentlyContinue
   if ($p.ExitCode -ne 0) { throw "$name installer exited $($p.ExitCode)" }
   if (-not (Test-Path $probe)) { throw "$name : installer reported success but $probe is missing" }
   Write-Output ("$name : " + (& $probe --version 2>&1 | Out-String).Trim())
}

# InstallAllUsers=1 -> machine-wide; PrependPath=1 -> on PATH for every shell;
# Include_test=0 -> the test suite is 30 MB nobody here runs.
Install-Silent 'Python' $PY_URL @(
   '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_test=0',
   'Include_launcher=1', 'AssociateFiles=0'
) $PY_EXE $PY_VERSION

# NOTE: /VERYSILENT and /NORESTART are Inno Setup switches; /o:PathOption=Cmd
# puts git on PATH without shadowing Windows tools with the MSYS ones.
Install-Silent 'Git' $GIT_URL @(
   '/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-', '/CLOSEAPPLICATIONS',
   '/RESTARTAPPLICATIONS', '/o:PathOption=Cmd'
) $GIT_EXE $GIT_VERSION

Write-Output ''
Write-Output 'PATH additions take effect in NEW shells only - reconnect before using them.'
