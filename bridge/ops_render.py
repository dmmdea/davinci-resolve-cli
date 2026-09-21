"""ops_render -- render formats/codecs, render settings, render presets, the
job queue, Quick Export, output verification by file, and the system-level
surface (layout/burn-in/prefs/keyboard/Fairlight presets, keyframe mode,
background tasks, DCTL validate/encrypt, TTS, thumbnail, quit).
Absorb-manifest rows 41-51.
"""

import json
import os
import subprocess
import sys

from ops_common import (arg, cmd, out, api_list, require_project, require_timeline,
                        select_timeline, guard, jsonval, need, check, find_items)


# ---------------------------------------------------------------------------
# Formats / codecs / current selection (row 42)
# ---------------------------------------------------------------------------

def cmd_render_formats(args, resolve):
    """Formats (desc -> extension), codecs for --format (desc -> ID: the ID
    is what every setter wants), audio formats/codecs (21.1), resolutions."""
    warnings = []
    project = require_project(resolve)
    fmt = getattr(args, "format", None)
    data = {"formats": project.GetRenderFormats() or {},
            "audio_formats": project.GetAudioRenderFormats() or {},
            "current": project.GetCurrentRenderFormatAndCodec(),
            "mode": {0: "individual", 1: "single"}.get(project.GetCurrentRenderMode(), project.GetCurrentRenderMode()),
            "rendering": project.IsRenderingInProgress()}
    if fmt:
        codecs = project.GetRenderCodecs(fmt)
        if codecs is None:
            warnings.append("GetRenderCodecs(%r) returned None" % fmt)
        data["codecs"] = codecs or {}
        data["audio_codecs"] = project.GetAudioRenderCodecs(fmt) or {}
        data["resolutions"] = api_list(project.GetRenderResolutions(fmt, getattr(args, "codec", None))
                                       if getattr(args, "codec", None) else project.GetRenderResolutions(fmt),
                                       "GetRenderResolutions", warnings)
    lines = ["current: %s, mode %s, rendering %s" % (data["current"], data["mode"], data["rendering"]),
             "%d format(s): %s" % (len(data["formats"]), ", ".join("%s=%s" % kv for kv in sorted(data["formats"].items())))]
    if fmt:
        lines += ["codecs for %s:" % fmt] + ["  %s -> %s" % kv for kv in sorted(data["codecs"].items())]
        lines += ["audio codecs for %s: %s" % (fmt, data["audio_codecs"])]
    out(args, data, lines, warnings)


def cmd_set_render_format(args, resolve):
    """SetCurrentRenderFormatAndCodec takes the extension + codec ID (not the
    description); a description is translated when it matches."""
    guard(args, "set the render format/codec")
    project = require_project(resolve)
    fmt, codec = need(args, "format"), need(args, "codec")
    codecs = project.GetRenderCodecs(fmt) or {}
    if codec not in codecs.values() and codec in codecs:
        codec = codecs[codec]
    if not project.SetCurrentRenderFormatAndCodec(fmt, codec):
        sys.exit("ERROR: SetCurrentRenderFormatAndCodec(%r, %r) returned False. Codec IDs for %s: %s"
                 % (fmt, codec, fmt, ", ".join(sorted(codecs.values())) or "(none -- open the Deliver page)"))
    out(args, {"current": project.GetCurrentRenderFormatAndCodec(), "ok": True},
        ["render format/codec: %s" % project.GetCurrentRenderFormatAndCodec()])


def cmd_set_render_mode(args, resolve):
    guard(args, "set the render mode")
    project = require_project(resolve)
    mode = {"individual": 0, "single": 1}[need(args, "mode")]
    check(project.SetCurrentRenderMode(mode), "SetCurrentRenderMode(%d)" % mode)
    out(args, {"mode": args.mode, "ok": True}, ["render mode: %s" % args.mode])


# ---------------------------------------------------------------------------
# Render settings (row 43): one key per call, per-key result
# ---------------------------------------------------------------------------

def _set_render_settings(project, settings):
    failed, applied = [], []
    for key, value in settings.items():
        (applied if project.SetRenderSettings({key: value}) else failed).append(key)
    return applied, failed


