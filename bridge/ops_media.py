"""ops_media -- media pool bins, clip lifecycle, clip properties/metadata,
markers, flags, marks, proxies, audio mapping, AI passes and transcript
readback (Resolve 21.1 API). Absorb-manifest rows 9-18.
"""

import json
import sys

from ops_common import (arg, cmd, out, api_list, require_project, guard, jsonval,
                        need, check, find_folder, find_clip, find_clips, walk_folders,
                        walk_clips, clip_summary, markers_rows, norm_path,
                        MARKER_COLORS, CLIP_COLORS)


def _pool(resolve):
    return require_project(resolve).GetMediaPool()


# ---------------------------------------------------------------------------
# Bins (row 10)
# ---------------------------------------------------------------------------

def cmd_bins(args, resolve):
    warnings = []
    pool = _pool(resolve)
    start = find_folder(pool, getattr(args, "bin", None), warnings)
    current = pool.GetCurrentFolder()
    current_id = current.GetUniqueId() if current else None
    rows = []
    for folder, path in walk_folders(start, start.GetName(), warnings):
        rows.append({"path": path, "name": folder.GetName(), "id": folder.GetUniqueId(),
                     "clips": len(api_list(folder.GetClipList(), "GetClipList(%s)" % path, warnings)),
                     "stale": folder.GetIsFolderStale(),
                     "current": folder.GetUniqueId() == current_id})
    out(args, {"bins": rows, "current": current.GetName() if current else None},
        ["%d bin(s):" % len(rows)] +
        ["  %s %s (%d clips)" % ("*" if r["current"] else " ", r["path"], r["clips"]) for r in rows],
        warnings)


def cmd_bin(args, resolve):
    """create (idempotent: an existing bin of that name is returned, not
    duplicated) / delete / set-current / move / refresh."""
    action = need(args, "action")
    path = getattr(args, "path", None)
    guard(args, "%s bin %s" % (action, path or ""))
    pool = _pool(resolve)
    warnings = []
    data = {"action": action, "path": path, "ok": True}
    if action == "create":
        path = need(args, "path")
        parts = [p for p in str(path).replace("\\", "/").split("/") if p]
        root = pool.GetRootFolder()
        if parts and parts[0] == root.GetName():
            parts = parts[1:]
        if not parts:
            sys.exit("ERROR: --path must name a bin below the root")
        parent = find_folder(pool, "/".join(parts[:-1]), warnings)
        existing = [s for s in api_list(parent.GetSubFolderList(), "GetSubFolderList", warnings)
                    if s.GetName() == parts[-1]]
        if existing:
            data["created"] = False
            data["id"] = existing[0].GetUniqueId()
            lines = ["bin already exists: %s" % path]
        else:
            folder = pool.AddSubFolder(parent, parts[-1])
            if folder is None:
                sys.exit("ERROR: AddSubFolder(%r, %r) returned None" % (parent.GetName(), parts[-1]))
            data["created"] = True
            data["id"] = folder.GetUniqueId()
            lines = ["created bin: %s" % path]
    elif action == "delete":
        folder = find_folder(pool, need(args, "path"), warnings)
        if folder.GetUniqueId() == pool.GetRootFolder().GetUniqueId():
            sys.exit("ERROR: refusing to delete the root bin")
        check(pool.DeleteFolders([folder]), "DeleteFolders(%r)" % path)
        lines = ["deleted bin: %s" % path]
    elif action == "set-current":
        folder = find_folder(pool, path, warnings)
        check(pool.SetCurrentFolder(folder), "SetCurrentFolder(%r)" % path)
        lines = ["current bin: %s" % folder.GetName()]
    elif action == "move":
        folder = find_folder(pool, need(args, "path"), warnings)
        target = find_folder(pool, need(args, "to"), warnings)
        check(pool.MoveFolders([folder], target), "MoveFolders(%r -> %r)" % (path, args.to))
        lines = ["moved bin %s -> %s" % (path, args.to)]
    else:  # refresh
        check(pool.RefreshFolders(), "RefreshFolders()")
        lines = ["folders refreshed"]
    out(args, data, lines, warnings)


# ---------------------------------------------------------------------------
# Clip lifecycle (rows 9, 11)
# ---------------------------------------------------------------------------

