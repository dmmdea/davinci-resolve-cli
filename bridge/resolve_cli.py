"""resolve_cli -- drive DaVinci Resolve Studio from the command line.

Read commands are safe to run any time. Commands that CHANGE the project are
marked (writes) and refuse to run without --yes, so an agent cannot alter an
edit by accident.

    python resolve_cli.py status
    python resolve_cli.py projects [--attributes]
    python resolve_cli.py timelines
    python resolve_cli.py clips [--audio] [--timeline NAME]
    python resolve_cli.py selection
    python resolve_cli.py pool [--recursive]
    python resolve_cli.py render-presets
    python resolve_cli.py export-spec [--timeline NAME]
    python resolve_cli.py render-status
    python resolve_cli.py import "C:/Footage/a.mp4" --yes          (writes)
    python resolve_cli.py page color --yes                          (writes)
    python resolve_cli.py create-project NAME --yes                 (writes)
    python resolve_cli.py load-project NAME --yes                   (writes; switches GUI)
    python resolve_cli.py delete-project NAME --yes                 (writes)
    python resolve_cli.py create-timeline NAME --yes               (writes)
    python resolve_cli.py render [--out DIR] [--name STEM] --yes    (writes; renders current timeline)
    python resolve_cli.py smoke [--out DIR] --yes --timeout 120     (writes; disposable-project render proof)
    python resolve_cli.py build --spec edit-spec.json [--fps 29.97] [--lut NAME=PATH]... --yes --timeout 120
                                                                    (writes; spec -> media pool + new timeline)
    python resolve_cli.py serve [--port 18800]                      (loopback HTTP sidecar over these same commands)

`build` is the spec->timeline half of Phase 0: it validates an edit-spec
(shared/schema/edit-spec.schema.json, integer frames), imports every `src`
that is not already in the media pool (matched by "File Path"), creates the
spec's timeline in the CURRENT project with the spec's resolution, appends
every V*/A* clip with pre-resolved geometry (startFrame/endFrame/recordFrame/
trackIndex -- no post-placement edits), applies `fill` (ZoomX/ZoomY) and
`grade` (SetLUT on node 1 through the --lut map), and then READS BACK every
placed item's start/duration against the spec: a mismatch is reported, never
hidden. Overlay entries (template/text titles) are skipped with a warning --
pre-render titles as media for now. --fps is the Resolve frame-rate STRING
("29.97"); it defaults to the spec's integer rate.

Renders take longer than the 25s watchdog default: `render` REFUSES --timeout
below 60 and `smoke` below 90 (the outer watchdog kills the process with os._exit
and runs NO cleanup, so a too-small budget is refused rather than gambled). The
internal render poll reserves setup + cleanup time under the watchdog. `smoke` is
self-cleaning on every Python failure path (disposable project deleted, the
editor's original project restored, restore failures printed to stderr); the
one hole is a watchdog trip (exit 3) -- then reload the original project FIRST
(`load-project <orig> --yes`) and only then delete the leftover `_pp_smoke_*`
project (delete refuses on the loaded project). `smoke` also disables Resolve's background
tasks for the rest of that Resolve session (no re-enable API; restart Resolve).

Add --json to any command for machine-readable output. Global flags (--json,
--yes, --timeout) are accepted before or after the subcommand. One limitation:
for `import`, keep the media paths contiguous -- a flag BETWEEN two paths
splits the list (argparse nargs="+"); put flags before the subcommand or after
the last path.

Every command runs under a watchdog (default 25s, --timeout N to change):
Resolve's API stalls silently while a modal dialog is open in the GUI
(auto-backup is the classic overnight case). Exit codes: 0 ok, 1 state/connect
error (message on stderr), 2 usage, 3 watchdog timeout ("Resolve busy -- back
off and retry"; the hung IPC call cannot be cancelled, only abandoned),
4 unexpected exception (bug or unguarded None from the API). A "warnings"
field/stderr lines mark API calls that returned None -- "empty or failed" is
not the same as "verified empty".

Read-only discipline: nothing outside the write-guarded commands may call a
mutating API (SetCurrentTimeline, LoadProject, ...). Enforced by
check_readonly.py, which the deploy script runs before every deploy.

`serve` puts a loopback-only HTTP face on these same command functions so tools
that speak REST (a generated CLI, an MCP server, an agent runtime) can drive
Resolve without reimplementing any of the connect/watchdog/page-null handling
above. It holds ONE warm connection and serializes every request through a
single lock, because the bridge underneath is one blocking IPC channel. See
serve_http.py; the route table is self-describing at GET /v1/commands.
"""

import argparse
import json
import os
import sys
import tempfile
import threading

from connect import get_resolve
# The shared primitives live in ops_common so the ops_* modules can import
# them without a circular import; the underscore names below are the ones
# this file (and serve_http / the tests) always used.
from ops_common import (set_output_sink, out as _out, api_list as _api_list,  # noqa: F401
                        require_project as _require_project, require_timeline as _require_timeline,
                        timeline_by_name as _timeline_by_name, select_timeline as _select_timeline,
                        guard as _guard, SPECS, spec_for, spec_defaults, field_name)
# Registering the 21.1 operation catalog is an import side effect by design:
# each module appends its command specs to ops_common.SPECS.
import ops_project   # noqa: F401,E402
import ops_media     # noqa: F401,E402
import ops_timeline  # noqa: F401,E402
import ops_color     # noqa: F401,E402
import ops_render    # noqa: F401,E402

EXIT_TIMEOUT = 3
DEFAULT_TIMEOUT_S = 25
SIDECAR_PORT = 18800  # loopback only; override with `serve --port`
# render/smoke output when --out is not given: $RESOLVE_BRIDGE_RENDER_OUT, else <temp>/_pp_smoke
DEFAULT_RENDER_OUT = os.environ.get("RESOLVE_BRIDGE_RENDER_OUT") or os.path.join(tempfile.gettempdir(), "_pp_smoke")
DEFAULT_RENDER_STEM = "pp_render"

# Every optional argument the LEGACY command handlers read, with the value an
# unset flag carries. main() feeds this to argparse and serve_http overlays
# request fields onto a copy, so a CLI call and an HTTP call of the same
# command cannot disagree about what "not supplied" means. Handlers may
# therefore assume the attribute exists; a missing REQUIRED value surfaces as
# that command's own error, not an AttributeError. Commands declared through
# ops_common.SPECS carry their own per-command defaults instead.
ARG_DEFAULTS = {
    "json": False,
    "yes": False,
    "timeout": DEFAULT_TIMEOUT_S,
    "audio": False,
    "recursive": False,
    "attributes": False,
    "timeline": None,
    "out": None,    # render/smoke fall back to DEFAULT_RENDER_OUT
    "name": None,   # render falls back to DEFAULT_RENDER_STEM; positional elsewhere
    "paths": [],
    "spec": None,
    "fps": None,
    "lut": [],
    "port": SIDECAR_PORT,
}


