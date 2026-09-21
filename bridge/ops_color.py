"""ops_color -- grade versions, node graph, CDL/LUT/DRX, grade copy, color
groups, gallery, LUT refresh/export, and Fusion tool scripting with the
Lock/StartUndo/EndUndo/Unlock discipline. Absorb-manifest rows 34-40.

Per-node lift/gamma/gain is NOT exposed by the scripting API; only whole-graph
operations are (LUT per node, CDL per node, DRX replace, CopyGrades). That
limit is stated in the command help rather than silently omitted.
"""

import json
import sys

from ops_common import (arg, cmd, out, api_list, require_project, select_timeline, guard,
                        jsonval, need, check, find_item, find_items, item_row, tri, TRI)


# ---------------------------------------------------------------------------
# Read: versions, graph, group (rows 34-36)
# ---------------------------------------------------------------------------

def _graph_rows(graph, warnings):
    if graph is None:
        warnings.append("GetNodeGraph returned None")
        return []
    rows = []
    count = graph.GetNumNodes() or 0
    for i in range(1, count + 1):
        rows.append({"node": i, "label": graph.GetNodeLabel(i), "lut": graph.GetLUT(i),
                     "tools": graph.GetToolsInNode(i) or [], "cache_mode": graph.GetNodeCacheMode(i)})
    return rows


def cmd_color_info(args, resolve):
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    group = item.GetColorGroup()
    data = item_row(item, r)
    data.update({
        "current_version": item.GetCurrentVersion(),
        "local_versions": api_list(item.GetVersionNameList(0), "GetVersionNameList(0)", warnings),
        "remote_versions": api_list(item.GetVersionNameList(1), "GetVersionNameList(1)", warnings),
        "group": group.GetName() if group else None,
        "nodes": _graph_rows(item.GetNodeGraph(), warnings),
        "color_output_cache": item.GetIsColorOutputCacheEnabled(),
    })
    out(args, data, ["%s %s: version %s, group %s, %d node(s)" % (
        r, item.GetName(), data["current_version"], data["group"], len(data["nodes"]))] +
        ["  node %d %s lut=%s tools=%s" % (n["node"], n["label"] or "", n["lut"] or "-", n["tools"])
         for n in data["nodes"]], warnings)


def cmd_timeline_graph(args, resolve):
    """The timeline-level grade graph (Color page 'Timeline' node tree)."""
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    rows = _graph_rows(tl.GetNodeGraph(), warnings)
    out(args, {"timeline": tl.GetName(), "nodes": rows},
        ["%s timeline graph: %d node(s)" % (tl.GetName(), len(rows))], warnings)


# ---------------------------------------------------------------------------
# Versions (row 34)
# ---------------------------------------------------------------------------

def cmd_color_version(args, resolve):
    action = need(args, "action")
    name = need(args, "name")
    guard(args, "%s grade version %r" % (action, name))
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    vtype = 1 if getattr(args, "type", None) == "remote" else 0
    if action == "add":
        ok = item.AddVersion(name, vtype)
    elif action == "delete":
        current = item.GetCurrentVersion() or {}
        if current.get("versionName") == name and int(current.get("versionType") or 0) == vtype:
            # Measured 2026-09-09: DeleteVersionByName returns False on the loaded version.
            sys.exit("ERROR: %r is the item's CURRENT grade version; load another version first "
                     "(color-version load ...), then delete it." % name)
        ok = item.DeleteVersionByName(name, vtype)
    elif action == "load":
        ok = item.LoadVersionByName(name, vtype)
    else:
        ok = item.RenameVersionByName(name, need(args, "new_name"), vtype)
    check(ok, "%s version %r" % (action, name))
    out(args, {"item": item.GetName(), "track": r, "action": action, "name": name,
               "current_version": item.GetCurrentVersion(),
               "versions": item.GetVersionNameList(vtype) or [], "ok": True},
        ["%s %s version %s; current %s" % (item.GetName(), action, name, item.GetCurrentVersion())], warnings)


# ---------------------------------------------------------------------------
# Whole-graph grade operations (row 35)
# ---------------------------------------------------------------------------