def cmd_import_media(args, resolve):
    """ImportMedia into a bin (default: current), skipping paths already in
    the pool by File Path unless --force. Image sequences: --sequence with
    --start-index/--end-index on a %0Nd pattern."""
    guard(args, "import media")
    pool = _pool(resolve)
    paths = need(args, "paths")
    warnings = []
    if getattr(args, "bin", None):
        folder = find_folder(pool, args.bin, warnings)
        check(pool.SetCurrentFolder(folder), "SetCurrentFolder(%r)" % args.bin)
    existing = {}
    if not getattr(args, "force", False):
        root = pool.GetRootFolder()
        for clip, _ in walk_clips(root, root.GetName(), warnings):
            fp = clip.GetClipProperty("File Path")
            if fp:
                existing[norm_path(fp)] = clip.GetName()
    wanted, skipped = [], []
    for p in paths:
        if norm_path(p) in existing:
            skipped.append({"path": p, "clip": existing[norm_path(p)]})
        else:
            wanted.append(p)
    imported = []
    if wanted:
        if getattr(args, "sequence", False):
            infos = [{"FilePath": p, "StartIndex": int(need(args, "start_index")),
                      "EndIndex": int(need(args, "end_index"))} for p in wanted]
            added = pool.ImportMedia(infos)
        else:
            added = pool.ImportMedia(list(wanted))
        if added is None:
            sys.exit("ERROR: ImportMedia returned None -- the call failed (or nothing was "
                     "importable). Requested: %s" % ", ".join(wanted))
        imported = [clip_summary(c) for c in added]
        if len(imported) < len(wanted):
            warnings.append("requested %d new path(s) but %d clip(s) came back"
                            % (len(wanted), len(imported)))
    out(args, {"imported": imported, "skipped": skipped, "requested": list(paths), "ok": True},
        ["imported %d clip(s), skipped %d already in the pool" % (len(imported), len(skipped))] +
        ["  + %s" % c["name"] for c in imported] + ["  = %s (%s)" % (s["clip"], s["path"]) for s in skipped],
        warnings)


def cmd_delete_clips(args, resolve):
    guard(args, "delete media pool clips")
    pool = _pool(resolve)
    warnings = []
    if getattr(args, "all_matches", False):
        # Timeline imports with importSourceClips=True duplicate pool entries for
        # media that is already there (measured 2026-09-09); this clears every
        # clip carrying one of the given names/paths instead of refusing as ambiguous.
        start = find_folder(pool, getattr(args, "bin", None), warnings)
        wanted = set(need(args, "clips"))
        wanted_paths = {norm_path(w) for w in wanted}
        clips = [c for c, _ in walk_clips(start, start.GetName(), warnings)
                 if c.GetName() in wanted or c.GetUniqueId() in wanted
                 or norm_path(c.GetClipProperty("File Path") or "") in wanted_paths]
        if not clips:
            sys.exit("ERROR: no clips match %s" % ", ".join(sorted(wanted)))
    else:
        clips = find_clips(pool, need(args, "clips"), warnings, getattr(args, "bin", None))
    names = [c.GetName() for c in clips]
    check(pool.DeleteClips(clips), "DeleteClips(%s)" % ", ".join(names))
    out(args, {"deleted": names, "ok": True}, ["deleted %d clip(s): %s" % (len(names), ", ".join(names))], warnings)


def cmd_move_clips(args, resolve):
    guard(args, "move media pool clips")
    pool = _pool(resolve)
    warnings = []
    clips = find_clips(pool, need(args, "clips"), warnings, getattr(args, "bin", None))
    target = find_folder(pool, need(args, "to"), warnings)
    check(pool.MoveClips(clips, target), "MoveClips(-> %r)" % args.to)
    out(args, {"moved": [c.GetName() for c in clips], "to": args.to, "ok": True},
        ["moved %d clip(s) -> %s" % (len(clips), args.to)], warnings)


def cmd_relink_clips(args, resolve):
    guard(args, "relink media pool clips")
    pool = _pool(resolve)
    warnings = []
    clips = find_clips(pool, need(args, "clips"), warnings, getattr(args, "bin", None))
    if getattr(args, "unlink", False):
        check(pool.UnlinkClips(clips), "UnlinkClips")
        lines = ["unlinked %d clip(s)" % len(clips)]
    else:
        folder = need(args, "folder", "filesystem folder to relink from")
        check(pool.RelinkClips(clips, folder), "RelinkClips(%r)" % folder)
        lines = ["relinked %d clip(s) from %s" % (len(clips), folder)]
    out(args, {"clips": [c.GetName() for c in clips], "unlink": bool(getattr(args, "unlink", False)),
               "folder": getattr(args, "folder", None), "ok": True}, lines, warnings)


