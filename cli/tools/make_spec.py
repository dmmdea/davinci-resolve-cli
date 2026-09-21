"""catalog.json (GET /v1/commands of the resolve_bridge sidecar) -> printing-press internal YAML spec.

The spec is DERIVED from the sidecar's self-description so the printed CLI can never drift
from the bridge: re-run this after any bridge change and regenerate.
"""
import json, re
import sys

import yaml

catalog = json.load(open(sys.argv[1], encoding="utf-8"))
OUT = sys.argv[2]
BASE = "/v1/"

# resource -> [(command, endpoint-key)]
GROUPS = {
    "system": ["status", "presets", "preset", "keyframe-mode", "set-keyframe-mode", "disable-background-tasks",
               "quit", "page", "validate-dctl", "encrypt-dctl", "thumbnail", "export-frame"],
    "project": ["projects", "create-project", "load-project", "delete-project", "save-project", "close-project",
                "rename-project", "export-project", "import-project", "archive-project", "restore-project",
                "cloud-project", "project-settings", "set-project-settings", "project-settings-presets",
                "project-settings-preset", "database", "set-database", "folders", "folder"],
    "storage": ["storage-volumes", "storage-browse", "storage-import", "clone-status", "clone-media"],
    "pool": ["pool", "bins", "bin", "import", "import-media", "delete-clips", "move-clips", "relink-clips",
             "select-clip", "stereo-clip", "sync-audio", "selection"],
    "clip": ["clip-info", "set-clip-property", "set-clip-metadata", "rename-clip", "clip-markers", "add-clip-marker",
             "delete-clip-marker", "clip-flag", "clip-color", "clip-mark", "proxy", "replace-clip", "monitor-growing",
             "set-audio-mapping", "transcription", "clip-ai"],
    "timeline": ["timelines", "timeline-info", "create-timeline", "create-timeline-from-clips", "duplicate-timeline",
                 "delete-timeline", "switch-timeline", "rename-timeline", "append", "export-timeline", "import-timeline",
                 "tracks", "add-track", "delete-track", "set-track", "timeline-markers", "add-marker", "delete-marker",
                 "timecode", "set-timecode", "timeline-mark", "timeline-settings", "set-timeline-settings",
                 "set-blanking", "insert-generator", "compound-clip", "fusion-clip", "grab-still", "normalize-modes",
                 "timeline-ai", "export-spec", "clips", "build"],
    "item": ["items", "item-info", "delete-items", "link-items", "set-item", "set-item-properties", "add-transition",
             "multicam", "fusion-comps", "item-fusion"],
    "color": ["color-info", "timeline-graph", "color-version", "apply-lut", "apply-cdl", "apply-drx", "copy-grade",
              "set-node", "export-lut", "refresh-luts", "color-groups", "color-group", "gallery", "gallery-album"],
    "fusion": ["fusion-tools", "fusion-set", "fusion-keyframes", "fusion-tool", "set-title"],
    "render": ["render-presets", "render-status", "render-formats", "set-render-format", "set-render-mode",
               "set-render-settings", "render-preset", "render-jobs", "render-job-status", "add-render-job",
               "delete-render-job", "start-render", "stop-render", "quick-export-presets", "quick-export",
               "verify-render", "render", "smoke"],
    "audio": ["tts", "insert-audio"],
}
RENAME = {
    "status": "status", "projects": "list", "timelines": "list", "items": "list", "clips": "clips", "pool": "clips",
    "bins": "bins", "bin": "bin", "import": "import-paths", "selection": "selection", "presets": "list",
    "preset": "apply", "clip-info": "info", "set-clip-property": "set-property", "set-clip-metadata": "set-metadata",
    "rename-clip": "rename", "clip-markers": "markers", "add-clip-marker": "add-marker", "delete-clip-marker": "delete-marker",
    "clip-flag": "flag", "clip-color": "color", "clip-mark": "mark", "clip-ai": "ai", "timeline-info": "info",
    "create-timeline": "create", "create-timeline-from-clips": "create-from-clips", "duplicate-timeline": "duplicate",
    "delete-timeline": "delete", "switch-timeline": "switch", "rename-timeline": "rename", "timeline-markers": "markers",
    "timeline-mark": "mark", "timeline-settings": "settings", "set-timeline-settings": "set-settings",
    "timeline-ai": "ai", "item-info": "info", "delete-items": "delete", "link-items": "link", "set-item": "set",
    "set-item-properties": "set-properties", "item-fusion": "fusion-comp", "color-info": "info",
    "color-version": "version", "color-groups": "groups", "color-group": "group", "fusion-tools": "tools",
    "fusion-set": "set", "fusion-keyframes": "keyframes", "fusion-tool": "tool", "render-presets": "presets",
    "render-status": "queue", "render-formats": "formats", "set-render-format": "set-format",
    "set-render-mode": "set-mode", "set-render-settings": "set-settings", "render-preset": "preset",
    "render-jobs": "jobs", "render-job-status": "job-status", "add-render-job": "add-job", "delete-render-job": "delete-job",
    "start-render": "start", "stop-render": "stop", "quick-export-presets": "quick-export-presets",
    "render": "render", "smoke": "smoke", "create-project": "create", "load-project": "load", "delete-project": "delete",
    "save-project": "save", "close-project": "close", "rename-project": "rename", "export-project": "export",
    "import-project": "import", "archive-project": "archive", "restore-project": "restore", "cloud-project": "cloud",
    "project-settings": "settings", "set-project-settings": "set-settings", "project-settings-presets": "settings-presets",
    "project-settings-preset": "settings-preset", "storage-volumes": "volumes", "storage-browse": "browse",
    "storage-import": "import", "import-media": "import", "delete-clips": "delete-clips", "gallery": "gallery",
    "gallery-album": "gallery-album", "timeline-graph": "timeline-graph", "export-spec": "export-spec",
}