def cmd_apply_lut(args, resolve):
    """Graph.SetLUT(node, path): path relative to the LUT root or absolute;
    the LUT must already be discovered (refresh-luts after copying a .cube)."""
    guard(args, "apply a LUT")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    items = find_items(tl, need(args, "items"), warnings)
    node, lut = int(getattr(args, "node", None) or 1), need(args, "lut")
    applied = []
    for item in items:
        graph = item.GetNodeGraph()
        if graph is None or not graph.SetLUT(node, lut):
            sys.exit("ERROR: SetLUT(%d, %r) returned False on %s -- LUT not under the LUT folder / not "
                     "refreshed (refresh-luts), node index out of range (1-based), or wrong page."
                     % (node, lut, item.GetName()))
        applied.append({"item": item.GetName(), "readback": graph.GetLUT(node)})
    out(args, {"node": node, "lut": lut, "applied": applied, "ok": True},
        ["applied %s on node %d of %d item(s)" % (lut, node, len(applied))], warnings)


def cmd_apply_cdl(args, resolve):
    guard(args, "apply a CDL")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    items = find_items(tl, need(args, "items"), warnings)
    cdl = {"NodeIndex": str(int(getattr(args, "node", None) or 1))}
    for key in ("slope", "offset", "power"):
        value = getattr(args, key, None)
        if value:
            cdl[key.capitalize()] = value
    if getattr(args, "saturation", None) is not None:
        cdl["Saturation"] = str(args.saturation)
    for item in items:
        check(item.SetCDL(cdl), "SetCDL(%s) on %s" % (json.dumps(cdl), item.GetName()))
    out(args, {"cdl": cdl, "items": [i.GetName() for i in items], "ok": True},
        ["applied CDL %s to %d item(s)" % (json.dumps(cdl), len(items))], warnings)


def cmd_apply_drx(args, resolve):
    """Graph.ApplyGradeFromDRX REPLACES the item's whole node graph."""
    guard(args, "apply a DRX grade (replaces the node graph)")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    items = find_items(tl, need(args, "items"), warnings)
    path = need(args, "path")
    mode = {"none": 0, "source": 1, "start": 2}[getattr(args, "mode", None) or "none"]
    for item in items:
        graph = item.GetNodeGraph()
        if graph is None or not graph.ApplyGradeFromDRX(path, mode):
            sys.exit("ERROR: ApplyGradeFromDRX(%r, %d) returned False on %s" % (path, mode, item.GetName()))
    out(args, {"path": path, "mode": mode, "items": [i.GetName() for i in items], "ok": True},
        ["applied %s to %d item(s)" % (path, len(items))], warnings)


def cmd_copy_grade(args, resolve):
    guard(args, "copy a grade between items")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    source, _ = find_item(tl, need(args, "source"), warnings)
    targets = find_items(tl, need(args, "items"), warnings)
    check(source.CopyGrades(targets), "CopyGrades(%s -> %d items)" % (source.GetName(), len(targets)))
    out(args, {"source": source.GetName(), "targets": [t.GetName() for t in targets], "ok": True},
        ["copied grade %s -> %d item(s)" % (source.GetName(), len(targets))], warnings)


def cmd_set_node(args, resolve):
    """Per-node enable/cache-mode/reset on an item's graph (no per-node
    lift/gamma/gain exists in the API)."""
    guard(args, "change a grade node")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    graph = item.GetNodeGraph()
    if graph is None:
        sys.exit("ERROR: GetNodeGraph returned None")
    changed = []
    if getattr(args, "reset_all", False):
        check(graph.ResetAllGrades(), "ResetAllGrades")
        changed.append("reset_all")
    if getattr(args, "reset_colors", False):
        check(item.ResetAllNodeColors(), "ResetAllNodeColors")
        changed.append("reset_colors")
    if getattr(args, "arri_cdl", False):
        check(graph.ApplyArriCdlLut(), "ApplyArriCdlLut")
        changed.append("arri_cdl")
    node = getattr(args, "node", None)
    enabled = tri(args, "enabled")
    if enabled is not None:
        check(graph.SetNodeEnabled(int(need(args, "node")), enabled), "SetNodeEnabled")
        changed.append("enabled")
    cache = getattr(args, "cache", None)
    if cache:
        value = {"auto": resolve.CACHE_AUTO_ENABLED, "off": resolve.CACHE_DISABLED, "on": resolve.CACHE_ENABLED}[cache]
        check(graph.SetNodeCacheMode(int(need(args, "node")), value), "SetNodeCacheMode")
        changed.append("cache")
    if not changed:
        sys.exit("ERROR: nothing to change (--enabled/--cache with --node, --reset-all, --reset-colors, --arri-cdl)")
    out(args, {"item": item.GetName(), "track": r, "node": node, "changed": changed,
               "nodes": _graph_rows(graph, warnings), "ok": True},
        ["%s: changed %s" % (item.GetName(), ", ".join(changed))], warnings)


