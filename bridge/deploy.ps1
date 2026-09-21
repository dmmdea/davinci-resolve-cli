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
  3. Interpreter: fusionscript's working CPython is PER-MACHINE (measure with
     _probe.py). With -PythonPin <abs path>, writes python-pin.txt and the
     launcher uses that interpreter (no embeddable provisioning). Without a
     pin, provisions the 3.12.10 embeddable from python-manifest.json if
     absent (download, SHA256-verify, expand) and ALWAYS overwrites
     python312._pth with the versioned copy (declarative, never append).
  4. Removes files a previous deploy placed that are no longer in the file
     manifest (tracked in deployed-manifest.json on the target) -- no ghost
     modules.

Remote mode (-Target user@host, over OpenSSH to a Windows machine): every ssh/scp/curl/git call is exit-code-checked -- a
transport failure throws instead of cascading into a false "deploy complete"
(-SkipVerify only skips the final identity check, never the per-step checks).
Local mode (-Local): same steps with filesystem operations on this machine.

Usage:  pwsh bridge/deploy.ps1 -Target user@editing-pc [-Dest 'C:/ResolveTools'] [-SkipVerify]
        pwsh bridge/deploy.ps1 -Local [-Dest 'C:/ResolveTools']
               [-PythonPin 'C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolvePython\ResolvePython.exe']
#>
param(
    [string]$Target = '',
    [string]$Dest = 'C:/ResolveTools',
    [switch]$SkipVerify,
    [switch]$Local,
    [string]$PythonPin = ''
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

if ($PythonPin -and -not $Local) {
    # The pin names a path on the TARGET machine; existence can only be checked
    # there. Keep remote pin support, but verify the path remotely first.
    $pinOk = ((ssh $Target "Test-Path '$PythonPin'") -join "`n").Trim()
    Assert-Native 'ssh (pin path check)'
    if ($pinOk -ne 'True') { throw "-PythonPin path not found on ${Target}: $PythonPin" }
}
if ($PythonPin -and $Local -and -not (Test-Path $PythonPin)) {
    throw "-PythonPin path not found locally: $PythonPin"
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

    if ($PythonPin) {
        Write-Host "== interpreter: per-machine pin -> $PythonPin =="
        Set-Content -Path (Join-Path $stage 'python-pin.txt') -Value $PythonPin -Encoding ascii -NoNewline
    }
    elseif ($Local) {
        Write-Host '== interpreter: ensure python312 locally =='
        $manifest = Get-Content (Join-Path $here 'python-manifest.json') -Raw | ConvertFrom-Json
        if (-not (Test-Path "$DestW\python312\python.exe")) {
            Write-Host ("python312 absent - provisioning " + $manifest.python_version)
            $zip = Join-Path $stage 'python-embed.zip'
            curl.exe -sSLo $zip $manifest.url
            Assert-Native 'curl (interpreter download)'
            $hash = (Get-FileHash $zip -Algorithm SHA256).Hash
            if ($hash -ne $manifest.sha256) { throw "interpreter zip SHA256 mismatch: got $hash, manifest says $($manifest.sha256)" }
            Expand-Archive $zip -DestinationPath "$DestW\python312" -Force
        }
        Copy-Item (Join-Path $here 'python312._pth') "$DestW\python312\python312._pth" -Force
    }
    else {
        Write-Host '== interpreter: ensure python312 on the target =='
        $manifest = Get-Content (Join-Path $here 'python-manifest.json') -Raw | ConvertFrom-Json
        $havePy = ((ssh $Target "Test-Path '$DestW\python312\python.exe'") -join "`n").Trim()
        Assert-Native 'ssh (python312 presence check)'
        if ($havePy -notin @('True', 'False')) {
            throw "could not determine python312 presence: remote answered '$havePy' (expected True/False)"
        }
        if ($havePy -eq 'False') {
            Write-Host ("python312 absent on target - provisioning " + $manifest.python_version)
            $zip = Join-Path $stage 'python-embed.zip'
            curl.exe -sSLo $zip $manifest.url
            Assert-Native 'curl (interpreter download)'
            $hash = (Get-FileHash $zip -Algorithm SHA256).Hash
            if ($hash -ne $manifest.sha256) { throw "interpreter zip SHA256 mismatch: got $hash, manifest says $($manifest.sha256)" }
            scp $zip "${Target}:$Dest/_python-embed.zip"
            Assert-Native 'scp (interpreter zip)'
            ssh $Target "Expand-Archive '$DestW\_python-embed.zip' -DestinationPath '$DestW\python312' -Force; Remove-Item '$DestW\_python-embed.zip' -Force"
            Assert-Native 'ssh (interpreter expand)'
        }
        # Declarative ._pth: always overwrite the stock file with the versioned one.
        scp (Join-Path $here 'python312._pth') "${Target}:$Dest/python312/python312._pth"
        Assert-Native 'scp (._pth)'
    }

    Write-Host '== files: copy manifest set, remove ghosts =='
    if ($Local) {
        New-Item -ItemType Directory -Force $DestW | Out-Null
        $prevJson = if (Test-Path "$DestW\deployed-manifest.json") { Get-Content "$DestW\deployed-manifest.json" -Raw } else { '' }
    }
    else {
        $prevJson = ((ssh $Target "if (Test-Path '$DestW\deployed-manifest.json') { Get-Content '$DestW\deployed-manifest.json' -Raw }") -join "`n")
        Assert-Native 'ssh (previous manifest read)'
    }
    $newSet = $Files + @('build-info.json')
    if ($PythonPin) { $newSet += 'python-pin.txt' }
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
    $srcPaths = ($Files | ForEach-Object { Join-Path $here $_ }) + (Join-Path $stage 'build-info.json')
    if ($PythonPin) { $srcPaths += (Join-Path $stage 'python-pin.txt') }
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
