# DaVinci Resolve CLI

**Every documented DaVinci Resolve scripting operation, plus the cross-project history and doctor diagnostics that Resolve itself — and every other Resolve CLI or MCP server — throws away.**

Fifteen-plus Resolve MCP servers and CLIs already wrap the same official scripting API. None of them persist anything: not render outcomes, not marker notes, not which grade version a timeline is on, not why yesterday's automated run failed. This CLI does, offline, searchable, and typed — on top of the already-proven bridge in this repository instead of a 16th independent reimplementation of the same known failure modes (page-null, modal-stall, per-machine Python ABI mismatch).

## Install

This CLI is a client for the Python bridge in [`../bridge`](../bridge) — install and start that first
(see the [repository README](../README.md)). Then install the CLI with Go (1.26 or newer):

```bash
go install github.com/dmmdea/davinci-resolve-cli/cli/cmd/davinci-resolve-pp-cli@latest
```

Or build from a clone of this repository:

```bash
cd cli
go build -o bin/ ./cmd/davinci-resolve-pp-cli
```

The CLI talks to the bridge's HTTP sidecar at `http://127.0.0.1:18800`
(`resolve.cmd serve --port 18800` on the machine running Resolve). To drive a Resolve on another
machine, forward the port over SSH (`ssh -L 18800:127.0.0.1:18800 user@editing-pc`).

## Use with Claude Desktop / Claude Code (MCP)

The same commands are exposed as an MCP server:

```bash
go install github.com/dmmdea/davinci-resolve-cli/cli/cmd/davinci-resolve-pp-mcp@latest
claude mcp add davinci-resolve-pp-mcp -- davinci-resolve-pp-mcp      # Claude Code
```

Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "davinci-resolve": {
      "command": "davinci-resolve-pp-mcp"
    }
  }
}
```

## Authentication

No cloud auth. DaVinci Resolve Studio must be running, licensed, with External Scripting set to Local. `doctor` checks every one of these preconditions by name instead of a silent hang or a bare False.

## Quick Start

```bash
# Verify-safe health check — confirms the sidecar and Resolve preconditions without touching anything
davinci-resolve-pp-cli doctor --dry-run

# See what page Resolve is on and whether writes will currently work
davinci-resolve-pp-cli system status --json

# See what's in the project database
davinci-resolve-pp-cli project list --json

# Preview a spec-driven timeline build before actually running it
davinci-resolve-pp-cli timeline build --spec edit.json --dry-run

# See what has actually rendered and been verified across every project
davinci-resolve-pp-cli render history --json --select project,file,verified

