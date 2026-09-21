@echo off
REM resolve.cmd -- run resolve_cli with a KNOWN-WORKING interpreter.
REM
REM fusionscript.dll's interpreter compatibility is PER-MACHINE, not per-Resolve
REM build: on machine A (Resolve 21.0.4.5) only CPython 3.12.x loads cleanly
REM (3.11.9/3.14.7 crash 0xC0000005 in PyInit_fusionscript; measured 2026-08-23),
REM while on machine B (same 21.0.4.5 build) system 3.14.7 works and the 3.12.10
REM embeddable crashes (measured 2026-08-28). Measure with _probe.py on each
REM machine and record the winner via deploy.ps1 -PythonPin. Resolution order:
REM   1. python-pin.txt next to this script (first line = absolute python.exe)
REM   2. bundled python312\python.exe (the embeddable default)
REM   3. any registered CPython 3.12 via the py launcher
REM Never repoint at a uv/Astral build.

setlocal
set "PIN_FILE=%~dp0python-pin.txt"
if not exist "%PIN_FILE%" goto :nopin
set /p RESOLVE_PY=<"%PIN_FILE%"
if exist "%RESOLVE_PY%" goto :run
echo ERROR: python-pin.txt names a missing interpreter:
echo   %RESOLVE_PY%
echo Fix the pin (deploy.ps1 -PythonPin) or delete python-pin.txt to use the bundled default.
exit /b 1

:nopin
set "RESOLVE_PY=%~dp0python312\python.exe"
if exist "%RESOLVE_PY%" goto :run

REM Fallback: any registered CPython 3.12 via the py launcher.
REM (Kept OUTSIDE parentheses: %ERRORLEVEL% inside a parenthesized block is
REM expanded at parse time and would report a stale exit code.)
py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo ERROR: no pinned interpreter, no bundled python312\, no registered CPython 3.12.
  echo Deploy with -PythonPin ^<abs path^> after measuring _probe.py on this machine,
  echo or restore python312\ from python.org: python-3.12.10-embed-amd64.zip
  exit /b 1
)
py -3.12 "%~dp0resolve_cli.py" %*
exit /b %ERRORLEVEL%

:run
"%RESOLVE_PY%" "%~dp0resolve_cli.py" %*