def cmd_export_lut(args, resolve):
    guard(args, "export an item's grade as a LUT")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    kinds = {"17": resolve.EXPORT_LUT_17PTCUBE, "33": resolve.EXPORT_LUT_33PTCUBE,
             "65": resolve.EXPORT_LUT_65PTCUBE, "vlut": resolve.EXPORT_LUT_PANASONICVLUT}
    path = need(args, "path")
    # Measured 2026-09-09 (21.1.0.14): ExportLUT returns False on the Edit page and
    # True (file written) on the Color page. Switch there and switch back.
    page = resolve.GetCurrentPage()
    if page != "color":
        warnings.append("switched to the color page for ExportLUT (was %r) and back" % page)
        resolve.OpenPage("color")
    try:
        check(item.ExportLUT(kinds[getattr(args, "cube", None) or "33"], path), "ExportLUT(%r)" % path)
    finally:
        if page and page != "color":
            resolve.OpenPage(page)
    out(args, {"item": item.GetName(), "path": path, "ok": True}, ["exported LUT of %s -> %s" % (item.GetName(), path)], warnings)


def cmd_refresh_luts(args, resolve):
    guard(args, "refresh the LUT list")
    project = require_project(resolve)
    check(project.RefreshLUTList(), "RefreshLUTList()")
    out(args, {"ok": True}, ["LUT list refreshed"])


# ---------------------------------------------------------------------------
# Color groups (row 36)
# ---------------------------------------------------------------------------

def cmd_color_groups(args, resolve):
    warnings = []
    project = require_project(resolve)
    groups = api_list(project.GetColorGroupsList(), "GetColorGroupsList", warnings)
    rows = []
    tl = project.GetCurrentTimeline()
    for g in groups:
        clips = api_list(g.GetClipsInTimeline(tl) if tl else g.GetClipsInTimeline(), "GetClipsInTimeline", warnings)
        rows.append({"name": g.GetName(), "clips": [c.GetName() for c in clips],
                     "pre_nodes": (g.GetPreClipNodeGraph().GetNumNodes() if g.GetPreClipNodeGraph() else None),
                     "post_nodes": (g.GetPostClipNodeGraph().GetNumNodes() if g.GetPostClipNodeGraph() else None)})
    out(args, {"groups": rows}, ["%d color group(s):" % len(rows)] +
        ["  %s: %d clip(s)" % (r["name"], len(r["clips"])) for r in rows], warnings)


def _find_group(project, name, warnings):
    for g in api_list(project.GetColorGroupsList(), "GetColorGroupsList", warnings):
        if g.GetName() == name:
            return g
    sys.exit("ERROR: color group %r not found" % name)


def cmd_color_group(args, resolve):
    action = need(args, "action")
    name = need(args, "name")
    guard(args, "%s color group %r" % (action, name))
    warnings = []
    project = require_project(resolve)
    data = {"action": action, "name": name, "ok": True}
    if action == "create":
        g = project.AddColorGroup(name)
        if g is None:
            sys.exit("ERROR: AddColorGroup(%r) returned None (duplicate name?)" % name)
        lines = ["created color group %s" % name]
    elif action == "delete":
        check(project.DeleteColorGroup(_find_group(project, name, warnings)), "DeleteColorGroup(%r)" % name)
        lines = ["deleted color group %s" % name]
    elif action == "rename":
        check(_find_group(project, name, warnings).SetName(need(args, "new_name")), "ColorGroup.SetName")
        lines = ["renamed color group %s -> %s" % (name, args.new_name)]
    else:
        tl = select_timeline(args, project)
        items = find_items(tl, need(args, "items"), warnings)
        if action == "assign":
            g = _find_group(project, name, warnings)
            for item in items:
                check(item.AssignToColorGroup(g), "AssignToColorGroup on %s" % item.GetName())
            lines = ["assigned %d item(s) to %s" % (len(items), name)]
        else:
            for item in items:
                check(item.RemoveFromColorGroup(), "RemoveFromColorGroup on %s" % item.GetName())
            lines = ["removed %d item(s) from their color group" % len(items)]
        data["items"] = [i.GetName() for i in items]
    out(args, data, lines, warnings)


# ---------------------------------------------------------------------------
# Gallery (row 37)
# ---------------------------------------------------------------------------