def cmd_set_render_settings(args, resolve):
    """No readback API exists for render settings; the only proof is the
    output file (verify-render). This reports which keys Resolve accepted."""
    guard(args, "change render settings")
    project = require_project(resolve)
    settings = jsonval(need(args, "settings"), "settings")
    if not isinstance(settings, dict) or not settings:
        sys.exit("ERROR: --settings must be a non-empty JSON object (RenderSettings keys)")
    if settings.get("EnableUpload"):
        sys.exit("ERROR: EnableUpload is refused from automation (accidental publish risk)")
    applied, failed = _set_render_settings(project, settings)
    out(args, {"applied": applied, "failed": failed, "ok": not failed},
        ["applied %d, failed %d" % (len(applied), len(failed))] +
        ["  %s %s" % ("ok " if k in applied else "FAIL", k) for k in settings])
    if failed:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Render presets (row 44)
# ---------------------------------------------------------------------------

def cmd_render_preset(args, resolve):
    action, name = need(args, "action"), need(args, "name")
    guard(args, "%s render preset %r" % (action, name))
    project = require_project(resolve)
    path = getattr(args, "path", None)
    if action == "load":
        ok = project.LoadRenderPreset(name)
    elif action == "save":
        ok = project.SaveAsNewRenderPreset(name)
    elif action == "update":
        ok = project.UpdateRenderPreset(name)
    elif action == "delete":
        ok = project.DeleteRenderPreset(name)
    elif action == "export":
        ok = resolve.ExportRenderPreset(name, need(args, "path"))
        if not ok:
            sys.exit("ERROR: ExportRenderPreset(%r) returned False -- stock presets may refuse export; "
                     "save it as a custom preset first" % name)
    elif action == "import":
        ok = resolve.ImportRenderPreset(need(args, "path"))
    elif action == "quick-export-on":
        ok = project.SetQuickExportEnabledForRenderPreset(name, True)
    else:
        ok = project.SetQuickExportEnabledForRenderPreset(name, False)
    check(ok, "render preset %s(%r)" % (action, name))
    written = path
    if action == "export" and path and os.path.isdir(path):
        # Measured 2026-09-09 (21.1.0.14): ExportRenderPreset treats exportPath as a
        # FOLDER and writes <name>.xml inside it (a directory is created at the path).
        written = os.path.join(path, name + ".xml")
    out(args, {"action": action, "name": name, "path": path, "file": written,
               "current": project.GetCurrentRenderFormatAndCodec(), "ok": True},
        ["render preset %s: %s%s" % (action, name, (" -> " + written) if action == "export" else "")])


# ---------------------------------------------------------------------------
# Job lifecycle (row 45)
# ---------------------------------------------------------------------------

def _job_status(status):
    if isinstance(status, str):
        return {"JobStatus": "Failed", "Error": status, "normalized_from": "string"}
    if isinstance(status, dict):
        return status
    return {"JobStatus": None, "raw": status}


def cmd_render_jobs(args, resolve):
    warnings = []
    project = require_project(resolve)
    jobs = api_list(project.GetRenderJobList(), "GetRenderJobList", warnings)
    rows = []
    for j in jobs:
        if not isinstance(j, dict):
            rows.append({"raw": j})
            continue
        row = dict(j)
        row["status"] = _job_status(project.GetRenderJobStatus(j.get("JobId"))) if j.get("JobId") else None
        rows.append(row)
    out(args, {"jobs": rows, "rendering": project.IsRenderingInProgress()},
        ["%d render job(s); rendering: %s" % (len(rows), project.IsRenderingInProgress())] +
        ["  %s %s -> %s/%s [%s %s%%]" % (r.get("JobId"), r.get("TimelineName"), r.get("TargetDir"),
                                         r.get("OutputFilename"), (r.get("status") or {}).get("JobStatus"),
                                         (r.get("status") or {}).get("CompletionPercentage"))
         for r in rows if isinstance(r, dict) and "JobId" in r], warnings)


def cmd_render_job_status(args, resolve):
    project = require_project(resolve)
    job_id = need(args, "job")
    status = _job_status(project.GetRenderJobStatus(job_id))
    out(args, {"job": job_id, "status": status}, ["%s: %s" % (job_id, json.dumps(status, default=str))])