def _build_info():
    """Read build-info.json written next to this script by deploy.ps1."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build-info.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return None  # not deployed -- running from a working tree
    except ValueError:
        return {"corrupt": True}  # file exists but is unreadable: say so, don't claim dev


def _version_string():
    info = _build_info()
    if info and info.get("corrupt"):
        return "resolve_cli UNKNOWN build (build-info.json exists but is unreadable -- corrupt/interrupted deploy?)"
    if info:
        dirty = "+dirty" if info.get("dirty") else ""
        return ("resolve_cli build %s%s (deployed %s)"
                % (info.get("sha", "?")[:9], dirty, info.get("deployed_at", "?")))
    return "resolve_cli dev (no build-info.json -- running from a working tree?)"


# Public alias: serve_http reports the deployed build on /healthz.
version_string = _version_string


def cmd_status(args, resolve):
    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetCurrentTimeline() if project else None
    db = resolve.GetProjectManager().GetCurrentDatabase()
    data = {
        "product": resolve.GetProductName(),
        "version": resolve.GetVersionString(),
        "studio": resolve.IsStudio(),
        "page": resolve.GetCurrentPage(),
        "project": project.GetName() if project else None,
        "timeline": timeline.GetName() if timeline else None,
        "database": db,
        "keyboard_preset": resolve.GetCurrentKeyboardPreset(),
        "rendering": project.IsRenderingInProgress() if project else None,
        "cli": _version_string(),
    }
    _out(args, data, [
        "%s %s%s" % (data["product"], data["version"], "" if data["studio"] else " (NOT Studio)"),
        "page:     %s" % data["page"],
        "project:  %s" % (data["project"] or "(none open)"),
        "timeline: %s" % (data["timeline"] or "(none open)"),
        "database: %s" % json.dumps(db, default=str),
        "cli:      %s" % data["cli"],
    ])


def cmd_projects(args, resolve):
    warnings = []
    pm = resolve.GetProjectManager()
    names = [n for n in _api_list(pm.GetProjectListInCurrentFolder(),
                                  "GetProjectListInCurrentFolder", warnings) if n]
    data = {"projects": names}
    lines = ["%d project(s):" % len(names)] + ["  %s" % n for n in names]
    if args.attributes:
        # 21.0.3+: attributes for every project in the folder WITHOUT opening any
        # of them (community-verified side-effect-free).
        attrs = pm.GetProjectAttributesInCurrentFolder()
        if attrs is None:
            warnings.append("GetProjectAttributesInCurrentFolder returned None (empty or failed)")
            attrs = {}
        elif names and len(attrs) != len(names):
            warnings.append("attributes count (%d) != project count (%d) -- partial read?"
                            % (len(attrs), len(names)))
        data["attributes"] = attrs
        for name, a in attrs.items():
            lines.append("  %s: %s" % (name, json.dumps(a, default=str)))
    _out(args, data, lines, warnings)


def cmd_timelines(args, resolve):
    warnings = []
    project = _require_project(resolve)
    count = project.GetTimelineCount()
    current = project.GetCurrentTimeline()
    current_name = current.GetName() if current else None
    items = []
    for i in range(1, count + 1):
        tl = project.GetTimelineByIndex(i)
        if tl is None:
            warnings.append("GetTimelineByIndex(%d) returned None (unreadable index)" % i)
            continue
        items.append({"name": tl.GetName(), "current": tl.GetName() == current_name})
    _out(args, {"timelines": items},
         ["%d timeline(s) in '%s':" % (len(items), project.GetName())] +
         ["  %s %s" % ("*" if t["current"] else " ", t["name"]) for t in items],
         warnings)


def _walk_track(timeline, track_type, index, warnings):
    role = "%s%d" % ("V" if track_type == "video" else "A", index)
    for item in _api_list(timeline.GetItemListInTrack(track_type, index),
                          "GetItemListInTrack(%s)" % role, warnings):
        yield {
            "track": role,
            "type": track_type,
            "name": item.GetName(),
            "start": item.GetStart(),
            "end": item.GetEnd(),
            "duration": item.GetDuration(),
        }


def cmd_clips(args, resolve):
    warnings = []
    timeline = _select_timeline(args, _require_project(resolve))
    items = []
    for track in range(1, timeline.GetTrackCount("video") + 1):
        items.extend(_walk_track(timeline, "video", track, warnings))
    if args.audio:
        for track in range(1, timeline.GetTrackCount("audio") + 1):
            items.extend(_walk_track(timeline, "audio", track, warnings))
    _out(args, {"timeline": timeline.GetName(), "items": items},
         ["%d clip(s) on '%s':" % (len(items), timeline.GetName())] +
         ["  %s  %s  (%s frames)" % (c["track"], c["name"], c["duration"]) for c in items],
         warnings)


def cmd_selection(args, resolve):
    """Read the editor's CURRENT selection (timeline items + media pool).

    Current-UI state only -- selection has no meaning on a non-current
    timeline, so --timeline deliberately does not apply here.
    NOTE (21.0.4, community-verified): selecting N video clips returns their
    linked audio items too (3 clips -> 15 items); the `type`/`track` fields
    let a caller keep the track types they mean. Both GetSelectedClips calls
    return None when nothing is selected.
    """
    warnings = []
    project = _require_project(resolve)
    data = {"timeline": None, "timeline_items": [], "pool_items": []}
    timeline = project.GetCurrentTimeline()
    if timeline is not None:
        data["timeline"] = timeline.GetName()
        for item in timeline.GetSelectedClips() or []:
            tt_ti = item.GetTrackTypeAndIndex()
            if tt_ti is None:
                warnings.append("GetTrackTypeAndIndex returned None for %r" % item.GetName())
                tt_ti = [None, None]
            track_type, track_index = tt_ti[0], tt_ti[1]
            data["timeline_items"].append({
                "name": item.GetName(),
                "type": track_type,
                "track": ("%s%s" % ((track_type or "?")[:1].upper(), track_index)
                          if track_index is not None else None),
                "start": item.GetStart(),
                "end": item.GetEnd(),
                "duration": item.GetDuration(),
            })
    for clip in project.GetMediaPool().GetSelectedClips() or []:
        data["pool_items"].append({"name": clip.GetName()})
    _out(args, data,
         ["timeline: %s" % (data["timeline"] or "(none open)"),
          "%d selected timeline item(s):" % len(data["timeline_items"])] +
         ["  %s  %s  (%s frames)" % (i["track"], i["name"], i["duration"])
          for i in data["timeline_items"]] +
         ["%d selected media-pool clip(s):" % len(data["pool_items"])] +
         ["  %s" % c["name"] for c in data["pool_items"]],
         warnings)


def _clip_row(clip, folder_path):
    return {
        "name": clip.GetName(),
        "folder": folder_path,
        "duration": clip.GetClipProperty("Duration"),
        "resolution": clip.GetClipProperty("Resolution"),
        "fps": clip.GetClipProperty("FPS"),
    }


def _walk_pool(folder, path, out, recursive, warnings):
    for clip in _api_list(folder.GetClipList(), "GetClipList(%s)" % path, warnings):
        out.append(_clip_row(clip, path))
    if recursive:
        for sub in _api_list(folder.GetSubFolderList(), "GetSubFolderList(%s)" % path, warnings):
            _walk_pool(sub, path + "/" + sub.GetName(), out, recursive, warnings)


def cmd_pool(args, resolve):
    warnings = []
    pool = _require_project(resolve).GetMediaPool()
    root = pool.GetRootFolder() if args.recursive else pool.GetCurrentFolder()
    if root is None:
        sys.exit("Media pool folder handle is None (call failed).")
    start_name = root.GetName()
    clips = []
    _walk_pool(root, start_name, clips, args.recursive, warnings)
    _out(args, {"folder": start_name, "recursive": bool(args.recursive), "clips": clips},
         ["%d clip(s) under '%s':" % (len(clips), start_name)] +
         ["  %s  %s  %sfps  [%s]" % (c["name"], c["resolution"], c["fps"], c["folder"])
          for c in clips],
         warnings)


def cmd_render_presets(args, resolve):
    warnings = []
    presets = _api_list(_require_project(resolve).GetRenderPresetList(),
                        "GetRenderPresetList", warnings)
    _out(args, {"presets": presets},
         ["%d render preset(s):" % len(presets)] + ["  %s" % p for p in presets],
         warnings)


def cmd_export_spec(args, resolve):
    """Serialize a timeline into edit-spec-shaped JSON (schema 1.0.0 subset).

    Read-only: the inverse of the future spec->timeline builder, usable as a
    fixture generator and brand-canon instrument. Frames are integers, per the
    edit-spec contract (shared/schema/edit-spec.schema.json).
    """
    warnings = []
    project = _require_project(resolve)
    timeline = _select_timeline(args, project)

    def _int_setting(name):
        raw = timeline.GetSetting(name)
        try:
            # round(), not int(): NTSC rates come back as "23.976"/"29.97"/"59.94"
            # and must map to their conventional integer rates 24/30/60.
            return int(round(float(raw)))
        except (TypeError, ValueError):
            warnings.append("GetSetting(%s) unreadable (returned %r)" % (name, raw))
            return None

    markers = timeline.GetMarkers()
    if markers is None:
        warnings.append("GetMarkers returned None (empty or failed)")
        markers = {}
    spec = {
        "schema_version": "1.0.0",
        "format": "export",
        "rate": _int_setting("timelineFrameRate"),
        "resolution": [_int_setting("timelineResolutionWidth"),
                       _int_setting("timelineResolutionHeight")],
        "timeline": timeline.GetName(),
        "start_frame": timeline.GetStartFrame(),
        "end_frame": timeline.GetEndFrame(),
        "tracks": {},
        "markers": markers,
        "generator": {"tool": "resolve_cli export-spec", "cli": _version_string(),
                      "project": project.GetName()},
    }
    for track_type, prefix in (("video", "V"), ("audio", "A")):
        for index in range(1, timeline.GetTrackCount(track_type) + 1):
            role = "%s%d" % (prefix, index)
            entries = []
            for n, item in enumerate(_api_list(timeline.GetItemListInTrack(track_type, index),
                                               "GetItemListInTrack(%s)" % role, warnings)):
                entry = {
                    "id": "%s-%d" % (role.lower(), n),
                    "name": item.GetName(),
                    "record": item.GetStart(),
                    "end": item.GetEnd(),
                    "duration": item.GetDuration(),
                }
                try:
                    mpi = item.GetMediaPoolItem()
                    if mpi:
                        entry["src"] = mpi.GetClipProperty("File Path") or mpi.GetName()
                except Exception as exc:  # noqa: BLE001
                    # Expected for generated titles (no media item), but say so --
                    # an unexpected error here must not be indistinguishable from that.
                    warnings.append("GetMediaPoolItem failed for %r: %s: %s"
                                    % (entry["name"], type(exc).__name__, exc))
                entries.append(entry)
            if entries:
                spec["tracks"][role] = entries
    if warnings:
        spec["generator"]["warnings"] = warnings
        for w in warnings:
            sys.stderr.write("warning: %s\n" % w)
    # export-spec output is consumed by machines; default to JSON either way.
    print(json.dumps(spec, indent=2, default=str))


def cmd_import(args, resolve):
    _guard(args, "import media")
    pool = _require_project(resolve).GetMediaPool()
    added = pool.ImportMedia(args.paths)
    if added is None:
        sys.exit("ERROR: ImportMedia returned None -- the call failed (or nothing "
                 "was importable). Requested: %s" % ", ".join(args.paths))
    names = [c.GetName() for c in added]
    warnings = []
    if len(names) < len(args.paths):
        warnings.append("requested %d path(s) but only %d clip(s) came back -- "
                        "requested: %s; imported: %s"
                        % (len(args.paths), len(names), ", ".join(args.paths),
                           ", ".join(names) or "(none)"))
    _out(args, {"imported": names, "requested": list(args.paths)},
         ["imported %d clip(s):" % len(names)] + ["  %s" % n for n in names],
         warnings)


def cmd_page(args, resolve):
    _guard(args, "switch to the %s page" % args.name)
    ok = resolve.OpenPage(args.name)
    _out(args, {"page": resolve.GetCurrentPage(), "ok": bool(ok)},
         ["page is now: %s" % resolve.GetCurrentPage()])


# ---------------------------------------------------------------------------
# Write path (Phase 0 engine). Every command here is --yes-guarded and listed
# in WRITE_COMMANDS so check_readonly.py permits its mutating API calls.
# ---------------------------------------------------------------------------

def cmd_create_project(args, resolve):
    _guard(args, "create project %r" % args.name)
    pm = resolve.GetProjectManager()
    project = pm.CreateProject(args.name)
    if project is None:
        sys.exit("ERROR: CreateProject(%r) returned None -- the name is not unique "
                 "in the current folder (or the call failed)." % args.name)
    _out(args, {"created": args.name, "current": project.GetName()},
         ["created and loaded project: %s" % project.GetName()])


def cmd_delete_project(args, resolve):
    _guard(args, "delete project %r" % args.name)
    pm = resolve.GetProjectManager()
    current = pm.GetCurrentProject()
    if current is not None and current.GetName() == args.name:
        sys.exit("ERROR: cannot delete %r -- it is the currently loaded project. "
                 "Load or create another project first (DeleteProject only works "
                 "on a project that is not loaded)." % args.name)
    ok = pm.DeleteProject(args.name)
    if not ok:
        sys.exit("ERROR: DeleteProject(%r) returned False -- not found in the "
                 "current folder, or it is loaded." % args.name)
    _out(args, {"deleted": args.name, "ok": True}, ["deleted project: %s" % args.name])


def cmd_load_project(args, resolve):
    # LoadProject switches the GUI's current project -- disruptive to a live
    # editor session, hence --yes plus this loud warning.
    _guard(args, "load project %r (this switches the editor's current project)" % args.name)
    pm = resolve.GetProjectManager()
    project = pm.LoadProject(args.name)
    if project is None:
        sys.exit("ERROR: LoadProject(%r) returned None -- no project with that name "
                 "in the current folder." % args.name)
    _out(args, {"loaded": project.GetName(), "current": project.GetName()},
         ["loaded project: %s" % project.GetName()])


def cmd_create_timeline(args, resolve):
    _guard(args, "create timeline %r" % args.name)
    project = _require_project(resolve)
    timeline = project.GetMediaPool().CreateEmptyTimeline(args.name)
    if timeline is None:
        sys.exit("ERROR: CreateEmptyTimeline(%r) returned None (duplicate name, or "
                 "the call failed)." % args.name)
    # Report the object we already verified; a re-fetch of "current" can be None
    # in edge cases and would turn a SUCCESSFUL write into a scary exit-4 crash.
    current = project.GetCurrentTimeline()
    _out(args, {"created_timeline": timeline.GetName(),
                "current": current.GetName() if current else None},
         ["created timeline: %s" % timeline.GetName()])


def _set_render_settings_checked(project, settings):
    """SetRenderSettings ONE key per call (21.0.4 partial-apply gotcha: a dict
    reports one Bool for the whole dict and False does not mean nothing changed).
    Fail loud on any False so a silently-dropped setting cannot corrupt output."""
    failed = []
    for key, value in settings.items():
        ok = project.SetRenderSettings({key: value})
        if not ok:
            failed.append(key)
    if failed:
        sys.exit("ERROR: SetRenderSettings rejected key(s): %s. The render was NOT "
                 "started (a dropped setting would corrupt the output silently)."
                 % ", ".join(failed))


def _add_render_job_retry(project, warnings, attempts=3):
    """AddRenderJob's first call can return "" -- assert non-empty, retry. A
    successful retry is recorded as a warning: Resolve flakiness is a signal."""
    last = None
    for attempt in range(1, attempts + 1):
        job_id = project.AddRenderJob()
        if job_id:
            if attempt > 1:
                warnings.append("AddRenderJob needed %d attempts (first returned %r)"
                                % (attempt, last))
            return job_id
        last = job_id
    sys.exit("ERROR: AddRenderJob returned empty (%r) after %d attempts -- the "
             "render queue did not accept the job." % (last, attempts))


# Terminal JobStatus values as observed in the field; the README documents the
# dict shape ("job status and completion percentage") but not the vocabulary,
# and one variant returns an error STRING when the render failed. Normalize.
_TERMINAL_OK = {"complete", "completed"}
_TERMINAL_BAD = {"cancelled", "canceled", "failed", "error"}


def _job_state(status):
    """(state_lower, dict_or_None). Accepts None, {}, a dict, or an error string."""
    if isinstance(status, str):
        return "failed", {"JobStatus": "Failed", "error": status}
    if isinstance(status, dict) and status:
        return str(status.get("JobStatus") or "").strip().lower(), status
    return "", None


def _render_and_verify(project, out_dir, name, poll_deadline_s, warnings):
    """Queue -> render -> poll -> verify-by-file. Returns the output path.
    verify-by-file is the only trusted signal (GetRenderSettings readback is not
    reliable). Raises via sys.exit on any failure; the render job is ALWAYS
    removed from the queue afterwards (try/finally), and a failed removal is
    surfaced -- it is not the command's silent leftover."""
    import time

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        sys.exit("ERROR: could not create output directory %r: %s" % (out_dir, exc))
    if not project.SetCurrentRenderFormatAndCodec("mp4", "H264"):
        sys.exit("ERROR: SetCurrentRenderFormatAndCodec('mp4','H264') returned False.")
    _set_render_settings_checked(project, {
        "SelectAllFrames": True,
        "TargetDir": out_dir,
        "CustomName": name,
        "ExportVideo": True,
        "ExportAudio": False,
    })

    def _snapshot():
        # (mtime, size) per candidate file, so an OVERWRITTEN same-name output
        # (ReplaceExistingFilesInPlace, second run with the default stem) counts
        # as produced instead of being masked by mere pre-existence.
        snap = {}
        try:
            names = os.listdir(out_dir)
        except OSError as exc:
            sys.exit("ERROR: could not read output directory %r: %s" % (out_dir, exc))
        for f in names:
            if f.startswith(name):
                try:
                    st = os.stat(os.path.join(out_dir, f))
                    snap[f] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    continue
        return snap

    before = _snapshot()
    job_id = _add_render_job_retry(project, warnings)
    try:
        if not project.StartRendering([job_id], isInteractiveMode=False):
            sys.exit("ERROR: StartRendering returned False for job %s." % job_id)

        deadline = time.monotonic() + poll_deadline_s
        state, status, ever_answered, unexpected_seen = "", None, False, set()
        while time.monotonic() < deadline:
            state, status = _job_state(project.GetRenderJobStatus(job_id))
            if status is not None:
                ever_answered = True
            if state in _TERMINAL_OK or state in _TERMINAL_BAD:
                break
            if state and state not in ("rendering", "ready", "queued", "pending") \
                    and state not in unexpected_seen:
                unexpected_seen.add(state)
                warnings.append("GetRenderJobStatus reported unexpected JobStatus %r "
                                "(treated as still running)" % state)
            time.sleep(0.5)
        else:
            if not ever_answered:
                sys.exit("ERROR: GetRenderJobStatus returned nothing for the entire %ds "
                         "poll -- the status API may be failing, not necessarily a slow "
                         "render. Check `render-status` directly." % poll_deadline_s)
            sys.exit("ERROR: render job %s did not finish within %ds (last status=%s). "
                     "The render may still be running in the GUI; raise --timeout."
                     % (job_id, poll_deadline_s, json.dumps(status, default=str)))

        if state not in _TERMINAL_OK:
            sys.exit("ERROR: render job %s ended %r, not Complete: %s"
                     % (job_id, state, json.dumps(status, default=str)))

        # Verify by output file -- the only trusted signal. A file counts as
        # produced if it is new OR its (mtime, size) changed since the pre-render
        # snapshot, and it is non-empty. Newest first, so the pick is deterministic.
        after = _snapshot()
        produced = sorted(
            (f for f, sig in after.items() if sig[1] > 0 and before.get(f) != sig),
            key=lambda f: after[f][0], reverse=True)
        if not produced:
            sys.exit("ERROR: render reported Complete but no non-empty output file "
                     "starting with %r appeared or changed in %s." % (name, out_dir))
        return os.path.join(out_dir, produced[0])
    finally:
        if not project.DeleteRenderJob(job_id):
            msg = "could not remove render job %s from the queue -- clean up by hand" % job_id
            warnings.append(msg)
            sys.stderr.write("warning: %s\n" % msg)


