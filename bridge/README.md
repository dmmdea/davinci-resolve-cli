# bridge — drive DaVinci Resolve Studio from the command line

`resolve_cli.py` inspects and controls a running DaVinci Resolve Studio through Blackmagic's
official scripting API. It exposes **146 commands** (`resolve.cmd --help`), organised in
`ops_common.py` (registry + lookups) and five domain modules:

- `ops_project.py`: project manager, database, settings, presets, media storage, clone tool
- `ops_media.py`: bins, clips, markers, flags, proxies, audio mapping, AI passes, `transcription` readback
- `ops_timeline.py`: timelines, tracks, items, markers, timecode, settings, generators,
  compound/Fusion clips, stills, timeline AI, transitions, speed, fades, multicam
- `ops_color.py`: grade versions, LUT/CDL/DRX, groups, gallery, Fusion tool scripting
- `ops_render.py`: formats, settings, presets, job queue, quick export, `verify-render`,
  system presets, DCTL, TTS

`python resolve_cli.py serve --port 18800` puts a loopback HTTP face on the same handlers
(`GET /v1/commands` describes every route and argument). The Go CLI in [`../cli`](../cli) is
generated from that catalog. Command semantics and the measurements behind each guard are in
[`../docs`](../docs) (`09-resolve-21-1.md`, `10-sidecar-and-cli.md`).

Verified live against Studio **21.0.4.5** and **21.1.0.14** on Windows 11, headless and GUI.
Two rules from those measurements: `archive-project` is **refused** because
`ProjectManager.ArchiveProject` crashed Resolve 21.1.0.14 when called from a script, and
`multicam create` defaults `createBinForSourceClips` OFF because a moved-source multicam clip
cannot be appended.

## Two hard requirements

1. **Resolve STUDIO, running.** External scripting has been Studio-only since Resolve 19.1, and
   Studio must be activated on the Windows account whose session runs Resolve. A client script in
   ANOTHER session/account on the same machine can connect to it. Enable scripting once per account:

   > **Resolve → Preferences → System → General → "External scripting using" → Local**

   Then restart Resolve. Do not hand-edit `Resolve.conf`; Resolve rewrites it on exit.