def cmd_add_render_job(args, resolve):
    """AddRenderJob with the empty-first-return retry, optionally after
    loading a preset / format+codec / settings in one go."""
    guard(args, "add a render job")
    project = require_project(resolve)
    warnings = []
    if getattr(args, "preset", None):
        check(project.LoadRenderPreset(args.preset), "LoadRenderPreset(%r)" % args.preset)
    if getattr(args, "format", None) and getattr(args, "codec", None):
        check(project.SetCurrentRenderFormatAndCodec(args.format, args.codec), "SetCurrentRenderFormatAndCodec")
    settings = jsonval(getattr(args, "settings", None), "settings") or {}
    if getattr(args, "out", None):
        settings["TargetDir"] = args.out
    if getattr(args, "name", None):
        settings["CustomName"] = args.name
    if settings.get("EnableUpload"):
        sys.exit("ERROR: EnableUpload is refused from automation")
    applied, failed = _set_render_settings(project, settings)
    if failed:
        sys.exit("ERROR: SetRenderSettings rejected %s -- job NOT added" % ", ".join(failed))
    job_id, last = None, None
    for attempt in range(1, 4):
        job_id = project.AddRenderJob()
        if job_id:
            if attempt > 1:
                warnings.append("AddRenderJob needed %d attempts (first returned %r)" % (attempt, last))
            break
        last = job_id
    if not job_id:
        sys.exit("ERROR: AddRenderJob returned %r after 3 attempts (open the Deliver page, check the "
                 "current timeline)" % last)
    out(args, {"job": job_id, "applied": applied, "ok": True}, ["added render job %s" % job_id], warnings)


def cmd_delete_render_job(args, resolve):
    guard(args, "delete render job(s)")
    project = require_project(resolve)
    if getattr(args, "all", False):
        check(project.DeleteAllRenderJobs(), "DeleteAllRenderJobs()")
        out(args, {"deleted": "all", "ok": True}, ["deleted all render jobs"])
        return
    job_id = need(args, "job")
    check(project.DeleteRenderJob(job_id), "DeleteRenderJob(%r)" % job_id)
    out(args, {"deleted": job_id, "ok": True}, ["deleted render job %s" % job_id])


def cmd_start_render(args, resolve):
    """StartRendering(jobs, isInteractiveMode=False); --wait polls to a
    terminal state (within the watchdog budget: keep --timeout above it)."""
    guard(args, "start rendering")
    import time
    project = require_project(resolve)
    jobs = list(getattr(args, "jobs", None) or [])
    ok = project.StartRendering(jobs, isInteractiveMode=False) if jobs else project.StartRendering()
    check(ok, "StartRendering(%s)" % (jobs or "all"))
    data = {"jobs": jobs or "all", "started": True, "ok": True}
    if getattr(args, "wait", False) and jobs:
        budget = max(5, int(getattr(args, "timeout", 25)) - 10)
        deadline = time.monotonic() + budget
        final = {}
        while time.monotonic() < deadline:
            final = {j: _job_status(project.GetRenderJobStatus(j)) for j in jobs}
            states = {str(s.get("JobStatus") or "").lower() for s in final.values()}
            if states and all(st in ("complete", "completed", "cancelled", "canceled", "failed", "error")
                              for st in states):
                break
            time.sleep(0.5)
        data["status"] = final
        data["ok"] = all(str(s.get("JobStatus") or "").lower() in ("complete", "completed") for s in final.values())
    out(args, data, ["started rendering %s%s" % (jobs or "all", "" if not data.get("status") else
                                                  ": " + json.dumps(data["status"], default=str))])
    if not data["ok"]:
        sys.exit(1)


def cmd_stop_render(args, resolve):
    guard(args, "stop rendering")
    project = require_project(resolve)
    project.StopRendering()
    out(args, {"rendering": project.IsRenderingInProgress(), "ok": True},
        ["stop requested; rendering: %s" % project.IsRenderingInProgress()])


def cmd_quick_export_presets(args, resolve):
    warnings = []
    project = require_project(resolve)
    presets = api_list(project.GetQuickExportRenderPresets(), "GetQuickExportRenderPresets", warnings)
    out(args, {"presets": presets}, ["%d quick-export preset(s):" % len(presets)] + ["  %s" % p for p in presets], warnings)


