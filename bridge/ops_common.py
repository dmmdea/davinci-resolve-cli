"""ops_common -- the command registry and shared helpers every ops_* module uses.

resolve_cli.py owns the CLI (argparse, watchdog, exit codes) and serve_http.py
owns the HTTP face; both build their view of a command from the SPECS registry
declared here, so a command's arguments are written ONCE and cannot drift
between the CLI and the sidecar.

A spec is a plain dict:

    cmd("add-marker", cmd_add_marker, writes=True, help="...", args=[
        arg("frame", "int", required=True, help="timeline-relative frame"),
        arg("color", choices=MARKER_COLORS, default="Blue"),
        arg("custom-data", "json", help="stored as JSON in the marker"),
    ])

Argument kinds (the coercion contract shared by argparse and the sidecar):
    text   str (default)            bool   flag / true|false
    int    integer                   float  number
    list   one or more strings       json   JSON value: an object/array on the
                                            wire, a JSON string or @file on the CLI

Every handler is `fn(args, resolve)`, reports through `out()`, exits through
`sys.exit("ERROR: ...")` for state errors, and calls `guard()` first when it
writes. Read handlers may only call Get*/Is*/Has* Resolve methods
(check_readonly.py enforces it; `Fusion`/`FindTool*` are allow-listed reads).
"""

import json
import os
import sys
import threading

# ---------------------------------------------------------------------------
# Output sink (moved here from resolve_cli so the ops modules can import it
# without a circular import; resolve_cli re-exports the same names).
# ---------------------------------------------------------------------------

# Normally None and out() prints, exactly as the CLI always has. serve_http
# sets one per request so the same handlers return their result to an HTTP
# caller instead of stdout. THREAD-LOCAL on purpose: a worker the watchdog
# abandoned may wake up long after its request was answered, and a
# process-global sink would let that zombie overwrite a later request's payload.
_sink = threading.local()


def set_output_sink(target):
    """Capture this thread's next out() into `target` instead of printing."""
    _sink.target = target


def out(args, data, lines, warnings=None):
    """Print JSON or human-readable text. Warnings mark API calls that
    returned None -- 'empty or failed' is NOT the same as 'verified empty'."""
    if warnings:
        data = dict(data)
        data["warnings"] = list(warnings)
        for w in warnings:
            sys.stderr.write("warning: %s\n" % w)
    target = getattr(_sink, "target", None)
    if target is not None:
        target["data"] = data
        target["lines"] = list(lines)
        return
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
    else:
        for line in lines:
            print(line)


def api_list(value, what, warnings):
    """None-vs-empty honesty for list-returning Resolve calls: a None return
    (call failed OR genuinely nothing, indistinguishable at this API) is
    recorded as a warning instead of being silently treated as []."""
    if value is None:
        warnings.append("%s returned None (empty or failed -- cross-check before trusting)" % what)
        return []
    return value


def require_project(resolve):
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        sys.exit("No project is open in Resolve. Open one first.")
    return project


def require_timeline(project):
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        sys.exit("No timeline is open in the current project.")
    return timeline


def timeline_by_name(project, name):
    """Find a timeline OBJECT by name without making it current (read-safe)."""
    count = project.GetTimelineCount()
    names = []
    unreadable = 0
    for i in range(1, count + 1):
        tl = project.GetTimelineByIndex(i)
        if tl is None:
            unreadable += 1
            continue
        tl_name = tl.GetName()
        names.append(tl_name)
        if tl_name == name:
            return tl
    note = (" (%d index(es) unreadable -- GetTimelineByIndex returned None)" % unreadable
            if unreadable else "")
    sys.exit("Timeline %r not found. Timelines in this project: %s%s"
             % (name, ", ".join(names) or "(none)", note))


def select_timeline(args, project):
    """--timeline NAME -> that timeline object (never current-switching);
    otherwise the current timeline."""
    if getattr(args, "timeline", None):
        return timeline_by_name(project, args.timeline)
    return require_timeline(project)


def guard(args, action):
    if not getattr(args, "yes", False):
        sys.exit("Refusing to %s without --yes (this modifies the project)." % action)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ARG_KINDS = ("text", "bool", "int", "float", "list", "json")

SPECS = []          # every command spec, in registration order
_BY_NAME = {}


def arg(name, kind="text", required=False, help="", positional=False,
        choices=None, default=None, metavar=None):
    if kind not in ARG_KINDS:
        raise ValueError("unknown arg kind %r for %s" % (kind, name))
    if default is None:
        default = {"bool": False, "list": []}.get(kind)
    return {"name": name, "kind": kind, "required": required, "help": help,
            "positional": positional, "choices": choices, "default": default,
            "metavar": metavar}


def cmd(name, fn, writes, help, args=()):
    if name in _BY_NAME:
        raise ValueError("duplicate command %r" % name)
    spec = {"name": name, "fn": fn, "writes": bool(writes), "help": help,
            "args": list(args)}
    seen = set()
    for a in spec["args"]:
        if a["name"] in seen:
            raise ValueError("duplicate arg %r on %s" % (a["name"], name))
        seen.add(a["name"])
    SPECS.append(spec)
    _BY_NAME[name] = spec
    return spec


