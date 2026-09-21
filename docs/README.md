# DaVinci Resolve scripting: a field guide

A working reference for driving DaVinci Resolve Studio from code. It was built while writing the
bridge and CLI in this repository, from:

- Resolve's own scripting `README.txt` and 21.1 `README.md`/`CHANGELOG.md`/`DaVinciResolveScript.pyi`
- the Fusion 8 Scripting Guide
- live read-only dumps and write tests against Resolve Studio 21.0.4.5 and 21.1.0.14 on Windows 11
- community references (forum threads, export guides, other Resolve MCP projects)

Every claim is tagged:

- **[measured]**: observed live
- **[doc]**: official Blackmagic text
- **[community]**: third-party report
- **[inferred]**: reasoning; verify before relying on it

Where two Windows machines behaved differently, they are called *machine A* and *machine B*.

**Resolve 21.1:** read `09-resolve-21-1.md` FIRST. It covers:

- the native MCP server findings
- the new API (transitions, fades, speed, multicam, transcript readback, settings presets, clone
  tool, DCTL validation)
- the interpreter matrix
- a crash (`ArchiveProject`)
- the table of older rules that 21.1 overturns

The older chapters keep their 21.0.4 measurements, with a pointer at every overturned line.

## Reading order (load only what the task needs)

| File | Load when |
|---|---|
| `02-connection-transport.md` | connecting, interpreter crashes, headless, watchdog, page-null, launching Resolve in another user's session, SSH |
| `03-cli-reference.md` | using `resolve.cmd` core verbs (exit codes, gotchas, recovery) |
| `04-api-reference.md` | writing any Python against the API (condensed catalog + constants + settings keys) |
| `05-editing-playbook.md` | turning an editor's instruction into operations; frame math; what the API cannot do and the workaround |
| `06-render-delivery.md` | rendering, codecs, YouTube/Shorts numbers, NVENC, verify-by-file |
| `07-fusion-titles-color-audio.md` | Text+ titles, Fusion scripting, keyframes, LUT/CDL/grades, audio limits, captions |
| `08-failure-modes.md` | anything that returned None/False/hung: the catalog of known traps |
| `09-resolve-21-1.md` | **21.1**: native MCP server (stdio zipapp, 14 tools), built-in Python, new API, overturned rules, crash list |
| `10-sidecar-and-cli.md` | the 146-command bridge/sidecar and the Go CLI: routes, argument kinds, refs (`V1:3`), what each group measured |
| `api-catalog-21.1.md` | every class/method/signature of `DaVinciResolveScript.pyi` (21.1), flat and greppable |
| `resolve-21.0.4-settings-dump.json` | exact `Project.GetSetting()` keys (= `Timeline.GetSetting()` keys), the 309 Text+ input IDs with defaults, render formats/codec ids, preset names on 21.0.4.5 |

## The ten rules (the rest of the folder is detail)

1. Read first (`status --json`). `"page": null` means every write silently returns None.
   `load-project NAME --yes` (or `CreateProject` from script) is the way out, and
   `Resolve.exe -nogui` never shows the trap at all. **[measured on two machines]**
2. Studio must be RUNNING. The free edition answers `scriptapp()` with None; it does not crash.
   A 0xC0000005 crash is the wrong CPython, which was per machine on 21.0.4. **On 21.1 every 64-bit
   CPython 3.11–3.14 bound, and Resolve ships its own `ResolvePython.exe` (3.14.4) that needs no
   env vars** (09 §2). **[measured]**
3. Every API call can hang forever behind a GUI modal. Always run under a watchdog; exit 3 means
   "busy, retry later", never a fact about the project. **On 21.1, `ArchiveProject` called from a
   script stalls and then CRASHES Resolve, so never call it** (09 §5). **[measured]**
4. The API is silent on failure. Methods return False/None with no message, and sometimes an
   error STRING (`GetRenderJobStatus`, `GenerateSpeech`, `ValidateDCTL`). Check every return by
   type. **[doc + measured]**
5. No trim/move/razor/keyframe on a placed clip. Pre-resolve geometry into `AppendToTimeline`
   clipInfo, then read back `GetStart/GetDuration`. **21.1 adds `AddTransition` (needs media
   handles), `SetFades`, `SetSpeed`, `SetProperties`**, which are the ONLY post-placement edits
   (09 §5). **[doc + measured]**
6. `SetRenderSettings` takes one key per call, so fail loud on False. `AddRenderJob` may return ""
   the first time. Completion is proven by the output FILE only. **[measured]**
7. Codec ids ≠ codec descriptions. `GetRenderCodecs` maps description → id, and every setter
   wants the id (`H264`, `H264_NVIDIA`, `H265_NVIDIA`, `ProRes422HQ`). **[measured]**
8. Titles: `InsertFusionTitleIntoTimeline("Text+")` takes no clipInfo. Drive text through the
   item's Fusion comp (`GetFusionCompByIndex(1)` → `GetToolList(False,"TextPlus")` →
   `SetInput("StyledText", …)`). Keyframes come from `BezierSpline` or a pre-animated `.setting`
   template. **[measured]**
9. **21.1: `MediaPoolItem.GetTranscription()` returns per-word timecodes** after a synchronous
   `TranscribeAudio()`. It works on media/cut/edit/fairlight, returns False on
   fusion/color/deliver, and takes ~20 s cold (09 §5). **[measured]**
10. Never disturb an editor's live session. Reads are fine any time; do writes only in an
    idle/supervised window. `smoke` disables auto-backup for the rest of that Resolve session.