def cmd_quick_export(args, resolve):
    """RenderWithQuickExport renders the CURRENT timeline immediately.
    EnableUpload is hard-coded False."""
    guard(args, "quick-export the current timeline")
    project = require_project(resolve)
    tl = require_timeline(project)
    preset = need(args, "preset")
    settings = {"TargetDir": need(args, "out"), "CustomName": need(args, "name"), "EnableUpload": False}
    if getattr(args, "quality", None) is not None:
        settings["VideoQuality"] = int(args.quality)
    status = project.RenderWithQuickExport(preset, settings)
    status = _job_status(status)
    ok = str(status.get("JobStatus") or "").lower().startswith("render complete")
    out(args, {"timeline": tl.GetName(), "preset": preset, "settings": settings, "status": status, "ok": ok},
        ["quick export %s: %s" % (preset, json.dumps(status, default=str))])
    if not ok:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Verify by file (row 48) -- no Resolve call at all
# ---------------------------------------------------------------------------

def _ffprobe():
    for cand in [c for c in (os.environ.get("FFPROBE"), "ffprobe") if c]:
        try:
            subprocess.run([cand, "-version"], capture_output=True, check=True)
            return cand
        except (OSError, subprocess.CalledProcessError):
            continue
    return None


def cmd_verify_render(args, resolve):
    """ffprobe the output file: codec, resolution, fps, duration, audio.
    Because Resolve has no render-settings readback, the FILE is the only
    proof of what was delivered. Optional expectations fail the command."""
    path = need(args, "path")
    if not os.path.isfile(path):
        sys.exit("ERROR: no file at %s" % path)
    size = os.path.getsize(path)
    data = {"path": path, "bytes": size, "ok": size > 0, "problems": []}
    probe = _ffprobe()
    if probe is None:
        data["problems"].append("ffprobe not found; only existence/size checked")
    else:
        try:
            raw = subprocess.run([probe, "-v", "error", "-show_entries",
                                  "stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels,"
                                  "bit_rate:format=duration,bit_rate", "-of", "json", path],
                                 capture_output=True, text=True, check=True).stdout
            info = json.loads(raw)
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            sys.exit("ERROR: ffprobe failed on %s: %s" % (path, exc))
        video = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
        audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
        data["duration"] = float((info.get("format") or {}).get("duration") or 0)
        data["bit_rate"] = (info.get("format") or {}).get("bit_rate")
        if video:
            v = video[0]
            num, _, den = (v.get("r_frame_rate") or "0/1").partition("/")
            data["video"] = {"codec": v.get("codec_name"), "width": v.get("width"), "height": v.get("height"),
                             "fps": round(float(num) / float(den or 1), 3)}
        data["audio"] = [{"codec": a.get("codec_name"), "sample_rate": a.get("sample_rate"),
                          "channels": a.get("channels")} for a in audio]
        want = {"codec": getattr(args, "codec", None), "width": getattr(args, "width", None),
                "height": getattr(args, "height", None), "fps": getattr(args, "fps", None)}
        v = data.get("video") or {}
        if want["codec"] and v.get("codec") != want["codec"]:
            data["problems"].append("codec %s != %s" % (v.get("codec"), want["codec"]))
        if want["width"] and v.get("width") != int(want["width"]):
            data["problems"].append("width %s != %s" % (v.get("width"), want["width"]))
        if want["height"] and v.get("height") != int(want["height"]):
            data["problems"].append("height %s != %s" % (v.get("height"), want["height"]))
        if want["fps"] and abs(float(v.get("fps") or 0) - float(want["fps"])) > 0.01:
            data["problems"].append("fps %s != %s" % (v.get("fps"), want["fps"]))
        if getattr(args, "audio", False) and not audio:
            data["problems"].append("no audio stream")
        if getattr(args, "min_duration", None) is not None and data["duration"] < float(args.min_duration):
            data["problems"].append("duration %.3f < %s" % (data["duration"], args.min_duration))
    data["ok"] = size > 0 and not [p for p in data["problems"] if not p.startswith("ffprobe not found")]
    out(args, data, ["%s: %d bytes, %s, %.2fs, audio %d stream(s)%s" % (
        os.path.basename(path), size, json.dumps(data.get("video"), default=str), data.get("duration", 0),
        len(data.get("audio") or []), (" PROBLEMS: " + "; ".join(data["problems"])) if data["problems"] else "")])
    if not data["ok"]:
        sys.exit(1)