# Legacy (pre-catalog) commands: their argument contract, hand-declared.
LEGACY = {
    "status": [], "timelines": [], "selection": [], "render-presets": [], "render-status": [],
    "projects": [{"name": "attributes", "kind": "bool", "help": "include per-project attributes (never opens them)"}],
    "clips": [{"name": "audio", "kind": "bool", "help": "include audio tracks"},
              {"name": "timeline", "kind": "text", "help": "named timeline (never switches the GUI)"}],
    "pool": [{"name": "recursive", "kind": "bool", "help": "walk all media pool folders"}],
    "export-spec": [{"name": "timeline", "kind": "text", "help": "named timeline"}],
    "import": [{"name": "paths", "kind": "list", "required": True, "help": "media file paths"}],
    "page": [{"name": "name", "kind": "text", "required": True, "choices": ["media", "cut", "edit", "fusion", "color", "fairlight", "deliver"], "help": "page"}],
    "create-project": [{"name": "name", "kind": "text", "required": True, "help": "project name"}],
    "delete-project": [{"name": "name", "kind": "text", "required": True, "help": "project name (not the loaded one)"}],
    "load-project": [{"name": "name", "kind": "text", "required": True, "help": "project name (switches the GUI)"}],
    "create-timeline": [{"name": "name", "kind": "text", "required": True, "help": "timeline name"}],
    "render": [{"name": "out", "kind": "text", "help": "output directory (default <system temp>/_pp_smoke)"},
               {"name": "name", "kind": "text", "help": "output stem (default pp_render)"}],
    "smoke": [{"name": "out", "kind": "text", "help": "output directory for the disposable render"}],
    "build": [{"name": "spec", "kind": "text", "required": True, "help": "edit-spec JSON (schema 1.0.0)"},
              {"name": "fps", "kind": "text", "help": "Resolve frame-rate string, e.g. 29.97"},
              {"name": "lut", "kind": "list", "help": "grade NAME=PATH mappings"}],
}
LEGACY_HELP = {
    "status": "product, version, Studio flag, page (null = writes will fail), project, timeline, database, keyboard preset, rendering",
    "projects": "projects in the current database folder", "timelines": "timelines in the open project",
    "clips": "items per track of a timeline", "selection": "the editor's current selection", "pool": "media pool clips",
    "render-presets": "render preset names", "render-status": "the render queue", "export-spec": "serialize a timeline to edit-spec JSON",
    "import": "ImportMedia into the current pool folder", "page": "switch the GUI page", "create-project": "create AND load a project",
    "delete-project": "delete a project that is not loaded", "load-project": "load a project (the page-null escape hatch)",
    "create-timeline": "CreateEmptyTimeline in the current project", "render": "render the current timeline (mp4/H264) with every guard",
    "smoke": "disposable-project end-to-end render proof", "build": "edit-spec -> media pool + new timeline with readback",
}
TIMEOUT_HINTS = {"render": 180, "smoke": 180, "build": 180, "clip-ai": 180, "start-render": 120, "quick-export": 120,
                 "import-timeline": 90, "timeline-ai": 180}