RENDER_MIN_TIMEOUT_S = 60    # render: setup calls + poll + queue cleanup
RENDER_POLL_RESERVE_S = 20   # render: OpenPage + format + settings + AddRenderJob(+retries) + Start + Delete
SMOKE_MIN_TIMEOUT_S = 90     # smoke: + project create/close/delete/restore
SMOKE_SETUP_RESERVE_S = 20   # budget for the pre-render Resolve calls
SMOKE_CLEANUP_RESERVE_S = 25 # budget for the finally-block restore calls


def _require_timeout_floor(args, floor, what):
    """The outer watchdog kills the process with os._exit (no finally blocks
    run), so a too-small --timeout is refused up front instead of gambling the
    editor's session state on it."""
    if args.timeout < floor:
        sys.exit("ERROR: %s needs --timeout >= %d (got %d): the per-command watchdog "
                 "would abort mid-render/mid-cleanup and skip the restore steps."
                 % (what, floor, args.timeout))


def cmd_render(args, resolve):
    out_dir = args.out or DEFAULT_RENDER_OUT
    stem = args.name or DEFAULT_RENDER_STEM
    _guard(args, "render the current timeline to %s" % out_dir)
    _require_timeout_floor(args, RENDER_MIN_TIMEOUT_S, "render")
    project = _require_project(resolve)
    timeline = _require_timeline(project)
    resolve.OpenPage("deliver")
    warnings = []
    poll = max(5, args.timeout - RENDER_POLL_RESERVE_S)  # headroom under the watchdog
    path = _render_and_verify(project, out_dir, stem, poll, warnings)
    _out(args, {"rendered": path, "timeline": timeline.GetName()},
         ["rendered: %s" % path], warnings)


