"""ops_timeline -- timelines, tracks, items, markers, timecode, settings,
generators/titles, compound/fusion clips, stills, timeline AI, output
blanking, voice isolation, and the 21.1 per-item edits that did not exist
before (AddTransition, SetSpeed, SetFades, SetProperties, multicam).
Absorb-manifest rows 19-33.
"""

import json
import os
import sys

from ops_common import (arg, cmd, out, api_list, require_project, require_timeline,
                        select_timeline, timeline_by_name, guard, jsonval, need, check,
                        find_clip, find_clips, find_item, find_items, item_row, role,
                        parse_track, walk_items, markers_rows, MARKER_COLORS, CLIP_COLORS,
                        TRACK_TYPES, tri, TRI)


# ---------------------------------------------------------------------------
# Timeline lifecycle (row 19)
# ---------------------------------------------------------------------------

def cmd_timeline_info(args, resolve):
    warnings = []
    project = require_project(resolve)
    tl = select_timeline(args, project)
    tracks = {t: tl.GetTrackCount(t) for t in TRACK_TYPES}
    data = {
        "name": tl.GetName(), "id": tl.GetUniqueId(),
        "start_frame": tl.GetStartFrame(), "end_frame": tl.GetEndFrame(),
        "start_timecode": tl.GetStartTimecode(), "current_timecode": tl.GetCurrentTimecode(),
        "tracks": tracks,
        "frame_rate": tl.GetSetting("timelineFrameRate"),
        "resolution": [tl.GetSetting("timelineResolutionWidth"), tl.GetSetting("timelineResolutionHeight")],
        "custom_settings": tl.GetSetting("useCustomSettings"),
        "markers": len(tl.GetMarkers() or {}),
        "mark_in_out": tl.GetMarkInOut(),
        "output_blanking": tl.GetOutputBlanking(),
        "is_current": (project.GetCurrentTimeline() or tl).GetName() == tl.GetName(),
    }
    out(args, data, ["%s: %s..%s @%s %sx%s tracks V%d A%d S%d" % (
        data["name"], data["start_frame"], data["end_frame"], data["frame_rate"],
        data["resolution"][0], data["resolution"][1], tracks["video"], tracks["audio"], tracks["subtitle"])],
        warnings)


def cmd_create_timeline_from_clips(args, resolve):
    """CreateTimelineFromClips: whole clips, or --items JSON of
    {clip, startFrame, endFrame, recordFrame}."""
    guard(args, "create timeline %r from clips" % getattr(args, "name", None))
    project = require_project(resolve)
    pool = project.GetMediaPool()
    name = need(args, "name")
    warnings = []
    items = jsonval(getattr(args, "batch", None), "batch")
    if items:
        infos = []
        for spec in items:
            info = {"mediaPoolItem": find_clip(pool, spec.get("clip"), warnings, getattr(args, "bin", None))}
            for k in ("startFrame", "endFrame", "recordFrame"):
                if k in spec:
                    info[k] = int(spec[k])
            infos.append(info)
    else:
        infos = find_clips(pool, need(args, "clips"), warnings, getattr(args, "bin", None))
    tl = pool.CreateTimelineFromClips(name, infos)
    if tl is None:
        sys.exit("ERROR: CreateTimelineFromClips(%r) returned None (duplicate name, page-null, "
                 "or an unusable clip)." % name)
    placed = [item_row(i, r) for i, r, _ in walk_items(tl, warnings, ("video",))]
    out(args, {"created_timeline": tl.GetName(), "placed": placed, "ok": True},
        ["created timeline %s with %d video item(s)" % (tl.GetName(), len(placed))], warnings)


def cmd_duplicate_timeline(args, resolve):
    guard(args, "duplicate a timeline")
    project = require_project(resolve)
    tl = select_timeline(args, project)
    new = tl.DuplicateTimeline(need(args, "name"))
    if new is None:
        sys.exit("ERROR: DuplicateTimeline(%r) returned None" % args.name)
    out(args, {"source": tl.GetName(), "duplicate": new.GetName(), "ok": True},
        ["duplicated %s -> %s" % (tl.GetName(), new.GetName())])


def cmd_delete_timeline(args, resolve):
    guard(args, "delete timeline %r" % getattr(args, "name", None))
    project = require_project(resolve)
    tl = timeline_by_name(project, need(args, "name"))
    check(project.GetMediaPool().DeleteTimelines([tl]), "DeleteTimelines(%r)" % args.name)
    out(args, {"deleted": args.name, "ok": True}, ["deleted timeline: %s" % args.name])


def cmd_switch_timeline(args, resolve):
    guard(args, "switch the current timeline (this changes the editor's GUI)")
    project = require_project(resolve)
    tl = timeline_by_name(project, need(args, "name"))
    check(project.SetCurrentTimeline(tl), "SetCurrentTimeline(%r)" % args.name)
    out(args, {"current": project.GetCurrentTimeline().GetName(), "ok": True},
        ["current timeline: %s" % project.GetCurrentTimeline().GetName()])


def cmd_rename_timeline(args, resolve):
    guard(args, "rename a timeline")
    project = require_project(resolve)
    tl = select_timeline(args, project)
    old = tl.GetName()
    check(tl.SetName(need(args, "name")), "Timeline.SetName(%r)" % args.name)
    out(args, {"renamed": old, "to": tl.GetName(), "ok": True}, ["%s -> %s" % (old, tl.GetName())])


# ---------------------------------------------------------------------------
# Append (row 20) -- the only placement primitive; verified by readback
# ---------------------------------------------------------------------------

def cmd_append(args, resolve):
    """AppendToTimeline with pre-resolved geometry, then GetStart/GetDuration
    read back against the request. Appends to the CURRENT timeline (API rule)."""
    guard(args, "append clip(s) to the current timeline")
    project = require_project(resolve)
    pool = project.GetMediaPool()
    tl = require_timeline(project)
    warnings = []
    specs = jsonval(getattr(args, "batch", None), "batch")
    if not specs:
        spec = {"clip": need(args, "clip")}
        for cli, key in (("start_frame", "startFrame"), ("end_frame", "endFrame"),
                         ("record_frame", "recordFrame"), ("track_index", "trackIndex")):
            if getattr(args, cli, None) is not None:
                spec[key] = getattr(args, cli)
        if getattr(args, "media_type", None):
            spec["mediaType"] = {"video": 1, "audio": 2}[args.media_type]
        specs = [spec]
    placed, mismatches = [], []
    for spec in specs:
        clip = find_clip(pool, spec.get("clip"), warnings, getattr(args, "bin", None))
        info = {"mediaPoolItem": clip}
        for k in ("startFrame", "endFrame", "recordFrame", "trackIndex", "mediaType"):
            if spec.get(k) is not None:
                info[k] = int(spec[k])
        items = pool.AppendToTimeline([info])
        if not items or items[0] is None:
            # Measured 2026-09-09: [None] comes back (not [] / None) when the current
            # timeline is unusable -- e.g. right after LoadProject with no timeline open.
            sys.exit("ERROR: AppendToTimeline returned %r for %s (%s) -- %d placed before it. "
                     "Check the current timeline (switch-timeline NAME), the track index, and "
                     "that the clip is usable on this timeline. A MULTICAM clip created with "
                     "createBinForSourceClips=True answers [None] until another multicam is created "
                     "(21.1 measured) -- create it with --source-bin off, or use "
                     "create-timeline-from-clips, which works regardless."
                     % (items, clip.GetName(), json.dumps({k: v for k, v in info.items() if k != "mediaPoolItem"}),
                        len(placed)))
        item = items[0]
        row = item_row(item)
        row["clip"] = clip.GetName()
        if "recordFrame" in info and item.GetStart() != info["recordFrame"]:
            mismatches.append("%s placed at %s, wanted %s" % (clip.GetName(), item.GetStart(), info["recordFrame"]))
        if "startFrame" in info and "endFrame" in info:
            want = info["endFrame"] - info["startFrame"] + 1
            if item.GetDuration() not in (want, want - 1):
                mismatches.append("%s duration %s, wanted %s" % (clip.GetName(), item.GetDuration(), want))
        placed.append(row)
    data = {"timeline": tl.GetName(), "placed": placed, "mismatches": mismatches, "ok": not mismatches}
    out(args, data, ["appended %d item(s) to %s%s" % (len(placed), tl.GetName(),
                                                       "; %d MISMATCH" % len(mismatches) if mismatches else "")] +
        ["  %s %s @%s %s frames" % (p["track"], p["name"], p["start"], p["duration"]) for p in placed] +
        ["  ! %s" % m for m in mismatches], warnings)
    if mismatches:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Import / export (row 21)