def cmd_select_clip(args, resolve):
    guard(args, "change the media pool selection")
    pool = _pool(resolve)
    warnings = []
    clip = find_clip(pool, need(args, "clip"), warnings, getattr(args, "bin", None))
    check(pool.SetSelectedClip(clip), "SetSelectedClip(%r)" % clip.GetName())
    out(args, {"selected": clip.GetName(), "ok": True}, ["selected: %s" % clip.GetName()], warnings)


def cmd_stereo_clip(args, resolve):
    guard(args, "create a stereo clip")
    pool = _pool(resolve)
    warnings = []
    left = find_clip(pool, need(args, "left_clip"), warnings, getattr(args, "bin", None))
    right = find_clip(pool, need(args, "right_clip"), warnings, getattr(args, "bin", None))
    result = pool.CreateStereoClip(left, right)
    if not result:
        sys.exit("ERROR: CreateStereoClip returned %r" % result)
    name = result.GetName() if hasattr(result, "GetName") else str(result)
    out(args, {"stereo": name, "left": left.GetName(), "right": right.GetName(), "ok": True},
        ["stereo clip: %s" % name], warnings)


def cmd_sync_audio(args, resolve):
    """MediaPool.AutoSyncAudio: >=1 video + >=1 audio clip, waveform or timecode."""
    guard(args, "auto-sync audio")
    pool = _pool(resolve)
    warnings = []
    clips = find_clips(pool, need(args, "clips"), warnings, getattr(args, "bin", None))
    mode = getattr(args, "mode", None) or "waveform"
    settings = {resolve.AUDIO_SYNC_MODE: resolve.AUDIO_SYNC_WAVEFORM if mode == "waveform"
                else resolve.AUDIO_SYNC_TIMECODE}
    channel = getattr(args, "channel", None)
    if channel is not None:
        settings[resolve.AUDIO_SYNC_CHANNEL_NUMBER] = (
            resolve.AUDIO_SYNC_CHANNEL_AUTOMATIC if str(channel) == "auto" else
            resolve.AUDIO_SYNC_CHANNEL_MIX if str(channel) == "mix" else int(channel))
    if getattr(args, "retain_embedded_audio", False):
        settings[resolve.AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO] = True
    if getattr(args, "retain_video_metadata", False):
        settings[resolve.AUDIO_SYNC_RETAIN_VIDEO_METADATA] = True
    ok = pool.AutoSyncAudio(clips, settings)
    if not ok:
        sys.exit("ERROR: AutoSyncAudio returned %r (content-dependent: needs >=1 video + >=1 audio "
                 "clip with matching waveform/timecode; synthetic media fails)" % ok)
    out(args, {"synced": [c.GetName() for c in clips], "mode": mode, "ok": True},
        ["auto-synced %d clip(s) by %s" % (len(clips), mode)], warnings)


# ---------------------------------------------------------------------------
# Clip info / properties / metadata (rows 12, 13)
# ---------------------------------------------------------------------------

def cmd_clip_info(args, resolve):
    warnings = []
    pool = _pool(resolve)
    clip = find_clip(pool, need(args, "clip"), warnings, getattr(args, "bin", None))
    props = clip.GetClipProperty()
    if props is None:
        warnings.append("GetClipProperty() returned None")
        props = {}
    data = {
        "name": clip.GetName(), "id": clip.GetUniqueId(), "media_id": clip.GetMediaId(),
        "properties": props,
        "metadata": clip.GetMetadata() or {},
        "third_party_metadata": clip.GetThirdPartyMetadata() or {},
        "flags": clip.GetFlagList() or [],
        "clip_color": clip.GetClipColor(),
        "mark_in_out": clip.GetMarkInOut(),
        "markers": markers_rows(clip.GetMarkers()),
        "audio_mapping": None,
        "is_timeline": clip.GetTimeline() is not None,
    }
    mapping = clip.GetAudioMapping()
    if isinstance(mapping, str) and mapping:
        try:
            data["audio_mapping"] = json.loads(mapping)
        except ValueError:
            data["audio_mapping"] = mapping
    lines = ["%s (%s)" % (data["name"], data["id"])] + \
            ["  %s = %s" % (k, v) for k, v in sorted(props.items())] + \
            ["  metadata: %s" % json.dumps(data["metadata"], default=str),
             "  flags: %s  color: %s  markers: %d" % (data["flags"], data["clip_color"], len(data["markers"]))]
    out(args, data, lines, warnings)