```

## Unique Features

These capabilities aren't available in any other tool for this API.

### Local run history
- **`runs list`** — See exactly which commands this CLI has run, whether each succeeded, and whether Resolve was too busy and had to be retried.

  _Reach for this first when diagnosing any pipeline hiccup instead of re-deriving state from a terminal scrollback._

  ```bash
  davinci-resolve-pp-cli runs list --json --select id,command,status,exit_code
  ```

### Cross-project marker index
- **`marker search`** — Find a marker by text across every project this CLI has touched, not just the one currently open in Resolve.

  _Use when the user references a marker/note from an unspecified or past project._

  ```bash
  davinci-resolve-pp-cli marker search "pickup shot" --json
  ```

### Render delivery history
- **`render history`** — See every render this CLI has ever queued across all projects, with the actual output file verified against the requested spec.

  _Use for any delivery-audit or 'did this actually render correctly' question spanning more than the current session._

  ```bash
  davinci-resolve-pp-cli render history --project "Client Delivery Q3" --json
  ```

### Grade-version audit
- **`color audit`** — See which timelines are still on an old color grade version across your whole project library, not just the one open in Resolve.

  _Use before a client delivery to confirm every episode/timeline carries the latest approved grade._

  ```bash
  davinci-resolve-pp-cli color audit --json --select project,timeline,current_version
  ```

### Title text catalog
- **`fusion titles search`** — Find what text was used in a past Text+/title, across every project, without reopening it.

  _Use when the user asks what a past title/lower-third said instead of reopening that project._

  ```bash
  davinci-resolve-pp-cli fusion titles search "episode 12" --json
  ```

### Timeline operation trail
- **`timeline log`** — See the full append/delete history for one timeline — the only record of what actually changed, since Resolve has no undo API.

  _Use to audit or explain what an automated build/edit pass actually did to a timeline._

  ```bash
  davinci-resolve-pp-cli timeline log tl_042 --json
  ```

### Environment drift history
- **`doctor history`** — See whether this machine's Resolve/Python environment has drifted since yesterday, not just whether it's healthy right now.

  _Use first when an unattended pipeline starts failing intermittently on one machine but not another._

  ```bash
  davinci-resolve-pp-cli doctor history --json
  ```

## Recipes

### Build a rough cut from a spec and verify placement

```bash
davinci-resolve-pp-cli timeline build --spec edit.json --json --yes --timeout 180
```

Ingests media, creates the timeline, appends every clip with resolved geometry, and reads back start/duration to confirm placement.

### Find what a past episode's lower-third said

```bash
davinci-resolve-pp-cli fusion titles search "welcome back" --json
```

Searches locally captured Text+ content across every project this CLI has touched.

### Audit render deliveries for a client, narrowed to the fields that matter

```bash
davinci-resolve-pp-cli render history --project "Client Q3" --agent --select project,file,codec,verified
```

Pairs --agent with --select so an agent gets only the verification-relevant fields from a potentially large render history.

### Confirm every episode carries the latest grade before delivery

```bash
davinci-resolve-pp-cli color audit --json
```

Cross-project join of observed grade-version events against the current timeline list.

### Diagnose an intermittent pipeline failure on one machine

```bash
davinci-resolve-pp-cli doctor history --json
```

Time-series of environment health checks, catching drift a single point-in-time doctor run would miss.

## Usage

Run `davinci-resolve-pp-cli --help` for the full command reference and flag list.

## Paths & environment variables

This CLI separates local files into four path kinds:

| Kind | Contents |
|------|----------|
| `config` | User-editable settings such as `config.toml` and saved profiles |
| `data` | Durable local data such as `data.db` |
| `state` | Runtime state such as persisted queries, jobs, and `teach.log` |
| `cache` | Regenerable HTTP/cache files |

Each kind resolves independently. The ladder is:

1. Per-kind env var: `DAVINCI_RESOLVE_CONFIG_DIR`, `DAVINCI_RESOLVE_DATA_DIR`, `DAVINCI_RESOLVE_STATE_DIR`, or `DAVINCI_RESOLVE_CACHE_DIR`
2. `--home <dir>` for this invocation
3. `DAVINCI_RESOLVE_HOME` for a flat relocated root
4. XDG env vars: `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`
5. Platform defaults matching existing installs

For containers and agent sandboxes, prefer a single relocated root:

```bash
export DAVINCI_RESOLVE_HOME=/srv/davinci-resolve
davinci-resolve-pp-cli doctor
```

Under `DAVINCI_RESOLVE_HOME=/srv/davinci-resolve`, the four dirs resolve to `/srv/davinci-resolve/config`, `/srv/davinci-resolve/data`, `/srv/davinci-resolve/state`, and `/srv/davinci-resolve/cache`.

MCP servers do not receive CLI flags from the host. Put relocation in the host `env` block:

```json
{
  "mcpServers": {
    "davinci-resolve": {
      "command": "davinci-resolve-pp-mcp",
      "env": {
        "DAVINCI_RESOLVE_HOME": "/srv/davinci-resolve"
      }
    }
  }
}
```

Precedence matters in fleets: an ambient per-kind variable such as `DAVINCI_RESOLVE_DATA_DIR` overrides an explicit `--home` for that kind. Use `DAVINCI_RESOLVE_HOME` or the per-kind variables for durable fleet relocation; treat `--home` as the weaker per-invocation lever.

Relocation is one-way. Unsetting `DAVINCI_RESOLVE_HOME` does not move files back to platform defaults, and `doctor` cannot find files left under a former root. Move the files manually before unsetting relocation variables.

Existing installs keep working because the platform-default rung matches the legacy layout. Run `davinci-resolve-pp-cli doctor --fail-on warn` to check path warnings in automation.

## Commands

### audio

text-to-speech and audio insertion

- **`davinci-resolve-pp-cli audio insert`** - InsertAudioToCurrentTrackAtPlayhead (Fairlight page)
- **`davinci-resolve-pp-cli audio tts`** - GenerateSpeech text-to-speech (needs the AI Speech Generator Extras pack; returns an error without it): --text --voice 'Female 1'|'Male 1'|'Custom Voice'

### clip

one media pool clip: properties, metadata, markers, flags, marks, proxies, AI passes, transcription

- **`davinci-resolve-pp-cli clip add-marker`** - add a marker to a media pool clip
- **`davinci-resolve-pp-cli clip ai`** - AI pass on a clip or bin: transcribe / clear-transcription / classify-audio / clear-classification / deblur / intellisearch / slate
- **`davinci-resolve-pp-cli clip color`** - set the clip color, or 'clear'
- **`davinci-resolve-pp-cli clip delete-marker`** - delete clip markers by --frame, --custom-data or --color/All
- **`davinci-resolve-pp-cli clip flag`** - add a flag (--add COLOR) or clear flags (--remove COLOR|All)
- **`davinci-resolve-pp-cli clip info`** - every clip property + metadata, flags, color, marks, markers, audio mapping
- **`davinci-resolve-pp-cli clip mark`** - set (--mark-in/--mark-out) or --clear clip mark in/out
- **`davinci-resolve-pp-cli clip markers`** - markers on a media pool clip (customData decoded)
- **`davinci-resolve-pp-cli clip monitor-growing`** - MonitorGrowingFile on a clip
- **`davinci-resolve-pp-cli clip proxy`** - link a proxy (--link PATH), full-res media (--full-res PATH) or --unlink
- **`davinci-resolve-pp-cli clip rename`** - rename a media pool clip
- **`davinci-resolve-pp-cli clip replace`** - ReplaceClip / --preserve-subclip
- **`davinci-resolve-pp-cli clip set-audio-mapping`** - 21-1 SetAudioMapping from JSON (track_mapping)
- **`davinci-resolve-pp-cli clip set-metadata`** - SetMetadata / --third-party SetThirdPartyMetadata from JSON
- **`davinci-resolve-pp-cli clip set-property`** - SetClipProperty (FPS, Alpha mode, Super Scale, ) with readback
- **`davinci-resolve-pp-cli clip transcription`** - 21-1 transcript readback (segments, speakers, word timing)

### color

grade versions, LUT/CDL/DRX, node graph, color groups, gallery

- **`davinci-resolve-pp-cli color apply-cdl`** - SetCDL on a node: --slope 'r g b' --offset 'r g b' --power 'r g b' --saturation s
- **`davinci-resolve-pp-cli color apply-drx`** - ApplyGradeFromDRX (REPLACES the node graph); --mode none|source|start
- **`davinci-resolve-pp-cli color apply-lut`** - Graph.SetLUT on a node of each item (LUT must be discovered: refresh-luts)
- **`davinci-resolve-pp-cli color copy-grade`** - CopyGrades from --source item to items
- **`davinci-resolve-pp-cli color export-lut`** - ExportLUT of an item's grade (--cube 17|33|65|vlut)
- **`davinci-resolve-pp-cli color gallery`** - gallery still and PowerGrade albums with still counts
- **`davinci-resolve-pp-cli color gallery-album`** - create / create-powergrade / set-current / rename / import (--paths) / export (--path --prefix --format) / delete-stills
- **`davinci-resolve-pp-cli color group`** - create / delete / rename / assign / remove a color group
- **`davinci-resolve-pp-cli color groups`** - color groups with their clips (current timeline) and node counts
- **`davinci-resolve-pp-cli color info`** - grade versions, group and node graph (labels, LUT per node, tools) of an item -- no lift/gamma/gain readback exists
- **`davinci-resolve-pp-cli color refresh-luts`** - RefreshLUTList after copying a .cube into the LUT folder
- **`davinci-resolve-pp-cli color set-node`** - node enable/cache, ResetAllGrades, ResetAllNodeColors, ApplyArriCdlLut
- **`davinci-resolve-pp-cli color timeline-graph`** - timeline-level node graph
- **`davinci-resolve-pp-cli color version`** - add / delete / load / rename a grade version (local or remote)

### fusion

Fusion tool scripting on an item's comp (Lock/Undo wrapped) and the Text+ shortcut

- **`davinci-resolve-pp-cli fusion keyframes`** - BezierSpline keyframes on one input (--keys {frame: value}) or --clear
- **`davinci-resolve-pp-cli fusion set`** - SetInput values on a tool (Lock/StartUndo wrapped) with readback
- **`davinci-resolve-pp-cli fusion set-title`** - Text+ shortcut: --text --font --style --size --color r,g,b --center x,y --outline w
- **`davinci-resolve-pp-cli fusion tool`** - add / delete / bypass / unbypass a tool in the comp
- **`davinci-resolve-pp-cli fusion tools`** - tools in an item's Fusion comp (+ input values with --inputs / --all-inputs)

### item

timeline items: properties, speed, fades, transitions, multicam, Fusion comps

- **`davinci-resolve-pp-cli item add-transition`** - 21-1 AddTransition on an item edge (consumes media handles: 'right' needs the outgoing clip's tail handle, 'left'/'center' also the incoming clip's head handle; give position + alignment + duration)
- **`davinci-resolve-pp-cli item delete`** - DeleteClips on items (--ripple closes the gap)
- **`davinci-resolve-pp-cli item fusion-comp`** - Fusion comps on an item: add / import / export / delete / load / rename
- **`davinci-resolve-pp-cli item fusion-comps`** - Fusion comps on an item (count + names)
- **`davinci-resolve-pp-cli item info`** - one item: properties, speed, fades, blanking, markers, comps, version, caches
- **`davinci-resolve-pp-cli item link`** - SetClipsLinked (or --unlink)
- **`davinci-resolve-pp-cli item list`** - every timeline item with ref (V1:3), type, geometry, source range, clip
- **`davinci-resolve-pp-cli item multicam`** - 21-1 multicam: create (from clips) / flatten / smart-switch (on an item)
- **`davinci-resolve-pp-cli item set`** - per-item edits: name, enabled, color, flags, speed (21-1), fades (21-1), blanking, caches, audio mapping, voice isolation
- **`davinci-resolve-pp-cli item set-properties`** - TimelineItemProperties (Pan/Tilt/Zoom/Crop/Opacity/CompositeMode/audio) -- STATIC values, no keyframes

### pool

media pool bins and clip lifecycle

- **`davinci-resolve-pp-cli pool bin`** - create (idempotent) / delete / set-current / move / refresh a bin
- **`davinci-resolve-pp-cli pool bins`** - media pool bin tree with clip counts
- **`davinci-resolve-pp-cli pool clips`** - media pool clips
- **`davinci-resolve-pp-cli pool delete-clips`** - delete media pool clips (--all-matches: every duplicate of a name/path)
- **`davinci-resolve-pp-cli pool import`** - import media into a bin, skipping paths already in the pool (idempotent)
- **`davinci-resolve-pp-cli pool import-paths`** - ImportMedia into the current pool folder
- **`davinci-resolve-pp-cli pool move-clips`** - move media pool clips to a bin
- **`davinci-resolve-pp-cli pool relink-clips`** - relink clips from a filesystem folder, or --unlink
- **`davinci-resolve-pp-cli pool select-clip`** - select a media pool clip (GUI selection)
- **`davinci-resolve-pp-cli pool selection`** - the editor's current selection
- **`davinci-resolve-pp-cli pool stereo-clip`** - create a stereo clip from left + right clips
- **`davinci-resolve-pp-cli pool sync-audio`** - AutoSyncAudio on video + audio clips (waveform or timecode)

### project

project manager, database, folders, project settings and settings presets

- **`davinci-resolve-pp-cli project archive`** - REFUSED on purpose: ProjectManager.ArchiveProject crashed Resolve 21-1 from a script (measured 2026-09-09); use export-project or the GUI
- **`davinci-resolve-pp-cli project close`** - close the current project (no save unless --save)
- **`davinci-resolve-pp-cli project cloud`** - Blackmagic Cloud project create/load/import/restore (unverified: no cloud library here)
- **`davinci-resolve-pp-cli project create`** - create AND load a project
- **`davinci-resolve-pp-cli project database`** - current project database and the list of databases
- **`davinci-resolve-pp-cli project delete`** - delete a project that is not loaded
- **`davinci-resolve-pp-cli project export`** - export a project to a .drp file
- **`davinci-resolve-pp-cli project folder`** - project-manager folder navigation: create/delete/open/root/parent
- **`davinci-resolve-pp-cli project folders`** - project-manager folder listing (folders + projects in the current folder)
- **`davinci-resolve-pp-cli project import`** - import a .drp project file
- **`davinci-resolve-pp-cli project list`** - projects in the current database folder
- **`davinci-resolve-pp-cli project load`** - load a project (the page-null escape hatch)
- **`davinci-resolve-pp-cli project rename`** - rename the current project
- **`davinci-resolve-pp-cli project restore`** - RestoreProject from a .dra archive
- **`davinci-resolve-pp-cli project save`** - SaveProject on the current project
- **`davinci-resolve-pp-cli project set-database`** - switch the project database (Disk or PostgreSQL); CLOSES the open project
- **`davinci-resolve-pp-cli project set-settings`** - set project settings from JSON; one key per call, per-key result, read back
- **`davinci-resolve-pp-cli project settings`** - all 158 project settings, or one key
- **`davinci-resolve-pp-cli project settings-preset`** - apply/save/update/delete/export/import a project-settings preset
- **`davinci-resolve-pp-cli project settings-presets`** - project-settings presets (name, width, height)

### render

render formats, settings, presets, job queue, quick export, verify-by-file

- **`davinci-resolve-pp-cli render add-job`** - configure (--preset / --format+--codec / --settings / --out / --name) and AddRenderJob with retry
- **`davinci-resolve-pp-cli render delete-job`** - DeleteRenderJob JOB or --all
- **`davinci-resolve-pp-cli render formats`** - render formats, codecs (desc -> ID) for --format, audio formats/codecs (21-1), resolutions, current selection
- **`davinci-resolve-pp-cli render job-status`** - one job's status (error strings normalized to Failed)
- **`davinci-resolve-pp-cli render jobs`** - render queue with normalized per-job status
- **`davinci-resolve-pp-cli render preset`** - load / save / update / delete / export / import / quick-export-on / quick-export-off a render preset
- **`davinci-resolve-pp-cli render presets`** - render preset names
- **`davinci-resolve-pp-cli render queue`** - the render queue
- **`davinci-resolve-pp-cli render quick-export`** - RenderWithQuickExport of the current timeline (EnableUpload always off)
- **`davinci-resolve-pp-cli render quick-export-presets`** - Quick Export preset names
- **`davinci-resolve-pp-cli render render`** - render the current timeline (mp4/H264) with every guard
- **`davinci-resolve-pp-cli render set-format`** - SetCurrentRenderFormatAndCodec (extension + codec ID)
- **`davinci-resolve-pp-cli render set-mode`** - render mode: single (one file) or individual (per clip)
- **`davinci-resolve-pp-cli render set-settings`** - SetRenderSettings one key per call with per-key result (no readback exists; verify by file)
- **`davinci-resolve-pp-cli render smoke`** - disposable-project end-to-end render proof
- **`davinci-resolve-pp-cli render start`** - StartRendering jobs (or all); --wait polls to completion
- **`davinci-resolve-pp-cli render stop`** - StopRendering
- **`davinci-resolve-pp-cli render verify`** - ffprobe an output file (codec/size/fps/duration/audio) and check expectations -- the only render readback

### storage

media storage browsing/import and the 21.1 clone tool

- **`davinci-resolve-pp-cli storage browse`** - media storage folders (and files with --files) under a path
- **`davinci-resolve-pp-cli storage clone-media`** - start (or --stop) a verified media clone (21-1 clone tool)
- **`davinci-resolve-pp-cli storage clone-status`** - media clone job status (21-1)
- **`davinci-resolve-pp-cli storage import`** - import storage paths into the current pool folder (subclip range, clip mattes, timeline mattes)
- **`davinci-resolve-pp-cli storage volumes`** - media storage mounted volumes

### system

Resolve app state: status, page, system presets, keyframe mode, background tasks, DCTL, quit

- **`davinci-resolve-pp-cli system apply`** - system presets: KIND layout|burnin|prefs|keyboard|fairlight, ACTION load|save|update|delete|export|import
- **`davinci-resolve-pp-cli system disable-background-tasks`** - DisableBackgroundTasksForCurrentResolveSession (no re-enable; restart Resolve to restore auto-backup)
- **`davinci-resolve-pp-cli system encrypt-dctl`** - 21-1 EncryptDCTL a file (--name, --expiry ISO 8601, --out folder)
- **`davinci-resolve-pp-cli system export-frame`** - ExportCurrentFrameAsStill (.jpg/.png/.tif/.dpx/.drx; page-dependent)
- **`davinci-resolve-pp-cli system keyframe-mode`** - read the keyframe mode (all|color|sizing)
- **`davinci-resolve-pp-cli system list`** - layout / burn-in / prefs / keyboard (+current) / Fairlight preset lists
- **`davinci-resolve-pp-cli system page`** - switch the GUI page
- **`davinci-resolve-pp-cli system quit`** - Resolve.Quit() (--save first)
- **`davinci-resolve-pp-cli system set-keyframe-mode`** - set the keyframe mode
- **`davinci-resolve-pp-cli system status`** - product, version, Studio flag, page (null = writes will fail), project, timeline, database, keyboard preset, rendering
- **`davinci-resolve-pp-cli system thumbnail`** - GetCurrentClipThumbnailImage (Color page) -> --out FILE.ppm
- **`davinci-resolve-pp-cli system validate-dctl`** - 21-1 ValidateDCTL from --source text or --path file

### timeline

timelines, tracks, markers, timecode, settings, generators, timeline AI, import/export, build

- **`davinci-resolve-pp-cli timeline add-marker`** - add a timeline marker (or --item for a clip-relative item marker)
- **`davinci-resolve-pp-cli timeline add-track`** - AddTrack video/audio/subtitle (audio: --subtype mono|stereo|5.1|)
- **`davinci-resolve-pp-cli timeline ai`** - subtitles / scene-cuts / stereo / dolby-vision / normalize (21-1) / auto-align (21-1)
- **`davinci-resolve-pp-cli timeline append`** - AppendToTimeline with pre-resolved geometry + readback (current timeline only)
- **`davinci-resolve-pp-cli timeline build`** - edit-spec -> media pool + new timeline with readback
- **`davinci-resolve-pp-cli timeline clips`** - items per track of a timeline
- **`davinci-resolve-pp-cli timeline compound-clip`** - CreateCompoundClip from items
- **`davinci-resolve-pp-cli timeline create`** - CreateEmptyTimeline in the current project
- **`davinci-resolve-pp-cli timeline create-from-clips`** - CreateTimelineFromClips: whole clips (positional refs) or --items JSON [{clip,startFrame,endFrame,recordFrame}]
- **`davinci-resolve-pp-cli timeline delete`** - delete a timeline by name
- **`davinci-resolve-pp-cli timeline delete-marker`** - delete timeline/item markers by --frame, --custom-data or --color/All
- **`davinci-resolve-pp-cli timeline delete-track`** - DeleteTrack
- **`davinci-resolve-pp-cli timeline duplicate`** - duplicate a timeline under a new name
- **`davinci-resolve-pp-cli timeline export`** - Timeline.Export to AAF/DRT/EDL/FCPXML/OTIO/CSV/ALE/
- **`davinci-resolve-pp-cli timeline export-spec`** - serialize a timeline to edit-spec JSON
- **`davinci-resolve-pp-cli timeline fusion-clip`** - CreateFusionClip from items
- **`davinci-resolve-pp-cli timeline grab-still`** - GrabStill at the playhead, or --all first|middle for every clip
- **`davinci-resolve-pp-cli timeline import`** - ImportTimelineFromFile (new timeline) or --into (AAF into the current timeline)
- **`davinci-resolve-pp-cli timeline info`** - timeline geometry, rate, resolution, track counts, marks, blanking
- **`davinci-resolve-pp-cli timeline insert-generator`** - insert a generator / fusion-generator / ofx-generator / title / fusion-title / fusion-composition at the playhead
- **`davinci-resolve-pp-cli timeline list`** - timelines in the open project
- **`davinci-resolve-pp-cli timeline mark`** - set (--mark-in/--mark-out) or --clear timeline mark in/out
- **`davinci-resolve-pp-cli timeline markers`** - timeline markers (frames relative to start)
- **`davinci-resolve-pp-cli timeline normalize-modes`** - 21-1 GetNormalizeAudioModes
- **`davinci-resolve-pp-cli timeline rename`** - rename a timeline
- **`davinci-resolve-pp-cli timeline set-blanking`** - 21-1 timeline output blanking (pixels)
- **`davinci-resolve-pp-cli timeline set-settings`** - set timeline settings from JSON; useCustomSettings applied first; frame rate is fixed at creation
- **`davinci-resolve-pp-cli timeline set-timecode`** - move the playhead (--playhead TC) / set start TC (--start TC)
- **`davinci-resolve-pp-cli timeline set-track`** - rename / enable / lock / voice-isolate a track
- **`davinci-resolve-pp-cli timeline settings`** - timeline settings (project settings unless useCustomSettings=1)
- **`davinci-resolve-pp-cli timeline switch`** - make a timeline current (switches the GUI)
- **`davinci-resolve-pp-cli timeline timecode`** - playhead + start timecode + frame range
- **`davinci-resolve-pp-cli timeline tracks`** - tracks with name, enabled, locked, subtype, item count, voice isolation


## Output Formats

```bash
# Human-readable table (default in terminal, JSON when piped)
davinci-resolve-pp-cli item list