# ---------------------------------------------------------------------------

def _export_types(resolve):
    return {
        "aaf": resolve.EXPORT_AAF, "drt": resolve.EXPORT_DRT, "edl": resolve.EXPORT_EDL,
        "fcp7xml": resolve.EXPORT_FCP_7_XML, "fcpxml_1_8": resolve.EXPORT_FCPXML_1_8,
        "fcpxml_1_9": resolve.EXPORT_FCPXML_1_9, "fcpxml": resolve.EXPORT_FCPXML_1_10,
        "fcpxml_1_10": resolve.EXPORT_FCPXML_1_10, "hdr10_a": resolve.EXPORT_HDR_10_PROFILE_A,
        "hdr10_b": resolve.EXPORT_HDR_10_PROFILE_B, "csv": resolve.EXPORT_TEXT_CSV,
        "tab": resolve.EXPORT_TEXT_TAB, "dolby_2_9": resolve.EXPORT_DOLBY_VISION_VER_2_9,
        "dolby_4_0": resolve.EXPORT_DOLBY_VISION_VER_4_0, "dolby_5_1": resolve.EXPORT_DOLBY_VISION_VER_5_1,
        "otio": resolve.EXPORT_OTIO, "ale": resolve.EXPORT_ALE, "ale_cdl": resolve.EXPORT_ALE_CDL,
    }


EXPORT_TYPE_NAMES = ["aaf", "drt", "edl", "fcp7xml", "fcpxml", "fcpxml_1_8", "fcpxml_1_9", "fcpxml_1_10",
                     "hdr10_a", "hdr10_b", "csv", "tab", "dolby_2_9", "dolby_4_0", "dolby_5_1",
                     "otio", "ale", "ale_cdl"]
EXPORT_SUBTYPE_NAMES = ["none", "aaf_new", "aaf_existing", "cdl", "sdl", "missing_clips"]


def cmd_export_timeline(args, resolve):
    guard(args, "export a timeline file")
    project = require_project(resolve)
    tl = select_timeline(args, project)
    path, kind = need(args, "path"), need(args, "type")
    subtypes = {"none": resolve.EXPORT_NONE, "aaf_new": resolve.EXPORT_AAF_NEW,
                "aaf_existing": resolve.EXPORT_AAF_EXISTING, "cdl": resolve.EXPORT_CDL,
                "sdl": resolve.EXPORT_SDL, "missing_clips": resolve.EXPORT_MISSING_CLIPS}
    sub = getattr(args, "subtype", None)
    if kind == "aaf" and not sub:
        sub = "aaf_new"
    subtype = subtypes[sub or "none"]
    if os.path.isdir(path):
        sys.exit("ERROR: %s is a directory (a previous FCPXML bundle export?). Timeline.Export returns "
                 "False rather than overwrite it -- remove it or export to a new path." % path)
    check(tl.Export(path, _export_types(resolve)[kind], subtype), "Timeline.Export(%r, %s)" % (path, kind))
    # Measured 2026-09-09 (21.1.0.14): EXPORT_FCPXML_1_10 writes a BUNDLE directory at
    # `path` containing Info.fcpxml (the file ImportTimelineFromFile wants). Report
    # the real file so a caller never feeds the directory back in.
    written = path
    if os.path.isdir(path):
        inner = [f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))]
        written = os.path.join(path, inner[0]) if len(inner) == 1 else path
    out(args, {"timeline": tl.GetName(), "path": path, "file": written, "bundle": os.path.isdir(path),
               "type": kind, "subtype": sub or "none", "ok": True},
        ["exported %s -> %s (%s)%s" % (tl.GetName(), written, kind,
                                       " [bundle directory at %s]" % path if os.path.isdir(path) else "")])


def cmd_import_timeline(args, resolve):
    """ImportTimelineFromFile (AAF/EDL/XML/FCPXML/DRT/OTIO) into the current
    project; --into imports an AAF INTO the current timeline instead."""
    guard(args, "import a timeline file")
    project = require_project(resolve)
    path = need(args, "path")
    options = jsonval(getattr(args, "options", None), "options") or {}
    warnings = []
    if getattr(args, "into", False):
        tl = require_timeline(project)
        check(tl.ImportIntoTimeline(path, options) if options else tl.ImportIntoTimeline(path),
              "ImportIntoTimeline(%r)" % path)
        out(args, {"timeline": tl.GetName(), "path": path, "into": True, "ok": True},
            ["imported %s into %s" % (path, tl.GetName())], warnings)
        return
    if getattr(args, "name", None):
        options["timelineName"] = args.name
    if getattr(args, "source_clips_path", None):
        options["sourceClipsPath"] = args.source_clips_path
    if getattr(args, "no_import_source_clips", False):
        options["importSourceClips"] = False
    # An FCPXML 1.10 export is a bundle DIRECTORY holding Info.fcpxml (measured
    # 2026-09-09); the importer wants the file inside it.
    if os.path.isdir(path):
        inner = [f for f in os.listdir(path) if f.lower().endswith((".fcpxml", ".xml"))]
        if len(inner) == 1:
            warnings.append("%s is a bundle directory; importing %s inside it" % (path, inner[0]))
            path = os.path.join(path, inner[0])
    path = os.path.normpath(path)
    pool = project.GetMediaPool()
    tl = pool.ImportTimelineFromFile(path, options) if options else pool.ImportTimelineFromFile(path)
    if tl is None and "importSourceClips" not in options:
        # Measured 2026-09-09 (21.1.0.14): an OTIO whose media is already in the pool
        # imports with importSourceClips=False and logs "Operation canceled" without it.
        retry = dict(options, importSourceClips=False)
        tl = pool.ImportTimelineFromFile(path, retry)
        if tl is not None:
            warnings.append("import succeeded only with importSourceClips=False (media already in the pool)")
    if tl is None:
        sys.exit("ERROR: ImportTimelineFromFile(%r) returned None (unsupported format, media not "
                 "found -- try --source-clips-path, or the media is already in the pool and the "
                 "importer cancelled -- try --no-import-source-clips; or page-null)" % path)
    counts = {t: tl.GetTrackCount(t) for t in TRACK_TYPES}
    out(args, {"timeline": tl.GetName(), "path": path, "tracks": counts, "ok": True},
        ["imported %s -> timeline %s (V%d A%d S%d)" % (path, tl.GetName(), counts["video"],
                                                      counts["audio"], counts["subtitle"])], warnings)


# ---------------------------------------------------------------------------
# Tracks (rows 22, 31)
# ---------------------------------------------------------------------------

def cmd_tracks(args, resolve):
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    rows = []
    for track_type in TRACK_TYPES:
        for index in range(1, tl.GetTrackCount(track_type) + 1):
            row = {"track": role(track_type, index), "type": track_type, "index": index,
                   "name": tl.GetTrackName(track_type, index),
                   "enabled": tl.GetIsTrackEnabled(track_type, index),
                   "locked": tl.GetIsTrackLocked(track_type, index),
                   "items": len(api_list(tl.GetItemListInTrack(track_type, index),
                                         "GetItemListInTrack", warnings))}
            if track_type == "audio":
                row["subtype"] = tl.GetTrackSubType(track_type, index)
                row["voice_isolation"] = tl.GetVoiceIsolationState(index)
            rows.append(row)
    out(args, {"timeline": tl.GetName(), "tracks": rows},
        ["%d track(s) on %s:" % (len(rows), tl.GetName())] +
        ["  %-4s %-20s %s%s %d item(s)%s" % (r["track"], r["name"], "" if r["enabled"] else "[off] ",
                                             "[locked] " if r["locked"] else "", r["items"],
                                             (" " + str(r.get("subtype"))) if r.get("subtype") else "")
         for r in rows], warnings)