def cmd_set_clip_property(args, resolve):
    guard(args, "set a clip property")
    pool = _pool(resolve)
    warnings = []
    clip = find_clip(pool, need(args, "clip"), warnings, getattr(args, "bin", None))
    key, value = need(args, "key"), need(args, "value")
    if key == "Super Scale" and getattr(args, "sharpness", None) is not None:
        ok = clip.SetClipProperty(key, int(value), float(args.sharpness),
                                  float(getattr(args, "noise_reduction", 0.0) or 0.0))
    else:
        ok = clip.SetClipProperty(key, value)
    check(ok, "SetClipProperty(%r, %r)" % (key, value))
    out(args, {"clip": clip.GetName(), "key": key, "value": value,
               "readback": clip.GetClipProperty(key), "ok": True},
        ["%s: %s = %s (readback %s)" % (clip.GetName(), key, value, clip.GetClipProperty(key))], warnings)


def cmd_set_clip_metadata(args, resolve):
    guard(args, "set clip metadata")
    pool = _pool(resolve)
    warnings = []
    clip = find_clip(pool, need(args, "clip"), warnings, getattr(args, "bin", None))
    metadata = jsonval(need(args, "metadata"), "metadata")
    if not isinstance(metadata, dict) or not metadata:
        sys.exit("ERROR: --metadata must be a non-empty JSON object")
    if getattr(args, "third_party", False):
        check(clip.SetThirdPartyMetadata(metadata), "SetThirdPartyMetadata")
        readback = clip.GetThirdPartyMetadata() or {}
    else:
        check(clip.SetMetadata(metadata), "SetMetadata")
        readback = clip.GetMetadata() or {}
    out(args, {"clip": clip.GetName(), "set": metadata,
               "readback": {k: readback.get(k) for k in metadata}, "ok": True},
        ["%s: set %d metadata key(s)" % (clip.GetName(), len(metadata))], warnings)


def cmd_rename_clip(args, resolve):
    guard(args, "rename a clip")
    pool = _pool(resolve)
    warnings = []
    clip = find_clip(pool, need(args, "clip"), warnings, getattr(args, "bin", None))
    old = clip.GetName()
    check(clip.SetName(need(args, "name")), "MediaPoolItem.SetName(%r)" % args.name)
    out(args, {"renamed": old, "to": clip.GetName(), "ok": True}, ["%s -> %s" % (old, clip.GetName())], warnings)


# ---------------------------------------------------------------------------
# Markers / flags / color / marks (rows 14, 15, 16)
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


def cmd_clip_markers(args, resolve):
    warnings = []
    clip = find_clip(_pool(resolve), need(args, "clip"), warnings, getattr(args, "bin", None))
    rows = markers_rows(clip.GetMarkers())
    out(args, {"clip": clip.GetName(), "markers": rows},
        ["%d marker(s) on %s:" % (len(rows), clip.GetName())] +
        ["  %6d %-8s %s %s" % (r["frame"], r.get("color"), r.get("name"), r.get("note")) for r in rows],
        warnings)


def cmd_add_clip_marker(args, resolve):
    guard(args, "add a clip marker")
    warnings = []
    clip = find_clip(_pool(resolve), need(args, "clip"), warnings, getattr(args, "bin", None))
    frame = int(need(args, "frame"))
    cd = _custom_data(args)
    ok = clip.AddMarker(frame, args.color or "Blue", args.name or "", args.note or "",
                        int(args.duration or 1), cd) if cd is not None else \
        clip.AddMarker(frame, args.color or "Blue", args.name or "", args.note or "", int(args.duration or 1))
    check(ok, "AddMarker(%d)" % frame)
    out(args, {"clip": clip.GetName(), "frame": frame, "ok": True,
               "markers": markers_rows(clip.GetMarkers())},
        ["added marker at %d on %s" % (frame, clip.GetName())], warnings)


def cmd_delete_clip_marker(args, resolve):
    guard(args, "delete clip marker(s)")
    warnings = []
    clip = find_clip(_pool(resolve), need(args, "clip"), warnings, getattr(args, "bin", None))
    if getattr(args, "frame", None) is not None:
        check(clip.DeleteMarkerAtFrame(int(args.frame)), "DeleteMarkerAtFrame(%s)" % args.frame)
        what = "frame %s" % args.frame
    elif getattr(args, "custom_data", None) is not None:
        check(clip.DeleteMarkerByCustomData(_custom_data(args)), "DeleteMarkerByCustomData")
        what = "customData match"
    elif getattr(args, "color", None):
        check(clip.DeleteMarkersByColor(args.color), "DeleteMarkersByColor(%s)" % args.color)
        what = "color %s" % args.color
    else:
        sys.exit("ERROR: give one of --frame, --custom-data, --color (or --color All)")
    out(args, {"clip": clip.GetName(), "deleted": what, "markers": markers_rows(clip.GetMarkers()), "ok": True},
        ["deleted marker(s) by %s on %s" % (what, clip.GetName())], warnings)