# ---------------------------------------------------------------------------
# System presets (rows 49-51)
# ---------------------------------------------------------------------------

def cmd_presets(args, resolve):
    """Layout / burn-in / user-prefs / keyboard / Fairlight preset lists."""
    warnings = []
    data = {
        "layout": api_list(resolve.GetLayoutPresetList(), "GetLayoutPresetList", warnings),
        "burnin": api_list(resolve.GetBurnInPresetList(), "GetBurnInPresetList", warnings),
        "prefs": api_list(resolve.GetUserPreferencesPresetList(), "GetUserPreferencesPresetList", warnings),
        "keyboard": api_list(resolve.GetKeyboardPresetList(), "GetKeyboardPresetList", warnings),
        "keyboard_current": resolve.GetCurrentKeyboardPreset(),
        "fairlight": resolve.GetFairlightPresets(),
    }
    out(args, data, ["layout: %s" % data["layout"], "burn-in: %s" % data["burnin"], "prefs: %s" % data["prefs"],
                     "keyboard: %s (current %s)" % (data["keyboard"], data["keyboard_current"]),
                     "fairlight: %s" % json.dumps(data["fairlight"], default=str)], warnings)


def cmd_preset(args, resolve):
    """One write command for every system preset family."""
    kind, action = need(args, "kind"), need(args, "action")
    name = getattr(args, "name", None)
    guard(args, "%s %s preset %r" % (action, kind, name))
    path = getattr(args, "path", None)
    project = resolve.GetProjectManager().GetCurrentProject()
    if kind == "layout":
        ok = {"load": lambda: resolve.LoadLayoutPreset(need(args, "name")),
              "save": lambda: resolve.SaveLayoutPreset(need(args, "name")),
              "update": lambda: resolve.UpdateLayoutPreset(need(args, "name")),
              "delete": lambda: resolve.DeleteLayoutPreset(need(args, "name")),
              "export": lambda: resolve.ExportLayoutPreset(need(args, "name"), need(args, "path")),
              "import": lambda: resolve.ImportLayoutPreset(need(args, "path"), name) if name
              else resolve.ImportLayoutPreset(need(args, "path"))}
    elif kind == "burnin":
        ok = {"load": lambda: (project or sys.exit("No project is open")).LoadBurnInPreset(need(args, "name")),
              "delete": lambda: resolve.DeleteBurnInPreset(need(args, "name")),
              "export": lambda: resolve.ExportBurnInPreset(need(args, "name"), need(args, "path")),
              "import": lambda: resolve.ImportBurnInPreset(need(args, "path"))}
    elif kind == "prefs":
        ok = {"load": lambda: resolve.LoadUserPreferencesPreset(need(args, "name")),
              "save": lambda: resolve.SaveUserPreferencesPreset(need(args, "name")),
              "delete": lambda: resolve.DeleteUserPreferencesPreset(need(args, "name")),
              "export": lambda: resolve.ExportUserPreferencesPreset(need(args, "name"), need(args, "path")),
              "import": lambda: resolve.ImportUserPreferencesPreset(need(args, "path"), name) if name
              else resolve.ImportUserPreferencesPreset(need(args, "path"))}
    elif kind == "keyboard":
        ok = {"load": lambda: resolve.LoadKeyboardPreset(need(args, "name")),
              "delete": lambda: resolve.DeleteKeyboardPreset(need(args, "name")),
              "export": lambda: resolve.ExportKeyboardPreset(need(args, "name"), need(args, "path")),
              "import": lambda: resolve.ImportKeyboardPreset(need(args, "path"), name) if name
              else resolve.ImportKeyboardPreset(need(args, "path"))}
    else:  # fairlight
        ok = {"load": lambda: (project or sys.exit("No project is open")).ApplyFairlightPresetToCurrentTimeline(need(args, "name"))}
    if action not in ok:
        sys.exit("ERROR: %s presets support: %s" % (kind, ", ".join(sorted(ok))))
    check(ok[action](), "%s preset %s(%r)" % (kind, action, name))
    out(args, {"kind": kind, "action": action, "name": name, "path": path, "ok": True},
        ["%s preset %s: %s" % (kind, action, name or path)])


def _keyframe_modes(resolve):
    return {"all": resolve.KEYFRAME_MODE_ALL, "color": resolve.KEYFRAME_MODE_COLOR,
            "sizing": resolve.KEYFRAME_MODE_SIZING}