def cmd_add_track(args, resolve):
    guard(args, "add a track")
    tl = select_timeline(args, require_project(resolve))
    track_type = need(args, "type")
    subtype = getattr(args, "subtype", None)
    check(tl.AddTrack(track_type, subtype) if subtype else tl.AddTrack(track_type),
          "AddTrack(%s%s)" % (track_type, (", " + subtype) if subtype else ""))
    count = tl.GetTrackCount(track_type)
    out(args, {"timeline": tl.GetName(), "type": track_type, "count": count, "ok": True},
        ["%s now has %d %s track(s)" % (tl.GetName(), count, track_type)])


def cmd_delete_track(args, resolve):
    guard(args, "delete a track")
    tl = select_timeline(args, require_project(resolve))
    track_type, index = parse_track(need(args, "track"))
    check(tl.DeleteTrack(track_type, index), "DeleteTrack(%s, %d)" % (track_type, index))
    out(args, {"timeline": tl.GetName(), "deleted": role(track_type, index),
               "count": tl.GetTrackCount(track_type), "ok": True},
        ["deleted %s; %d %s track(s) remain" % (role(track_type, index), tl.GetTrackCount(track_type), track_type)])


def cmd_set_track(args, resolve):
    guard(args, "change track state")
    tl = select_timeline(args, require_project(resolve))
    track_type, index = parse_track(need(args, "track"))
    changed = []
    if getattr(args, "name", None):
        check(tl.SetTrackName(track_type, index, args.name), "SetTrackName")
        changed.append("name")
    enable = tri(args, "enable")
    if enable is not None:
        check(tl.SetTrackEnable(track_type, index, enable), "SetTrackEnable")
        changed.append("enable")
    lock = tri(args, "lock")
    if lock is not None:
        check(tl.SetTrackLock(track_type, index, lock), "SetTrackLock")
        changed.append("lock")
    vi_enable, vi_amount = tri(args, "voice_isolation"), getattr(args, "voice_isolation_amount", None)
    if vi_enable is not None or vi_amount is not None:
        if track_type != "audio":
            sys.exit("ERROR: voice isolation applies to audio tracks only")
        state = tl.GetVoiceIsolationState(index) or {}
        state = {"isEnabled": bool(vi_enable) if vi_enable is not None else bool(state.get("isEnabled")),
                 "amount": int(vi_amount) if vi_amount is not None else int(state.get("amount") or 0)}
        check(tl.SetVoiceIsolationState(index, state), "SetVoiceIsolationState(%d)" % index)
        changed.append("voice_isolation")
    if not changed:
        sys.exit("ERROR: nothing to change (give --name/--enable/--lock/--voice-isolation)")
    row = {"track": role(track_type, index), "name": tl.GetTrackName(track_type, index),
           "enabled": tl.GetIsTrackEnabled(track_type, index), "locked": tl.GetIsTrackLocked(track_type, index)}
    if track_type == "audio":
        row["voice_isolation"] = tl.GetVoiceIsolationState(index)
    out(args, dict(row, changed=changed, ok=True), ["%s: %s" % (row["track"], json.dumps(row, default=str))])


# ---------------------------------------------------------------------------
# Items (rows 23, 32, 33 + 21.1 transitions/speed/fades/multicam)
# ---------------------------------------------------------------------------

def cmd_items(args, resolve):
    """Every item on every track (video/audio/subtitle) with type, source
    range, and the media pool clip it came from."""
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    types = tuple(t for t in TRACK_TYPES if not getattr(args, "type", None) or t == args.type)
    rows = []
    for item, r, pos in walk_items(tl, warnings, types):
        row = item_row(item, r)
        row["position"] = pos
        row["ref"] = "%s:%d" % (r, pos)
        row["type"] = item.GetType()
        row["enabled"] = item.GetClipEnabled()
        row["source_start"] = item.GetSourceStartFrame()
        row["source_end"] = item.GetSourceEndFrame()
        try:
            mpi = item.GetMediaPoolItem()
            row["clip"] = mpi.GetName() if mpi else None
        except Exception as exc:  # noqa: BLE001 - generators have no pool item
            row["clip"] = None
            warnings.append("GetMediaPoolItem failed for %r: %s" % (row["name"], exc))
        rows.append(row)
    out(args, {"timeline": tl.GetName(), "items": rows},
        ["%d item(s) on %s:" % (len(rows), tl.GetName())] +
        ["  %-6s %-10s %-30s @%s %s frames" % (r["ref"], r["type"], r["name"], r["start"], r["duration"])
         for r in rows], warnings)


def cmd_item_info(args, resolve):
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    data = item_row(item, r)
    data.update({
        "type": item.GetType(), "enabled": item.GetClipEnabled(),
        "source_start": item.GetSourceStartFrame(), "source_end": item.GetSourceEndFrame(),
        "left_offset": item.GetLeftOffset(), "right_offset": item.GetRightOffset(),
        "properties": item.GetProperties(),
        "speed": item.GetSpeed(), "fades": item.GetFades(),
        "output_blanking": item.GetOutputBlanking(),
        "use_timeline_blanking": item.GetUseTimelineForOutputBlanking(),
        "flags": item.GetFlagList() or [], "clip_color": item.GetClipColor(),
        "markers": markers_rows(item.GetMarkers()),
        "fusion_comps": item.GetFusionCompNameList() or [],
        "color_version": item.GetCurrentVersion(),
        "linked_items": [i.GetName() for i in (item.GetLinkedItems() or [])],
        "color_output_cache": item.GetIsColorOutputCacheEnabled(),
        "fusion_output_cache": item.GetIsFusionOutputCacheEnabled(),
        "takes": item.GetTakesCount(),
        "voice_isolation": item.GetVoiceIsolationState(),
    })
    mapping = item.GetSourceAudioChannelMapping()
    if isinstance(mapping, str) and mapping:
        try:
            data["audio_mapping"] = json.loads(mapping)
        except ValueError:
            data["audio_mapping"] = mapping
    try:
        mpi = item.GetMediaPoolItem()
        data["clip"] = mpi.GetName() if mpi else None
    except Exception as exc:  # noqa: BLE001
        data["clip"] = None
        warnings.append("GetMediaPoolItem failed: %s" % exc)
    out(args, data, ["%s %s (%s) @%s %s frames, speed %s, fades %s, %d comp(s), version %s" % (
        data["track"], data["name"], data["type"], data["start"], data["duration"], data["speed"],
        data["fades"], len(data["fusion_comps"]), data["color_version"])], warnings)


def cmd_delete_items(args, resolve):
    guard(args, "delete timeline items")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    items = find_items(tl, need(args, "items"), warnings)
    names = [i.GetName() for i in items]
    check(tl.DeleteClips(items, bool(getattr(args, "ripple", False))), "DeleteClips(ripple=%s)" % bool(args.ripple))
    out(args, {"deleted": names, "ripple": bool(getattr(args, "ripple", False)), "ok": True},
        ["deleted %d item(s)%s" % (len(names), " (ripple)" if getattr(args, "ripple", False) else "")], warnings)


def cmd_link_items(args, resolve):
    guard(args, "change item linking")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    items = find_items(tl, need(args, "items"), warnings)
    linked = not getattr(args, "unlink", False)
    check(tl.SetClipsLinked(items, linked), "SetClipsLinked(%s)" % linked)
    out(args, {"items": [i.GetName() for i in items], "linked": linked, "ok": True},
        ["%s %d item(s)" % ("linked" if linked else "unlinked", len(items))], warnings)