KIND_TYPE = {"text": "string", "int": "int", "float": "float", "bool": "bool", "list": "string", "json": "string"}

# Phase 5 live-dogfood fixtures (pp:happy-args). The matrix runs against a REAL
# Resolve, so every write defaults to --dry-run; the whitelist below runs real,
# harmless writes on the disposable _ref_scratch project (timeline _ref_cli_tl,
# clip ref_a.mp4, Text+ on V1:2), and reads get the fixture refs they need.
HAPPY = {
    "status": "", "projects": "", "timelines": "", "clips": "--timeline=_ref_cli_tl", "selection": "",
    "pool": "--recursive", "render-presets": "", "render-status": "", "export-spec": "--timeline=_ref_cli_tl",
    "bins": "", "clip-info": "--clip=ref_a.mp4", "clip-markers": "--clip=ref_a.mp4",
    "transcription": "--clip=ref_a.mp4", "timeline-info": "--timeline=_ref_cli_tl", "tracks": "--timeline=_ref_cli_tl",
    "items": "--timeline=_ref_cli_tl", "item-info": "--item=V1:2 --timeline=_ref_cli_tl",
    "timeline-markers": "--timeline=_ref_cli_tl", "timecode": "--timeline=_ref_cli_tl",
    "timeline-settings": "--timeline=_ref_cli_tl", "normalize-modes": "--timeline=_ref_cli_tl",
    "fusion-comps": "--item=V1:1 --timeline=_ref_cli_tl", "color-info": "--item=V1:2 --timeline=_ref_cli_tl",
    "timeline-graph": "--timeline=_ref_cli_tl", "color-groups": "", "gallery": "",
    "fusion-tools": "--item=V1:1 --timeline=_ref_cli_tl --regid=TextPlus", "render-formats": "--format=mp4",
    "render-jobs": "", "quick-export-presets": "", "verify-render": "--path=C:/Renders/ref_render.mp4",
    "presets": "", "keyframe-mode": "", "validate-dctl": "--path=C:/ProgramData/Blackmagic Design/DaVinci Resolve/Support/Developer/DaVinciCTL/Gain.dctl",
    "storage-volumes": "", "storage-browse": "--path=C:/Media", "clone-status": "",
    "database": "", "folders": "", "project-settings": "--key=timelineFrameRate", "project-settings-presets": "",
    "render-job-status": "--job=example --dry-run", "thumbnail": "--dry-run",
    # harmless real writes on the disposable fixture
    "add-marker": "--frame=12 --color=Blue --name=dogfood --custom-data={\"by\":\"dogfood\"} --timeline=_ref_cli_tl --yes",
    "delete-marker": "--custom-data={\"by\":\"dogfood\"} --timeline=_ref_cli_tl --yes",
    "set-title": "--item=V1:1 --timeline=_ref_cli_tl --text=dogfood --yes",
    "timeline-mark": "--clear --timeline=_ref_cli_tl --yes",
    "set-timecode": "--playhead=01:00:00:12 --timeline=_ref_cli_tl --yes",
    "refresh-luts": "--yes", "save-project": "--yes", "set-keyframe-mode": "--mode=all --yes",
    "page": "--name=edit --yes", "set-item-properties": "--item=V1:2 --timeline=_ref_cli_tl --properties={\"Opacity\":100.0} --yes",
    "set-render-mode": "--mode=single --yes",
}