def cmd_keyframe_mode(args, resolve):
    names = {v: k for k, v in _keyframe_modes(resolve).items()}
    current = resolve.GetKeyframeMode()
    out(args, {"mode": names.get(current, current), "raw": current},
        ["keyframe mode: %s (%s)" % (names.get(current, "unknown"), current)])


def cmd_set_keyframe_mode(args, resolve):
    """Setting the mode it already has returns False (measured 20.3) -- that
    case is reported as a no-op, not an error."""
    guard(args, "set the keyframe mode")
    modes = _keyframe_modes(resolve)
    names = {v: k for k, v in modes.items()}
    want = modes[need(args, "mode")]
    current = resolve.GetKeyframeMode()
    if current != want and not resolve.SetKeyframeMode(want):
        sys.exit("ERROR: SetKeyframeMode(%s) returned False (needs the Color page open?)" % args.mode)
    current = resolve.GetKeyframeMode()
    out(args, {"mode": names.get(current, current), "raw": current, "ok": True},
        ["keyframe mode: %s (%s)" % (names.get(current, "unknown"), current)])


def cmd_disable_background_tasks(args, resolve):
    """No re-enable API exists: auto-backup stays off until Resolve restarts."""
    guard(args, "disable background tasks for the rest of this Resolve session (no re-enable API)")
    resolve.DisableBackgroundTasksForCurrentResolveSession()
    out(args, {"ok": True, "note": "background tasks (auto-backup included) are off until Resolve restarts"},
        ["background tasks disabled for this Resolve session -- restart Resolve to restore auto-backup"])


def cmd_quit(args, resolve):
    guard(args, "quit Resolve")
    if getattr(args, "save", False):
        pm = resolve.GetProjectManager()
        if pm.GetCurrentProject() is not None:
            check(pm.SaveProject(), "SaveProject()")
    resolve.Quit()
    out(args, {"quit": True, "ok": True}, ["Resolve.Quit() sent (the process exits within ~10 s)"])


def cmd_encrypt_dctl(args, resolve):
    """21.1 EncryptDCTL: writes an encrypted copy to --out (default: home)."""
    guard(args, "encrypt a DCTL")
    path = need(args, "path")
    options = {}
    if getattr(args, "name", None):
        options["Name"] = args.name
    if getattr(args, "expiry", None):
        options["Expiry"] = args.expiry
    if getattr(args, "out", None):
        options["OutputFolder"] = args.out
    check(resolve.EncryptDCTL(path, options) if options else resolve.EncryptDCTL(path), "EncryptDCTL(%r)" % path)
    out(args, {"encrypted": path, "options": options, "ok": True},
        ["encrypted %s -> %s" % (path, options.get("OutputFolder") or "home folder")])


def cmd_validate_dctl(args, resolve):
    """21.1 ValidateDCTL: None on success, the compiler's error text on failure.
    A read -- nothing is written."""
    src = getattr(args, "source", None)
    path = getattr(args, "path", None)
    if not src:
        if not path:
            sys.exit("ERROR: give --source TEXT or --path FILE to validate")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                src = fh.read()
        except OSError as exc:
            sys.exit("ERROR: cannot read %s: %s" % (path, exc))
    error = resolve.ValidateDCTL(src)
    out(args, {"valid": not error, "error": error, "path": path, "ok": not error},
        ["DCTL %s" % ("valid" if not error else "INVALID: %s" % error)])
    if error:
        sys.exit(1)


def cmd_tts(args, resolve):
    """Project.GenerateSpeech (AI Speech Generator Extras pack)."""
    guard(args, "generate speech")
    project = require_project(resolve)
    settings = {"TextInput": need(args, "text"), "VoiceModel": getattr(args, "voice", None) or "Female 1"}
    for cli, key, cast in (("speed", "Speed", float), ("pitch", "Pitch", float), ("variation", "Variation", float),
                           ("generation_id", "GenerationID", int), ("filename", "Filename", str),
                           ("track", "AudioTrack", int), ("voice_file", "CustomVoiceFile", str)):
        if getattr(args, cli, None) is not None:
            settings[key] = cast(getattr(args, cli))
    if getattr(args, "add_to_timeline", False):
        settings["AddToTimeline"] = True
    if len(settings["TextInput"]) > 350:
        sys.exit("ERROR: TextInput is limited to 350 characters (got %d)" % len(settings["TextInput"]))
    item = project.GenerateSpeech(settings)
    if not item or isinstance(item, str):
        # Measured 2026-09-09: without the Extras pack the call returns the STRING
        # "Required Package, 'AI Speech Generator' is not Installed." -- not None/False.
        sys.exit("ERROR: GenerateSpeech returned %r -- AI Speech Generator Extras pack missing "
                 "(Extras Download Manager in the GUI), or an invalid voice" % item)
    name = item.GetName() if hasattr(item, "GetName") else str(item)
    out(args, {"clip": name, "settings": settings, "ok": True}, ["generated speech clip: %s" % name])


