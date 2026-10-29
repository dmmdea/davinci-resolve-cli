#Requires -Version 7
<#
deploy.ps1 -- deploy the Resolve bridge from this repo to a machine.

The repo is the SOURCE OF TRUTH; the deployed copy (e.g. C:\ResolveTools)
is a build artifact and must never be hand-edited. This script is the only
sanctioned write path. It is idempotent and convergent:

  1. Gates: py_compile + check_readonly.py must pass (read-safety is enforced,
     not promised).
  2. Stamps the git SHA into build-info.json; `resolve_cli --version` reports
     it, and the post-deploy check asserts the deployed CLI answers with THIS
     commit's SHA (deployed tree == pure function of a repo commit).
  3. Interpreter: chosen by MEASUREMENT on the target machine, never assumed
     (fusionscript's working CPython is per-machine on some Resolve builds).
     Candidates, in order: the -Python path if given; Resolve's own bundled
     ResolvePython\ResolvePython.exe; every 64-bit CPython 3.x that `py -0p`
     lists, newest first. _probe.py runs under each one in a child process with
     PYTHONHOME/PYTHONPATH removed, so a crash fails that candidate, not the
     deploy. The first candidate that passes is written to python-path.txt,
     which resolve.cmd reads. Nothing is downloaded or provisioned.
  4. Removes files a previous deploy placed that are no longer in the file
     manifest (tracked in deployed-manifest.json on the target) -- no ghost
     modules -- plus the python-pin.txt and python312\ left by older deploys.

Remote mode (-Target user@host, over OpenSSH to a Windows machine): every ssh/scp/git call is exit-code-checked -- a
transport failure throws instead of cascading into a false "deploy complete"
(-SkipVerify only skips the final identity check, never the per-step checks).
The interpreter lookup and probe run ON the target over ssh.
Local mode (-Local): same steps with filesystem operations on this machine.

Usage:  pwsh bridge/deploy.ps1 -Target user@editing-pc [-Dest 'C:/ResolveTools'] [-SkipVerify]
        pwsh bridge/deploy.ps1 -Local [-Dest 'C:/ResolveTools']
               [-Python 'C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolvePython\ResolvePython.exe']
#>
param(
    [string]$Target = '',
    [string]$Dest = 'C:/ResolveTools',
    [switch]$SkipVerify,
    [switch]$Local,
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
if (-not $Local -and -not $Target) { throw 'pass -Target user@host for a remote deploy, or -Local' }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$DestW = $Dest -replace '/', '\'
$Files = @('connect.py', 'resolve_cli.py', 'resolve.cmd', 'README.md', 'check_readonly.py', '_probe.py',
           'serve_http.py', 'ops_common.py', 'ops_project.py', 'ops_media.py', 'ops_timeline.py',
           'ops_color.py', 'ops_render.py')

function Assert-Native([string]$what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

# Run a PowerShell script text on the target (this machine, or over ssh) and return its
# stdout as one string. Positional string args reach the script's param() block.
function Invoke-Target([string]$ScriptText, [string[]]$ScriptArgs) {
    if ($Local) {
        return ((& ([scriptblock]::Create($ScriptText)) @ScriptArgs) | Out-String)
    }
    $quoted = ($ScriptArgs | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ' '
    $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes("& {`n$ScriptText`n} $quoted"))
    $out = (ssh $Target "powershell -NoProfile -NonInteractive -EncodedCommand $b64") -join "`n"
    Assert-Native 'ssh (remote script)'
    return $out
}

# Runs ON the target (Windows PowerShell 5.1 compatible). Builds the candidate list,
# probes each one, prints one marker line of JSON.
$SelectScript = @'
param([string]$Explicit, [string]$Probe)
$ErrorActionPreference = 'Continue'; $ProgressPreference = 'SilentlyContinue'
function Run([string]$exe, [string]$argLine) {
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $exe; $psi.Arguments = $argLine; $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.CreateNoWindow = $true
    foreach ($v in 'PYTHONHOME', 'PYTHONPATH') { [void]$psi.EnvironmentVariables.Remove($v) }
    try { $p = [Diagnostics.Process]::Start($psi) } catch { return @{ exit = -1; out = "cannot start: $($_.Exception.Message)" } }
    $o = $p.StandardOutput.ReadToEndAsync(); $e = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit(60000)) { try { $p.Kill() } catch {}; return @{ exit = -2; out = 'timeout after 60s' } }
    $p.WaitForExit()
    return @{ exit = $p.ExitCode; out = (($o.Result + ' ' + $e.Result).Trim()) }
}
$c = New-Object Collections.ArrayList; $skip = New-Object Collections.ArrayList
function Add-C($path, $src) { if ($path -and -not ($c | Where-Object { $_.path -ieq $path })) { [void]$c.Add([pscustomobject]@{ path = $path; source = $src }) } }
if ($Explicit) { Add-C $Explicit '-Python' }
$d = New-Object Collections.ArrayList
$lib = [Environment]::GetEnvironmentVariable('RESOLVE_SCRIPT_LIB', 'Machine')
if ($lib) { [void]$d.Add((Split-Path -Parent $lib)) }
foreach ($k in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*') {
    foreach ($p in @(Get-ItemProperty $k -ErrorAction SilentlyContinue)) { if ($p.DisplayName -eq 'DaVinci Resolve' -and $p.InstallLocation) { [void]$d.Add($p.InstallLocation) } }
}
[void]$d.Add((Join-Path $env:ProgramFiles 'Blackmagic Design\DaVinci Resolve'))
foreach ($x in $d) { $rp = Join-Path $x 'ResolvePython\ResolvePython.exe'; if (Test-Path -LiteralPath $rp) { Add-C $rp 'resolve-bundled'; break } }
$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($py) {
    $found = @()
    foreach ($ln in ((Run $py.Source '-0p').out -split '\r?\n')) {
        if ($ln -notmatch '([A-Za-z]:\\.+\.exe)\s*$') { continue }
        $path = $Matches[1]
        $i = Run $path '-c "import sys,struct;print(sys.version_info[0],sys.version_info[1],sys.version_info[2],struct.calcsize(''P'')*8)"'
        $n = @([regex]::Matches($i.out, '\d+') | ForEach-Object { [int]$_.Value })
        if ($i.exit -ne 0 -or $n.Count -ne 4) { [void]$skip.Add("$path (version query failed)"); continue }
        if ($n[0] -lt 3) { [void]$skip.Add("$path (Python $($n[0]).$($n[1]), not 3.x)"); continue }
        if ($n[3] -ne 64) { [void]$skip.Add("$path ($($n[3])-bit)"); continue }
        $found += [pscustomobject]@{ path = $path; a = $n[0]; b = $n[1]; c = $n[2] }
    }
    foreach ($f in ($found | Sort-Object -Property @{ e = 'a'; Descending = $true }, @{ e = 'b'; Descending = $true }, @{ e = 'c'; Descending = $true })) { Add-C $f.path 'py -0p' }
}
$res = foreach ($x in $c) {
    $r = Run $x.path ('"' + $Probe + '"')
    $tail = $r.out -replace '\s+', ' '
    if ($tail.Length -gt 200) { $tail = $tail.Substring($tail.Length - 200) }
    [pscustomobject]@{ path = $x.path; source = $x.source; exit = $r.exit; ok = ($r.exit -eq 0 -and $r.out -match 'scriptapp:'); detail = $tail }
}
'CANDIDATES-JSON:' + (ConvertTo-Json -InputObject @{ candidates = @($res); skipped = @($skip) } -Compress -Depth 4)
'@

# Convergent cleanup of what older deploys left in Dest: python-pin.txt and the bundled
# python312\ embeddable. The directory is removed only if it really lies inside Dest.
$CleanupScript = @'
param([string]$DestDir)
$root = [IO.Path]::GetFullPath($DestDir).TrimEnd('\') + '\'
Remove-Item -LiteralPath (Join-Path $root 'python-pin.txt') -Force -ErrorAction SilentlyContinue
$old = [IO.Path]::GetFullPath((Join-Path $root 'python312'))
if (-not (Test-Path -LiteralPath $old)) { return }
if (-not $old.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { 'refused: python312 is outside Dest: ' + $old; return }
if ((Get-Item -LiteralPath $old -Force).LinkType) { [IO.Directory]::Delete($old) } else { Remove-Item -LiteralPath $old -Recurse -Force }
'removed stale ' + $old
'@

function Select-Interpreter([string]$Explicit, [string]$ProbePath) {
    Write-Host '== interpreter: probe candidates (_probe.py in a child process, PYTHONHOME/PYTHONPATH removed) =='
    $raw = Invoke-Target $SelectScript @($Explicit, $ProbePath)
    $line = @($raw -split "`r?`n" | Where-Object { $_ -like 'CANDIDATES-JSON:*' }) | Select-Object -Last 1
    if (-not $line) { throw "interpreter probe produced no result; target said: $raw" }
    $r = $line.Substring('CANDIDATES-JSON:'.Length) | ConvertFrom-Json
    foreach ($s in @($r.skipped)) { if ($s) { Write-Host "  [skip] $s" } }
    $chosen = $null
    foreach ($c in @($r.candidates)) {
        $code = '0x{0:X8}' -f [int]$c.exit
        if ($c.ok) {
            Write-Host "  [pass] $($c.path) ($($c.source)) exit $code :: $($c.detail)"
            if (-not $chosen) { $chosen = $c }
        }
        else { Write-Host "  [FAIL] $($c.path) ($($c.source)) exit $code :: $($c.detail)" }
    }
    if (-not $chosen) {
        $lines = @($r.candidates | ForEach-Object { "  $($_.path) ($($_.source)) exit $('0x{0:X8}' -f [int]$_.exit)" }) -join "`n"
        throw ("no interpreter passed _probe.py on this machine. Candidates tried:`n$lines`n" +
               'Install a 64-bit CPython that _probe.py accepts (its import of DaVinciResolveScript must succeed) and pass it with -Python <abs path>.')
    }
    if ($Explicit -and $chosen.path -ine $Explicit) {
        Write-Warning "-Python '$Explicit' did not pass the probe; using $($chosen.path) instead"
    }
    return $chosen.path
}

if ($Python -and -not $Local) {
    # -Python names a path on the TARGET machine; existence can only be checked there.
    $pyOk = ((ssh $Target "Test-Path '$Python'") -join "`n").Trim()
    Assert-Native 'ssh (-Python path check)'
    if ($pyOk -ne 'True') { throw "-Python path not found on ${Target}: $Python" }
}
if ($Python -and $Local -and -not (Test-Path $Python)) {
    throw "-Python path not found locally: $Python"
}

Write-Host '== gate: py_compile + check_readonly =='
$pyFiles = @($Files | Where-Object { $_ -like '*.py' } | ForEach-Object { Join-Path $here $_ })
py -3 -m py_compile @pyFiles
Assert-Native 'py_compile'
py -3 (Join-Path $here 'check_readonly.py')
Assert-Native 'check_readonly (a read path calls a mutating API?)'

Write-Host '== stamp: build-info.json =='
$sha = (git -C $here rev-parse HEAD | Out-String).Trim()
Assert-Native 'git rev-parse'
if (-not $sha -or $sha.Length -lt 9) { throw "git rev-parse produced no usable SHA (got '$sha')" }
$dirty = [bool](git -C $here status --porcelain -- $here)
Assert-Native 'git status'
if ($dirty) { Write-Warning 'working tree dirty under bridge/ - deploying uncommitted state' }
$stage = Join-Path ([IO.Path]::GetTempPath()) ("resolve-bridge-deploy-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    @{ sha = $sha; dirty = $dirty; deployed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); source = 'davinci-resolve-cli/bridge' } |
        ConvertTo-Json | Set-Content -Path (Join-Path $stage 'build-info.json') -Encoding utf8

    if ($Local) {
        $probePath = Join-Path $here '_probe.py'
    }
    else {
        # The probe must run on the target: make sure Dest exists and put _probe.py there.
        ssh $Target "New-Item -ItemType Directory -Force '$DestW' | Out-Null"
        Assert-Native 'ssh (create Dest)'
        scp (Join-Path $here '_probe.py') "${Target}:$Dest/_probe.py"
        Assert-Native 'scp (_probe.py)'
        $probePath = "$DestW\_probe.py"
    }
    $chosenPython = Select-Interpreter $Python $probePath
    Write-Host "== interpreter: chosen -> $chosenPython =="
    Set-Content -Path (Join-Path $stage 'python-path.txt') -Value $chosenPython -Encoding ascii -NoNewline

    Write-Host '== files: copy manifest set, remove ghosts =='
    if ($Local) {
        New-Item -ItemType Directory -Force $DestW | Out-Null
        $prevJson = if (Test-Path "$DestW\deployed-manifest.json") { Get-Content "$DestW\deployed-manifest.json" -Raw } else { '' }
    }
    else {
        $prevJson = ((ssh $Target "if (Test-Path '$DestW\deployed-manifest.json') { Get-Content '$DestW\deployed-manifest.json' -Raw }") -join "`n")
        Assert-Native 'ssh (previous manifest read)'
    }
    $newSet = $Files + @('build-info.json', 'python-path.txt')
    if ($prevJson.Trim()) {
        $prev = $null
        try { $prev = ($prevJson | ConvertFrom-Json).files }
        catch { Write-Warning 'previous deployed-manifest.json unreadable - skipping ghost cleanup this deploy (will self-heal next deploy)' }
        foreach ($ghost in @($prev | Where-Object { $_ -and ($newSet -notcontains $_) })) {
            Write-Host "  removing ghost: $ghost"
            if ($Local) { Remove-Item "$DestW\$ghost" -Force -ErrorAction SilentlyContinue }
            else {
                ssh $Target "Remove-Item '$DestW\$ghost' -Force -ErrorAction SilentlyContinue"
                Assert-Native "ssh (ghost removal: $ghost)"
            }
        }
    }
    $srcPaths = ($Files | ForEach-Object { Join-Path $here $_ }) + (Join-Path $stage 'build-info.json') + (Join-Path $stage 'python-path.txt')
    @{ files = $newSet; sha = $sha } | ConvertTo-Json | Set-Content -Path (Join-Path $stage 'deployed-manifest.json') -Encoding utf8
    if ($Local) {
        Copy-Item $srcPaths $DestW -Force
        Copy-Item (Join-Path $stage 'deployed-manifest.json') $DestW -Force
    }
    else {
        scp @srcPaths "${Target}:$Dest/"
        Assert-Native 'scp (bridge files)'
        scp (Join-Path $stage 'deployed-manifest.json') "${Target}:$Dest/"
        Assert-Native 'scp (deployed manifest)'
    }
    $cleaned = ([string](Invoke-Target $CleanupScript @($DestW))).Trim()
    if ($cleaned) { Write-Host "  $cleaned" }

    if (-not $SkipVerify) {
        Write-Host '== verify: deployed CLI answers with THIS commit SHA =='
        if ($Local) {
            $ver = (cmd /c "`"$DestW\resolve.cmd`" --version" 2>&1) -join "`n"
            Assert-Native 'local post-deploy verify'
        }
        else {
            $ver = ((ssh $Target "cmd /c '$DestW\resolve.cmd --version' 2>&1") -join "`n")
            Assert-Native 'ssh (post-deploy verify)'
        }
        Write-Host "  target says: $ver"
        if ($ver -notmatch [regex]::Escape($sha.Substring(0, 9))) {
            throw "deploy verification FAILED: deployed CLI does not report SHA $($sha.Substring(0,9))"
        }
        Write-Host '  PASS: deployed tree is this commit.'
    }
}
finally {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
}
$where = if ($Local) { 'LOCAL' } else { $Target }
Write-Host "deploy complete: $sha -> $where $Dest"