def cmd_set_item(args, resolve):
    """Per-item edits: enabled, name, clip color, flags, speed (21.1 SetSpeed),
    fades (21.1 SetFades), output blanking, caches, audio mapping, voice
    isolation. Each is one Resolve call, each checked."""
    guard(args, "change a timeline item")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    changed = []
    if getattr(args, "name", None):
        check(item.SetName(args.name), "SetName")
        changed.append("name")
    if tri(args, "enabled") is not None:
        check(item.SetClipEnabled(tri(args, "enabled")), "SetClipEnabled")
        changed.append("enabled")
    if getattr(args, "color", None):
        check(item.ClearClipColor() if args.color == "clear" else item.SetClipColor(args.color), "SetClipColor")
        changed.append("color")
    if getattr(args, "add_flag", None):
        check(item.AddFlag(args.add_flag), "AddFlag")
        changed.append("flag")
    if getattr(args, "remove_flag", None):
        check(item.ClearFlags(args.remove_flag), "ClearFlags")
        changed.append("flag")
    if getattr(args, "speed", None) is not None:
        opts = jsonval(args.speed, "speed")
        if not isinstance(opts, dict):
            opts = {"Percentage": float(opts)}
        check(item.SetSpeed(opts), "SetSpeed(%s)" % json.dumps(opts))
        changed.append("speed")
    if getattr(args, "fade_in", None) is not None or getattr(args, "fade_out", None) is not None:
        fades = item.GetFades() or {}
        fades = {"FadeIn": int(args.fade_in) if args.fade_in is not None else int(fades.get("FadeIn") or 0),
                 "FadeOut": int(args.fade_out) if args.fade_out is not None else int(fades.get("FadeOut") or 0)}
        check(item.SetFades(fades), "SetFades(%s)" % json.dumps(fades))
        changed.append("fades")
    if getattr(args, "blanking", None) is not None:
        b = jsonval(args.blanking, "blanking")
        check(item.SetOutputBlanking(b), "SetOutputBlanking")
        changed.append("blanking")
    if tri(args, "use_timeline_blanking") is not None:
        check(item.SetUseTimelineForOutputBlanking(tri(args, "use_timeline_blanking")), "SetUseTimelineForOutputBlanking")
        changed.append("use_timeline_blanking")
    if tri(args, "color_cache") is not None:
        check(item.SetColorOutputCache(tri(args, "color_cache")), "SetColorOutputCache")
        changed.append("color_cache")
    if getattr(args, "fusion_cache", None) is not None:
        check(item.SetFusionOutputCache(args.fusion_cache), "SetFusionOutputCache")
        changed.append("fusion_cache")
    if getattr(args, "audio_mapping", None) is not None:
        mapping = jsonval(args.audio_mapping, "audio_mapping")
        check(item.SetSourceAudioChannelMapping(json.dumps(mapping)), "SetSourceAudioChannelMapping")
        changed.append("audio_mapping")
    if tri(args, "voice_isolation") is not None or getattr(args, "voice_isolation_amount", None) is not None:
        state = item.GetVoiceIsolationState() or {}
        vi = tri(args, "voice_isolation")
        state = {"isEnabled": vi if vi is not None else bool(state.get("isEnabled")),
                 "amount": int(args.voice_isolation_amount) if args.voice_isolation_amount is not None
                 else int(state.get("amount") or 0)}
        check(item.SetVoiceIsolationState(state), "SetVoiceIsolationState")
        changed.append("voice_isolation")
    if not changed:
        sys.exit("ERROR: nothing to change")
    data = item_row(item, r)
    data.update({"changed": changed, "enabled": item.GetClipEnabled(), "speed": item.GetSpeed(),
                 "fades": item.GetFades(), "color": item.GetClipColor(), "flags": item.GetFlagList(), "ok": True})
    out(args, data, ["%s %s: changed %s" % (r, item.GetName(), ", ".join(changed))], warnings)


def cmd_set_item_properties(args, resolve):
    """TimelineItem.SetProperties (21.1, all-or-nothing validation) with a
    per-key fallback through SetProperty when the dict call is refused, so
    the caller learns WHICH key Resolve rejects. STATIC values only."""
    guard(args, "set timeline item properties")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    props = jsonval(need(args, "properties"), "properties")
    if not isinstance(props, dict) or not props:
        sys.exit("ERROR: --properties must be a non-empty JSON object (Pan, Tilt, ZoomX, Opacity, ...)")
    ok = item.SetProperties(props)
    failed = []
    if not ok:
        for key, value in props.items():
            if not item.SetProperty(key, value):
                failed.append(key)
        warnings.append("SetProperties(dict) returned False; applied per key instead (failed: %s)"
                        % (", ".join(failed) or "none"))
    readback = item.GetProperties() or {}
    data = {"item": item.GetName(), "track": r, "set": props, "failed": failed,
            "readback": {k: readback.get(k) for k in props}, "ok": not failed}
    out(args, data, ["%s %s: %d propert%s set%s" % (r, item.GetName(), len(props) - len(failed),
                                                    "y" if len(props) == 1 else "ies",
                                                    (", FAILED: " + ", ".join(failed)) if failed else "")] +
        ["  %s = %s" % (k, v) for k, v in data["readback"].items()], warnings)
    if failed:
        sys.exit(1)


def cmd_add_transition(args, resolve):
    """21.1 TimelineItem.AddTransition -- transitions did not exist in the API
    before this version."""
    guard(args, "add a transition")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    opts = {"type": getattr(args, "type", None) or "Cross Dissolve",
            "category": getattr(args, "category", None) or "simple",
            "position": getattr(args, "position", None) or "start",
            "alignment": getattr(args, "alignment", None) or "center"}
    if getattr(args, "duration", None) is not None:
        opts["duration"] = int(args.duration)
    created = item.AddTransition(opts)
    if created is None:
        # Measured 2026-09-09 (21.1.0.14): a transition consumes media HANDLES.
        # alignment 'right' at a cut needs a tail handle on the outgoing clip
        # (GetRightOffset > 0); 'left'/'center' also need a head handle on the
        # incoming clip (GetLeftOffset > 0). With no handle on that side, and
        # with position/alignment/duration omitted, the call returns None.
        sys.exit("ERROR: AddTransition(%s) returned None on %s (handles: head %s, tail %s frames). "
                 "Transitions consume media handles: 'right' alignment needs a tail handle on the "
                 "outgoing clip, 'left'/'center' need a head handle on the incoming clip; pass "
                 "position + alignment + duration explicitly; the item must be on the active timeline "
                 "and the name must be a real transition ('Cross Dissolve', 'Dip To Color Dissolve', ...)."
                 % (json.dumps(opts), item.GetName(), item.GetLeftOffset(), item.GetRightOffset()))
    row = item_row(created)
    row["type"] = created.GetType()
    out(args, {"item": item.GetName(), "track": r, "options": opts, "transition": row, "ok": True},
        ["added %s at %s of %s: %s @%s %s frames" % (opts["type"], opts["position"], item.GetName(),
                                                    row["name"], row["start"], row["duration"])], warnings)


def cmd_multicam(args, resolve):
    """21.1 multicam: create a multicam clip from pool clips, flatten a
    multicam timeline item, or run SmartSwitch on it."""
    action = need(args, "action")
    guard(args, "multicam %s" % action)
    warnings = []
    project = require_project(resolve)
    if action == "create":
        pool = project.GetMediaPool()
        clips = find_clips(pool, need(args, "clips"), warnings, getattr(args, "bin", None))
        opts = jsonval(getattr(args, "options", None), "options") or {}
        if getattr(args, "name", None):
            opts["name"] = args.name
        # Measured 2026-09-09 (21.1.0.14): with the API default createBinForSourceClips=True
        # the sources are MOVED into an "Original Clips" bin and the new multicam clip
        # answers AppendToTimeline with [None] until ANOTHER multicam is created; with
        # False it appends immediately. Default off here; --source-bin opts back in.
        opts.setdefault("createBinForSourceClips", bool(getattr(args, "source_bin", False)))
        sync = getattr(args, "sync", None)
        if sync:
            opts["angleSyncMode"] = {"timecode": resolve.MULTICAM_ANGLE_SYNC_TIMECODE,
                                     "audio": resolve.MULTICAM_ANGLE_SYNC_AUDIO,
                                     "in": resolve.MULTICAM_ANGLE_SYNC_IN, "out": resolve.MULTICAM_ANGLE_SYNC_OUT,
                                     "marker": resolve.MULTICAM_ANGLE_SYNC_MARKER}[sync]
        created = pool.CreateMulticamClip(clips, opts)
        if not created:
            sys.exit("ERROR: CreateMulticamClip returned %r (clips must share sync data for the chosen mode)" % created)
        names = [c.GetName() for c in created] if isinstance(created, list) else [str(created)]
        out(args, {"created": names, "sources": [c.GetName() for c in clips], "ok": True},
            ["created multicam clip(s): %s" % ", ".join(names)], warnings)
        return
    tl = select_timeline(args, project)
    item, r = find_item(tl, need(args, "item"), warnings)
    if action == "flatten":
        grade = resolve.FLATTEN_MULTICAM_COPY_GRADE if (getattr(args, "grade", None) or "copy") == "copy" \
            else resolve.FLATTEN_MULTICAM_RETAIN_GRADE_FROM_ANGLE
        check(item.FlattenMulticam(grade), "FlattenMulticam")
        out(args, {"item": item.GetName(), "track": r, "flattened": True, "ok": True},
            ["flattened multicam item %s" % item.GetName()], warnings)
    else:  # smart-switch
        opts = jsonval(getattr(args, "options", None), "options") or {}
        modes = {"none": resolve.SMART_SWITCH_ANALYSIS_MODE_NONE,
                 "wide-angle": resolve.SMART_SWITCH_ANALYSIS_MODE_DETECT_WIDE_ANGLE,
                 "audio": resolve.SMART_SWITCH_ANALYSIS_MODE_AUDIO_ONLY}
        # Measured 2026-09-09: {"minEditDuration": 1.0} alone -> False (the API default
        # wants a wide-angle analysis); with analysisMode AUDIO_ONLY -> True in ~1 s.
        opts.setdefault("analysisMode", modes[getattr(args, "analysis", None) or "audio"])
        check(item.PerformMulticamSmartSwitch(opts),
              "PerformMulticamSmartSwitch(%s) (wide-angle mode needs the item analysed first)" % json.dumps(opts))
        out(args, {"item": item.GetName(), "track": r, "options": opts, "ok": True},
            ["smart-switch done on %s" % item.GetName()], warnings)