def cmd_insert_audio(args, resolve):
    guard(args, "insert audio at the playhead")
    project = require_project(resolve)
    check(project.InsertAudioToCurrentTrackAtPlayhead(need(args, "path"), int(getattr(args, "offset", None) or 0),
                                                       int(need(args, "duration"))),
          "InsertAudioToCurrentTrackAtPlayhead (Fairlight page, a selected track)")
    out(args, {"path": args.path, "ok": True}, ["inserted %s at the playhead" % args.path])


def cmd_thumbnail(args, resolve):
    """Timeline.GetCurrentClipThumbnailImage (Color page) -> PNG/RAW file."""
    project = require_project(resolve)
    tl = require_timeline(project)
    data = tl.GetCurrentClipThumbnailImage()
    if not data:
        sys.exit("ERROR: GetCurrentClipThumbnailImage returned %r (open the Color page with a clip)" % data)
    path = getattr(args, "out", None)
    written = None
    if path:
        import base64
        raw = base64.b64decode(data.get("data") or "")
        width, height = int(data.get("width") or 0), int(data.get("height") or 0)
        if path.lower().endswith(".ppm") and width and height:
            with open(path, "wb") as fh:
                fh.write(("P6 %d %d 255\n" % (width, height)).encode("ascii") + raw)
        else:
            with open(path, "wb") as fh:
                fh.write(raw)
        written = path
    out(args, {"width": data.get("width"), "height": data.get("height"), "format": data.get("format"),
               "written": written, "ok": True},
        ["thumbnail %sx%s %s%s" % (data.get("width"), data.get("height"), data.get("format"),
                                   (" -> " + written) if written else " (pass --out FILE.ppm to save)")])


def cmd_export_frame(args, resolve):
    guard(args, "export the current frame as a still")
    project = require_project(resolve)
    path = need(args, "path")
    check(project.ExportCurrentFrameAsStill(path), "ExportCurrentFrameAsStill(%r) (page-dependent)" % path)
    out(args, {"path": path, "ok": True}, ["exported current frame -> %s" % path])


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

cmd("render-formats", cmd_render_formats, False,
    "render formats, codecs (desc -> ID) for --format, audio formats/codecs (21.1), resolutions, current selection",
    [arg("format", help="format extension, e.g. mp4"), arg("codec", help="codec id for resolutions")])
cmd("set-render-format", cmd_set_render_format, True, "SetCurrentRenderFormatAndCodec (extension + codec ID)",
    [arg("format", positional=True, required=True), arg("codec", positional=True, required=True)])
cmd("set-render-mode", cmd_set_render_mode, True, "render mode: single (one file) or individual (per clip)",
    [arg("mode", positional=True, required=True, choices=["single", "individual"])])
cmd("set-render-settings", cmd_set_render_settings, True,
    "SetRenderSettings one key per call with per-key result (no readback exists; verify by file)",
    [arg("settings", "json", required=True)])
cmd("render-preset", cmd_render_preset, True,
    "load / save / update / delete / export / import / quick-export-on / quick-export-off a render preset",
    [arg("action", positional=True, required=True,
         choices=["load", "save", "update", "delete", "export", "import", "quick-export-on", "quick-export-off"]),
     arg("name", positional=True, required=True), arg("path")])
cmd("render-jobs", cmd_render_jobs, False, "render queue with normalized per-job status")
cmd("render-job-status", cmd_render_job_status, False, "one job's status (error strings normalized to Failed)",
    [arg("job", positional=True, required=True)])