def cmd_clip_flag(args, resolve):
    guard(args, "change clip flags")
    warnings = []
    clip = find_clip(_pool(resolve), need(args, "clip"), warnings, getattr(args, "bin", None))
    if getattr(args, "remove", None):
        check(clip.ClearFlags(args.remove), "ClearFlags(%s)" % args.remove)
    else:
        check(clip.AddFlag(need(args, "add")), "AddFlag(%s)" % args.add)
    out(args, {"clip": clip.GetName(), "flags": clip.GetFlagList() or [], "ok": True},
        ["%s flags: %s" % (clip.GetName(), clip.GetFlagList())], warnings)


def cmd_clip_color(args, resolve):
    guard(args, "change the clip color")
    warnings = []
    clip = find_clip(_pool(resolve), need(args, "clip"), warnings, getattr(args, "bin", None))
    color = need(args, "color")
    if color.lower() == "clear":
        check(clip.ClearClipColor(), "ClearClipColor()")
    else:
        check(clip.SetClipColor(color), "SetClipColor(%s)" % color)
    out(args, {"clip": clip.GetName(), "color": clip.GetClipColor(), "ok": True},
        ["%s color: %s" % (clip.GetName(), clip.GetClipColor())], warnings)


def cmd_clip_mark(args, resolve):
    guard(args, "change clip mark in/out")
    warnings = []
    clip = find_clip(_pool(resolve), need(args, "clip"), warnings, getattr(args, "bin", None))
    mark_type = getattr(args, "type", None) or "all"
    if getattr(args, "clear", False):
        check(clip.ClearMarkInOut(mark_type), "ClearMarkInOut(%s)" % mark_type)
    else:
        check(clip.SetMarkInOut(int(need(args, "mark_in")), int(need(args, "mark_out")), mark_type),
              "SetMarkInOut")
    out(args, {"clip": clip.GetName(), "mark_in_out": clip.GetMarkInOut(), "ok": True},
        ["%s marks: %s" % (clip.GetName(), json.dumps(clip.GetMarkInOut(), default=str))], warnings)


# ---------------------------------------------------------------------------
# Proxy / replace / growing file / audio mapping (row 17 + 21.1)
# ---------------------------------------------------------------------------

def cmd_proxy(args, resolve):
    guard(args, "change proxy/full-res linking")
    warnings = []
    clip = find_clip(_pool(resolve), need(args, "clip"), warnings, getattr(args, "bin", None))
    if getattr(args, "unlink", False):
        check(clip.UnlinkProxyMedia(), "UnlinkProxyMedia()")
        what = "unlinked proxy"
    elif getattr(args, "full_res", None):
        check(clip.LinkFullResolutionMedia(args.full_res), "LinkFullResolutionMedia(%r)" % args.full_res)
        what = "linked full-res %s" % args.full_res
    else:
        path = need(args, "link", "proxy media path")
        check(clip.LinkProxyMedia(path), "LinkProxyMedia(%r)" % path)
        what = "linked proxy %s" % path
    out(args, {"clip": clip.GetName(), "proxy": clip.GetClipProperty("Proxy"),
               "proxy_path": clip.GetClipProperty("Proxy Media Path"), "ok": True},
        ["%s: %s" % (clip.GetName(), what)], warnings)


def cmd_replace_clip(args, resolve):
    guard(args, "replace clip media")
    warnings = []
    clip = find_clip(_pool(resolve), need(args, "clip"), warnings, getattr(args, "bin", None))
    path = need(args, "path")
    if getattr(args, "preserve_subclip", False):
        check(clip.ReplaceClipPreserveSubClip(path), "ReplaceClipPreserveSubClip(%r)" % path)
    else:
        check(clip.ReplaceClip(path), "ReplaceClip(%r)" % path)
    out(args, {"clip": clip.GetName(), "path": clip.GetClipProperty("File Path"), "ok": True},
        ["%s now points at %s" % (clip.GetName(), clip.GetClipProperty("File Path"))], warnings)


def cmd_monitor_growing(args, resolve):
    guard(args, "enable growing-file monitoring")
    warnings = []
    clip = find_clip(_pool(resolve), need(args, "clip"), warnings, getattr(args, "bin", None))
    check(clip.MonitorGrowingFile(), "MonitorGrowingFile()")
    out(args, {"clip": clip.GetName(), "ok": True}, ["monitoring growing file: %s" % clip.GetName()], warnings)


