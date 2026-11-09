<#
.SYNOPSIS
  One-time SSH key setup for s4l_remote.py (Windows PowerShell 5.1 or PowerShell 7).

.DESCRIPTION
  Creates an ed25519 key in ~\.ssh\id_ed25519 if you do not have one, and adds
  the public key to ~/.ssh/authorized_keys on the server. You type the server
  account password once, into ssh itself; this script never reads, stores or
  passes a password.

  The key comment is "<owner>@<computer>" so the administrator can revoke one
  person's access by deleting that line. Nothing is added if the key is
  already on the server. Connect with GlobalProtect VPN running.

.PARAMETER Passphrase
  Protect the new key with a passphrase. Note that s4l_remote.py never prompts,
  so a passphrase-protected key must be loaded into ssh-agent first.
#>
param(
    [string]$Config = (Join-Path $PSScriptRoot 'config.ini'),
    [switch]$Passphrase
)
$ErrorActionPreference = 'Stop'

# --- read host and owner from config.ini (same defaults as s4l_remote.py) ------
$hostName = 'USER@SERVER_HOSTNAME'
$owner = ''
if (Test-Path -LiteralPath $Config) {
    foreach ($line in Get-Content -LiteralPath $Config) {
        if ($line -match '^\s*host\s*=\s*(\S+)') { $hostName = $Matches[1] }
        elseif ($line -match '^\s*owner\s*=\s*(.*?)\s*$') { $owner = $Matches[1] }
    }
}
if ($hostName -match 'SERVER_HOSTNAME|^USER@') { throw "Edit 'host' in $Config first: replace USER@SERVER_HOSTNAME with the real account and server host name." }
if (-not $owner) { $owner = $env:USERNAME }
$comment = ("$owner@$env:COMPUTERNAME") -replace '\s', '_'

# --- key -------------------------------------------------------------------
$sshDir = Join-Path $HOME '.ssh'
$key = Join-Path $sshDir 'id_ed25519'
New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
if (-not (Test-Path -LiteralPath $key)) {
    Write-Host "Generating $key ($comment)"
    # Start-Process passes the argument string verbatim, so -N "" really is an empty passphrase.
    $kgArgs = "-t ed25519 -f `"$key`" -C `"$comment`""
    if (-not $Passphrase) { $kgArgs += ' -N ""' }
    Start-Process -FilePath ssh-keygen -ArgumentList $kgArgs -NoNewWindow -Wait
    if (-not (Test-Path -LiteralPath $key)) { throw 'ssh-keygen did not create a key.' }
} else {
    Write-Host "Using existing key $key"
}

# Public key line with our comment, whatever comment the key was created with.
$parts = (Get-Content -LiteralPath "$key.pub" -TotalCount 1).Trim() -split '\s+'
$pubLine = "$($parts[0]) $($parts[1]) $comment"

# --- install on the server -----------------------------------------------------
# The key travels on stdin; the remote command is single-quoted so that neither
# PowerShell nor the remote shell can reinterpret it. tr strips the CR that
# Windows PowerShell appends when piping.
$remote = 'umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; read -r line; ' +
          'line=$(echo $line | tr -d ''\r''); key=$(echo $line | cut -d '' '' -f2); ' +
          'if grep -qF -e $key ~/.ssh/authorized_keys; then echo ALREADY_PRESENT; ' +
          'else sed -i -e ''$a\'' ~/.ssh/authorized_keys; echo $line >> ~/.ssh/authorized_keys; echo ADDED; fi'

Write-Host "Connecting to $hostName - enter the account password when asked (once)."
$result = $pubLine | ssh $hostName $remote
if ($LASTEXITCODE -ne 0) { throw "ssh failed (exit $LASTEXITCODE). Is the GlobalProtect VPN connected?" }
if ($result -match 'ALREADY_PRESENT') { Write-Host 'Your key was already authorized on the server; nothing changed.' }
else { Write-Host "Key added to $hostName as '$comment'." }

# --- verify passwordless login ---------------------------------------------------
ssh -o BatchMode=yes -o ConnectTimeout=10 $hostName 'echo SSH key login works'
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Passwordless login still fails. If you set a passphrase, load the key with ssh-add first.'
    exit 1
}
