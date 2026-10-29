"""_probe.py -- measure fusionscript interpreter compatibility ON THIS MACHINE.

deploy.ps1 runs it under every candidate interpreter (in a child process, with
PYTHONHOME/PYTHONPATH removed) and records the first that passes. By hand:

    "C:\\Program Files\\Blackmagic Design\\DaVinci Resolve\\ResolvePython\\ResolvePython.exe" _probe.py
    py -3 _probe.py

Exit 0 and a "scriptapp:" line = that interpreter loads fusionscript here
("scriptapp: None" only means Resolve is not running or external scripting is
off; the import is the measurement). A 0xC0000005 access violation in
PyInit_fusionscript = it does not. On 21.0.4 the result was PER-MACHINE (same
Resolve build, different machines, different winners --
measured on two Windows machines, 2026-08). deploy.ps1 -Python <abs path> adds a candidate.
"""
import sys
import faulthandler

faulthandler.enable()
print("python", sys.version.split()[0])
sys.path.append(r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules")
import DaVinciResolveScript as dvr  # noqa: E402 - the probe IS this import

r = dvr.scriptapp("Resolve")
print("scriptapp:", (r.GetProductName() + " " + r.GetVersionString()) if r else "None (Resolve not running, or external scripting not enabled)")