2. **A Python that `fusionscript.dll` accepts.**
   - **Resolve 21.1:** every 64-bit CPython 3.11–3.14 bound in testing. Resolve ships its own
     interpreter, `C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolvePython\ResolvePython.exe`
     (`import DaVinciResolveScript` needs no env vars). That is the recommended pin.
   - **Resolve 21.0.4:** compatibility was **per-machine**. The same Resolve build accepted
     different interpreters on different machines. Results of `import DaVinciResolveScript`
     (`_probe.py`):

   | Machine (Resolve 21.0.4.5) | Interpreter | Result |
   |---|---|---|
   | machine A | CPython 3.12.10 (bundled `python312\`) | works |
   | machine A | CPython 3.11.9 / 3.14.7 | crash 0xC0000005 in `PyInit_fusionscript` |
   | machine B | CPython 3.14.7 (system install) | works |
   | machine B | CPython 3.12.10 embeddable | crash 0xC0000005 |
   | both | `fuscript.exe` (Resolve's own host, Lua or `-l py3`) | works |

   Measure with `_probe.py` on each machine after any Resolve upgrade or Python change. The pin is a
   crash fix, not a preference. A 0xC0000005 crash is an interpreter mismatch; the free edition
   instead answers `scriptapp("Resolve")` with `None`.

## Install

From a clone of this repository, on the Windows machine that runs Resolve:

```powershell
pwsh bridge/deploy.ps1 -Local -Dest 'C:/ResolveTools' `
    -PythonPin 'C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolvePython\ResolvePython.exe'
```

Or deploy to another Windows machine over OpenSSH:

```powershell
pwsh bridge/deploy.ps1 -Target user@editing-pc -Dest 'C:/ResolveTools' [-PythonPin '<python.exe on that machine>']
```

The deploy is idempotent and convergent:

1. It runs `py_compile` and `check_readonly.py` as gates.
2. It SHA-stamps `build-info.json`.
3. It sets up the interpreter. With `-PythonPin`, it writes `python-pin.txt`, which the launcher
   prefers. Without a pin, it provisions the 3.12.10 embeddable from `python-manifest.json` if
   absent (SHA256-verified) and always overwrites `python312\python312._pth` with the versioned copy.
4. It removes files left by previous deploys that are no longer in the file list.
5. It asserts that the target's `resolve.cmd --version` answers with this commit's SHA.

Treat the deployed copy as a build artifact and do not hand-edit it.

`resolve.cmd` resolves the interpreter in this order:

1. `python-pin.txt`
2. the bundled `python312\`
3. a registered `py -3.12`

You can also run the bridge straight from the clone with any compatible Python:
`python bridge/resolve_cli.py status`.

## Quick start

Resolve Studio must be **open**. Then, from the deploy folder:

```
resolve.cmd status                 # version, page, current project, current timeline
resolve.cmd projects               # all projects in the database
resolve.cmd projects --attributes  # + per-project attributes (never opens them)
resolve.cmd timelines              # timelines in the open project
resolve.cmd clips                  # clips on the current timeline
resolve.cmd clips --audio          # + audio tracks
resolve.cmd clips --timeline "My Reel v3"   # a NAMED timeline (read-safe: never switches the GUI)
resolve.cmd selection              # what the editor has selected right now
resolve.cmd pool --recursive       # media pool, whole tree
resolve.cmd render-presets         # available render presets
resolve.cmd export-spec            # serialize the timeline to edit-spec JSON
resolve.cmd serve --port 18800     # loopback HTTP sidecar for the Go CLI / MCP server
```

Add `--json` to any command for machine-readable output. `--json`, `--yes` and `--timeout N`
are accepted before or after the subcommand. For `import`, keep the media paths contiguous,
because a flag between two paths splits the list.

Commands that **change** the project require `--yes`, so nothing modifies an edit by accident:

```
resolve.cmd import "C:/Footage/clip.mp4" --yes
resolve.cmd page color --yes
resolve.cmd create-project NAME --yes         # creates AND loads it (switches the GUI's current project)
resolve.cmd load-project NAME --yes           # switches the GUI's current project: disruptive to a live editor
resolve.cmd delete-project NAME --yes         # refuses on the currently loaded project (API contract)
resolve.cmd create-timeline NAME --yes
resolve.cmd render --out DIR --name STEM --yes --timeout 180   # renders the CURRENT timeline (mp4/H264)
resolve.cmd smoke --yes --timeout 180         # write-path proof (below)
resolve.cmd build --spec edit-spec.json --fps 29.97 --lut look=MyLooks/look.cube --yes --timeout 180
```

`render`/`smoke` write to `--out`, else to `$RESOLVE_BRIDGE_RENDER_OUT`, else to
`<system temp>/_pp_smoke`. `verify-render` uses `ffprobe` from `$FFPROBE` or `PATH`.

**`build` (spec → timeline).** Takes an edit spec that follows [`../schema/edit-spec.schema.json`](../schema/edit-spec.schema.json)
(schema 1.0.0: integer frames, `out` exclusive, `record` = placement frame). The steps, in order:

1. Validates the spec.
2. Sets the project frame rate FIRST.
3. Imports every `src` not already in the media pool, matched by `File Path` (case- and
   slash-insensitive).
4. Creates the spec's timeline at the spec resolution.
5. Grows video/audio tracks to the highest V*/A* index.
6. Appends every clip with pre-resolved `startFrame/endFrame/recordFrame/trackIndex/mediaType`.
7. Reads back each item's `GetStart()/GetDuration()` and exits 1 on any mismatch.

`fill` is applied with `SetProperty(ZoomX/ZoomY)`, and `grade` with `SetLUT(1, path)` through the
`--lut NAME=PATH` map (paths relative to Resolve's LUT folder). It refuses:

- missing media
- a `grade` with no LUT mapping
- overlapping clips on one track
- a project whose frame rate cannot be changed

**Write-path guards, all enforced in code:**

- `SetRenderSettings` is sent ONE key per call, and any `False` aborts before rendering.
- `AddRenderJob`'s first-call empty string is detected and retried.
- Completion is proven by the OUTPUT FILE (exists + non-zero), never by settings readback.
- The queue entry is removed afterwards.

**`smoke` cleans up after itself on every Python failure path.** The sequence:

1. Records the current project.
2. Calls `DisableBackgroundTasksForCurrentResolveSession()`. This turns off auto-backup for the
   REST of that Resolve session; restart Resolve to get it back.
3. Creates a disposable `_pp_smoke_<id>` project + timeline.
4. Inserts a Solid Color generator.
5. Renders 1 s and verifies the file.
6. Deletes the disposable project and reloads the original one.

The one gap is a WATCHDOG trip (exit 3), which kills the process without cleanup. To recover, run
`projects`, then `load-project <original> --yes`, then `delete-project _pp_smoke_* --yes`. The GUI
visibly switches projects for the duration, so run it only when the machine is idle.

**Watchdog.** Every command runs under a timeout (default 25 s, `--timeout N`). Resolve's API
stalls silently while any modal dialog is open in the GUI. On timeout the CLI prints a
diagnosis and exits **3**. Treat that as "Resolve busy, back off and retry", not as a fact
about the project.

**Read-safety is enforced.** `check_readonly.py`, a default-deny AST gate run by `deploy.ps1`,
fails if any non-write code path calls a mutating Resolve API (`SetCurrentTimeline`,
`LoadProject`, `Set*`, `Add*`, …).

Tests: `python -m pytest bridge/tests -q` (no Resolve needed).

## Escape hatch: fuscript

`fuscript.exe` (in the Resolve program folder) connects without any external Python, which
makes it a useful health probe even when the Python tooling is broken:

```
"C:\Program Files\Blackmagic Design\DaVinci Resolve\fuscript.exe" -x "r = bmd.scriptapp('Resolve'); print(r and r:GetProductName() or 'not reachable')"
```

## Writing your own scripts

`connect.py` handles the connection so scripts stay short:

```python
from connect import get_resolve, get_project
```

Run them with the deployed interpreter, for example `python312\python.exe yourscript.py`. The
versioned `._pth` puts the deploy folder on `sys.path`.
