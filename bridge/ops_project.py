"""ops_project -- project manager, database, folders, project settings/presets,
media storage and clone-tool commands (Resolve 21.1 API).

Absorb-manifest rows 1-8. Every write is --yes guarded and listed in
resolve_cli.WRITE_COMMANDS; reads only call Get*/Is*/Has*.
"""

import json
import os
import sys

from ops_common import (arg, cmd, out, api_list, require_project, guard, jsonval,
                        need, check)


# ---------------------------------------------------------------------------
# Database + project-manager folders (rows 2, 3)
# ---------------------------------------------------------------------------

def cmd_database(args, resolve):
    warnings = []
    pm = resolve.GetProjectManager()
    current = pm.GetCurrentDatabase()
    if current is None:
        warnings.append("GetCurrentDatabase returned None")
    dbs = api_list(pm.GetDatabaseList(), "GetDatabaseList", warnings)
    out(args, {"current": current, "databases": dbs},
        ["current: %s" % json.dumps(current, default=str)] +
        ["  %s" % json.dumps(d, default=str) for d in dbs], warnings)


def cmd_set_database(args, resolve):
    """SetCurrentDatabase CLOSES the open project -- say so before doing it."""
    guard(args, "switch the project database (this closes the open project)")
    pm = resolve.GetProjectManager()
    info = {"DbType": need(args, "type"), "DbName": need(args, "name")}
    if getattr(args, "ip", None):
        info["IpAddress"] = args.ip
    current = pm.GetCurrentProject()
    warnings = []
    if current is not None:
        warnings.append("project %r was open and is closed by the database switch" % current.GetName())
    check(pm.SetCurrentDatabase(info), "SetCurrentDatabase(%s)" % json.dumps(info))
    out(args, {"database": pm.GetCurrentDatabase(), "ok": True},
        ["database is now: %s" % json.dumps(pm.GetCurrentDatabase(), default=str)], warnings)


def cmd_folders(args, resolve):
    warnings = []
    pm = resolve.GetProjectManager()
    folders = api_list(pm.GetFolderListInCurrentFolder(), "GetFolderListInCurrentFolder", warnings)
    projects = api_list(pm.GetProjectListInCurrentFolder(), "GetProjectListInCurrentFolder", warnings)
    current = pm.GetCurrentFolder()
    out(args, {"current": current or "", "folders": folders, "projects": projects},
        ["folder: %s" % (current or "(root)")] +
        ["  [dir] %s" % f for f in folders] + ["  %s" % p for p in projects], warnings)


def cmd_folder(args, resolve):
    """Project-manager folder navigation and lifecycle in one write command."""
    action = need(args, "action")
    name = getattr(args, "name", None)
    guard(args, "%s project-manager folder %s" % (action, name or ""))
    pm = resolve.GetProjectManager()
    if action == "create":
        check(pm.CreateFolder(need(args, "name")), "CreateFolder(%r)" % name)
    elif action == "delete":
        check(pm.DeleteFolder(need(args, "name")), "DeleteFolder(%r)" % name)
    elif action == "open":
        check(pm.OpenFolder(need(args, "name")), "OpenFolder(%r)" % name)
    elif action == "root":
        check(pm.GotoRootFolder(), "GotoRootFolder()")
    elif action == "parent":
        check(pm.GotoParentFolder(), "GotoParentFolder()")
    out(args, {"action": action, "name": name, "current": pm.GetCurrentFolder() or "", "ok": True},
        ["%s ok; current folder: %s" % (action, pm.GetCurrentFolder() or "(root)")])


# ---------------------------------------------------------------------------
# Project lifecycle beyond create/load/delete (rows 1, 4, 5)
# ---------------------------------------------------------------------------

def cmd_save_project(args, resolve):
    guard(args, "save the current project")
    pm = resolve.GetProjectManager()
    project = require_project(resolve)
    check(pm.SaveProject(), "SaveProject()")
    out(args, {"saved": project.GetName(), "ok": True}, ["saved project: %s" % project.GetName()])


def cmd_close_project(args, resolve):
    guard(args, "close the current project (unsaved changes are NOT saved)")
    pm = resolve.GetProjectManager()
    project = require_project(resolve)
    name = project.GetName()
    if getattr(args, "save", False):
        check(pm.SaveProject(), "SaveProject()")
    check(pm.CloseProject(project), "CloseProject(%r)" % name)
    out(args, {"closed": name, "saved": bool(getattr(args, "save", False)), "ok": True},
        ["closed project: %s" % name])