# ---------------------------------------------------------------------------
# Timeline markers / timecode / marks (rows 24, 25)
# ---------------------------------------------------------------------------

def _custom_data(args):
    """customData for a marker: a json-kind arg (JSON text or @file on the CLI,
    structured over HTTP) stored as its JSON text; a bare string stays a string."""
    cd = getattr(args, "custom_data", None)
    if cd is None:
        return None
    if isinstance(cd, str) and cd.strip().startswith("@"):
        cd = jsonval(cd, "custom-data")
    return cd if isinstance(cd, str) else json.dumps(cd)


def cmd_timeline_markers(args, resolve):
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    rows = markers_rows(tl.GetMarkers())
    out(args, {"timeline": tl.GetName(), "start_frame": tl.GetStartFrame(), "markers": rows},
        ["%d marker(s) on %s (frames relative to start %s):" % (len(rows), tl.GetName(), tl.GetStartFrame())] +
        ["  %6d %-8s %s %s" % (r["frame"], r.get("color"), r.get("name"), r.get("note")) for r in rows], warnings)


def _marker_target(args, resolve, warnings):
    tl = select_timeline(args, require_project(resolve))
    if getattr(args, "item", None):
        item, r = find_item(tl, args.item, warnings)
        return item, "%s %s" % (r, item.GetName())
    return tl, "timeline %s" % tl.GetName()


def cmd_add_marker(args, resolve):
    """Timeline marker (frameId relative to the timeline start) or, with
    --item, a clip-relative marker on that timeline item."""
    guard(args, "add a marker")
    warnings = []
    target, label = _marker_target(args, resolve, warnings)
    frame = int(need(args, "frame"))
    cd = _custom_data(args)
    color, name, note, duration = args.color or "Blue", args.name or "", args.note or "", int(args.duration or 1)
    ok = target.AddMarker(frame, color, name, note, duration, cd) if cd is not None \
        else target.AddMarker(frame, color, name, note, duration)
    check(ok, "AddMarker(%d) on %s" % (frame, label))
    out(args, {"target": label, "frame": frame, "markers": markers_rows(target.GetMarkers()), "ok": True},
        ["added %s marker at %d on %s" % (color, frame, label)], warnings)


def cmd_delete_marker(args, resolve):
    guard(args, "delete marker(s)")
    warnings = []
    target, label = _marker_target(args, resolve, warnings)
    if getattr(args, "frame", None) is not None:
        check(target.DeleteMarkerAtFrame(int(args.frame)), "DeleteMarkerAtFrame(%s)" % args.frame)
        what = "frame %s" % args.frame
    elif getattr(args, "custom_data", None) is not None:
        check(target.DeleteMarkerByCustomData(_custom_data(args)), "DeleteMarkerByCustomData")
        what = "customData match"
    elif getattr(args, "color", None):
        check(target.DeleteMarkersByColor(args.color), "DeleteMarkersByColor(%s)" % args.color)
        what = "color %s" % args.color
    else:
        sys.exit("ERROR: give one of --frame, --custom-data, --color (or --color All)")
    out(args, {"target": label, "deleted": what, "markers": markers_rows(target.GetMarkers()), "ok": True},
        ["deleted marker(s) by %s on %s" % (what, label)], warnings)


def cmd_timecode(args, resolve):
    tl = select_timeline(args, require_project(resolve))
    data = {"timeline": tl.GetName(), "current": tl.GetCurrentTimecode(), "start": tl.GetStartTimecode(),
            "start_frame": tl.GetStartFrame(), "end_frame": tl.GetEndFrame()}
    out(args, data, ["%s: playhead %s, start %s (frame %s), end frame %s" % (
        data["timeline"], data["current"], data["start"], data["start_frame"], data["end_frame"])])


def cmd_set_timecode(args, resolve):
    guard(args, "move the playhead / start timecode")
    tl = select_timeline(args, require_project(resolve))
    changed = []
    if getattr(args, "playhead", None):
        check(tl.SetCurrentTimecode(args.playhead), "SetCurrentTimecode(%r)" % args.playhead)
        changed.append("playhead")
    if getattr(args, "start", None):
        check(tl.SetStartTimecode(args.start), "SetStartTimecode(%r)" % args.start)
        changed.append("start")
    if not changed:
        sys.exit("ERROR: give --playhead TC and/or --start TC")
    out(args, {"timeline": tl.GetName(), "current": tl.GetCurrentTimecode(), "start": tl.GetStartTimecode(),
               "changed": changed, "ok": True},
        ["%s: playhead %s, start %s" % (tl.GetName(), tl.GetCurrentTimecode(), tl.GetStartTimecode())])


def cmd_timeline_mark(args, resolve):
    guard(args, "change timeline mark in/out")
    tl = select_timeline(args, require_project(resolve))
    mark_type = getattr(args, "type", None) or "all"
    if getattr(args, "clear", False):
        check(tl.ClearMarkInOut(mark_type), "ClearMarkInOut(%s)" % mark_type)
    else:
        check(tl.SetMarkInOut(int(need(args, "mark_in")), int(need(args, "mark_out")), mark_type), "SetMarkInOut")
    out(args, {"timeline": tl.GetName(), "mark_in_out": tl.GetMarkInOut(), "ok": True},
        ["%s marks: %s" % (tl.GetName(), json.dumps(tl.GetMarkInOut(), default=str))])


# ---------------------------------------------------------------------------
# Timeline settings + output blanking (row 26, 21.1)
# ---------------------------------------------------------------------------

def cmd_timeline_settings(args, resolve):
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    key = getattr(args, "key", None)
    if key:
        value = tl.GetSetting(key)
        out(args, {"timeline": tl.GetName(), "key": key, "value": value}, ["%s = %s" % (key, value)], warnings)
        return
    settings = tl.GetSettings()
    if settings is None:
        warnings.append("Timeline.GetSettings returned None")
        settings = {}
    custom = settings.get("useCustomSettings")
    note = None if custom == "1" else ("useCustomSettings is %r: these are the PROJECT settings "
                                       "(README rule); set useCustomSettings=1 to override per timeline" % custom)
    out(args, {"timeline": tl.GetName(), "custom": custom == "1", "note": note, "settings": settings},
        ["%d setting(s) on %s%s" % (len(settings), tl.GetName(), (" -- " + note) if note else "")] +
        ["  %s = %s" % (k, v) for k, v in sorted(settings.items())], warnings)