def cmd_render_status(args, resolve):
    warnings = []
    project = _require_project(resolve)
    jobs = _api_list(project.GetRenderJobList(), "GetRenderJobList", warnings)
    data = {"jobs": jobs}
    _out(args, data, ["%d render job(s) in the queue" % len(jobs)] +
         ["  %s: %s" % (j.get("JobId", "?"), j.get("JobStatus", "?"))
          for j in jobs if isinstance(j, dict)],
         warnings)


def cmd_smoke(args, resolve):
    """End-to-end Phase 0 write-path proof, self-cleaning on every Python failure
    path (see KNOWN LIMIT below for the one hole):
    capture the editor's current project -> disable background tasks -> create a
    uniquely-named disposable project + timeline -> insert a Solid Color
    generator -> render 1s -> verify the output file -> delete the disposable
    project -> restore the editor's original project. Restore runs on every
    Python failure path (try/finally) and its own failures are printed to
    stderr even when the command exits with an error.

    KNOWN LIMIT: if the outer WATCHDOG fires (exit 3), the process dies with
    os._exit and NO cleanup runs -- a `_pp_smoke_*` project can be left loaded
    in the editor's GUI. That is why --timeout is refused below
    SMOKE_MIN_TIMEOUT_S and the poll budget reserves setup + cleanup time. After
    an exit 3: `projects`, then `load-project <original> --yes` and
    `delete-project _pp_smoke_* --yes` by hand.

    SIDE EFFECT: DisableBackgroundTasksForCurrentResolveSession() turns off
    Resolve's background tasks (auto-backup included) for the REST of that
    Resolve session; there is no re-enable API -- restart Resolve to restore."""
    import uuid

    _guard(args, "run the write-path smoke (creates + deletes a disposable project)")
    _require_timeout_floor(args, SMOKE_MIN_TIMEOUT_S, "smoke")
    pm = resolve.GetProjectManager()
    original = pm.GetCurrentProject()
    original_name = original.GetName() if original else None
    steps = []
    warnings = []
    resolve.DisableBackgroundTasksForCurrentResolveSession()
    warnings.append("background tasks disabled for this Resolve session and NOT "
                    "re-enabled (no API for it) -- restart Resolve to restore auto-backup")

    suffix = uuid.uuid4().hex[:8]
    proj_name = "_pp_smoke_%s" % suffix
    smoke_project = None
    rendered_path = None
    try:
        smoke_project = pm.CreateProject(proj_name)
        if smoke_project is None:
            sys.exit("ERROR: smoke could not create disposable project %r." % proj_name)
        steps.append("created disposable project %s" % proj_name)

        timeline = smoke_project.GetMediaPool().CreateEmptyTimeline("_pp_smoke_tl")
        if timeline is None:
            sys.exit("ERROR: smoke could not create the disposable timeline.")
        steps.append("created timeline")

        resolve.OpenPage("edit")
        item = timeline.InsertGeneratorIntoTimeline("Solid Color")
        if item is None:
            sys.exit("ERROR: InsertGeneratorIntoTimeline('Solid Color') returned "
                     "None -- cannot build a render source without media.")
        steps.append("inserted Solid Color generator")

        resolve.OpenPage("deliver")
        poll = max(5, args.timeout - SMOKE_SETUP_RESERVE_S - SMOKE_CLEANUP_RESERVE_S)
        rendered_path = _render_and_verify(smoke_project, args.out or DEFAULT_RENDER_OUT,
                                           "pp_smoke_%s" % suffix, poll, warnings)
        steps.append("rendered and verified: %s" % rendered_path)
    finally:
        # Restore as-found state no matter what: close+delete the disposable
        # project, then reload whatever the editor originally had open. Every
        # restore failure is written to stderr HERE, because a sys.exit from the
        # try block would otherwise skip the final _out and hide it.
        cleanup_warnings = []
        if smoke_project is not None:
            if not pm.CloseProject(smoke_project):
                cleanup_warnings.append("CloseProject(%r) returned False" % proj_name)
            if pm.DeleteProject(proj_name):
                steps.append("deleted disposable project")
            else:
                cleanup_warnings.append("could not delete disposable project %r -- "
                                        "delete it by hand" % proj_name)
        if original_name and original_name != proj_name:
            if pm.LoadProject(original_name) is not None:
                steps.append("restored editor project %r" % original_name)
            else:
                cleanup_warnings.append("could not restore editor project %r -- the "
                                        "GUI is now on the WRONG project; load it by hand"
                                        % original_name)
        if sys.exc_info()[0] is not None:
            # A failure is propagating: _out below will never run, so this is the
            # only chance to surface restore problems. On success _out prints them.
            for w in cleanup_warnings:
                sys.stderr.write("warning: %s\n" % w)
        warnings.extend(cleanup_warnings)

    _out(args, {"ok": True, "rendered": rendered_path, "steps": steps},
         ["SMOKE PASS"] + ["  - %s" % s for s in steps], warnings)