def cmd_rename_project(args, resolve):
    guard(args, "rename the current project")
    project = require_project(resolve)
    old = project.GetName()
    check(project.SetName(need(args, "name")), "Project.SetName(%r)" % args.name)
    out(args, {"renamed": old, "to": project.GetName(), "ok": True},
        ["renamed %s -> %s" % (old, project.GetName())])


def cmd_export_project(args, resolve):
    guard(args, "export project %r to %r" % (getattr(args, "name", None), getattr(args, "path", None)))
    pm = resolve.GetProjectManager()
    name, path = need(args, "name"), need(args, "path")
    check(pm.ExportProject(name, path, not getattr(args, "no_stills", False)),
          "ExportProject(%r, %r)" % (name, path))
    out(args, {"exported": name, "path": path, "ok": True}, ["exported %s -> %s" % (name, path)])


def cmd_import_project(args, resolve):
    guard(args, "import project from %r" % getattr(args, "path", None))
    pm = resolve.GetProjectManager()
    path = need(args, "path")
    name = getattr(args, "name", None)
    ok = pm.ImportProject(path, name) if name else pm.ImportProject(path)
    check(ok, "ImportProject(%r)" % path)
    out(args, {"imported": path, "name": name, "ok": True}, ["imported project from %s" % path])


def cmd_archive_project(args, resolve):
    """REFUSED. Measured 2026-09-09 on Studio 21.1.0.14 (Windows 11, GUI instance, Disk
    database): ProjectManager.ArchiveProject(<loaded project>, '<dir>/x.dra')
    stalled the scripting API and then CRASHED Resolve outright ("Problem Report
    for DaVinci Resolve"). On 20.3 it merely returned False (reference 08 #32).
    Until a Resolve release fixes it, this command documents the gap instead of
    calling it; archive from the GUI, or export-project (.drp) + import-project."""
    guard(args, "archive project %r" % getattr(args, "name", None))
    name, path = need(args, "name"), need(args, "path")
    sys.exit("ERROR: archive-project is disabled: ProjectManager.ArchiveProject crashed Resolve "
             "21.1.0.14 when called from a script (measured 2026-09-09; 20.3 returned False). "
             "Archive %r from the GUI (File > Project Manager > Archive), or use export-project "
             "(.drp) + import-project. Requested path: %s" % (name, path))


def cmd_restore_project(args, resolve):
    guard(args, "restore project archive %r" % getattr(args, "path", None))
    pm = resolve.GetProjectManager()
    path = need(args, "path")
    name = getattr(args, "name", None)
    ok = pm.RestoreProject(path, name) if name else pm.RestoreProject(path)
    if not ok:
        sys.exit("ERROR: RestoreProject(%r) returned %r. It expects a .dra archive; for a .drp "
                 "use import-project." % (path, ok))
    out(args, {"restored": path, "name": name, "ok": True}, ["restored %s" % path])


def cmd_cloud_project(args, resolve):
    """Blackmagic Cloud projects. NOT verified live (no cloud library on our
    boxes); the README rules are encoded: projectMediaPath is always required,
    projectName for create/load."""
    action = need(args, "action")
    guard(args, "%s a cloud project" % action)
    pm = resolve.GetProjectManager()
    settings = jsonval(getattr(args, "settings", None), "settings") or {}
    if getattr(args, "name", None):
        settings["projectName"] = args.name
    if getattr(args, "media_path", None):
        settings["projectMediaPath"] = args.media_path
    if "projectMediaPath" not in settings:
        sys.exit("ERROR: projectMediaPath is required for every cloud-project action "
                 "(--media-path or --settings)")
    if action in ("create", "load") and "projectName" not in settings:
        sys.exit("ERROR: projectName is required for %s (--name)" % action)
    if action == "create":
        result = pm.CreateCloudProject(settings)
    elif action == "load":
        result = pm.LoadCloudProject(settings)
    elif action == "import":
        result = pm.ImportCloudProject(need(args, "path"), settings)
    else:
        result = pm.RestoreCloudProject(need(args, "path"), settings)
    if not result:
        sys.exit("ERROR: cloud-project %s returned %r (not signed in to Blackmagic Cloud, or "
                 "invalid settings)." % (action, result))
    name = result.GetName() if hasattr(result, "GetName") else None
    out(args, {"action": action, "project": name, "ok": True},
        ["cloud-project %s ok%s" % (action, (": " + name) if name else "")])


# ---------------------------------------------------------------------------
# Project settings + settings presets (row 6)
# ---------------------------------------------------------------------------