def cmd_set_timeline_settings(args, resolve):
    """One key per call, useCustomSettings first. Frame rate is fixed at
    creation (measured 2026-09-01): a timelineFrameRate failure says so."""
    guard(args, "change timeline settings")
    tl = select_timeline(args, require_project(resolve))
    settings = jsonval(need(args, "settings"), "settings")
    if not isinstance(settings, dict) or not settings:
        sys.exit("ERROR: --settings must be a non-empty JSON object")
    from ops_project import _apply_settings
    applied, failed = _apply_settings(tl, settings, "Timeline")
    readback = {k: tl.GetSetting(k) for k in settings}
    notes = []
    if "timelineFrameRate" in failed:
        # Measured 2026-09-09 (21.1.0.14): 24 -> 25 applied on an EMPTY timeline with
        # useCustomSettings=1 (start frame moved 86400 -> 90000); refused once clips exist.
        notes.append("timelineFrameRate is locked once the timeline holds clips -- change it on an "
                     "EMPTY timeline (useCustomSettings=1 first), set it on the PROJECT before "
                     "create-timeline, or build from clips/import with the rate baked in")
    if failed and tl.GetSetting("useCustomSettings") != "1":
        notes.append("useCustomSettings is not '1': timeline overrides are refused until it is")
    data = {"timeline": tl.GetName(), "applied": applied, "failed": failed, "readback": readback,
            "notes": notes, "ok": not failed}
    out(args, data, ["applied %d, failed %d" % (len(applied), len(failed))] +
        ["  %s %s = %s" % ("ok " if k in applied else "FAIL", k, readback.get(k)) for k in settings] +
        ["  note: %s" % n for n in notes])
    if failed:
        sys.exit(1)


def cmd_set_blanking(args, resolve):
    guard(args, "set timeline output blanking")
    tl = select_timeline(args, require_project(resolve))
    current = tl.GetOutputBlanking() or {}
    blanking = {k: int(getattr(args, k.lower(), None) if getattr(args, k.lower(), None) is not None
                       else (current.get(k) or 0)) for k in ("Top", "Bottom", "Left", "Right")}
    check(tl.SetOutputBlanking(blanking), "SetOutputBlanking(%s)" % json.dumps(blanking))
    out(args, {"timeline": tl.GetName(), "output_blanking": tl.GetOutputBlanking(), "ok": True},
        ["%s blanking: %s" % (tl.GetName(), json.dumps(tl.GetOutputBlanking(), default=str))])


# ---------------------------------------------------------------------------
# Generators / titles / compound / fusion clips / stills (rows 27-29)
# ---------------------------------------------------------------------------

def cmd_insert_generator(args, resolve):
    """Inserts at the PLAYHEAD on the first free track with the default
    duration (5 s); use --at TC to place it. No clipInfo exists for these."""
    guard(args, "insert a generator/title")
    project = require_project(resolve)
    tl = require_timeline(project)
    kind = getattr(args, "kind", None) or "generator"
    name = getattr(args, "name", None)
    if kind != "fusion-composition" and not name:
        sys.exit("ERROR: --name is required for kind %s (e.g. 'Solid Color', 'Text+')" % kind)
    if getattr(args, "at", None):
        check(tl.SetCurrentTimecode(args.at), "SetCurrentTimecode(%r)" % args.at)
    if kind == "generator":
        item = tl.InsertGeneratorIntoTimeline(name)
    elif kind == "fusion-generator":
        item = tl.InsertFusionGeneratorIntoTimeline(name)
    elif kind == "ofx-generator":
        item = tl.InsertOFXGeneratorIntoTimeline(name)
    elif kind == "title":
        item = tl.InsertTitleIntoTimeline(name)
    elif kind == "fusion-title":
        item = tl.InsertFusionTitleIntoTimeline(name)
    else:
        item = tl.InsertFusionCompositionIntoTimeline()
    if item is None:
        sys.exit("ERROR: insert %s %r returned None (unknown name, no current timeline, or page-null; "
                 "try the Edit page)" % (kind, name))
    row = item_row(item)
    row["fusion_comps"] = item.GetFusionCompCount()
    out(args, {"timeline": tl.GetName(), "kind": kind, "name": name, "item": row, "ok": True},
        ["inserted %s %r on %s @%s for %s frames" % (kind, name, row["track"], row["start"], row["duration"])])


def cmd_compound_clip(args, resolve):
    guard(args, "create a compound clip")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    items = find_items(tl, need(args, "items"), warnings)
    opts = {}
    if getattr(args, "name", None):
        opts["name"] = args.name
    if getattr(args, "start_timecode", None):
        opts["startTimecode"] = args.start_timecode
    created = tl.CreateCompoundClip(items, opts) if opts else tl.CreateCompoundClip(items)
    if created is None:
        sys.exit("ERROR: CreateCompoundClip returned None")
    out(args, {"compound": item_row(created), "sources": [i.GetName() for i in items], "ok": True},
        ["compound clip: %s" % created.GetName()], warnings)


def cmd_fusion_clip(args, resolve):
    guard(args, "create a Fusion clip")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    items = find_items(tl, need(args, "items"), warnings)
    created = tl.CreateFusionClip(items)
    if created is None:
        sys.exit("ERROR: CreateFusionClip returned None")
    out(args, {"fusion_clip": item_row(created), "sources": [i.GetName() for i in items], "ok": True},
        ["fusion clip: %s" % created.GetName()], warnings)


def cmd_grab_still(args, resolve):
    """GrabStill (current frame) or --all first|middle (GrabAllStills). The
    Color page must be open for stills to land in the gallery."""
    guard(args, "grab gallery still(s)")
    tl = select_timeline(args, require_project(resolve))
    if getattr(args, "all", None):
        stills = tl.GrabAllStills({"first": 1, "middle": 2}[args.all])
        if not stills:
            sys.exit("ERROR: GrabAllStills returned %r (open the Color page first)" % stills)
        out(args, {"timeline": tl.GetName(), "count": len(stills), "ok": True},
            ["grabbed %d still(s)" % len(stills)])
        return
    still = tl.GrabStill()
    if not still:
        sys.exit("ERROR: GrabStill returned %r (open the Color page with a clip under the playhead)" % still)
    out(args, {"timeline": tl.GetName(), "count": 1, "ok": True}, ["grabbed 1 still"])


# ---------------------------------------------------------------------------
# Timeline AI + audio (rows 30, 31, 41)
# ---------------------------------------------------------------------------

def cmd_normalize_modes(args, resolve):
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    modes = api_list(tl.GetNormalizeAudioModes(), "GetNormalizeAudioModes", warnings)
    out(args, {"modes": modes}, ["%d normalization mode(s):" % len(modes)] + ["  %s" % m for m in modes], warnings)