# ---------------------------------------------------------------------------
# build: edit-spec -> media pool + timeline (spec->timeline half of Phase 0).
# Pure helpers first (unit-tested without Resolve), then the command.
# ---------------------------------------------------------------------------

import re as _re

_TRACK_ROLE = _re.compile(r"^([VA])([0-9]+)$")
DEFAULT_LUT_MAP = {}  # add site defaults here, e.g. {"look-a": "MyLooks/Look A.cube"}


def _norm_path(p):
    """Case-/slash-insensitive path key (Windows media pool 'File Path' vs spec)."""
    return os.path.normcase(os.path.normpath(str(p)))


def parse_lut_map(pairs):
    """--lut NAME=PATH (repeatable) -> dict, layered over DEFAULT_LUT_MAP.
    Malformed entries are an error, not a silent skip."""
    lut_map = dict(DEFAULT_LUT_MAP)
    for pair in pairs or []:
        name, sep, path = pair.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise ValueError("--lut expects NAME=PATH, got %r" % pair)
        lut_map[name.strip()] = path.strip()
    return lut_map


def validate_spec(spec):
    """Structural validation of a BUILD-time spec (schema 1.0.0 subset the
    builder consumes). Returns (media_clips, overlay_clips, problems).
    media_clips: [(role, letter, index, clip)] sorted by track then record.
    Frames are integers (never floats), out is EXCLUSIVE, record is the
    timeline placement frame."""
    problems = []
    if not isinstance(spec, dict):
        return [], [], ["spec is not a JSON object"]
    if spec.get("schema_version") != "1.0.0":
        problems.append("schema_version must be '1.0.0' (got %r)" % spec.get("schema_version"))
    rate = spec.get("rate")
    if not isinstance(rate, int) or isinstance(rate, bool) or rate < 1:
        problems.append("rate must be a positive integer (got %r)" % (rate,))
    res = spec.get("resolution")
    if (not isinstance(res, list) or len(res) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in res)):
        problems.append("resolution must be [width, height] positive integers (got %r)" % (res,))
    if not isinstance(spec.get("timeline"), str) or not spec.get("timeline").strip():
        problems.append("timeline (target timeline name) is required for build")
    tracks = spec.get("tracks")
    media, overlays = [], []
    if not isinstance(tracks, dict) or not tracks:
        problems.append("tracks must be a non-empty object of V*/A* roles")
        return media, overlays, problems
    seen_ids = set()
    for role, clips in tracks.items():
        m = _TRACK_ROLE.match(role)
        if not m:
            problems.append("track role %r is not V<n>/A<n>" % role)
            continue
        letter, index = m.group(1), int(m.group(2))
        if index < 1:
            problems.append("track %r index must be >= 1" % role)
            continue
        if not isinstance(clips, list):
            problems.append("track %r must be a list" % role)
            continue
        for n, clip in enumerate(clips):
            if not isinstance(clip, dict):
                problems.append("%s[%d] is not an object" % (role, n))
                continue
            cid = clip.get("id")
            if not isinstance(cid, str) or not cid:
                problems.append("%s[%d] has no string id" % (role, n))
                continue
            if cid in seen_ids:
                problems.append("duplicate clip id %r" % cid)
            seen_ids.add(cid)
            if clip.get("overlay") or clip.get("template"):
                overlays.append((role, clip))
                continue
            missing = [k for k in ("src", "in", "out", "record") if k not in clip]
            if missing:
                problems.append("%s/%s missing %s" % (role, cid, ", ".join(missing)))
                continue
            bad = [k for k in ("in", "out", "record")
                   if not isinstance(clip[k], int) or isinstance(clip[k], bool)]
            if bad:
                problems.append("%s/%s: %s must be integer frames" % (role, cid, ", ".join(bad)))
                continue
            if clip["out"] <= clip["in"]:
                problems.append("%s/%s: out (%d) must be > in (%d)" % (role, cid, clip["out"], clip["in"]))
                continue
            if clip["in"] < 0 or clip["record"] < 0:
                problems.append("%s/%s: negative frame" % (role, cid))
                continue
            if "fill" in clip and not isinstance(clip["fill"], (int, float)):
                problems.append("%s/%s: fill must be a number" % (role, cid))
            media.append((role, letter, index, clip))
    media.sort(key=lambda t: (t[1], t[2], t[3]["record"]))
    # Overlaps on one track are a spec bug (append-time geometry cannot fix them).
    by_track = {}
    for role, letter, index, clip in media:
        by_track.setdefault(role, []).append(clip)
    for role, clips in by_track.items():
        prev_end, prev_id = None, None
        for clip in clips:
            if prev_end is not None and clip["record"] < prev_end:
                problems.append("%s: %r (record %d) overlaps %r (ends %d)"
                                % (role, clip["id"], clip["record"], prev_id, prev_end))
            prev_end = clip["record"] + (clip["out"] - clip["in"])
            prev_id = clip["id"]
    return media, overlays, problems