def cmd_project_settings(args, resolve):
    warnings = []
    project = require_project(resolve)
    key = getattr(args, "key", None)
    if key:
        value = project.GetSetting(key)
        if value is None:
            warnings.append("GetSetting(%r) returned None (unknown key or failed)" % key)
        out(args, {"key": key, "value": value}, ["%s = %s" % (key, value)], warnings)
        return
    settings = project.GetSettings()
    if settings is None:
        warnings.append("GetSettings returned None")
        settings = {}
    out(args, {"project": project.GetName(), "count": len(settings), "settings": settings},
        ["%d setting(s) in %r:" % (len(settings), project.GetName())] +
        ["  %s = %s" % (k, v) for k, v in sorted(settings.items())], warnings)


def _apply_settings(target, settings, what):
    """One key per call, useCustomSettings first (the README says Resolve
    applies it first itself; we make the order explicit and report per key).
    Returns (applied, failed)."""
    applied, failed = [], []
    ordered = list(settings.items())
    ordered.sort(key=lambda kv: 0 if kv[0] == "useCustomSettings" else 1)
    for key, value in ordered:
        if isinstance(value, bool):
            value = "1" if value else "0"
        elif isinstance(value, (int, float)):
            value = str(value)
        ok = target.SetSetting(key, value)
        (applied if ok else failed).append(key)
    return applied, failed


def cmd_set_project_settings(args, resolve):
    guard(args, "change project settings")
    project = require_project(resolve)
    settings = jsonval(need(args, "settings"), "settings")
    if not isinstance(settings, dict) or not settings:
        sys.exit("ERROR: --settings must be a non-empty JSON object of key -> value")
    applied, failed = _apply_settings(project, settings, "Project")
    readback = {k: project.GetSetting(k) for k in settings}
    data = {"applied": applied, "failed": failed, "readback": readback, "ok": not failed}
    lines = ["applied %d, failed %d" % (len(applied), len(failed))] + \
            ["  %s %s = %s" % ("ok " if k in applied else "FAIL", k, readback.get(k)) for k in settings]
    out(args, data, lines)
    if failed:
        sys.exit(1)


def cmd_project_settings_presets(args, resolve):
    warnings = []
    project = require_project(resolve)
    presets = api_list(project.GetProjectSettingsPresetList(), "GetProjectSettingsPresetList", warnings)
    out(args, {"presets": presets},
        ["%d project-settings preset(s):" % len(presets)] +
        ["  %s" % json.dumps(p, default=str) for p in presets], warnings)


def cmd_project_settings_preset(args, resolve):
    action = need(args, "action")
    name = need(args, "name")
    guard(args, "%s project-settings preset %r" % (action, name))
    project = require_project(resolve)
    path = getattr(args, "path", None)
    if action == "apply":
        ok = project.SetProjectSettingsPreset(name)
    elif action == "save":
        ok = project.SaveCurrentProjectSettingsAsNewPreset(name)
    elif action == "update":
        ok = project.UpdateProjectSettingsPreset(name)
    elif action == "delete":
        ok = project.DeleteProjectSettingsPreset(name)
    elif action == "export":
        ok = project.ExportProjectSettingsPreset(name, need(args, "path"))
    else:  # import: path is the file, name the preset name it gets
        ok = project.ImportProjectSettingsPreset(need(args, "path"), name)
    check(ok, "project-settings preset %s(%r)" % (action, name))
    out(args, {"action": action, "name": name, "path": path, "ok": True},
        ["project-settings preset %s: %s" % (action, name)])


# ---------------------------------------------------------------------------
# Media storage (rows 7, 8) + clone tool (21.1)
# ---------------------------------------------------------------------------

def cmd_storage_volumes(args, resolve):
    warnings = []
    vols = api_list(resolve.GetMediaStorage().GetMountedVolumeList(), "GetMountedVolumeList", warnings)
    out(args, {"volumes": vols}, ["%d volume(s):" % len(vols)] + ["  %s" % v for v in vols], warnings)


def cmd_storage_browse(args, resolve):
    warnings = []
    ms = resolve.GetMediaStorage()
    # MediaStorage wants the platform's own separators (forward slashes on
    # Windows answer an empty list, measured 2026-09-09 on 21.1).
    path = os.path.normpath(need(args, "path"))
    folders = api_list(ms.GetSubFolderList(path), "GetSubFolderList(%r)" % path, warnings)
    files = api_list(ms.GetFileList(path), "GetFileList(%r)" % path, warnings) \
        if getattr(args, "files", False) else []
    out(args, {"path": path, "folders": folders, "files": files},
        ["%s: %d folder(s), %d file(s)" % (path, len(folders), len(files))] +
        ["  [dir] %s" % f for f in folders] + ["  %s" % f for f in files], warnings)