def _albums(gallery, warnings):
    stills = api_list(gallery.GetGalleryStillAlbums(), "GetGalleryStillAlbums", warnings)
    power = api_list(gallery.GetGalleryPowerGradeAlbums(), "GetGalleryPowerGradeAlbums", warnings)
    current = gallery.GetCurrentStillAlbum()
    current_name = gallery.GetAlbumName(current) if current else None
    rows = []
    for kind, albums in (("still", stills), ("powergrade", power)):
        for a in albums:
            name = gallery.GetAlbumName(a)
            rows.append({"name": name, "kind": kind, "current": name == current_name,
                         "stills": len(api_list(a.GetStills(), "GetStills(%s)" % name, warnings))})
    return rows, current_name


def _find_album(gallery, name, warnings):
    for kind_list in (gallery.GetGalleryStillAlbums(), gallery.GetGalleryPowerGradeAlbums()):
        for a in kind_list or []:
            if gallery.GetAlbumName(a) == name:
                return a
    sys.exit("ERROR: gallery album %r not found" % name)


def cmd_gallery(args, resolve):
    warnings = []
    project = require_project(resolve)
    gallery = project.GetGallery()
    if gallery is None:
        sys.exit("ERROR: GetGallery returned None")
    rows, current = _albums(gallery, warnings)
    out(args, {"albums": rows, "current": current}, ["%d album(s); current %s" % (len(rows), current)] +
        ["  %s%s (%s, %d stills)" % ("*" if r["current"] else " ", r["name"], r["kind"], r["stills"]) for r in rows],
        warnings)


def cmd_gallery_album(args, resolve):
    """create / create-powergrade / set-current / rename / import (stills
    from files) / export (stills to a folder, page-dependent) / delete-stills."""
    action = need(args, "action")
    guard(args, "gallery %s" % action)
    warnings = []
    project = require_project(resolve)
    gallery = project.GetGallery()
    if gallery is None:
        sys.exit("ERROR: GetGallery returned None")
    name = getattr(args, "name", None)
    data = {"action": action, "name": name, "ok": True}
    if action == "create":
        album = gallery.CreateGalleryStillAlbum()
        if album is None:
            sys.exit("ERROR: CreateGalleryStillAlbum returned None")
        if name:
            check(gallery.SetAlbumName(album, name), "SetAlbumName")
        lines = ["created still album %s" % (gallery.GetAlbumName(album))]
    elif action == "create-powergrade":
        album = gallery.CreateGalleryPowerGradeAlbum()
        if album is None:
            sys.exit("ERROR: CreateGalleryPowerGradeAlbum returned None")
        if name:
            check(gallery.SetAlbumName(album, name), "SetAlbumName")
        lines = ["created PowerGrade album %s" % gallery.GetAlbumName(album)]
    else:
        album = _find_album(gallery, need(args, "name"), warnings)
        if action == "set-current":
            check(gallery.SetCurrentStillAlbum(album), "SetCurrentStillAlbum")
            lines = ["current album: %s" % name]
        elif action == "rename":
            check(gallery.SetAlbumName(album, need(args, "new_name")), "SetAlbumName")
            lines = ["renamed album %s -> %s" % (name, args.new_name)]
        elif action == "import":
            check(album.ImportStills(list(need(args, "paths"))), "ImportStills")
            lines = ["imported %d still(s) into %s" % (len(args.paths), name)]
        elif action == "export":
            stills = api_list(album.GetStills(), "GetStills", warnings)
            if not stills:
                sys.exit("ERROR: album %r has no stills to export" % name)
            ok = album.ExportStills(stills, need(args, "path"), getattr(args, "prefix", None) or "still",
                                    getattr(args, "format", None) or "drx")
            if not ok:
                sys.exit("ERROR: ExportStills returned False (page-dependent: open the Color page with the "
                         "gallery visible, then retry)")
            data["count"] = len(stills)
            lines = ["exported %d still(s) from %s -> %s" % (len(stills), name, args.path)]
        else:  # delete-stills
            stills = api_list(album.GetStills(), "GetStills", warnings)
            check(album.DeleteStills(stills), "DeleteStills")
            lines = ["deleted %d still(s) from %s" % (len(stills), name)]
    data["albums"], data["current"] = _albums(gallery, warnings)
    out(args, data, lines, warnings)


