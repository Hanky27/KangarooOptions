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
Enable OpenSSH key-only access on a Windows host, run once over RDP.

.DESCRIPTION
Installs the OpenSSH server, makes PowerShell the remote shell and installs
ONE public key for the current account.

THE TRAP THIS SCRIPT EXISTS FOR: for a member of the local Administrators
group, sshd does NOT read ~/.ssh/authorized_keys. It reads
C:\ProgramData\ssh\administrators_authorized_keys, and it IGNORES that file
unless its ACL grants only SYSTEM and Administrators. Both mistakes fail the
same silent way - "Permission denied (publickey)" with a server log that says
the key was offered. The script picks the right file for the account and
sets the ACL.

Password authentication is switched OFF: this host answers on a public IP,
and a password-enabled sshd on a public port is brute-forced continuously
(measured on the sibling VPS 85.215.177.188 before it was closed off).

.PARAMETER PublicKey
The full one-line public key, e.g. "ssh-ed25519 AAAA... comment".

.PARAMETER AllowFrom
Optional. Restrict inbound port 22 to this address or CIDR. Leave empty to
allow any source - only sensible while a VPN (Tailscale) is not yet up,
because key-only sshd on a public port still gets hammered.

.EXAMPLE
.\setup_ssh_windows.ps1 -PublicKey "ssh-ed25519 AAAA... claude" -AllowFrom 1.2.3.4
#>

[CmdletBinding()]
param(
   [Parameter(Mandatory = $true)][string]$PublicKey,
   [string]$AllowFrom = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }

# --- 0. must be elevated ------------------------------------------------
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
   throw "Run this in an ELEVATED PowerShell (right-click > Run as administrator)."
}
if ($PublicKey -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-)') {
   throw "PublicKey does not look like a public key: '$PublicKey'"
}

# --- 1. OpenSSH server ---------------------------------------------------
Step 'Installing the OpenSSH server capability'
$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*'
if ($cap.State -ne 'Installed') { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
Write-Host ("    " + (Get-WindowsCapability -Online -Name 'OpenSSH.Server*').State)

Step 'Service to Automatic and started'
Set-Service -Name sshd -StartupType Automatic
Start-Service -Name sshd
Write-Host ("    sshd: " + (Get-Service sshd).Status)

# --- 2. PowerShell as the remote shell -----------------------------------
# Without this the remote shell is cmd.exe, and every PowerShell one-liner
# sent over ssh dies on the first cmdlet.
Step 'Default remote shell = PowerShell'
if (-not (Test-Path 'HKLM:\SOFTWARE\OpenSSH')) {
   New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
}
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
   -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' `
   -PropertyType String -Force | Out-Null

# --- 3. the public key, in the file sshd actually reads ------------------
$isAdmin = $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
   $keyFile = 'C:\ProgramData\ssh\administrators_authorized_keys'
   Step "Installing the key for an ADMIN account -> $keyFile"
}
else {
   $keyFile = Join-Path $env:USERPROFILE '.ssh\authorized_keys'
   Step "Installing the key for a standard account -> $keyFile"
}
$dir = Split-Path $keyFile -Parent
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
$existing = if (Test-Path $keyFile) { Get-Content $keyFile } else { @() }
if ($existing -contains $PublicKey) {
   Write-Host '    key already present - not added twice'
}
else {
   # UTF8 WITHOUT BOM: sshd does not strip a byte-order mark, it reads it as
   # part of the key type and the line never matches.
   $lines = @($existing | Where-Object { $_ -and $_.Trim() }) + $PublicKey
   [IO.File]::WriteAllLines($keyFile, $lines,
      (New-Object Text.UTF8Encoding($false)))
   Write-Host "    key appended ($($lines.Count) key(s) in the file)"
}
if ($isAdmin) {
   # sshd REFUSES this file unless only SYSTEM and Administrators can write it.
   icacls $keyFile /inheritance:r /grant 'SYSTEM:F' /grant 'Administratoren:F' /grant 'Administrators:F' 2>$null | Out-Null
   Write-Host '    ACL restricted to SYSTEM + Administrators'
}

# --- 4. key-only ---------------------------------------------------------
Step 'Password authentication OFF (key only)'
$cfg = 'C:\ProgramData\ssh\sshd_config'
Copy-Item $cfg "$cfg.bak-$(Get-Date -Format yyyyMMdd-HHmmss)" -Force
$txt = Get-Content $cfg
$txt = $txt -replace '^\s*#?\s*PasswordAuthentication\s+.*$', 'PasswordAuthentication no'
if ($txt -notmatch '^PasswordAuthentication no') { $txt += 'PasswordAuthentication no' }
$txt = $txt -replace '^\s*#?\s*PubkeyAuthentication\s+.*$', 'PubkeyAuthentication yes'
if ($txt -notmatch '^PubkeyAuthentication yes') { $txt += 'PubkeyAuthentication yes' }
[IO.File]::WriteAllLines($cfg, $txt, (New-Object Text.UTF8Encoding($false)))

# --- 5. firewall ---------------------------------------------------------
Step 'Firewall rule for port 22'
$rule = Get-NetFirewallRule -Name 'sshd-kangaroo' -ErrorAction SilentlyContinue
if ($rule) { Remove-NetFirewallRule -Name 'sshd-kangaroo' }
$p = @{ Name = 'sshd-kangaroo'; DisplayName = 'OpenSSH Server (Kangaroo)'
   Enabled = 'True'; Direction = 'Inbound'; Protocol = 'TCP'
   LocalPort = 22; Action = 'Allow'
}
if ($AllowFrom) { $p['RemoteAddress'] = $AllowFrom }
New-NetFirewallRule @p | Out-Null
Write-Host ("    port 22 open for: " + $(if ($AllowFrom) { $AllowFrom } else { 'ANY (tighten this once a VPN is up)' }))

Step 'Restarting sshd so every change takes effect'
Restart-Service sshd
Write-Host ("    sshd: " + (Get-Service sshd).Status)

Write-Host ''
Write-Host 'Done. Verify FROM THE OTHER MACHINE, not here:' -ForegroundColor Green
Write-Host ('    ssh -i <privatekey> ' + $env:USERNAME + '@<this-host-ip> "hostname"')
Write-Host ''
Write-Host ("Account used for the key: " + $env:USERNAME + " (admin: $isAdmin)")
Write-Host ("Key file: " + $keyFile)