def cmd_storage_import(args, resolve):
    """MediaStorage.AddItemListToMediaPool (with optional subclip range), or
    matte imports when --matte-for / --timeline-mattes is given."""
    guard(args, "import from media storage into the media pool")
    ms = resolve.GetMediaStorage()
    project = require_project(resolve)
    paths = need(args, "paths")
    warnings = []
    if getattr(args, "timeline_mattes", False):
        added = ms.AddTimelineMattesToMediaPool(list(paths))
        what = "AddTimelineMattesToMediaPool"
    elif getattr(args, "matte_for", None):
        from ops_common import find_clip
        clip = find_clip(project.GetMediaPool(), args.matte_for, warnings, getattr(args, "bin", None))
        eye = getattr(args, "stereo_eye", None)
        added = ms.AddClipMattesToMediaPool(clip, list(paths), eye) if eye \
            else ms.AddClipMattesToMediaPool(clip, list(paths))
        what = "AddClipMattesToMediaPool"
        if added is False:
            sys.exit("ERROR: %s returned False" % what)
        out(args, {"matte_for": clip.GetName(), "paths": list(paths), "ok": True},
            ["added %d matte(s) to %s" % (len(paths), clip.GetName())], warnings)
        return
    else:
        infos = []
        for p in paths:
            info = {"media": p}
            if getattr(args, "start_frame", None) is not None:
                info["startFrame"] = int(args.start_frame)
            if getattr(args, "end_frame", None) is not None:
                info["endFrame"] = int(args.end_frame)
            infos.append(info)
        added = ms.AddItemListToMediaPool(infos)
        what = "AddItemListToMediaPool"
    if added is None:
        sys.exit("ERROR: %s returned None -- the call failed or nothing was importable: %s"
                 % (what, ", ".join(paths)))
    names = [c.GetName() for c in added] if isinstance(added, list) else []
    if isinstance(added, list) and len(names) < len(paths):
        warnings.append("requested %d path(s), %d clip(s) came back" % (len(paths), len(names)))
    out(args, {"imported": names, "requested": list(paths), "ok": True},
        ["imported %d clip(s):" % len(names)] + ["  %s" % n for n in names], warnings)


def cmd_clone_status(args, resolve):
    status = resolve.GetMediaStorage().GetCloneStatus()
    warnings = []
    if status is None:
        warnings.append("GetCloneStatus returned None")
    out(args, {"status": status}, ["clone: %s" % json.dumps(status, default=str)], warnings)


def cmd_clone_media(args, resolve):
    """21.1 clone tool: SetCloneToolSettings then StartCloneMedia. Poll with
    clone-status; stop with --stop."""
    if getattr(args, "stop", False):
        guard(args, "stop the running media clone")
        ms = resolve.GetMediaStorage()
        check(ms.StopCloneMedia(), "StopCloneMedia()")
        out(args, {"stopped": True, "status": ms.GetCloneStatus()}, ["clone stopped"])
        return
    source = os.path.normpath(need(args, "source"))
    targets = [os.path.normpath(t) for t in need(args, "targets")]  # MediaStorage wants backslashes
    guard(args, "clone %s -> %s" % (source, ", ".join(targets)))
    ms = resolve.GetMediaStorage()
    settings = {}
    if getattr(args, "preserve_folder_name", False):
        settings["PreserveFolderName"] = True
    checksum = getattr(args, "checksum", None)
    if checksum:
        settings["ChecksumType"] = {
            "none": resolve.CLONE_CHECKSUM_TYPE_NONE, "filesize": resolve.CLONE_CHECKSUM_TYPE_FILESIZE,
            "crc32": resolve.CLONE_CHECKSUM_TYPE_CRC32, "md5": resolve.CLONE_CHECKSUM_TYPE_MD5,
            "sha256": resolve.CLONE_CHECKSUM_TYPE_SHA256, "sha512": resolve.CLONE_CHECKSUM_TYPE_SHA512,
            "xxh64": resolve.CLONE_CHECKSUM_TYPE_XXH_64}[checksum]
    if settings:
        check(ms.SetCloneToolSettings(settings), "SetCloneToolSettings(%s)" % json.dumps(settings))
    check(ms.StartCloneMedia(source, list(targets)), "StartCloneMedia(%r, %r)" % (source, targets))
    out(args, {"source": source, "targets": list(targets), "settings": settings,
               "status": ms.GetCloneStatus(), "ok": True},
        ["clone started: %s -> %s" % (source, ", ".join(targets))])


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_A_NAME = arg("name", positional=True, help="name")

