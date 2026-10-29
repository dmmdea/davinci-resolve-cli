@echo off
REM resolve.cmd -- run resolve_cli with an interpreter that was MEASURED to work.
REM
REM fusionscript.dll's interpreter compatibility can be PER-MACHINE (on some Resolve
REM builds a wrong CPython ABI crashes with 0xC0000005 in PyInit_fusionscript), so
REM deploy.ps1 runs _probe.py under each candidate interpreter and records the first
REM one that passes in python-path.txt. See README.md for the measurements.
REM Resolution order:
REM   1. python-path.txt next to this script (first line = absolute interpreter path)
REM   2. Resolve's own bundled ResolvePython.exe at the default install path
REM   3. the newest registered CPython 3.x via the py launcher
REM Never repoint at a uv/Astral build unless the probe passed it.

setlocal
set "PATH_FILE=%~dp0python-path.txt"
if not exist "%PATH_FILE%" goto :nopath
set /p RESOLVE_PY=<"%PATH_FILE%"
if exist "%RESOLVE_PY%" goto :run
echo ERROR: python-path.txt names a missing interpreter:
echo   %RESOLVE_PY%
echo Redeploy to re-probe (deploy.ps1, optionally -Python ^<abs path^>), or delete python-path.txt to fall back.
exit /b 1

:nopath
set "RESOLVE_PY=%ProgramFiles%\Blackmagic Design\DaVinci Resolve\ResolvePython\ResolvePython.exe"
if exist "%RESOLVE_PY%" goto :run

REM Fallback: the newest registered CPython 3.x via the py launcher.
REM (Kept OUTSIDE parentheses: %ERRORLEVEL% inside a parenthesized block is
REM expanded at parse time and would report a stale exit code.)
py -3 -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo ERROR: no python-path.txt, no Resolve bundled interpreter, no registered CPython 3.x.
  echo Install a 64-bit CPython and deploy with -Python ^<abs path^>.
  exit /b 1
)
py -3 "%~dp0resolve_cli.py" %*
exit /b %ERRORLEVEL%

:run
"%RESOLVE_PY%" "%~dp0resolve_cli.py" %*