def field_name(arg_name):
    return arg_name.replace("-", "_")


def spec_for(name):
    return _BY_NAME.get(name)


def spec_defaults(spec):
    """{field: default} for ONE command -- defaults are per command, never
    shared across commands that happen to reuse a flag name."""
    return {field_name(a["name"]): a["default"] for a in spec["args"]}


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def jsonval(value, field):
    """A `json` kind argument as a Python value. On the CLI it arrives as text
    (JSON, or @path to a JSON file); over HTTP it is already structured."""
    if value is None or not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("@"):
        try:
            with open(text[1:], "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            sys.exit("ERROR: --%s: cannot read %s: %s" % (field, text[1:], exc))
    try:
        return json.loads(text)
    except ValueError as exc:
        sys.exit("ERROR: --%s must be JSON (or @file): %s" % (field, exc))


def need(args, field, what=None):
    value = getattr(args, field, None)
    if value is None or value == "" or value == []:
        sys.exit("ERROR: --%s is required%s" % (field.replace("_", "-"),
                                                 (" (%s)" % what) if what else ""))
    return value


def as_bool_text(value):
    return "1" if value else "0"


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
TRI = ["true", "false"]   # choices for a tri-state flag (unset = leave alone)


def tri(args, field):
    """A tri-state option: None when not given, else True/False. Declared as
    kind text with choices TRI, because a bool kind cannot express 'unset'."""
    value = getattr(args, field, None)
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    sys.exit("ERROR: --%s must be true or false (got %r)" % (field.replace("_", "-"), value))


def norm_path(p):
    """Case-/slash-insensitive path key (Windows media pool 'File Path' vs spec)."""
    return os.path.normcase(os.path.normpath(str(p)))


def check(result, what):
    """Setter contract: Resolve setters return False/None on failure with no
    message. Fail loud rather than reporting success for a dropped write."""
    if not result:
        sys.exit("ERROR: %s returned %r -- the call failed (page-null, wrong page, "
                 "invalid value, or unsupported here)." % (what, result))
    return result


# ---------------------------------------------------------------------------
# Media pool lookups (reads)
# ---------------------------------------------------------------------------

def find_folder(pool, path, warnings):
    """'' / '/' / 'Master' -> root; 'A/B' -> nested bin under the root."""
    root = pool.GetRootFolder()
    if root is None:
        sys.exit("ERROR: media pool root folder is None (call failed).")
    if not path or path in ("/", root.GetName()):
        return root
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    if parts and parts[0] == root.GetName():
        parts = parts[1:]
    folder = root
    for part in parts:
        subs = api_list(folder.GetSubFolderList(), "GetSubFolderList", warnings)
        match = [s for s in subs if s.GetName() == part]
        if not match:
            match = [s for s in subs if s.GetName().lower() == part.lower()]
        if not match:
            sys.exit("ERROR: bin %r not found under %r (have: %s)"
                     % (part, folder.GetName(), ", ".join(s.GetName() for s in subs) or "none"))
        folder = match[0]
    return folder


def walk_folders(folder, path, warnings):
    """Yield (folder, path) depth-first from `folder`."""
    yield folder, path
    for sub in api_list(folder.GetSubFolderList(), "GetSubFolderList(%s)" % path, warnings):
        for pair in walk_folders(sub, path + "/" + sub.GetName(), warnings):
            yield pair


def walk_clips(folder, path, warnings):
    for f, p in walk_folders(folder, path, warnings):
        for clip in api_list(f.GetClipList(), "GetClipList(%s)" % p, warnings):
            yield clip, p


def find_clip(pool, ref, warnings, bin_path=None):
    """A MediaPoolItem by unique id, exact name, or file path. Scoped to
    --bin when given, otherwise the whole pool. Ambiguity is an error, never
    a silent first-match."""
    if not ref:
        sys.exit("ERROR: --clip is required (a clip name, unique id, or file path)")
    start = find_folder(pool, bin_path, warnings)
    ref_path = norm_path(ref)
    by_id, by_name, by_path = [], [], []
    for clip, path in walk_clips(start, start.GetName(), warnings):
        if clip.GetUniqueId() == ref:
            by_id.append((clip, path))
        elif clip.GetName() == ref:
            by_name.append((clip, path))
        else:
            fp = clip.GetClipProperty("File Path")
            if fp and norm_path(fp) == ref_path:
                by_path.append((clip, path))
    for hits in (by_id, by_path, by_name):
        if len(hits) == 1:
            return hits[0][0]
        if len(hits) > 1:
            sys.exit("ERROR: %r matches %d clips (%s) -- use its unique id or --bin"
                     % (ref, len(hits), ", ".join("%s/%s" % (p, c.GetName()) for c, p in hits)))
    sys.exit("ERROR: no media pool clip matches %r (by unique id, name, or file path)" % ref)


def find_clips(pool, refs, warnings, bin_path=None):
    return [find_clip(pool, r, warnings, bin_path) for r in (refs or [])]


def clip_summary(clip):
    return {
        "name": clip.GetName(),
        "id": clip.GetUniqueId(),
        "path": clip.GetClipProperty("File Path"),
        "duration": clip.GetClipProperty("Duration"),
        "fps": clip.GetClipProperty("FPS"),
        "resolution": clip.GetClipProperty("Resolution"),
        "type": clip.GetClipProperty("Type"),
    }


# ---------------------------------------------------------------------------
# Timeline item lookups (reads)
# ---------------------------------------------------------------------------

TRACK_TYPES = ("video", "audio", "subtitle")
_ROLE_LETTER = {"video": "V", "audio": "A", "subtitle": "S"}
_LETTER_ROLE = {v: k for k, v in _ROLE_LETTER.items()}


def role(track_type, index):
    return "%s%d" % (_ROLE_LETTER[track_type], index)


def parse_track(text):
    """'V2' -> ('video', 2); 'audio:1' -> ('audio', 1)."""
    t = str(text).strip()
    if ":" in t:
        kind, idx = t.split(":", 1)
        kind = kind.lower()
    else:
        kind, idx = _LETTER_ROLE.get(t[:1].upper(), ""), t[1:]
    if kind not in TRACK_TYPES:
        sys.exit("ERROR: track %r is not V<n>/A<n>/S<n> or video:<n>/audio:<n>/subtitle:<n>" % text)
    try:
        return kind, int(idx)
    except ValueError:
        sys.exit("ERROR: track %r has no numeric index" % text)


def walk_items(timeline, warnings, types=TRACK_TYPES):
    """Yield (item, role, position) over every track of the given types."""
    for track_type in types:
        for index in range(1, timeline.GetTrackCount(track_type) + 1):
            items = api_list(timeline.GetItemListInTrack(track_type, index),
                             "GetItemListInTrack(%s)" % role(track_type, index), warnings)
            for n, item in enumerate(items, start=1):
                yield item, role(track_type, index), n


def find_item(timeline, ref, warnings):
    """A TimelineItem by 'V1:3' (track role + 1-based position in that
    track), unique id, or name. Returns (item, role)."""
    if not ref:
        sys.exit("ERROR: --item is required ('V1:3', a unique id, or a clip name)")
    text = str(ref)
    if ":" in text and text[:1].upper() in _LETTER_ROLE and text[1:].split(":", 1)[0].isdigit():
        track_role, pos = text.split(":", 1)
        track_type, index = parse_track(track_role)
        try:
            pos = int(pos)
        except ValueError:
            sys.exit("ERROR: item ref %r: position must be an integer" % ref)
        items = api_list(timeline.GetItemListInTrack(track_type, index),
                         "GetItemListInTrack(%s)" % track_role, warnings)
        if not 1 <= pos <= len(items):
            sys.exit("ERROR: %s has %d item(s); position %d is out of range"
                     % (track_role.upper(), len(items), pos))
        return items[pos - 1], role(track_type, index)
    by_id, by_name = [], []
    for item, r, _ in walk_items(timeline, warnings):
        if item.GetUniqueId() == text:
            by_id.append((item, r))
        elif item.GetName() == text:
            by_name.append((item, r))
    if len(by_id) == 1:
        return by_id[0]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        sys.exit("ERROR: %r names %d items (%s) -- use 'V1:3' style or the unique id"
                 % (ref, len(by_name), ", ".join("%s@%s" % (r, i.GetStart()) for i, r in by_name)))
    sys.exit("ERROR: no timeline item matches %r" % ref)


def find_items(timeline, refs, warnings):
    return [find_item(timeline, r, warnings)[0] for r in (refs or [])]


def item_row(item, track_role=None):
    row = {
        "name": item.GetName(),
        "id": item.GetUniqueId(),
        "track": track_role,
        "start": item.GetStart(),
        "end": item.GetEnd(),
        "duration": item.GetDuration(),
    }
    if track_role is None:
        tt = item.GetTrackTypeAndIndex()
        if tt:
            row["track"] = role(tt[0], tt[1]) if tt[0] in _ROLE_LETTER else "%s%s" % (tt[0], tt[1])
    return row


def markers_rows(markers):
    """GetMarkers() -> {frame: {...}} into a sorted list, customData decoded
    from JSON when it is JSON (it is invisible in the UI, so machine-made
    markers store structured tags there)."""
    rows = []
    for frame, info in (markers or {}).items():
        row = {"frame": int(frame)}
        row.update(info or {})
        cd = row.get("customData")
        if isinstance(cd, str) and cd[:1] in ("{", "["):
            try:
                row["customData"] = json.loads(cd)
            except ValueError:
                pass
        rows.append(row)
    rows.sort(key=lambda r: r["frame"])
    return rows


MARKER_COLORS = ["Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia",
                 "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream"]
CLIP_COLORS = ["Orange", "Apricot", "Yellow", "Lime", "Olive", "Green", "Teal", "Navy",
               "Blue", "Purple", "Violet", "Pink", "Tan", "Beige", "Brown", "Chocolate"]
PAGES = ["media", "cut", "edit", "fusion", "color", "fairlight", "deliver"]
