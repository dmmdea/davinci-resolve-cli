"""Connect to a running DaVinci Resolve Studio instance.

Usage:
    from connect import get_resolve
    resolve = get_resolve()

REQUIREMENTS (both are hard requirements, not preferences):
  1. DaVinci Resolve STUDIO must be RUNNING. External scripting is a Studio-only
     feature -- the free version only allows scripts from Resolve's own console.
  2. Use CPython 3.12.x -- the bundled interpreter next to this file:
         python312\\python.exe   (python.org 3.12.10 embeddable)
     Measured on Resolve 21.0.4.5 (2026-08-23): 3.12.10 works; 3.11.9 and
     3.14.7 crash 0xC0000005 in PyInit_fusionscript. uv/Astral builds are
     also known-bad (crash reported earlier; not re-measured on this build).

Resolve must also permit external scripting:
     Resolve > Preferences > System > General >
       "External scripting using" = Local
"""

import os
import sys

# Machine-scope env vars are set by the installer, but a process started before
# they existed will not see them -- fall back to the documented default paths.
_DEFAULT_API = r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
_DEFAULT_LIB = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"


def _ensure_env():
    """Make the Resolve scripting module importable in this process."""
    api = os.environ.get("RESOLVE_SCRIPT_API") or _DEFAULT_API
    lib = os.environ.get("RESOLVE_SCRIPT_LIB") or _DEFAULT_LIB
    os.environ.setdefault("RESOLVE_SCRIPT_API", api)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", lib)

    modules = os.path.join(api, "Modules")
    if not os.path.isdir(modules):
        raise RuntimeError(
            f"Resolve scripting modules not found at:\n  {modules}\n"
            "Is DaVinci Resolve Studio installed?"
        )
    if modules not in sys.path:
        sys.path.append(modules)
    return lib


def get_resolve():
    """Return the Resolve app object, or raise a RuntimeError explaining why not."""
    lib = _ensure_env()

    try:
        import DaVinciResolveScript as dvr  # noqa: N813  (vendor module name)
    except ImportError as exc:
        raise RuntimeError(
            f"Could not import DaVinciResolveScript: {exc}\n"
            "Check RESOLVE_SCRIPT_API / PYTHONPATH."
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Failed loading {lib}: {exc}\n"
            "This usually means the wrong interpreter -- use CPython 3.12.x "
            "(the bundled python312\\python.exe)."
        ) from exc

    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise RuntimeError(
            "Resolve is not reachable. Check, in order:\n"
            "  1. DaVinci Resolve STUDIO is running (free version cannot do this).\n"
            "  2. Preferences > System > General > 'External scripting using' = Local.\n"
            "  3. This is CPython 3.12.x (the bundled python312\\python.exe)."
        )
    return resolve


def get_project():
    """Return the currently open project (raises if none)."""
    pm = get_resolve().GetProjectManager()
    project = pm.GetCurrentProject()
    if project is None:
        raise RuntimeError("No project is currently open in Resolve.")
    return project


if __name__ == "__main__":
    r = get_resolve()
    print(f"Connected: {r.GetProductName()} {r.GetVersionString()}")