def cmd_set_audio_mapping(args, resolve):
    """21.1 MediaPoolItem.SetAudioMapping: only track_mapping is honoured."""
    guard(args, "set the clip audio mapping")
    warnings = []
    clip = find_clip(_pool(resolve), need(args, "clip"), warnings, getattr(args, "bin", None))
    mapping = jsonval(need(args, "mapping"), "mapping")
    if not isinstance(mapping, dict) or "track_mapping" not in mapping:
        sys.exit("ERROR: --mapping must be a JSON object with a 'track_mapping' key")
    check(clip.SetAudioMapping(json.dumps(mapping)), "SetAudioMapping")
    readback = clip.GetAudioMapping()
    try:
        readback = json.loads(readback) if isinstance(readback, str) else readback
    except ValueError:
        pass
    out(args, {"clip": clip.GetName(), "audio_mapping": readback, "ok": True},
        ["%s audio mapping updated" % clip.GetName()], warnings)


# ---------------------------------------------------------------------------
# AI passes + transcript readback (row 18, 21.1 GetTranscription)
# ---------------------------------------------------------------------------

def cmd_transcription(args, resolve):
    """21.1: MediaPoolItem.GetTranscription -- the transcript readback that
    did not exist before 21.1. Segments carry start/end timecode, text,
    speaker and per-word timing."""
    warnings = []
    clip = find_clip(_pool(resolve), need(args, "clip"), warnings, getattr(args, "bin", None))
    nested = bool(getattr(args, "nested", False))
    data = clip.GetTranscription(nested) if nested else clip.GetTranscription()
    if not data:
        sys.exit("ERROR: GetTranscription returned %r for %s -- not transcribed yet "
                 "(run: clip-ai --op transcribe) or the clip has no audio." % (data, clip.GetName()))
    segments = data.get("segments") or [] if isinstance(data, dict) else []
    words = sum(len(s.get("words") or []) for s in segments)
    text = " ".join((s.get("text") or "") for s in segments)
    out(args, {"clip": clip.GetName(), "language": data.get("language") if isinstance(data, dict) else None,
               "segments": segments, "segment_count": len(segments), "word_count": words, "text": text},
        ["%s [%s]: %d segment(s), %d word(s)" % (clip.GetName(), data.get("language"), len(segments), words)] +
        ["  %s-%s %s%s" % (s.get("start"), s.get("end"), ("%s: " % s["speaker"]) if s.get("speaker") else "",
                           s.get("text")) for s in segments],
        warnings)


AI_SLOW_OPS = ("transcribe", "deblur", "intellisearch", "slate", "classify-audio")
AI_MIN_TIMEOUT_S = 120
# Measured 2026-09-09 on 21.1.0.14: TranscribeAudio -> True with a transcript on
# media/cut/edit/fairlight (0.7-0.9 s warm, 20.7 s cold), False in ~0 ms on
# fusion/color/deliver.
AI_DEAD_PAGES = ("fusion", "color", "deliver")


def _ai_target(args, resolve, warnings):
    pool = _pool(resolve)
    if getattr(args, "folder", None) is not None and not getattr(args, "clip", None):
        f = find_folder(pool, args.folder, warnings)
        return f, "bin %s" % f.GetName()
    clip = find_clip(pool, need(args, "clip"), warnings, getattr(args, "bin", None))
    return clip, "clip %s" % clip.GetName()