def clip_info(clip, letter, index, pool_item, start_offset):
    """The AppendToTimeline clipInfo dict for one spec clip. Spec `out` is
    EXCLUSIVE; Resolve's endFrame is the last frame INCLUSIVE, hence out-1.
    recordFrame is the absolute timeline frame: spec records are relative to
    the spec's start_frame (or 0), start_offset converts them."""
    return {
        "mediaPoolItem": pool_item,
        "startFrame": clip["in"],
        "endFrame": clip["out"] - 1,
        "recordFrame": clip["record"] + start_offset,
        "trackIndex": index,
        "mediaType": 1 if letter == "V" else 2,
    }


def verify_placement(clip, item_start, item_duration, start_offset):
    """Compare a placed item's read-back geometry with the spec. Returns a
    problem string or None. This readback is the proof AppendToTimeline did
    what the spec said -- the API's return value alone is not."""
    want_start = clip["record"] + start_offset
    want_dur = clip["out"] - clip["in"]
    if item_start != want_start or item_duration != want_dur:
        return ("%s: placed at %s for %s frames, spec wanted %d for %d"
                % (clip["id"], item_start, item_duration, want_start, want_dur))
    return None


def _walk_pool_items(folder, out, warnings, path="root"):
    for clip in _api_list(folder.GetClipList(), "GetClipList(%s)" % path, warnings):
        out.append(clip)
    for sub in _api_list(folder.GetSubFolderList(), "GetSubFolderList(%s)" % path, warnings):
        _walk_pool_items(sub, out, warnings, path + "/" + sub.GetName())


def _pool_index(pool, warnings):
    """{normalized File Path: MediaPoolItem} over the whole pool."""
    root = pool.GetRootFolder()
    if root is None:
        sys.exit("ERROR: media pool root folder is None (call failed).")
    items = []
    _walk_pool_items(root, items, warnings)
    index = {}
    for it in items:
        fp = it.GetClipProperty("File Path")
        if fp:
            index[_norm_path(fp)] = it
    return index


def cmd_build(args, resolve):
    _guard(args, "build timeline from spec %r (imports media, creates a timeline)" % args.spec)
    try:
        with open(args.spec, "r", encoding="utf-8") as fh:
            spec = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.exit("ERROR: cannot read spec %s: %s" % (args.spec, exc))
    media, overlays, problems = validate_spec(spec)
    if problems:
        sys.exit("ERROR: spec rejected:\n  " + "\n  ".join(problems))
    try:
        lut_map = parse_lut_map(args.lut)
    except ValueError as exc:
        sys.exit("ERROR: %s" % exc)
    for role, letter, index, clip in media:
        if clip.get("grade") and clip["grade"] not in lut_map:
            sys.exit("ERROR: %s/%s references grade %r with no --lut mapping"
                     % (role, clip["id"], clip["grade"]))
    missing_files = sorted({clip["src"] for _, _, _, clip in media
                            if not os.path.isfile(clip["src"])})
    if missing_files:
        sys.exit("ERROR: spec media not found on disk:\n  " + "\n  ".join(missing_files))

    warnings = []
    for role, clip in overlays:
        warnings.append("%s/%s: overlay/template entries are not built yet -- skipped"
                        % (role, clip["id"]))
    project = _require_project(resolve)
    pool = project.GetMediaPool()
    fps = args.fps or str(spec["rate"])
    # Frame rate BEFORE the timeline exists (Resolve locks it once media/timelines are in).
    if not project.SetSetting("timelineFrameRate", fps):
        sys.exit("ERROR: SetSetting('timelineFrameRate', %r) returned False -- a project "
                 "with media/timelines may refuse a rate change; use an empty project." % fps)

    # 1. Import every src that is not already in the pool (by File Path).
    index = _pool_index(pool, warnings)
    wanted = []
    for _, _, _, clip in media:
        key = _norm_path(clip["src"])
        if key not in index and clip["src"] not in wanted:
            wanted.append(clip["src"])
    imported = []
    if wanted:
        added = pool.ImportMedia(wanted)
        if added is None:
            sys.exit("ERROR: ImportMedia returned None for: %s" % ", ".join(wanted))
        imported = [c.GetName() for c in added]
        index = _pool_index(pool, warnings)
        still_missing = [p for p in wanted if _norm_path(p) not in index]
        if still_missing:
            sys.exit("ERROR: imported %d clip(s) but these paths are still not in the pool "
                     "(unsupported media?): %s" % (len(imported), ", ".join(still_missing)))

    # 2. Timeline with the spec's resolution.
    name = spec["timeline"]
    timeline = pool.CreateEmptyTimeline(name)
    if timeline is None:
        sys.exit("ERROR: CreateEmptyTimeline(%r) returned None (name already exists?)." % name)
    width, height = spec["resolution"]
    for key, value in (("useCustomSettings", "1"),
                       ("timelineResolutionWidth", str(width)),
                       ("timelineResolutionHeight", str(height)),
                       ("timelineOutputResolutionWidth", str(width)),
                       ("timelineOutputResolutionHeight", str(height))):
        if not timeline.SetSetting(key, value):
            warnings.append("Timeline.SetSetting(%s, %s) returned False" % (key, value))
    for track_type, letter in (("video", "V"), ("audio", "A")):
        need = max([idx for _, l, idx, _ in media if l == letter] or [0])
        while timeline.GetTrackCount(track_type) < need:
            if not timeline.AddTrack(track_type):
                sys.exit("ERROR: AddTrack(%r) returned False while growing to %d tracks"
                         % (track_type, need))
    start_offset = timeline.GetStartFrame() if spec.get("start_frame") is None else 0

    # 3. Append every clip with pre-resolved geometry, then read it back.
    placed, mismatches = [], []
    for role, letter, idx, clip in media:
        pool_item = index[_norm_path(clip["src"])]
        info = clip_info(clip, letter, idx, pool_item, start_offset)
        items = pool.AppendToTimeline([info])
        if not items:
            sys.exit("ERROR: AppendToTimeline returned %r for %s/%s (src=%s in=%d out=%d "
                     "record=%d) -- %d clip(s) placed before it; the timeline %r is left "
                     "as-is for inspection." % (items, role, clip["id"], clip["src"],
                                                 clip["in"], clip["out"], clip["record"],
                                                 len(placed), name))
        item = items[0]
        problem = verify_placement(clip, item.GetStart(), item.GetDuration(), start_offset)
        if problem:
            mismatches.append(problem)
        if letter == "V":
            fill = clip.get("fill")
            if fill is not None and fill != 1:
                for key in ("ZoomX", "ZoomY"):
                    if not item.SetProperty(key, float(fill)):
                        warnings.append("%s: SetProperty(%s, %s) returned False" % (clip["id"], key, fill))
            grade = clip.get("grade")
            if grade:
                if not item.SetLUT(1, lut_map[grade]):
                    warnings.append("%s: SetLUT(1, %r) returned False (LUT not found under "
                                    "Resolve's LUT folder?)" % (clip["id"], lut_map[grade]))
        placed.append({"id": clip["id"], "track": role, "start": item.GetStart(),
                       "duration": item.GetDuration(), "name": item.GetName()})

    data = {"timeline": name, "fps": fps, "resolution": [width, height],
            "imported": imported, "placed": placed, "mismatches": mismatches,
            "skipped_overlays": [c["id"] for _, c in overlays]}
    if mismatches:
        data["ok"] = False
        _out(args, data, ["BUILD PLACED %d clip(s) BUT %d geometry mismatch(es):" % (len(placed), len(mismatches))]
             + ["  - %s" % m for m in mismatches], warnings)
        sys.exit(1)
    data["ok"] = True
    _out(args, data, ["built timeline %r: %d clip(s) placed, %d imported, %d overlay(s) skipped"
                      % (name, len(placed), len(imported), len(overlays))]
         + ["  %s  %s  @%s  %s frames" % (p["track"], p["id"], p["start"], p["duration"]) for p in placed],
         warnings)