cmd("add-render-job", cmd_add_render_job, True,
    "configure (--preset / --format+--codec / --settings / --out / --name) and AddRenderJob with retry",
    [arg("preset"), arg("format"), arg("codec"), arg("settings", "json"), arg("out"), arg("name")])
cmd("delete-render-job", cmd_delete_render_job, True, "DeleteRenderJob JOB or --all",
    [arg("job", positional=True), arg("all", "bool")])
cmd("start-render", cmd_start_render, True, "StartRendering jobs (or all); --wait polls to completion",
    [arg("jobs", "list", positional=True, help="job ids (default: all)"), arg("wait", "bool")])
cmd("stop-render", cmd_stop_render, True, "StopRendering")
cmd("quick-export-presets", cmd_quick_export_presets, False, "Quick Export preset names")
cmd("quick-export", cmd_quick_export, True, "RenderWithQuickExport of the current timeline (EnableUpload always off)",
    [arg("preset", positional=True, required=True), arg("out", required=True), arg("name", required=True),
     arg("quality", "int", help="VideoQuality bit rate limit (0 auto)")])
cmd("verify-render", cmd_verify_render, False,
    "ffprobe an output file (codec/size/fps/duration/audio) and check expectations -- the only render readback",
    [arg("path", positional=True, required=True), arg("codec"), arg("width", "int"), arg("height", "int"),
     arg("fps", "float"), arg("audio", "bool", help="require an audio stream"), arg("min-duration", "float")])
cmd("presets", cmd_presets, False, "layout / burn-in / prefs / keyboard (+current) / Fairlight preset lists")
cmd("preset", cmd_preset, True, "system presets: KIND layout|burnin|prefs|keyboard|fairlight, ACTION load|save|update|delete|export|import",
    [arg("kind", positional=True, required=True, choices=["layout", "burnin", "prefs", "keyboard", "fairlight"]),
     arg("action", positional=True, required=True, choices=["load", "save", "update", "delete", "export", "import"]),
     arg("name"), arg("path")])
cmd("keyframe-mode", cmd_keyframe_mode, False, "read the keyframe mode (all|color|sizing)")
cmd("set-keyframe-mode", cmd_set_keyframe_mode, True, "set the keyframe mode",
    [arg("mode", positional=True, required=True, choices=["all", "color", "sizing"])])
cmd("disable-background-tasks", cmd_disable_background_tasks, True,
    "DisableBackgroundTasksForCurrentResolveSession (no re-enable; restart Resolve to restore auto-backup)")
cmd("quit", cmd_quit, True, "Resolve.Quit() (--save first)", [arg("save", "bool")])
cmd("validate-dctl", cmd_validate_dctl, False, "21.1 ValidateDCTL from --source text or --path file",
    [arg("source", help="DCTL source text"), arg("path", help="DCTL file")])
cmd("encrypt-dctl", cmd_encrypt_dctl, True, "21.1 EncryptDCTL a file (--name, --expiry ISO 8601, --out folder)",
    [arg("path", positional=True, required=True, help="DCTL file"),
     arg("name", help="encrypted output name"), arg("expiry", help="ISO 8601"), arg("out", help="output folder")])
cmd("tts", cmd_tts, True, "GenerateSpeech text-to-speech (needs the AI Speech Generator Extras pack; returns an error without it): --text --voice 'Female 1'|'Male 1'|'Custom Voice'",
    [arg("text", required=True), arg("voice"), arg("voice-file"), arg("speed", "float"), arg("pitch", "float"),
     arg("variation", "float"), arg("generation-id", "int"), arg("filename"), arg("add-to-timeline", "bool"),
     arg("track", help="AudioTrack number (0 = new track)")])
cmd("insert-audio", cmd_insert_audio, True, "InsertAudioToCurrentTrackAtPlayhead (Fairlight page)",
    [arg("path", required=True), arg("offset", "int", help="start offset in samples"),
     arg("duration", "int", required=True, help="duration in samples")])
cmd("thumbnail", cmd_thumbnail, False, "GetCurrentClipThumbnailImage (Color page) -> --out FILE.ppm",
    [arg("out")])
cmd("export-frame", cmd_export_frame, True, "ExportCurrentFrameAsStill (.jpg/.png/.tif/.dpx/.drx; page-dependent)",
    [arg("path", positional=True, required=True)])