# ---------------------------------------------------------------------------
# Fusion tool scripting (rows 39, 40)
# ---------------------------------------------------------------------------

def _comp(item, index):
    comp = item.GetFusionCompByIndex(int(index or 1))
    if comp is None:
        sys.exit("ERROR: item %r has no Fusion comp at index %s (item-fusion add first, or insert a "
                 "Fusion title)" % (item.GetName(), index or 1))
    return comp


def _tools(comp, regid=None):
    tools = comp.GetToolList(False, regid) if regid else comp.GetToolList(False)
    return list((tools or {}).values())


def _tool(comp, name):
    """By tool name (TOOLS_Name), else by RegID when exactly one tool has it."""
    if not name:
        sys.exit("ERROR: --tool is required (a tool name like 'Template', or a RegID like TextPlus)")
    tool = comp.FindTool(name)
    if tool is not None:
        return tool
    by_id = _tools(comp, name)
    if len(by_id) == 1:
        return by_id[0]
    have = ["%s (%s)" % (t.Name, t.ID) for t in _tools(comp)]
    sys.exit("ERROR: no tool named %r (and %d tools have that RegID). Tools: %s"
             % (name, len(by_id), ", ".join(have)))


def _input_value(tool, input_id, time=None):
    return tool.GetInput(input_id, time) if time is not None else tool.GetInput(input_id)


def _input_obj(tool, input_id):
    """The Input object for an id (GetInputList is keyed 1..n, not by id)."""
    for _, inp in (tool.GetInputList() or {}).items():
        if (inp.GetAttrs() or {}).get("INPS_ID") == input_id:
            return inp
    sys.exit("ERROR: tool %r has no input %r" % (tool.Name, input_id))


def cmd_fusion_tools(args, resolve):
    """Tools in an item's Fusion comp (name, RegID, pass-through), optionally
    filtered by --regid, with the values of --inputs IDs on each."""
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    comp = _comp(item, getattr(args, "comp", None))
    ids = getattr(args, "read_inputs", None) or []
    rows = []
    for tool in _tools(comp, getattr(args, "regid", None)):
        attrs = tool.GetAttrs() or {}
        row = {"name": tool.Name, "regid": tool.ID, "pass_through": attrs.get("TOOLB_PassThrough"),
               "inputs": {}}
        if getattr(args, "all_inputs", False):
            for _, inp in (tool.GetInputList() or {}).items():
                ia = inp.GetAttrs() or {}
                iid = ia.get("INPS_ID")
                if iid:
                    row["inputs"][iid] = tool.GetInput(iid)
        for iid in ids:
            row["inputs"][iid] = tool.GetInput(iid)
        rows.append(row)
    attrs = comp.GetAttrs() or {}
    data = {"item": item.GetName(), "track": r, "comp": {
        "name": attrs.get("COMPS_Name"), "render_start": attrs.get("COMPN_RenderStart"),
        "render_end": attrs.get("COMPN_RenderEnd"), "locked": attrs.get("COMPB_Locked")}, "tools": rows}
    out(args, data, ["%s: %d tool(s) in comp %s" % (item.GetName(), len(rows), data["comp"]["name"])] +
        ["  %-20s %-14s %s" % (t["name"], t["regid"], json.dumps(t["inputs"], default=str) if t["inputs"] else "")
         for t in rows], warnings)


def _fusion_write(comp, label, fn):
    """Every scripted Fusion mutation runs inside Lock/StartUndo ... EndUndo/
    Unlock: no dialogs, no per-change re-render, one undo step for the editor."""
    comp.Lock()
    comp.StartUndo(label)
    try:
        return fn()
    finally:
        comp.EndUndo(True)
        comp.Unlock()