def cmd_clip_ai(args, resolve):
    """Studio AI passes on a clip or a whole bin. Some need Extras packs
    (IntelliSearch Faster/Better, Slate ID) -- a False here is reported as
    such rather than hidden."""
    op = need(args, "op")
    guard(args, "run AI pass %r" % op)
    warnings = []
    if op in AI_SLOW_OPS and getattr(args, "timeout", 25) < AI_MIN_TIMEOUT_S:
        sys.exit("ERROR: clip-ai %s needs --timeout >= %d: TranscribeAudio is SYNCHRONOUS (measured "
                 "20.7 s for a 7 s clip on 21.1 -- the model loads inside the call) and a watchdog trip "
                 "stops the sidecar." % (op, AI_MIN_TIMEOUT_S))
    page = resolve.GetCurrentPage()
    if page in AI_DEAD_PAGES:
        # Measured 2026-09-09 (21.1.0.14): TranscribeAudio returns False in ~10 ms
        # on the Deliver page and True (with a transcript) on the Edit page.
        warnings.append("page was %r where AI passes return False; switched to edit" % page)
        resolve.OpenPage("edit")
    target, label = _ai_target(args, resolve, warnings)
    speaker = bool(getattr(args, "speaker_detection", False))
    if op == "transcribe":
        ok = target.TranscribeAudio(speaker) if speaker else target.TranscribeAudio()
        note = ("TranscribeAudio blocks until the transcript exists; read it back with "
                "`transcription --clip` (21.1 GetTranscription: segments, speakers, word timing)")
    elif op == "clear-transcription":
        ok = target.ClearTranscription()
        note = None
    elif op == "classify-audio":
        ok = target.PerformAudioClassification()
        note = None
    elif op == "clear-classification":
        ok = target.ClearAudioClassification()
        note = None
    elif op == "deblur":
        options = jsonval(getattr(args, "options", None), "options") or {}
        ok = target.RemoveMotionBlur(options) if options else target.RemoveMotionBlur()
        note = "RemoveMotionBlur returns the new clip pair(s); options: FileName, Format, Codec, UseExtremeMode..."
    elif op == "intellisearch":
        ok = target.AnalyzeForIntellisearch(bool(getattr(args, "faces", False)), bool(getattr(args, "better", False)))
        note = "needs the AI IntelliSearch Faster/Better Extras pack"
    elif op == "slate":
        color = getattr(args, "marker_color", None) or "Blue"
        constants = {
            "None": resolve.MARKER_NONE, "Blue": resolve.MARKER_BLUE, "Cyan": resolve.MARKER_CYAN,
            "Green": resolve.MARKER_GREEN, "Yellow": resolve.MARKER_YELLOW, "Red": resolve.MARKER_RED,
            "Pink": resolve.MARKER_PINK, "Purple": resolve.MARKER_PURPLE, "Fuchsia": resolve.MARKER_FUCHSIA,
            "Rose": resolve.MARKER_ROSE, "Lavender": resolve.MARKER_LAVENDER, "Sky": resolve.MARKER_SKY,
            "Mint": resolve.MARKER_MINT, "Lemon": resolve.MARKER_LEMON, "Sand": resolve.MARKER_SAND,
            "Cocoa": resolve.MARKER_COCOA, "Cream": resolve.MARKER_CREAM}
        ok = target.AnalyzeForSlate(constants[color])
        note = "needs the AI Slate ID Extras pack"
    else:
        sys.exit("ERROR: unknown --op %r" % op)
    if not ok:
        sys.exit("ERROR: %s on %s returned %r -- Studio-only, Extras pack missing, or the media "
                 "has nothing to analyse. %s" % (op, label, ok, note or ""))
    result = ok if not isinstance(ok, bool) else None
    out(args, {"op": op, "target": label, "result": result, "ok": True, "note": note},
        ["%s on %s: ok%s" % (op, label, (" -- " + note) if note else "")], warnings)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_CLIP = arg("clip", help="clip name, unique id, or file path")
_BIN = arg("bin", help="scope the clip lookup to this bin path (A/B)")

cmd("bins", cmd_bins, False, "media pool bin tree with clip counts", [_BIN])
cmd("bin", cmd_bin, True, "create (idempotent) / delete / set-current / move / refresh a bin",
    [arg("action", positional=True, choices=["create", "delete", "set-current", "move", "refresh"], required=True),
     arg("path", help="bin path A/B"), arg("to", help="target bin path (move)")])
cmd("import-media", cmd_import_media, True,
    "import media into a bin, skipping paths already in the pool (idempotent)",
    [arg("paths", "list", positional=True, required=True, help="media file paths"),
     _BIN, arg("force", "bool", help="import even if the File Path is already in the pool"),
     arg("sequence", "bool", help="paths are %0Nd image-sequence patterns"),
     arg("start-index", "int"), arg("end-index", "int")])
cmd("delete-clips", cmd_delete_clips, True, "delete media pool clips (--all-matches: every duplicate of a name/path)",
    [arg("clips", "list", positional=True, required=True, help="clip refs"), arg("all-matches", "bool"), _BIN])
cmd("move-clips", cmd_move_clips, True, "move media pool clips to a bin",
    [arg("clips", "list", positional=True, required=True), arg("to", required=True, help="target bin"), _BIN])
cmd("relink-clips", cmd_relink_clips, True, "relink clips from a filesystem folder, or --unlink",
    [arg("clips", "list", positional=True, required=True), arg("folder", help="folder to relink from"),
     arg("unlink", "bool"), _BIN])
cmd("select-clip", cmd_select_clip, True, "select a media pool clip (GUI selection)", [_CLIP, _BIN])
cmd("stereo-clip", cmd_stereo_clip, True, "create a stereo clip from left + right clips",
    [arg("left-clip", required=True), arg("right-clip", required=True), _BIN])
