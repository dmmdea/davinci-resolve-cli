"""_probe.py -- measure fusionscript interpreter compatibility ON THIS MACHINE.

Run it under each candidate CPython against a RUNNING Resolve:

    "C:\\Program Files\\Python314\\python.exe" _probe.py
    python312\\python.exe _probe.py

Exit 0 + "scriptapp: DaVinci Resolve Studio ..." = that interpreter works here;
a 0xC0000005 access violation in PyInit_fusionscript = it does not. The result
is PER-MACHINE (same Resolve build, different machines, different winners --
measured on two Windows machines, 2026-08). Record the winner via deploy.ps1 -PythonPin.
"""
import sys
import faulthandler

faulthandler.enable()
print("python", sys.version.split()[0])
sys.path.append(r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules")
import DaVinciResolveScript as dvr  # noqa: E402 - the probe IS this import

r = dvr.scriptapp("Resolve")
print("scriptapp:", (r.GetProductName() + " " + r.GetVersionString()) if r else "None (Resolve not running, or external scripting not enabled)")