def cmd_fusion_set(args, resolve):
    """SetInput on a tool for every key of --inputs JSON (at --time when
    given), then GetInput readback. SetInput returns None even on success,
    so the readback IS the verification. Point inputs (Center) take
    [x, y] or {"1": x, "2": y}; attribute assignment is silently ignored by
    Resolve, so only SetInput is used here."""
    guard(args, "set Fusion tool inputs")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    comp = _comp(item, getattr(args, "comp", None))
    tool = _tool(comp, need(args, "tool"))
    values = jsonval(need(args, "inputs"), "inputs")
    if not isinstance(values, dict) or not values:
        sys.exit("ERROR: --inputs must be a non-empty JSON object of input id -> value")
    time = getattr(args, "time", None)

    def apply():
        for iid, value in values.items():
            if isinstance(value, list) and len(value) in (2, 3) and all(isinstance(v, (int, float)) for v in value):
                value = {i + 1: float(v) for i, v in enumerate(value)}
            if time is not None:
                tool.SetInput(iid, value, int(time))
            else:
                tool.SetInput(iid, value)
    _fusion_write(comp, "bridge fusion-set", apply)
    readback = {iid: _input_value(tool, iid, int(time) if time is not None else None) for iid in values}
    mismatch = [iid for iid, v in values.items()
                if not isinstance(v, (dict, list)) and readback.get(iid) != v
                and not (isinstance(v, (int, float)) and isinstance(readback.get(iid), (int, float))
                         and abs(float(readback[iid]) - float(v)) < 1e-6)]
    if mismatch:
        warnings.append("readback differs for %s -- animated input (keyframes override static "
                        "values), wrong input id, or value clipped" % ", ".join(mismatch))
    out(args, {"item": item.GetName(), "track": r, "tool": tool.Name, "set": values, "readback": readback,
               "mismatch": mismatch, "ok": not mismatch},
        ["%s/%s: set %d input(s)%s" % (item.GetName(), tool.Name, len(values),
                                       (", MISMATCH " + ", ".join(mismatch)) if mismatch else "")] +
        ["  %s = %s" % (k, v) for k, v in readback.items()], warnings)


def cmd_fusion_keyframes(args, resolve):
    """Animate one input with a BezierSpline: --keys {"0": 0.05, "24": 0.12}
    (frame -> value, comp-local frames). Read back with GetKeyFrames."""
    guard(args, "keyframe a Fusion input")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    comp = _comp(item, getattr(args, "comp", None))
    tool = _tool(comp, need(args, "tool"))
    input_id = need(args, "input")
    keys = jsonval(need(args, "keys"), "keys")
    if not isinstance(keys, dict) or not keys:
        sys.exit("ERROR: --keys must be a JSON object of frame -> value")

    inp = _input_obj(tool, input_id)

    def apply():
        if getattr(args, "clear", False):
            inp.ConnectTo(None)
            return
        tool.AddModifier(input_id, "BezierSpline")
        for frame, value in keys.items():
            tool.SetInput(input_id, value, int(frame))
    _fusion_write(comp, "bridge fusion-keyframes", apply)
    frames = inp.GetKeyFrames() if not getattr(args, "clear", False) else None
    readback = {str(f): tool.GetInput(input_id, int(f)) for f in keys} if not getattr(args, "clear", False) else {}
    out(args, {"item": item.GetName(), "track": r, "tool": tool.Name, "input": input_id, "keys": keys,
               "keyframes": frames, "readback": readback, "ok": True},
        ["%s/%s.%s: %d keyframe(s); spline frames %s" % (item.GetName(), tool.Name, input_id, len(keys), frames)],
        warnings)


def cmd_fusion_tool(args, resolve):
    """add a tool (--regid, optional --name, --connect-from/--connect-to by
    tool name), delete one (--delete), or toggle pass-through."""
    guard(args, "change Fusion tools")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    comp = _comp(item, getattr(args, "comp", None))
    action = need(args, "action")

    def apply():
        if action == "add":
            regid = need(args, "regid")
            tool = comp.AddTool(regid, -32768, -32768)
            if tool is None:
                sys.exit("ERROR: AddTool(%r) returned None (unknown RegID?)" % regid)
            if getattr(args, "name", None):
                tool.SetAttrs({"TOOLS_Name": args.name})
            if getattr(args, "connect_from", None):
                src = _tool(comp, args.connect_from)
                tool.ConnectInput(getattr(args, "input", None) or "Input", src)
            if getattr(args, "connect_to", None):
                dst = _tool(comp, args.connect_to)
                dst.ConnectInput(getattr(args, "to_input", None) or "Input", tool)
            return tool.Name
        tool = _tool(comp, need(args, "tool"))
        if action == "delete":
            name = tool.Name
            tool.Delete()
            return name
        if action == "bypass":
            tool.SetAttrs({"TOOLB_PassThrough": True})
        else:
            tool.SetAttrs({"TOOLB_PassThrough": False})
        return tool.Name
    name = _fusion_write(comp, "bridge fusion-tool %s" % action, apply)
    tools = [{"name": t.Name, "regid": t.ID} for t in _tools(comp)]
    out(args, {"item": item.GetName(), "track": r, "action": action, "tool": name, "tools": tools, "ok": True},
        ["%s: %s %s; tools now %s" % (item.GetName(), action, name, [t["name"] for t in tools])], warnings)