COMMANDS = {
    "status": cmd_status,
    "projects": cmd_projects,
    "timelines": cmd_timelines,
    "clips": cmd_clips,
    "selection": cmd_selection,
    "pool": cmd_pool,
    "render-presets": cmd_render_presets,
    "export-spec": cmd_export_spec,
    "render-status": cmd_render_status,
    "import": cmd_import,
    "page": cmd_page,
    "create-project": cmd_create_project,
    "delete-project": cmd_delete_project,
    "load-project": cmd_load_project,
    "create-timeline": cmd_create_timeline,
    "render": cmd_render,
    "smoke": cmd_smoke,
    "build": cmd_build,
}
LEGACY_COMMANDS = frozenset(COMMANDS)

# The 21.1 operation catalog (ops_*.py) registers itself in ops_common.SPECS;
# a name clash with a legacy command is a bug, not a silent override.
for _spec in SPECS:
    if _spec["name"] in COMMANDS:
        raise RuntimeError("command %r is defined twice" % _spec["name"])
    COMMANDS[_spec["name"]] = _spec["fn"]

# Commands allowed to call mutating Resolve APIs (enforced by check_readonly.py,
# which parses THIS literal from the AST -- it must stay a literal dict, and
# tests/test_specs.py asserts it matches every SPECS entry flagged writes=True).
# render-status is a READ (GetRenderJobList) and stays out of this set.
WRITE_COMMANDS = {
    "import": "cmd_import",
    "page": "cmd_page",
    "create-project": "cmd_create_project",
    "delete-project": "cmd_delete_project",
    "load-project": "cmd_load_project",
    "create-timeline": "cmd_create_timeline",
    "render": "cmd_render",
    "smoke": "cmd_smoke",
    "build": "cmd_build",
    # --- ops_project ---
    "set-database": "cmd_set_database",
    "folder": "cmd_folder",
    "save-project": "cmd_save_project",
    "close-project": "cmd_close_project",
    "rename-project": "cmd_rename_project",
    "export-project": "cmd_export_project",
    "import-project": "cmd_import_project",
    "archive-project": "cmd_archive_project",
    "restore-project": "cmd_restore_project",
    "cloud-project": "cmd_cloud_project",
    "set-project-settings": "cmd_set_project_settings",
    "project-settings-preset": "cmd_project_settings_preset",
    "storage-import": "cmd_storage_import",
    "clone-media": "cmd_clone_media",
    # --- ops_media ---
    "bin": "cmd_bin",
    "import-media": "cmd_import_media",
    "delete-clips": "cmd_delete_clips",
    "move-clips": "cmd_move_clips",
    "relink-clips": "cmd_relink_clips",
    "select-clip": "cmd_select_clip",
    "stereo-clip": "cmd_stereo_clip",
    "sync-audio": "cmd_sync_audio",
    "set-clip-property": "cmd_set_clip_property",
    "set-clip-metadata": "cmd_set_clip_metadata",
    "rename-clip": "cmd_rename_clip",
    "add-clip-marker": "cmd_add_clip_marker",
    "delete-clip-marker": "cmd_delete_clip_marker",
    "clip-flag": "cmd_clip_flag",
    "clip-color": "cmd_clip_color",
    "clip-mark": "cmd_clip_mark",
    "proxy": "cmd_proxy",
    "replace-clip": "cmd_replace_clip",
    "monitor-growing": "cmd_monitor_growing",
    "set-audio-mapping": "cmd_set_audio_mapping",
    "clip-ai": "cmd_clip_ai",
    # --- ops_timeline ---
    "create-timeline-from-clips": "cmd_create_timeline_from_clips",
    "duplicate-timeline": "cmd_duplicate_timeline",
    "delete-timeline": "cmd_delete_timeline",
    "switch-timeline": "cmd_switch_timeline",
    "rename-timeline": "cmd_rename_timeline",
    "append": "cmd_append",
    "export-timeline": "cmd_export_timeline",
    "import-timeline": "cmd_import_timeline",
    "add-track": "cmd_add_track",
    "delete-track": "cmd_delete_track",
    "set-track": "cmd_set_track",
    "delete-items": "cmd_delete_items",
    "link-items": "cmd_link_items",
    "set-item": "cmd_set_item",
    "set-item-properties": "cmd_set_item_properties",
    "add-transition": "cmd_add_transition",
    "multicam": "cmd_multicam",
    "add-marker": "cmd_add_marker",
    "delete-marker": "cmd_delete_marker",
    "set-timecode": "cmd_set_timecode",
    "timeline-mark": "cmd_timeline_mark",
    "set-timeline-settings": "cmd_set_timeline_settings",
    "set-blanking": "cmd_set_blanking",
    "insert-generator": "cmd_insert_generator",
    "compound-clip": "cmd_compound_clip",
    "fusion-clip": "cmd_fusion_clip",
    "grab-still": "cmd_grab_still",
    "timeline-ai": "cmd_timeline_ai",
    "item-fusion": "cmd_item_fusion",
    # --- ops_color ---
    "color-version": "cmd_color_version",
    "apply-lut": "cmd_apply_lut",
    "apply-cdl": "cmd_apply_cdl",
    "apply-drx": "cmd_apply_drx",
    "copy-grade": "cmd_copy_grade",
    "set-node": "cmd_set_node",
    "export-lut": "cmd_export_lut",
    "refresh-luts": "cmd_refresh_luts",
    "color-group": "cmd_color_group",
    "gallery-album": "cmd_gallery_album",
    "fusion-set": "cmd_fusion_set",
    "fusion-keyframes": "cmd_fusion_keyframes",
    "fusion-tool": "cmd_fusion_tool",
    "set-title": "cmd_set_title",
    # --- ops_render ---
    "set-render-format": "cmd_set_render_format",
    "set-render-mode": "cmd_set_render_mode",
    "set-render-settings": "cmd_set_render_settings",
    "render-preset": "cmd_render_preset",
    "add-render-job": "cmd_add_render_job",
    "delete-render-job": "cmd_delete_render_job",
    "start-render": "cmd_start_render",
    "stop-render": "cmd_stop_render",
    "quick-export": "cmd_quick_export",
    "preset": "cmd_preset",
    "set-keyframe-mode": "cmd_set_keyframe_mode",
    "disable-background-tasks": "cmd_disable_background_tasks",
    "quit": "cmd_quit",
    "encrypt-dctl": "cmd_encrypt_dctl",
    "tts": "cmd_tts",
    "insert-audio": "cmd_insert_audio",
    "export-frame": "cmd_export_frame",
}

# Private write-path helpers the write commands call; also allowed to mutate.
# Explicit allowlist (check_readonly reads it) -- adding one is a deliberate act.
# (_pool_index/_walk_pool_items are READS and deliberately stay out.)
WRITE_HELPERS = {
    "_set_render_settings_checked",
    "_add_render_job_retry",
    "_render_and_verify",
    "_apply_settings",        # ops_project: SetSetting one key at a time
    "_set_render_settings",   # ops_render: SetRenderSettings one key at a time
    "_fusion_write",          # ops_color: Lock/StartUndo ... EndUndo/Unlock wrapper
}