cmd("database", cmd_database, False, "current project database and the list of databases")
cmd("set-database", cmd_set_database, True,
    "switch the project database (Disk or PostgreSQL); CLOSES the open project",
    [arg("type", choices=["Disk", "PostgreSQL"], required=True, help="DbType"),
     arg("name", required=True, help="DbName"),
     arg("ip", help="IpAddress for PostgreSQL")])
cmd("folders", cmd_folders, False, "project-manager folder listing (folders + projects in the current folder)")
cmd("folder", cmd_folder, True, "project-manager folder navigation: create/delete/open/root/parent",
    [arg("action", positional=True, choices=["create", "delete", "open", "root", "parent"], required=True),
     arg("name", help="folder name (create/delete/open)")])
cmd("save-project", cmd_save_project, True, "SaveProject on the current project")
cmd("close-project", cmd_close_project, True, "close the current project (no save unless --save)",
    [arg("save", "bool", help="save before closing")])
cmd("rename-project", cmd_rename_project, True, "rename the current project", [_A_NAME])
cmd("export-project", cmd_export_project, True, "export a project to a .drp file",
    [_A_NAME, arg("path", required=True, help="output .drp path"),
     arg("no-stills", "bool", help="exclude stills and LUTs")])
cmd("import-project", cmd_import_project, True, "import a .drp project file",
    [arg("path", required=True, help=".drp path"), arg("name", help="project name to import as")])
cmd("archive-project", cmd_archive_project, True, "REFUSED on purpose: ProjectManager.ArchiveProject crashed Resolve 21.1 from a script (measured 2026-09-09); use export-project or the GUI",
    [_A_NAME, arg("path", required=True, help="output .dra path"),
     arg("no-source-media", "bool"), arg("no-render-cache", "bool"), arg("proxy-media", "bool")])
cmd("restore-project", cmd_restore_project, True, "RestoreProject from a .dra archive",
    [arg("path", required=True, help=".dra path"), arg("name")])
cmd("cloud-project", cmd_cloud_project, True, "Blackmagic Cloud project create/load/import/restore (unverified: no cloud library here)",
    [arg("action", positional=True, choices=["create", "load", "import", "restore"], required=True),
     arg("name", help="projectName"), arg("media-path", help="projectMediaPath (required)"),
     arg("path", help="file (import) or folder (restore)"),
     arg("settings", "json", help="extra CloudSettings JSON: isCollab, syncMode, isCameraAccess")])
cmd("project-settings", cmd_project_settings, False, "all 158 project settings, or one key",
    [arg("key", help="a single setting key")])
cmd("set-project-settings", cmd_set_project_settings, True,
    "set project settings from JSON; one key per call, per-key result, read back",
    [arg("settings", "json", required=True, help='{"timelineFrameRate": "29.97", ...}')])
cmd("project-settings-presets", cmd_project_settings_presets, False, "project-settings presets (name, width, height)")
cmd("project-settings-preset", cmd_project_settings_preset, True,
    "apply/save/update/delete/export/import a project-settings preset",
    [arg("action", positional=True, choices=["apply", "save", "update", "delete", "export", "import"], required=True),
     arg("name", positional=True, required=True, help="preset name"),
     arg("path", help="file path for export/import")])
cmd("storage-volumes", cmd_storage_volumes, False, "media storage mounted volumes")
cmd("storage-browse", cmd_storage_browse, False, "media storage folders (and files with --files) under a path",
    [arg("path", positional=True, required=True), arg("files", "bool", help="include files")])
cmd("storage-import", cmd_storage_import, True,
    "import storage paths into the current pool folder (subclip range, clip mattes, timeline mattes)",
    [arg("paths", "list", positional=True, required=True, help="storage paths"),
     arg("start-frame", "int"), arg("end-frame", "int"),
     arg("matte-for", help="clip ref: add these as clip mattes"), arg("bin", help="scope --matte-for lookup"),
     arg("stereo-eye", choices=["left", "right"]),
     arg("timeline-mattes", "bool", help="add as timeline mattes")])
cmd("clone-status", cmd_clone_status, False, "media clone job status (21.1)")
cmd("clone-media", cmd_clone_media, True, "start (or --stop) a verified media clone (21.1 clone tool)",
    [arg("source", help="source directory"), arg("targets", "list", help="target directories"),
     arg("checksum", choices=["none", "filesize", "crc32", "md5", "sha256", "sha512", "xxh64"]),
     arg("preserve-folder-name", "bool"), arg("stop", "bool", help="stop the running clone")])