def cmd_set_title(args, resolve):
    """The Text+ shortcut: find the TextPlus tool in the item's comp and set
    text / font / style / size / colour / center / justification in one
    Lock/Undo block. Verified input ids (2026-09-01): StyledText, Font,
    Style, Size, Red1/Green1/Blue1/Alpha1, Center, HorizontalJustificationNew,
    VerticalJustificationNew, Enabled2 + Thickness2 (outline)."""
    guard(args, "set a Text+ title")
    warnings = []
    tl = select_timeline(args, require_project(resolve))
    item, r = find_item(tl, need(args, "item"), warnings)
    comp = _comp(item, getattr(args, "comp", None))
    tools = _tools(comp, "TextPlus")
    if not tools:
        sys.exit("ERROR: no TextPlus tool in %s's comp (insert-generator 'Text+' --kind fusion-title first)"
                 % item.GetName())
    tp = tools[0]
    values = {}
    if getattr(args, "text", None) is not None:
        values["StyledText"] = args.text
    if getattr(args, "font", None):
        values["Font"] = args.font
    if getattr(args, "style", None):
        values["Style"] = args.style
    if getattr(args, "size", None) is not None:
        values["Size"] = float(args.size)
    if getattr(args, "color", None):
        parts = [float(p) for p in str(args.color).split(",")]
        if len(parts) not in (3, 4):
            sys.exit("ERROR: --color must be r,g,b[,a] in 0..1")
        values.update({"Red1": parts[0], "Green1": parts[1], "Blue1": parts[2]})
        if len(parts) == 4:
            values["Alpha1"] = parts[3]
    if getattr(args, "center", None):
        x, y = [float(p) for p in str(args.center).split(",")]
        values["Center"] = {1: x, 2: y}
    if getattr(args, "h_justify", None) is not None:
        values["HorizontalJustificationNew"] = int(args.h_justify)
    if getattr(args, "v_justify", None) is not None:
        values["VerticalJustificationNew"] = int(args.v_justify)
    if getattr(args, "outline", None) is not None:
        values["Enabled2"] = 1
        values["Thickness2"] = float(args.outline)
    extra = jsonval(getattr(args, "inputs", None), "inputs") or {}
    values.update(extra)
    if not values:
        sys.exit("ERROR: nothing to set")

    def apply():
        for iid, value in values.items():
            tp.SetInput(iid, value)
    _fusion_write(comp, "bridge set-title", apply)
    readback = {iid: tp.GetInput(iid) for iid in values}
    out(args, {"item": item.GetName(), "track": r, "tool": tp.Name, "set": values, "readback": readback, "ok": True},
        ["%s/%s: %s" % (item.GetName(), tp.Name, json.dumps(readback, default=str))], warnings)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_TL = arg("timeline", help="named timeline (never switches the GUI); default: current")
_ITEM = arg("item", help="timeline item: 'V1:3', unique id, or name")
_ITEMS = arg("items", "list", positional=True, required=True, help="timeline item refs")
_COMP = arg("comp", "int", help="Fusion comp index on the item (default 1)")

cmd("color-info", cmd_color_info, False,
    "grade versions, group and node graph (labels, LUT per node, tools) of an item -- no lift/gamma/gain readback exists",
    [_ITEM, _TL])
cmd("timeline-graph", cmd_timeline_graph, False, "timeline-level node graph", [_TL])
cmd("color-version", cmd_color_version, True, "add / delete / load / rename a grade version (local or remote)",
    [arg("action", positional=True, required=True, choices=["add", "delete", "load", "rename"]),
     arg("name", positional=True, required=True), _ITEM, arg("new-name"),
     arg("type", choices=["local", "remote"]), _TL])
cmd("apply-lut", cmd_apply_lut, True, "Graph.SetLUT on a node of each item (LUT must be discovered: refresh-luts)",
    [_ITEMS, arg("lut", required=True, help="path under the LUT folder, or absolute"), arg("node", "int"), _TL])
cmd("apply-cdl", cmd_apply_cdl, True, "SetCDL on a node: --slope 'r g b' --offset 'r g b' --power 'r g b' --saturation s",
    [_ITEMS, arg("node", "int"), arg("slope"), arg("offset"), arg("power"), arg("saturation", "float"), _TL])
