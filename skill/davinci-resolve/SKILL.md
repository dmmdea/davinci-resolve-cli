---
name: davinci-resolve
description: Use when the user wants to edit, cut, trim, assemble, color grade, title, caption, sync audio, add transitions, or render/export a video in DaVinci Resolve Studio — or any task that means operating Resolve: importing media, building or changing a timeline, the media pool, markers, multicam, Fusion, Fairlight audio, transcription, or rendering. Triggers on "edit my video", "cut the intro", "make a rough cut", "color grade the footage", "render/export this", "add a title", DaVinci Resolve, Resolve Studio.
---

# DaVinci Resolve

Drive DaVinci Resolve Studio through the bridge in this repository (`bridge/resolve_cli.py`,
146 commands, launched as `resolve.cmd`) or its Go client (`davinci-resolve-pp-cli`, which talks
to the bridge's sidecar on `127.0.0.1:18800`). Set `RESOLVE_TOOLS` to the folder the bridge is
deployed to (for example `C:\ResolveTools`). The examples below use `resolve.cmd` from that
folder.

## Before anything
1. `resolve.cmd status --json`. If it fails with exit 1, Resolve is not reachable: it's not running,
   it's not Studio, external scripting is not set to Local, or the Python is wrong (docs/02).
2. **Check `page`.** `"page": null` means every write will silently return None. Run
   `resolve.cmd load-project "<name>" --yes` first (it works from page-null), or run Resolve headless
   with `Resolve.exe -nogui`, which never comes up page-null.
3. Exit **3** = watchdog timeout: a modal dialog or a long operation is blocking the API. Back off
   and retry. It is not a fact about the project.

## How to work
1. **Read before acting.** Start with `status`, `timelines`, `clips [--timeline NAME]`,
   `selection` and `pool --recursive`. `--timeline NAME` reads a named timeline WITHOUT switching
   the editor's GUI. `selection` includes linked audio items, so filter by `type`.
2. **Never disturb a live editor.** Reads are safe any time. Every write needs `--yes` and an
   idle or supervised window. State the intent and get a yes before creating projects, switching
   timelines or rendering. `smoke` disables Resolve's auto-backup until Resolve restarts.
3. **Verify after.** Re-read (`status`, `clips`, `export-spec`, `verify-render PATH`) and report
   what actually changed. A render is done only when the output FILE checks out.
4. **Producing an edit:** write an edit spec following `schema/edit-spec.schema.json` (1.0.0,
   integer frames). `export-spec` shows the shape from a real timeline. Then run
   `build --spec PATH --yes`, which reads back every placement and exits 1 on a mismatch.

## What the API cannot do
- No trim/move/razor on a placed clip. Compute the geometry up front and place it with
  `append`/`build`. 21.1 adds `add-transition`, `set-item --speed/--fade-in/--fade-out` and
  `set-item-properties` as the only post-placement edits.
- No per-node lift/gamma/gain. Use LUT/CDL/DRX (`apply-lut`, `apply-cdl`, `apply-drx`).
- Never call `archive-project`. `ArchiveProject` crashes Resolve 21.1 from a script, so the bridge
  refuses it.

## Reference (load on demand)
`docs/README.md` indexes the field guide:

- `02` connection/interpreters/headless/SSH
- `03` core verbs and exit codes
- `04` API catalog
- `05` editing playbook (editor vocabulary → API operations)
- `06` render/delivery
- `07` Fusion/titles/colour/audio
- `08` failure modes (known traps with fixes)
- `09` Resolve 21.1
- `10` the full 146-command sidecar and the Go CLI

Read the chapter that matches the task before researching anything.
