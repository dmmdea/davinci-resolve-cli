# davinci-resolve-cli

Drive **DaVinci Resolve Studio** from the command line, from scripts, and from AI agents, plus
a field guide to Resolve's scripting API written from live measurements.

| Folder | What it is |
|---|---|
| [`bridge/`](bridge) | Python bridge over Blackmagic's scripting API: **146 commands** (projects, media pool, clips, markers, timelines, tracks, items, transitions, multicam, colour, Fusion, render queue, presets, AI passes, transcription). Every write is `--yes`-guarded, every call runs under a watchdog, and a static gate proves read commands never mutate. `serve` exposes it all as a loopback HTTP API. |
| [`cli/`](cli) | `davinci-resolve-pp-cli`, a Go CLI + MCP server generated from the bridge's own route catalog, with a local history layer (run log, marker search across projects, render history, grade audit, title search, timeline log, doctor). |
| [`docs/`](docs) | The field guide: connecting and interpreters, headless mode, the page-null and modal-stall traps, API catalog, editing playbook, render/delivery, Fusion/titles/colour/audio, 40+ failure modes with fixes, and what Resolve 21.1 changed. |
| [`schema/`](schema) | `edit-spec.schema.json`: the JSON edit spec that `build` turns into a timeline and `export-spec` produces from one. |
| [`skill/`](skill) | A Claude Code skill that teaches an agent to operate Resolve safely with the above. |

## Requirements

- **DaVinci Resolve Studio**, running, with **Preferences → System → General → External
  scripting using → Local**. External scripting has been Studio-only since 19.1; the free
  edition refuses the connection.
- **Windows** for the bridge as shipped (`resolve.cmd`, `deploy.ps1`). The Python modules
  themselves are plain CPython and the Go CLI is cross-platform, but only Windows has been
  measured.
- **Python**: on Resolve 21.1, use the bundled
  `C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolvePython\ResolvePython.exe` or any
  64-bit CPython 3.11–3.14. On 21.0.4, compatibility is per-machine, so measure it with
  `bridge/_probe.py` (see [`bridge/README.md`](bridge/README.md)).
- **Go 1.26+** to build the CLI. **ffprobe** on `PATH` (or `$FFPROBE`) for `verify-render`.

## Quick start

```powershell
# 1. Install the bridge next to Resolve (gates, SHA stamp, interpreter pin, self-verify)
pwsh bridge/deploy.ps1 -Local -Dest 'C:/ResolveTools' `
    -PythonPin 'C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolvePython\ResolvePython.exe'

# 2. Talk to Resolve (Studio must be open)
C:\ResolveTools\resolve.cmd status --json
C:\ResolveTools\resolve.cmd clips --audio --json

# 3. Optional: the HTTP sidecar + Go CLI / MCP server
C:\ResolveTools\resolve.cmd serve --port 18800
go install github.com/dmmdea/davinci-resolve-cli/cli/cmd/davinci-resolve-pp-cli@latest
davinci-resolve-pp-cli doctor
davinci-resolve-pp-cli timeline list --json
```

Writes always need `--yes`:

```powershell
resolve.cmd import "C:/Footage/a.mp4" --yes
resolve.cmd build --spec edit.json --fps 29.97 --yes --timeout 180
resolve.cmd render --out C:/Renders --name cut-v1 --yes --timeout 180
```

To drive Resolve on another machine, deploy with `-Target user@host` and either run
`resolve.cmd` over SSH or forward the sidecar: `ssh -L 18800:127.0.0.1:18800 user@host`.

## The traps this handles for you

- **page == null**: when Resolve sits on the Project Manager, reads work and every write
  silently returns None. `status` reports the page, and `load-project` gets you out.
- **Modal stall**: any open dialog hangs every API call forever. Commands run under a watchdog
  and exit 3 ("busy, retry") instead of hanging.
- **Silent failures**: the API returns False/None (or an error *string*) with no message.
  Writes are read back, and renders are verified by the output file with ffprobe.
- **Partial settings apply**: `SetRenderSettings`/`SetSetting` go one key per call with a
  per-key result.
- **`ArchiveProject` crashes Resolve 21.1**, so the bridge refuses it.

Details and the measurements behind each: [`docs/`](docs).

## Tests

```powershell
python -m pytest bridge/tests -q     # no Resolve needed
python bridge/check_readonly.py      # read-safety gate
cd cli; go test ./...                # on Windows: set GOTMPDIR outside %TEMP% (Defender false positive)
```

## Credits and license

The CLI was generated with [CLI Printing Press](https://github.com/mvanhorn/cli-printing-press)
from the bridge's route catalog. DaVinci Resolve and Fusion are trademarks of Blackmagic
Design; this project is not affiliated with or endorsed by Blackmagic Design.

Licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE).