def cmd_timeline_ai(args, resolve):
    """subtitles (CreateSubtitlesFromAudio), scene-cuts, stereo, dolby-vision,
    normalize (21.1 NormalizeAudioLevel), auto-align (21.1 AutoAlignClips)."""
    op = need(args, "op")
    guard(args, "run timeline pass %r" % op)
    warnings = []
    project = require_project(resolve)
    tl = select_timeline(args, project)
    data = {"op": op, "timeline": tl.GetName(), "ok": True}
    if op == "subtitles":
        langs = {"auto": resolve.AUTO_CAPTION_AUTO, "english": resolve.AUTO_CAPTION_ENGLISH,
                 "spanish": resolve.AUTO_CAPTION_SPANISH, "french": resolve.AUTO_CAPTION_FRENCH,
                 "german": resolve.AUTO_CAPTION_GERMAN, "italian": resolve.AUTO_CAPTION_ITALIAN,
                 "portuguese": resolve.AUTO_CAPTION_PORTUGUESE, "japanese": resolve.AUTO_CAPTION_JAPANESE,
                 "korean": resolve.AUTO_CAPTION_KOREAN, "mandarin": resolve.AUTO_CAPTION_MANDARIN_SIMPLIFIED}
        presets = {"default": resolve.AUTO_CAPTION_SUBTITLE_DEFAULT, "teletext": resolve.AUTO_CAPTION_TELETEXT,
                   "netflix": resolve.AUTO_CAPTION_NETFLIX}
        settings = {}
        if getattr(args, "language", None):
            settings["language"] = langs[args.language]
        if getattr(args, "preset", None):
            settings["captionPreset"] = presets[args.preset]
        if getattr(args, "chars_per_line", None) is not None:
            settings["charsPerLine"] = int(args.chars_per_line)
        if getattr(args, "line_break", None):
            settings["lineBreak"] = (resolve.AUTO_CAPTION_LINE_DOUBLE if args.line_break == "double"
                                     else resolve.AUTO_CAPTION_LINE_SINGLE)
        if getattr(args, "gap", None) is not None:
            settings["gap"] = int(args.gap)
        ok = tl.CreateSubtitlesFromAudio(settings) if settings else tl.CreateSubtitlesFromAudio()
        check(ok, "CreateSubtitlesFromAudio")
        subs = []
        for index in range(1, tl.GetTrackCount("subtitle") + 1):
            for item in api_list(tl.GetItemListInTrack("subtitle", index), "GetItemListInTrack(S)", warnings):
                subs.append({"track": role("subtitle", index), "start": item.GetStart(),
                             "duration": item.GetDuration(), "text": item.GetName()})
        data["subtitles"] = subs
        lines = ["created subtitles: %d caption(s) on %d subtitle track(s)" % (len(subs), tl.GetTrackCount("subtitle"))]
    elif op == "scene-cuts":
        check(tl.DetectSceneCuts(), "DetectSceneCuts")
        lines = ["scene-cut detection done"]
    elif op == "stereo":
        check(tl.ConvertTimelineToStereo(), "ConvertTimelineToStereo")
        lines = ["timeline converted to stereo"]
    elif op == "dolby-vision":
        items = find_items(tl, need(args, "items"), warnings)
        check(tl.AnalyzeDolbyVision(items, resolve.DLB_BLEND_SHOTS), "AnalyzeDolbyVision")
        lines = ["Dolby Vision analysis done on %d item(s)" % len(items)]
    elif op == "normalize":
        items = find_items(tl, need(args, "items"), warnings)
        opts = jsonval(getattr(args, "options", None), "options") or {}
        if getattr(args, "mode", None):
            opts["normalizationMode"] = args.mode
        if getattr(args, "target_level", None) is not None:
            opts["targetLevel"] = float(args.target_level)
        if getattr(args, "target_loudness", None) is not None:
            opts["targetLoudness"] = float(args.target_loudness)
        if getattr(args, "independent", False):
            opts["setLevelMode"] = resolve.NORMALIZE_AUDIO_SET_LEVEL_INDEPENDENT
        ok = tl.NormalizeAudioLevel(items, opts) if opts else tl.NormalizeAudioLevel(items)
        check(ok, "NormalizeAudioLevel(%s)" % json.dumps(opts))
        data["options"] = opts
        lines = ["normalized %d item(s): %s" % (len(items), json.dumps(opts))]
    elif op == "auto-align":
        items = find_items(tl, need(args, "items"), warnings)
        opts = {}
        if getattr(args, "mode", None) == "waveform":
            opts["SyncUsing"] = resolve.AUTO_ALIGN_CLIPS_USING_WAVEFORM
        elif getattr(args, "mode", None) == "timecode":
            opts["SyncUsing"] = resolve.AUTO_ALIGN_CLIPS_USING_TIMECODE
        ok = tl.AutoAlignClips(items, opts) if opts else tl.AutoAlignClips(items)
        check(ok, "AutoAlignClips")
        lines = ["auto-aligned %d item(s)" % len(items)]
    else:
        sys.exit("ERROR: unknown --op %r" % op)
    out(args, data, lines, warnings)


# ---------------------------------------------------------------------------
# Fusion comps on an item (row 33)
# ---------------------------------------------------------------------------

def cmd_fusion_comps(args, resolve):
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    names = item.GetFusionCompNameList() or []
    out(args, {"item": item.GetName(), "track": r, "count": item.GetFusionCompCount(), "comps": names},
        ["%s: %d Fusion comp(s): %s" % (item.GetName(), item.GetFusionCompCount(), ", ".join(names))], warnings)


def cmd_item_fusion(args, resolve):
    action = need(args, "action")
    guard(args, "%s a Fusion comp" % action)
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    name, path = getattr(args, "name", None), getattr(args, "path", None)
    if action == "add":
        comp = item.AddFusionComp()
        if comp is None:
            sys.exit("ERROR: AddFusionComp returned None")
    elif action == "import":
        check(item.ImportFusionComp(need(args, "path")), "ImportFusionComp(%r)" % path)
    elif action == "export":
        check(item.ExportFusionComp(need(args, "path"), int(getattr(args, "index", None) or 1)),
              "ExportFusionComp(%r)" % path)
    elif action == "delete":
        check(item.DeleteFusionCompByName(need(args, "name")), "DeleteFusionCompByName(%r)" % name)
    elif action == "load":
        check(item.LoadFusionCompByName(need(args, "name")), "LoadFusionCompByName(%r)" % name)
    else:  # rename
        check(item.RenameFusionCompByName(need(args, "name"), need(args, "new_name")), "RenameFusionCompByName")
    out(args, {"item": item.GetName(), "track": r, "action": action, "name": name, "path": path,
               "comps": item.GetFusionCompNameList() or [], "ok": True},
        ["%s comp %s: %s" % (item.GetName(), action, item.GetFusionCompNameList())], warnings)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_TL = arg("timeline", help="named timeline (never switches the GUI); default: current")
_ITEM = arg("item", help="timeline item: 'V1:3' (track:position), unique id, or name")
_ITEMS = arg("items", "list", help="timeline item refs")
_BIN = arg("bin", help="scope clip lookups to this bin path")

cmd("timeline-info", cmd_timeline_info, False, "timeline geometry, rate, resolution, track counts, marks, blanking", [_TL])
cmd("create-timeline-from-clips", cmd_create_timeline_from_clips, True,
    "CreateTimelineFromClips: whole clips (positional refs) or --items JSON [{clip,startFrame,endFrame,recordFrame}]",
    [arg("name", positional=True, required=True), arg("clips", "list", help="clip refs"),
     arg("batch", "json", help='[{"clip":..,"startFrame":..,"endFrame":..,"recordFrame":..}]'), _BIN])
cmd("duplicate-timeline", cmd_duplicate_timeline, True, "duplicate a timeline under a new name",
    [arg("name", positional=True, required=True, help="new name"), _TL])
cmd("delete-timeline", cmd_delete_timeline, True, "delete a timeline by name", [arg("name", positional=True, required=True)])
cmd("switch-timeline", cmd_switch_timeline, True, "make a timeline current (switches the GUI)",
    [arg("name", positional=True, required=True)])
cmd("rename-timeline", cmd_rename_timeline, True, "rename a timeline", [arg("name", positional=True, required=True), _TL])
cmd("append", cmd_append, True,
    "AppendToTimeline with pre-resolved geometry + readback (current timeline only)",
    [arg("clip", help="clip ref"), arg("start-frame", "int"), arg("end-frame", "int"), arg("record-frame", "int"),
     arg("track-index", "int", help="destination track index"), arg("media-type", choices=["video", "audio"]),
     arg("batch", "json", help='[{"clip":..,"startFrame":..,"endFrame":..,"recordFrame":..,"trackIndex":..,"mediaType":1|2}]'),
     _BIN])
cmd("export-timeline", cmd_export_timeline, True, "Timeline.Export to AAF/DRT/EDL/FCPXML/OTIO/CSV/ALE/...",
    [arg("path", required=True), arg("type", required=True, choices=EXPORT_TYPE_NAMES),
     arg("subtype", choices=EXPORT_SUBTYPE_NAMES, help="AAF: aaf_new|aaf_existing; EDL: cdl|sdl|missing_clips|none"), _TL])
cmd("import-timeline", cmd_import_timeline, True,
    "ImportTimelineFromFile (new timeline) or --into (AAF into the current timeline)",
    [arg("path", required=True), arg("name", help="timelineName"), arg("source-clips-path"),
     arg("no-import-source-clips", "bool"), arg("into", "bool"),
     arg("options", "json", help="extra ImportOptions / AAFImportOptions JSON")])
cmd("tracks", cmd_tracks, False, "tracks with name, enabled, locked, subtype, item count, voice isolation", [_TL])
cmd("add-track", cmd_add_track, True, "AddTrack video/audio/subtitle (audio: --subtype mono|stereo|5.1|...)",
    [arg("type", positional=True, required=True, choices=list(TRACK_TYPES)), arg("subtype"), _TL])