by_name = {c["name"]: c for c in catalog["commands"]}
resources = {}
seen = set()
for res, names in GROUPS.items():
    endpoints = {}
    for cmd in names:
        c = by_name.get(cmd)
        if c is None:
            sys.exit("catalog lacks %s" % cmd)
        seen.add(cmd)
        args = c.get("args")
        if args is None:
            args = LEGACY[cmd]
            help_text = LEGACY_HELP[cmd]
        else:
            help_text = c["help"]
        key = RENAME.get(cmd, cmd.replace(res + "-", "").replace("-" + res, ""))
        params = []
        for a in args:
            p = {"name": a["name"], "type": KIND_TYPE[a["kind"]], "required": bool(a.get("required")),
                 "positional": False, "description": a.get("help") or a["name"].replace("-", " ")}
            if a["kind"] == "list":
                p["description"] += " (comma-free single value or a JSON array string)"
            if a["kind"] == "json":
                p["description"] += " (JSON value as a string)"
            if a.get("choices"):
                p["enum"] = [str(x) for x in a["choices"]]
            if a.get("default") not in (None, False, [], ""):
                p["default"] = a["default"]
            params.append(p)
        # The generator takes the text before the FIRST period as the Cobra Short line, so a
        # version number or "e.g." would truncate it: keep the first sentence period-free.
        short = help_text.replace("21.1", "21-1").replace("e.g.", "for example").replace("...", "").replace("i.e.", "that is")
        ep = {"method": "POST" if c["writes"] else "GET", "path": BASE + cmd, "description": short}
        happy = HAPPY.get(cmd, "--dry-run" if c["writes"] else None)
        if happy:
            # generator grammar: semicolon-separated --flag=value tokens (parseHappyArgsAnnotation)
            ep["happy_args"] = re.sub(r" (?=--)", ";", happy)
        timeout = TIMEOUT_HINTS.get(cmd)
        if c["writes"]:
            body = list(params)
            body.append({"name": "yes", "type": "bool", "required": False, "positional": False, "default": True,
                         "description": "confirm the write (the bridge refuses without it)"})
            body.append({"name": "timeout", "type": "int", "required": False, "positional": False,
                         "default": timeout or 25,
                         "description": "per-call watchdog seconds (a trip stops the sidecar)"})
            ep["body"] = body
        else:
            if timeout:
                params.append({"name": "timeout", "type": "int", "required": False, "positional": False,
                               "default": timeout, "description": "per-call watchdog seconds"})
            ep["params"] = params
            ep["mutation"] = False
        ep["response"] = {"type": "object"}
        endpoints[key] = ep
    resources[res] = {"description": RESOURCE_HELP[res] if (RESOURCE_HELP := {
        "system": "Resolve app state: status, page, system presets, keyframe mode, background tasks, DCTL, quit",
        "project": "project manager, database, folders, project settings and settings presets",
        "storage": "media storage browsing/import and the 21.1 clone tool",
        "pool": "media pool bins and clip lifecycle",
        "clip": "one media pool clip: properties, metadata, markers, flags, marks, proxies, AI passes, transcription",
        "timeline": "timelines, tracks, markers, timecode, settings, generators, timeline AI, import/export, build",
        "item": "timeline items: properties, speed, fades, transitions, multicam, Fusion comps",
        "color": "grade versions, LUT/CDL/DRX, node graph, color groups, gallery",
        "fusion": "Fusion tool scripting on an item's comp (Lock/Undo wrapped) and the Text+ shortcut",
        "render": "render formats, settings, presets, job queue, quick export, verify-by-file",
        "audio": "text-to-speech and audio insertion",
    }) else "", "endpoints": endpoints}
missing = sorted(set(by_name) - seen)
if missing:
    sys.exit("commands not grouped: %s" % missing)

spec = {
    "name": "davinci-resolve",
    "description": "Drive DaVinci Resolve Studio through the resolve_bridge sidecar: 146 measured operations, page-null/modal-stall/handle-aware guards, transcript readback, transitions, multicam, render verify-by-file",
    "cli_description": "Every documented DaVinci Resolve scripting operation, plus the cross-project history and doctor diagnostics that Resolve itself — and every other Resolve CLI or MCP server — throws away.",
    "version": "0.1.0",
    "category": "media-and-entertainment",
    "base_url": "http://127.0.0.1:18800",
    "health_check_path": "/healthz",
    "auth": {"type": "none"},
    "config": {"format": "toml", "path": "~/.config/davinci-resolve-pp-cli/config.toml"},
    # learn: opt-out -- a local control surface; the only "entities" are the user's own
    # project/timeline/clip names, which are never stable across users.
    "learn": {"disabled": True},
    # 147 typed endpoints > 50: make the Cloudflare pattern explicit (thin search/execute pair,
    # per-endpoint tools hidden) so the MCP surface does not burn agent context.
    "mcp": {"transport": ["stdio", "http"], "orchestration": "code", "endpoint_tools": "hidden"},
    "resources": resources,
}
with open(OUT, "w", encoding="utf-8") as fh:
    yaml.safe_dump(spec, fh, sort_keys=False, width=120, allow_unicode=True)
n = sum(len(r["endpoints"]) for r in resources.values())
print("wrote", OUT, "resources", len(resources), "endpoints", n)