# JSON for scripting and agents
davinci-resolve-pp-cli item list --json
# Filter to specific fields by name
davinci-resolve-pp-cli item list --json --select <field>[,<field>...]

# Dry run — show the request without sending
davinci-resolve-pp-cli item list --dry-run

# Agent mode — JSON + compact + no prompts in one flag
davinci-resolve-pp-cli item list --agent
```

## Agent Usage

This CLI is designed for AI agent consumption:

- **Non-interactive** - never prompts, every input is a flag
- **Pipeable** - `--json` output to stdout, errors to stderr
- **Filterable** - `--select <field>[,<field>...]` returns only fields you need
- **Previewable** - `--dry-run` shows the request without sending
- **Explicit retries** - add `--idempotent` to create retries when a no-op success is acceptable
- **Explicit confirmation** - `--agent` does not imply `--yes`; pass `--yes` separately only after the target, arguments, and side effects are clear
- **Piped input** - write commands can accept structured input when their help lists `--stdin`
- **Offline-friendly** - sync/search commands can use the local SQLite store when available
- **Agent-safe by default** - no colors or formatting unless `--human-friendly` is set

Exit codes: `0` success, `2` usage error, `3` not found, `5` API error, `6` partial failure, `7` rate limited, `10` config error.

## Health Check

```bash
davinci-resolve-pp-cli doctor
```

Verifies configuration and connectivity to the API.

## Configuration

Run `davinci-resolve-pp-cli doctor` to see the resolved config, data, state, and cache directories. The platform-default config path is `~/.config/davinci-resolve-pp-cli/config.toml`; `--home`, `DAVINCI_RESOLVE_HOME`, and per-kind env vars can relocate it.

Static request headers can be configured under `headers`; per-command header overrides take precedence.

## Troubleshooting
**Not found errors (exit code 3)**
- Check the resource ID is correct
- Run the `list` command to see available items


### API-specific
- **Every write command returns an error even though reads work fine** — Run `davinci-resolve-pp-cli doctor` — this is almost always the page-null trap (Resolve sitting on the Project Manager); `project load <name>` is the escape hatch
- **A command hangs forever with no error** — A GUI modal in Resolve (auto-backup prompt, media-offline dialog) is stalling every call; the CLI's watchdog will exit with a busy code — check `runs list` for the retry trail
- **The sidecar crashes on startup with an interpreter error** — Run `doctor` — the bundled scripting library needs a specific CPython build that differs per machine even on identical Resolve versions

## Sources & Inspiration

This CLI was built by studying these projects and resources:

- [**davinci-resolve-mcp (samuelgursky)**](https://github.com/samuelgursky/davinci-resolve-mcp) — Python (2502 stars)
- [**resolve-claude-mcp (barckley75)**](https://github.com/barckley75/resolve-claude-mcp) — Python (353 stars)
- [**pydavinci (pedrolabonia)**](https://github.com/pedrolabonia/pydavinci) — Python (181 stars)
- [**davinci-resolve-cli (Poechant)**](https://github.com/Poechant/davinci-resolve-cli) — Python (51 stars)
- [**hiteshK03/davinci-resolve-mcp**](https://github.com/hiteshK03/davinci-resolve-mcp) — Python (38 stars)
- [**dvr (mhadifilms)**](https://github.com/mhadifilms/dvr) — Python (13 stars)
- [**davinci-resolve-mcp (lordhoell)**](https://github.com/lordhoell/davinci-resolve-mcp) — Python (5 stars)
- [**pybmd (WheheoHu)**](https://github.com/WheheoHu/pybmd) — Python

Generated by [CLI Printing Press](https://github.com/mvanhorn/cli-printing-press)