cmd("sync-audio", cmd_sync_audio, True, "AutoSyncAudio on video + audio clips (waveform or timecode)",
    [arg("clips", "list", positional=True, required=True), arg("mode", choices=["waveform", "timecode"]),
     arg("channel", help="1..n, auto, or mix (waveform mode)"),
     arg("retain-embedded-audio", "bool"), arg("retain-video-metadata", "bool"), _BIN])
cmd("clip-info", cmd_clip_info, False, "every clip property + metadata, flags, color, marks, markers, audio mapping",
    [_CLIP, _BIN])
cmd("set-clip-property", cmd_set_clip_property, True, "SetClipProperty (FPS, Alpha mode, Super Scale, ...) with readback",
    [_CLIP, arg("key", positional=True, required=True), arg("value", positional=True, required=True),
     arg("sharpness", "float", help="Super Scale 2x Enhanced sharpness 0..1"),
     arg("noise-reduction", "float", help="Super Scale 2x Enhanced NR 0..1"), _BIN])
cmd("set-clip-metadata", cmd_set_clip_metadata, True, "SetMetadata / --third-party SetThirdPartyMetadata from JSON",
    [_CLIP, arg("metadata", "json", required=True), arg("third-party", "bool"), _BIN])
cmd("rename-clip", cmd_rename_clip, True, "rename a media pool clip", [_CLIP, arg("name", positional=True, required=True), _BIN])
cmd("clip-markers", cmd_clip_markers, False, "markers on a media pool clip (customData decoded)", [_CLIP, _BIN])
cmd("add-clip-marker", cmd_add_clip_marker, True, "add a marker to a media pool clip",
    [_CLIP, arg("frame", "int", required=True), arg("color", choices=MARKER_COLORS, default="Blue"),
     arg("name"), arg("note"), arg("duration", "int", default=1),
     arg("custom-data", "json", help="stored in the marker's customData (JSON encoded)"), _BIN])
cmd("delete-clip-marker", cmd_delete_clip_marker, True, "delete clip markers by --frame, --custom-data or --color/All",
    [_CLIP, arg("frame", "int"), arg("custom-data", "json"), arg("color", choices=MARKER_COLORS + ["All"]), _BIN])
cmd("clip-flag", cmd_clip_flag, True, "add a flag (--add COLOR) or clear flags (--remove COLOR|All)",
    [_CLIP, arg("add", choices=MARKER_COLORS), arg("remove", choices=MARKER_COLORS + ["All"]), _BIN])
cmd("clip-color", cmd_clip_color, True, "set the clip color, or 'clear'",
    [_CLIP, arg("color", positional=True, required=True, choices=CLIP_COLORS + ["clear"]), _BIN])
cmd("clip-mark", cmd_clip_mark, True, "set (--mark-in/--mark-out) or --clear clip mark in/out",
    [_CLIP, arg("mark-in", "int"), arg("mark-out", "int"), arg("type", choices=["video", "audio", "all"]),
     arg("clear", "bool"), _BIN])
cmd("proxy", cmd_proxy, True, "link a proxy (--link PATH), full-res media (--full-res PATH) or --unlink",
    [_CLIP, arg("link"), arg("full-res"), arg("unlink", "bool"), _BIN])
cmd("replace-clip", cmd_replace_clip, True, "ReplaceClip / --preserve-subclip",
    [_CLIP, arg("path", required=True), arg("preserve-subclip", "bool"), _BIN])
cmd("monitor-growing", cmd_monitor_growing, True, "MonitorGrowingFile on a clip", [_CLIP, _BIN])
cmd("set-audio-mapping", cmd_set_audio_mapping, True, "21.1 SetAudioMapping from JSON (track_mapping)",
    [_CLIP, arg("mapping", "json", required=True), _BIN])
cmd("transcription", cmd_transcription, False, "21.1 transcript readback (segments, speakers, word timing)",
    [_CLIP, arg("nested", "bool", help="useNestedClipTranscription"), _BIN])
cmd("clip-ai", cmd_clip_ai, True,
    "AI pass on a clip or bin: transcribe / clear-transcription / classify-audio / clear-classification / deblur / intellisearch / slate",
    [arg("op", positional=True, required=True,
         choices=["transcribe", "clear-transcription", "classify-audio", "clear-classification",
                  "deblur", "intellisearch", "slate"]),
     _CLIP, arg("folder", help="bin path: run on the whole bin instead of --clip"),
     arg("speaker-detection", "bool"), arg("faces", "bool"), arg("better", "bool"),
     arg("marker-color", choices=MARKER_COLORS + ["None"]),
     arg("options", "json", help="DeblurOptions JSON"), _BIN])
