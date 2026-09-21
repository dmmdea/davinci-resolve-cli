// Copyright 2026 dmmdea and contributors. Licensed under Apache-2.0. See LICENSE.

package oplog

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"testing"
)

func openTemp(t *testing.T) *Log {
	t.Helper()
	l, err := Open(context.Background(), filepath.Join(t.TempDir(), "history.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = l.Close() })
	return l
}

func record(t *testing.T, l *Log, e Entry) int64 {
	t.Helper()
	id, err := l.Record(context.Background(), e)
	if err != nil {
		t.Fatalf("Record(%s): %v", e.Command, err)
	}
	return id
}

func raw(v any) json.RawMessage {
	b, _ := json.Marshal(v)
	return b
}

func TestOpenReadOnlyMissingIsErrNoHistory(t *testing.T) {
	_, err := OpenReadOnly(context.Background(), filepath.Join(t.TempDir(), "nope.db"))
	if !errors.Is(err, ErrNoHistory) {
		t.Fatalf("want ErrNoHistory, got %v", err)
	}
}

func TestRecordRunsRunAndStats(t *testing.T) {
	l := openTemp(t)
	ctx := context.Background()
	okID := record(t, l, Entry{Method: "GET", Path: "/v1/timelines", Command: "timelines", Status: 200, OK: true, ExitCode: 0, DurationMS: 12, Project: "P"})
	busyID := record(t, l, Entry{Method: "GET", Path: "/v1/status", Command: "status", Status: 503, OK: false, ExitCode: 3, Error: "watchdog"})
	failID := record(t, l, Entry{Method: "POST", Path: "/v1/append", Command: "append", Status: 400, OK: false, ExitCode: 1, Error: "no clip",
		Args: raw(map[string]any{"clip": "x.mp4"}), Response: raw(map[string]any{"ok": false})})

	runs, err := l.Runs(ctx, "", false, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(runs) != 3 || runs[0].ID != failID || runs[2].ID != okID {
		t.Fatalf("Runs newest-first: %+v", runs)
	}
	failed, err := l.Runs(ctx, "", true, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(failed) != 2 {
		t.Fatalf("failedOnly: want 2, got %d", len(failed))
	}
	only, err := l.Runs(ctx, "status", false, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(only) != 1 || only[0].ID != busyID || !only[0].Busy() {
		t.Fatalf("command filter / Busy: %+v", only)
	}

	one, err := l.Run(ctx, failID)
	if err != nil {
		t.Fatal(err)
	}
	if string(one.Args) != `{"clip":"x.mp4"}` || string(one.Response) != `{"ok":false}` || one.Error != "no clip" {
		t.Fatalf("Run bodies: %+v", one)
	}
	if _, err := l.Run(ctx, 999); err == nil {
		t.Fatal("Run(999) should fail")
	}

	st, err := l.Stats(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if st.Total != 3 || st.Failed != 2 || st.Busy != 1 || st.Commands["append"] != 1 || st.First == "" || st.Last == "" {
		t.Fatalf("Stats: %+v", st)
	}
}

func TestDeriveMarkersAndSearch(t *testing.T) {
	l := openTemp(t)
	ctx := context.Background()
	resp := map[string]any{"ok": true, "data": map[string]any{
		"timeline": "TL", "target": "V1:2",
		"markers": []any{
			map[string]any{"frame": 12, "color": "Blue", "name": "pickup shot", "note": "re-record the intro", "customData": map[string]any{"by": "dogfood"}},
			map[string]any{"frame": 40, "color": "Red", "name": "fix audio", "note": "", "customData": "plain"},
		},
	}}
	record(t, l, Entry{Command: "add-marker", Path: "/v1/add-marker", OK: true, Project: "P1", Timeline: "TL",
		Args: raw(map[string]any{"item": "V1:2"}), Response: raw(resp)})
	record(t, l, Entry{Command: "clip-markers", Path: "/v1/clip-markers", OK: true, Project: "P2",
		Response: raw(map[string]any{"ok": true, "data": map[string]any{"clip": "ref_a.mp4",
			"markers": []any{map[string]any{"frame": 3, "color": "Green", "name": "intro", "note": "pickup here"}}}})})

	hits, err := l.SearchMarkers(ctx, "pickup", "", 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) != 2 {
		t.Fatalf("SearchMarkers(pickup) across projects: want 2, got %+v", hits)
	}
	scoped, err := l.SearchMarkers(ctx, "pickup", "P1", 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(scoped) != 1 || scoped[0].Item != "V1:2" || scoped[0].Frame != 12 || scoped[0].CustomData != `{"by":"dogfood"}` {
		t.Fatalf("SearchMarkers scoped: %+v", scoped)
	}
	clip, err := l.SearchMarkers(ctx, "intro", "P2", 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(clip) != 1 || clip[0].Clip != "ref_a.mp4" {
		t.Fatalf("clip marker: %+v", clip)
	}
	// Punctuation in the query must not break the FTS grammar.
	if _, err := l.SearchMarkers(ctx, `"quoted" (paren) -dash`, "", 5); err != nil {
		t.Fatalf("punctuated query: %v", err)
	}
	if got := ftsQuery(`a "b"`); got != `"a" """b"""` {
		t.Fatalf("ftsQuery quoting: %s", got)
	}
	if got := ftsQuery("   "); got != `""` {
		t.Fatalf("ftsQuery empty: %s", got)
	}
}

func TestDeriveRendersTitlesGradesTimelineOps(t *testing.T) {
	l := openTemp(t)
	ctx := context.Background()

	record(t, l, Entry{Command: "add-render-job", OK: true, Project: "P", Timeline: "TL",
		Args:     raw(map[string]any{"out": "D:/out", "name": "ep12", "preset": "YouTube 1080p"}),
		Response: raw(map[string]any{"ok": true, "data": map[string]any{"job": "job-1"}})})
	record(t, l, Entry{Command: "verify-render", OK: true, Project: "P",
		Response: raw(map[string]any{"ok": true, "data": map[string]any{"path": "D:/out/ep12.mp4", "bytes": 1234, "ok": true, "duration": 6.0,
			"video": map[string]any{"codec": "h264", "width": 1920, "height": 1080, "fps": 25.0}, "audio": []any{map[string]any{}}, "problems": []any{}}})})
	renders, err := l.Renders(ctx, "P", 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(renders) != 2 {
		t.Fatalf("Renders: %+v", renders)
	}
	var queued, verified *Render
	for i := range renders {
		switch renders[i].Kind {
		case "queued":
			queued = &renders[i]
		case "verified":
			verified = &renders[i]
		}
	}
	if queued == nil || queued.Job != "job-1" || queued.Preset != "YouTube 1080p" || queued.OutDir != "D:/out" {
		t.Fatalf("queued row: %+v", queued)
	}
	if verified == nil || verified.Width != 1920 || verified.Codec != "h264" || verified.Verified == nil || !*verified.Verified || verified.AudioStreams != 1 {
		t.Fatalf("verified row: %+v", verified)
	}
	if other, _ := l.Renders(ctx, "OTHER", 10); len(other) != 0 {
		t.Fatalf("project filter leaked: %+v", other)
	}

	record(t, l, Entry{Command: "set-title", OK: true, Project: "P", Timeline: "TL",
		Response: raw(map[string]any{"ok": true, "data": map[string]any{"item": "V1:1", "tool": "Template",
			"readback": map[string]any{"StyledText": "WELCOME BACK EPISODE 12", "Size": 0.08}}})})
	titles, err := l.SearchTitles(ctx, "welcome", "", 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(titles) != 1 || titles[0].Text != "WELCOME BACK EPISODE 12" || titles[0].Item != "V1:1" {
		t.Fatalf("SearchTitles: %+v", titles)
	}

	// Grade audit: two items, the newest version name seen in the project is v2,
	// so the item still on v1 is stale.
	record(t, l, Entry{Command: "color-version", OK: true, Project: "P", Timeline: "TL", Args: raw(map[string]any{"action": "add"}),
		Response: raw(map[string]any{"ok": true, "data": map[string]any{"item": "V1:1", "track": "V1", "current_version": map[string]any{"versionName": "v2", "versionType": 0}}})})
	record(t, l, Entry{Command: "color-info", OK: true, Project: "P", Timeline: "TL",
		Response: raw(map[string]any{"ok": true, "data": map[string]any{"item": "V1:2", "track": "V1", "current_version": map[string]any{"versionName": "v1", "versionType": 0}}})})
	grades, err := l.GradeAudit(ctx, "P")
	if err != nil {
		t.Fatal(err)
	}
	if len(grades) != 2 {
		t.Fatalf("GradeAudit: %+v", grades)
	}
	stale := map[string]bool{}
	for _, g := range grades {
		stale[g.Item] = g.Stale
	}
	if stale["V1:1"] || !stale["V1:2"] {
		t.Fatalf("stale flags: %+v", grades)
	}

	// Timeline ops come back oldest-first regardless of the limit window.
	for i, name := range []string{"a.mp4", "b.mp4", "c.mp4"} {
		record(t, l, Entry{Command: "append", OK: true, Project: "P", Timeline: "TL",
			Response: raw(map[string]any{"ok": true, "data": map[string]any{"timeline": "TL",
				"placed": []any{map[string]any{"name": name, "track": "V1", "start": 100 * (i + 1), "duration": 50}}}})})
	}
	record(t, l, Entry{Command: "insert-generator", OK: true, Project: "P", Timeline: "TL",
		Response: raw(map[string]any{"ok": true, "data": map[string]any{"timeline": "TL", "item": map[string]any{"name": "Solid Color", "track": "V2", "start": 0, "duration": 25}}})})
	ops, err := l.TimelineLog(ctx, "TL", "P", 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(ops) != 2 || ops[0].Item != "c.mp4" || ops[1].Item != "Solid Color" || ops[0].OpID > ops[1].OpID {
		t.Fatalf("TimelineLog newest window, chronological order: %+v", ops)
	}
	all, _ := l.TimelineLog(ctx, "TL", "", 10)
	if len(all) != 4 || all[0].Item != "a.mp4" || all[0].Start != 100 {
		t.Fatalf("TimelineLog all: %+v", all)
	}
	if none, _ := l.TimelineLog(ctx, "missing", "", 10); len(none) != 0 {
		t.Fatalf("unknown timeline should be empty: %+v", none)
	}
}

func TestEnvHistoryReportsDrift(t *testing.T) {
	l := openTemp(t)
	ctx := context.Background()
	status := func(version, page, project string, studio bool) json.RawMessage {
		return raw(map[string]any{"ok": true, "data": map[string]any{"product": "DaVinci Resolve Studio", "version": version, "studio": studio,
			"page": page, "project": project, "timeline": "TL", "cli": "resolve_cli build abc", "database": map[string]any{"DbType": "Disk", "DbName": "Local Database"}}})
	}
	record(t, l, Entry{Command: "status", OK: true, Response: status("21.1.0.14", "edit", "P", true)})
	record(t, l, Entry{Command: "status", OK: true, Response: status("21.1.0.14", "color", "P", true)})
	record(t, l, Entry{Command: "status", OK: true, Response: status("21.2.0.1", "color", "Q", true)})

	env, err := l.EnvHistory(ctx, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(env) != 3 || env[0].Version != "21.2.0.1" || env[0].Database != "Disk:Local Database" || env[0].CLIBuild != "resolve_cli build abc" {
		t.Fatalf("EnvHistory rows: %+v", env)
	}
	if len(env[0].Changes) == 0 || len(env[2].Changes) != 0 {
		t.Fatalf("drift lines: newest %+v oldest %+v", env[0].Changes, env[2].Changes)
	}
	if env[0].Studio == nil || !*env[0].Studio {
		t.Fatalf("studio flag lost: %+v", env[0])
	}
}

func TestShortClamps(t *testing.T) {
	if got := Short("2026-09-09T17:33:20.4068748Z"); got != "2026-09-09T17:33:20" {
		t.Fatalf("Short: %s", got)
	}
	if got := Short("short"); got != "short" {
		t.Fatalf("Short passthrough: %s", got)
	}
}