cmd("apply-drx", cmd_apply_drx, True, "ApplyGradeFromDRX (REPLACES the node graph); --mode none|source|start",
    [_ITEMS, arg("path", required=True), arg("mode", choices=["none", "source", "start"]), _TL])
cmd("copy-grade", cmd_copy_grade, True, "CopyGrades from --source item to items",
    [_ITEMS, arg("source", required=True, help="source item ref"), _TL])
cmd("set-node", cmd_set_node, True, "node enable/cache, ResetAllGrades, ResetAllNodeColors, ApplyArriCdlLut",
    [_ITEM, arg("node", "int"), arg("enabled", choices=TRI), arg("cache", choices=["auto", "off", "on"]),
     arg("reset-all", "bool"), arg("reset-colors", "bool"), arg("arri-cdl", "bool"), _TL])
cmd("export-lut", cmd_export_lut, True, "ExportLUT of an item's grade (--cube 17|33|65|vlut)",
    [_ITEM, arg("path", required=True), arg("cube", choices=["17", "33", "65", "vlut"]), _TL])
cmd("refresh-luts", cmd_refresh_luts, True, "RefreshLUTList after copying a .cube into the LUT folder")
cmd("color-groups", cmd_color_groups, False, "color groups with their clips (current timeline) and node counts")
cmd("color-group", cmd_color_group, True, "create / delete / rename / assign / remove a color group",
    [arg("action", positional=True, required=True, choices=["create", "delete", "rename", "assign", "remove"]),
     arg("name", positional=True, required=True), arg("new-name"), arg("items", "list", help="item refs (assign/remove)"), _TL])
cmd("gallery", cmd_gallery, False, "gallery still and PowerGrade albums with still counts")
cmd("gallery-album", cmd_gallery_album, True,
    "create / create-powergrade / set-current / rename / import (--paths) / export (--path --prefix --format) / delete-stills",
    [arg("action", positional=True, required=True,
         choices=["create", "create-powergrade", "set-current", "rename", "import", "export", "delete-stills"]),
     arg("name"), arg("new-name"), arg("paths", "list", help="still files to import"),
     arg("path", help="export folder"), arg("prefix"), arg("format", choices=["dpx", "cin", "tif", "jpg", "png", "ppm", "bmp", "xpm", "drx"])])
cmd("fusion-tools", cmd_fusion_tools, False, "tools in an item's Fusion comp (+ input values with --inputs / --all-inputs)",
    [_ITEM, _COMP, arg("regid", help="filter by RegID, e.g. TextPlus"),
     arg("read-inputs", "list", help="input ids to read on each tool"), arg("all-inputs", "bool"), _TL])
cmd("fusion-set", cmd_fusion_set, True, "SetInput values on a tool (Lock/StartUndo wrapped) with readback",
    [_ITEM, _COMP, arg("tool", required=True, help="tool name or RegID"),
     arg("inputs", "json", required=True, help='{"StyledText": "...", "Size": 0.1, "Center": [0.5, 0.7]}'),
     arg("time", "int", help="comp-local frame for an animated input"), _TL])
cmd("fusion-keyframes", cmd_fusion_keyframes, True, "BezierSpline keyframes on one input (--keys {frame: value}) or --clear",
    [_ITEM, _COMP, arg("tool", required=True), arg("input", required=True), arg("keys", "json", required=True),
     arg("clear", "bool"), _TL])
cmd("fusion-tool", cmd_fusion_tool, True, "add / delete / bypass / unbypass a tool in the comp",
    [arg("action", positional=True, required=True, choices=["add", "delete", "bypass", "unbypass"]),
     _ITEM, _COMP, arg("tool", help="existing tool (delete/bypass)"), arg("regid", help="RegID to add"),
     arg("name", help="TOOLS_Name for the new tool"), arg("connect-from", help="tool feeding the new tool"),
     arg("input", help="input id on the new tool (default Input)"), arg("connect-to", help="tool the new tool feeds"),
     arg("to-input", help="input id on --connect-to (default Input)"), _TL])
cmd("set-title", cmd_set_title, True, "Text+ shortcut: --text --font --style --size --color r,g,b --center x,y --outline w",
    [_ITEM, _COMP, arg("text"), arg("font"), arg("style"), arg("size", "float"), arg("color"),
     arg("center"), arg("h-justify", "int"), arg("v-justify", "int"), arg("outline", "float"),
     arg("inputs", "json", help="extra input id -> value"), _TL])