cmd("delete-track", cmd_delete_track, True, "DeleteTrack", [arg("track", positional=True, required=True, help="V2 / A1"), _TL])
cmd("set-track", cmd_set_track, True, "rename / enable / lock / voice-isolate a track",
    [arg("track", positional=True, required=True, help="V2 / A1"), arg("name"),
     arg("enable", choices=TRI), arg("lock", choices=TRI),
     arg("voice-isolation", choices=TRI), arg("voice-isolation-amount", "int"), _TL])
cmd("items", cmd_items, False, "every timeline item with ref (V1:3), type, geometry, source range, clip",
    [arg("type", choices=list(TRACK_TYPES)), _TL])
cmd("item-info", cmd_item_info, False, "one item: properties, speed, fades, blanking, markers, comps, version, caches", [_ITEM, _TL])
cmd("delete-items", cmd_delete_items, True, "DeleteClips on items (--ripple closes the gap)",
    [arg("items", "list", positional=True, required=True), arg("ripple", "bool"), _TL])
cmd("link-items", cmd_link_items, True, "SetClipsLinked (or --unlink)",
    [arg("items", "list", positional=True, required=True), arg("unlink", "bool"), _TL])
cmd("set-item", cmd_set_item, True,
    "per-item edits: name, enabled, color, flags, speed (21.1), fades (21.1), blanking, caches, audio mapping, voice isolation",
    [_ITEM, arg("name"), arg("enabled", choices=TRI), arg("color", choices=CLIP_COLORS + ["clear"]),
     arg("add-flag", choices=MARKER_COLORS), arg("remove-flag", choices=MARKER_COLORS + ["All"]),
     arg("speed", "json", help='percentage number, or {"Percentage":50,"RippleTimeline":true,...}'),
     arg("fade-in", "int", help="frames"), arg("fade-out", "int", help="frames"),
     arg("blanking", "json", help='{"Top":0,"Bottom":0,"Left":0,"Right":0}'),
     arg("use-timeline-blanking", choices=TRI), arg("color-cache", choices=TRI),
     arg("fusion-cache", help="CACHE_AUTO_ENABLED|CACHE_DISABLED|CACHE_ENABLED value"),
     arg("audio-mapping", "json", help="SetSourceAudioChannelMapping JSON (exactly 1 track)"),
     arg("voice-isolation", choices=TRI), arg("voice-isolation-amount", "int"), _TL])
cmd("set-item-properties", cmd_set_item_properties, True,
    "TimelineItemProperties (Pan/Tilt/Zoom/Crop/Opacity/CompositeMode/audio...) -- STATIC values, no keyframes",
    [_ITEM, arg("properties", "json", required=True), _TL])
cmd("add-transition", cmd_add_transition, True,
    "21.1 AddTransition on an item edge (consumes media handles: 'right' needs the outgoing clip's tail handle, "
    "'left'/'center' also the incoming clip's head handle; give position + alignment + duration)",
    [_ITEM, arg("type", help="e.g. 'Cross Dissolve' (default), 'Dip To Color Dissolve'"),
     arg("category", choices=["simple", "fusion", "ofx", "audio"]),
     arg("position", choices=["start", "end"]), arg("alignment", choices=["left", "center", "right"]),
     arg("duration", "int", help="frames"), _TL])
cmd("multicam", cmd_multicam, True, "21.1 multicam: create (from clips) / flatten / smart-switch (on an item)",
    [arg("action", positional=True, required=True, choices=["create", "flatten", "smart-switch"]),
     arg("clips", "list", help="pool clip refs (create)"), arg("name", help="multicam clip name (create)"),
     arg("sync", choices=["timecode", "audio", "in", "out", "marker"]),
     arg("source-bin", "bool", help="createBinForSourceClips (default OFF: a moved-source multicam cannot be appended until another is created)"),
     _ITEM, arg("grade", choices=["copy", "retain"], help="flatten grade source"),
     arg("analysis", choices=["none", "wide-angle", "audio"], help="smart-switch analysisMode (default audio)"),
     arg("options", "json", help="MulticamOptions / SmartSwitchSettings JSON"), _BIN, _TL])
cmd("timeline-markers", cmd_timeline_markers, False, "timeline markers (frames relative to start)", [_TL])
cmd("add-marker", cmd_add_marker, True, "add a timeline marker (or --item for a clip-relative item marker)",
    [arg("frame", "int", required=True), arg("color", choices=MARKER_COLORS, default="Blue"),
     arg("name"), arg("note"), arg("duration", "int", default=1), arg("custom-data", "json"), _ITEM, _TL])
cmd("delete-marker", cmd_delete_marker, True, "delete timeline/item markers by --frame, --custom-data or --color/All",
    [arg("frame", "int"), arg("custom-data", "json"), arg("color", choices=MARKER_COLORS + ["All"]), _ITEM, _TL])
cmd("timecode", cmd_timecode, False, "playhead + start timecode + frame range", [_TL])
cmd("set-timecode", cmd_set_timecode, True, "move the playhead (--playhead TC) / set start TC (--start TC)",
    [arg("playhead"), arg("start"), _TL])
cmd("timeline-mark", cmd_timeline_mark, True, "set (--mark-in/--mark-out) or --clear timeline mark in/out",
    [arg("mark-in", "int"), arg("mark-out", "int"), arg("type", choices=["video", "audio", "all"]),
     arg("clear", "bool"), _TL])
cmd("timeline-settings", cmd_timeline_settings, False, "timeline settings (project settings unless useCustomSettings=1)",
    [arg("key"), _TL])
cmd("set-timeline-settings", cmd_set_timeline_settings, True,
    "set timeline settings from JSON; useCustomSettings applied first; frame rate is fixed at creation",
    [arg("settings", "json", required=True), _TL])
cmd("set-blanking", cmd_set_blanking, True, "21.1 timeline output blanking (pixels)",
    [arg("top", "int"), arg("bottom", "int"), arg("left", "int"), arg("right", "int"), _TL])
cmd("insert-generator", cmd_insert_generator, True,
    "insert a generator / fusion-generator / ofx-generator / title / fusion-title / fusion-composition at the playhead",
    [arg("name", positional=True, help="'Solid Color', 'Text+', ..."),
     arg("kind", choices=["generator", "fusion-generator", "ofx-generator", "title", "fusion-title", "fusion-composition"]),
     arg("at", help="timecode to move the playhead to first")])
cmd("compound-clip", cmd_compound_clip, True, "CreateCompoundClip from items",
    [arg("items", "list", positional=True, required=True), arg("name"), arg("start-timecode"), _TL])
cmd("fusion-clip", cmd_fusion_clip, True, "CreateFusionClip from items",
    [arg("items", "list", positional=True, required=True), _TL])
cmd("grab-still", cmd_grab_still, True, "GrabStill at the playhead, or --all first|middle for every clip",
    [arg("all", choices=["first", "middle"]), _TL])
cmd("normalize-modes", cmd_normalize_modes, False, "21.1 GetNormalizeAudioModes", [_TL])
cmd("timeline-ai", cmd_timeline_ai, True,
    "subtitles / scene-cuts / stereo / dolby-vision / normalize (21.1) / auto-align (21.1)",
    [arg("op", positional=True, required=True,
         choices=["subtitles", "scene-cuts", "stereo", "dolby-vision", "normalize", "auto-align"]),
     arg("language", choices=["auto", "english", "spanish", "french", "german", "italian", "portuguese",
                              "japanese", "korean", "mandarin"]),
     arg("preset", choices=["default", "teletext", "netflix"]), arg("chars-per-line", "int"),
     arg("line-break", choices=["single", "double"]), arg("gap", "int"),
     _ITEMS, arg("mode", help="normalize: mode name from normalize-modes; auto-align: waveform|timecode"),
     arg("target-level", "float"), arg("target-loudness", "float"), arg("independent", "bool"),
     arg("options", "json"), _TL])
cmd("fusion-comps", cmd_fusion_comps, False, "Fusion comps on an item (count + names)", [_ITEM, _TL])
cmd("item-fusion", cmd_item_fusion, True, "Fusion comps on an item: add / import / export / delete / load / rename",
    [arg("action", positional=True, required=True, choices=["add", "import", "export", "delete", "load", "rename"]),
     _ITEM, arg("name"), arg("new-name"), arg("path"), arg("index", "int", help="comp index for export (1)"), _TL])