def _add_global_flags(p):
    # default=SUPPRESS so a subparser never clobbers a value already set by the
    # root parser (argparse subparser defaults overwrite the shared namespace).
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="machine-readable output")
    p.add_argument("--yes", action="store_true", default=argparse.SUPPRESS,
                   help="allow commands that modify the project")
    p.add_argument("--timeout", type=int, default=argparse.SUPPRESS, metavar="S",
                   help="watchdog seconds per command (default %d)" % DEFAULT_TIMEOUT_S)


def _add_spec_parser(sub, spec):
    """argparse view of one ops_common spec. Per-command defaults are set on
    the subparser ON PURPOSE (not SUPPRESS): they must override the legacy
    root defaults for a same-named field such as `name` or `out`."""
    p = sub.add_parser(spec["name"], help=spec["help"], description=spec["help"])
    for a in spec["args"]:
        kind, field = a["kind"], field_name(a["name"])
        # argparse %-formats help text; a literal % (image-sequence "%0Nd") must be doubled.
        kw = {"help": (a["help"] or "").replace("%", "%%") or None, "dest": field}
        if a["choices"]:
            kw["choices"] = a["choices"]
        if a["metavar"]:
            kw["metavar"] = a["metavar"]
        if a["positional"]:
            del kw["dest"]  # positionals take their dest from the name
            if kind == "list":
                kw["nargs"] = "+" if a["required"] else "*"
            elif not a["required"]:
                kw["nargs"] = "?"
            kw["default"] = a["default"]
            if kind in ("int", "float"):
                kw["type"] = int if kind == "int" else float
            p.add_argument(field, **kw)
            continue
        flag = "--" + a["name"]
        if kind == "bool":
            p.add_argument(flag, action="store_true", default=False, **kw)
        elif kind == "list":
            p.add_argument(flag, nargs="+", default=a["default"] or [], required=a["required"], **kw)
        elif kind in ("int", "float"):
            p.add_argument(flag, type=int if kind == "int" else float, default=a["default"],
                           required=a["required"], **kw)
        else:  # text, json (a JSON string or @file on the CLI)
            p.add_argument(flag, default=a["default"], required=a["required"], **kw)
    _add_global_flags(p)
    return p


def _run_guarded(args):
    """Run connect + command in a worker thread under the watchdog."""
    outcome = {}

    def work():
        try:
            resolve = get_resolve()
        except RuntimeError as exc:
            outcome["message"] = "ERROR: %s" % exc
            outcome["code"] = 1
            return
        try:
            COMMANDS[args.command](args, resolve)
            outcome["code"] = 0
        except SystemExit as exc:  # sys.exit() inside helpers/guards
            if isinstance(exc.code, str):
                outcome["message"] = exc.code
                outcome["code"] = 1
            else:
                outcome["code"] = exc.code if exc.code is not None else 0
        except Exception as exc:  # noqa: BLE001 - unexpected API/None deref etc.
            outcome["message"] = ("ERROR: %s: %s (unexpected failure while running "
                                  "'%s' -- a Resolve API call likely returned an "
                                  "unguarded None)" % (type(exc).__name__, exc, args.command))
            outcome["code"] = 4

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(args.timeout)
    if worker.is_alive():
        sys.stderr.write(
            "ERROR: Resolve API call still blocked after %ds. A modal dialog in "
            "the Resolve GUI stalls the scripting API (auto-backup is the classic "
            "cause); the call cannot be cancelled. Exiting %d.\n"
            % (args.timeout, EXIT_TIMEOUT))
        sys.stderr.flush()
        os._exit(EXIT_TIMEOUT)
    if outcome.get("message"):
        sys.stderr.write(outcome["message"] + "\n")
    sys.exit(outcome.get("code", 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=_version_string())
    _add_global_flags(parser)
    parser.set_defaults(**ARG_DEFAULTS)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("status", "timelines", "selection", "render-presets", "render-status"):
        _add_global_flags(sub.add_parser(name))
    p_projects = sub.add_parser("projects")
    p_projects.add_argument("--attributes", action="store_true", default=argparse.SUPPRESS,
                            help="include per-project attributes (21.0.3+, does not open projects)")
    _add_global_flags(p_projects)
    p_clips = sub.add_parser("clips")
    p_clips.add_argument("--audio", action="store_true", default=argparse.SUPPRESS,
                         help="include audio tracks")
    p_clips.add_argument("--timeline", metavar="NAME", default=argparse.SUPPRESS,
                         help="read the named timeline (never switches the GUI's current timeline)")
    _add_global_flags(p_clips)
    p_pool = sub.add_parser("pool")
    p_pool.add_argument("--recursive", action="store_true", default=argparse.SUPPRESS,
                        help="walk all media pool folders from the root")
    _add_global_flags(p_pool)
    p_export = sub.add_parser("export-spec")
    p_export.add_argument("--timeline", metavar="NAME", default=argparse.SUPPRESS,
                          help="export the named timeline (never switches the GUI's current timeline)")
    _add_global_flags(p_export)
    p_import = sub.add_parser("import")
    p_import.add_argument("paths", nargs="+", help="media file paths to import")
    _add_global_flags(p_import)
    p_page = sub.add_parser("page")
    p_page.add_argument("name", choices=["media", "cut", "edit", "fusion", "color", "fairlight", "deliver"])
    _add_global_flags(p_page)

    # --- write path (all --yes-guarded) ---
    for cmd_name in ("create-project", "delete-project", "load-project", "create-timeline"):
        p = sub.add_parser(cmd_name)
        p.add_argument("name", help="project/timeline name")
        _add_global_flags(p)
    p_render = sub.add_parser("render")
    p_render.add_argument("--out", metavar="DIR", default=argparse.SUPPRESS,
                          help="output directory (default $RESOLVE_BRIDGE_RENDER_OUT or <temp>/_pp_smoke)")
    p_render.add_argument("--name", metavar="NAME", default=argparse.SUPPRESS,
                          help="output filename stem (default pp_render)")
    _add_global_flags(p_render)
    p_smoke = sub.add_parser("smoke")
    p_smoke.add_argument("--out", metavar="DIR", default=argparse.SUPPRESS,
                         help="output directory for the disposable render (default $RESOLVE_BRIDGE_RENDER_OUT or <temp>/_pp_smoke)")
    _add_global_flags(p_smoke)
    p_build = sub.add_parser("build")
    p_build.add_argument("--spec", required=True, metavar="PATH", help="edit-spec JSON (schema 1.0.0)")
    p_build.add_argument("--fps", metavar="RATE", default=None,
                         help="Resolve timeline frame-rate string, e.g. 29.97 (default: the spec's integer rate)")
    p_build.add_argument("--lut", action="append", metavar="NAME=PATH", default=[],
                         help="grade name -> LUT path (relative to Resolve's LUT folder); repeatable")
    _add_global_flags(p_build)

    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--port", type=int, default=argparse.SUPPRESS, metavar="N",
                         help="loopback port for the HTTP sidecar (default %d)" % SIDECAR_PORT)
    _add_global_flags(p_serve)

    # --- the 21.1 operation catalog: one subparser per registered spec ---
    for spec in SPECS:
        _add_spec_parser(sub, spec)

    args = parser.parse_args()
    if args.command == "serve":
        # Deliberately NOT routed through _run_guarded: that watchdog exists to
        # abandon a single stalled command after --timeout seconds, and a server
        # is supposed to outlive it. serve_http applies the same watchdog around
        # each REQUEST instead, which is where the stall risk actually lives.
        import serve_http
        sys.exit(serve_http.run_server(args))
    _run_guarded(args)


if __name__ == "__main__":
    main()
