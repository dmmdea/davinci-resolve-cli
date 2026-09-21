// Copyright 2026 dmmdea and contributors. Licensed under Apache-2.0. See LICENSE.

// Package oplog is the CLI's own history of everything it asked the Resolve
// sidecar to do. DaVinci Resolve keeps none of this: there is no undo/history
// API, no render-settings readback, no cross-project marker search, and the
// scripting bridge forgets a call the moment it returns. Every sidecar
// round-trip is recorded here (internal/cli/oplog_hook.go wraps the HTTP
// transport), and the seven history commands (runs, marker search, render
// history, color audit, fusion titles search, timeline log, doctor history)
// read from these tables.
//
// The log lives in its own SQLite file (history.db next to the generated
// data.db) so it never competes with the generated store's schema versioning.
package oplog

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/dmmdea/davinci-resolve-cli/cli/internal/cliutil"

	_ "modernc.org/sqlite"
)

// Entry is one sidecar round-trip as the transport saw it.
type Entry struct {
	ID         int64           `json:"id"`
	TS         string          `json:"ts"`
	Method     string          `json:"method"`
	Path       string          `json:"path"`
	Command    string          `json:"command"`
	Args       json.RawMessage `json:"args,omitempty"`
	Status     int             `json:"status"`
	OK         bool            `json:"ok"`
	ExitCode   int             `json:"exit_code"`
	DurationMS int64           `json:"duration_ms"`
	Project    string          `json:"project,omitempty"`
	Timeline   string          `json:"timeline,omitempty"`
	Page       string          `json:"page,omitempty"`
	Error      string          `json:"error,omitempty"`
	Response   json.RawMessage `json:"response,omitempty"`
}

// Busy reports the sidecar's watchdog trip (exit code 3): Resolve was stalled
// behind a modal, not a fact about the project.
func (e Entry) Busy() bool { return e.ExitCode == 3 }

// Log is an open history database.
type Log struct {
	db   *sql.DB
	path string
}

// DefaultPath is <data dir>/history.db, beside the generated data.db.
func DefaultPath() (string, error) {
	dir, err := cliutil.DataDir()
	if err != nil {
		home, herr := os.UserHomeDir()
		if herr != nil {
			return "", err
		}
		dir = filepath.Join(home, ".local", "share", "davinci-resolve-pp-cli")
	}
	return filepath.Join(dir, "history.db"), nil
}

// Exists reports whether a history file has been created yet.
func Exists(path string) bool {
	st, err := os.Stat(path)
	return err == nil && st.Size() > 0
}

const schema = `
CREATE TABLE IF NOT EXISTS ops (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	ts TEXT NOT NULL,
	method TEXT NOT NULL,
	path TEXT NOT NULL,
	command TEXT NOT NULL,
	args TEXT,
	status INTEGER,
	ok INTEGER NOT NULL,
	exit_code INTEGER NOT NULL,
	duration_ms INTEGER NOT NULL,
	project TEXT,
	timeline TEXT,
	page TEXT,
	error TEXT,
	response TEXT
);
CREATE INDEX IF NOT EXISTS ops_command ON ops(command, ts);
CREATE TABLE IF NOT EXISTS markers (
	op_id INTEGER NOT NULL, ts TEXT NOT NULL, project TEXT, timeline TEXT, clip TEXT, item TEXT,
	frame INTEGER, color TEXT, name TEXT, note TEXT, custom_data TEXT, deleted INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS markers_fts USING fts5(name, note, custom_data, project, timeline, clip, content='markers', content_rowid='rowid', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS markers_ai AFTER INSERT ON markers BEGIN
	INSERT INTO markers_fts(rowid, name, note, custom_data, project, timeline, clip) VALUES (new.rowid, new.name, new.note, new.custom_data, new.project, new.timeline, new.clip);
END;
CREATE TABLE IF NOT EXISTS renders (
	op_id INTEGER NOT NULL, ts TEXT NOT NULL, project TEXT, timeline TEXT, kind TEXT NOT NULL,
	job TEXT, out_dir TEXT, name TEXT, preset TEXT, job_status TEXT, file TEXT, bytes INTEGER,
	codec TEXT, width INTEGER, height INTEGER, fps REAL, duration REAL, audio_streams INTEGER,
	verified INTEGER, problems TEXT
);
CREATE TABLE IF NOT EXISTS grades (
	op_id INTEGER NOT NULL, ts TEXT NOT NULL, project TEXT, timeline TEXT, item TEXT, track TEXT,
	version TEXT, version_type INTEGER, action TEXT
);
CREATE TABLE IF NOT EXISTS titles (
	op_id INTEGER NOT NULL, ts TEXT NOT NULL, project TEXT, timeline TEXT, item TEXT, tool TEXT, text TEXT, inputs TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS titles_fts USING fts5(text, project, timeline, item, content='titles', content_rowid='rowid', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS titles_ai AFTER INSERT ON titles BEGIN
	INSERT INTO titles_fts(rowid, text, project, timeline, item) VALUES (new.rowid, new.text, new.project, new.timeline, new.item);
END;
CREATE TABLE IF NOT EXISTS timeline_ops (
	op_id INTEGER NOT NULL, ts TEXT NOT NULL, project TEXT, timeline TEXT, op TEXT NOT NULL,
	item TEXT, track TEXT, start INTEGER, duration INTEGER, detail TEXT
);
CREATE INDEX IF NOT EXISTS timeline_ops_tl ON timeline_ops(timeline, ts);
CREATE TABLE IF NOT EXISTS env (
	op_id INTEGER NOT NULL, ts TEXT NOT NULL, product TEXT, version TEXT, studio INTEGER, page TEXT,
	project TEXT, timeline TEXT, database TEXT, cli_build TEXT, sidecar_ok INTEGER, degraded TEXT
);
`

// Open opens (creating on first use) the history database.
func Open(ctx context.Context, path string) (*Log, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, fmt.Errorf("history dir: %w", err)
	}
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(path)+"?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if _, err := db.ExecContext(ctx, schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("history schema: %w", err)
	}
	return &Log{db: db, path: path}, nil
}

// OpenReadOnly opens an existing history for the query commands. A missing
// file is reported as ErrNoHistory so callers can answer with an empty result
// and a hint instead of an SQLite error.
func OpenReadOnly(ctx context.Context, path string) (*Log, error) {
	if !Exists(path) {
		return nil, ErrNoHistory
	}
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(path)+"?mode=ro&_pragma=busy_timeout(5000)")
	if err != nil {
		return nil, err
	}
	return &Log{db: db, path: path}, nil
}

// ErrNoHistory means nothing has been recorded on this machine yet.
var ErrNoHistory = errors.New("no history recorded yet -- run any davinci-resolve-pp-cli command that reaches the sidecar first")

func (l *Log) Close() error { return l.db.Close() }
func (l *Log) Path() string { return l.path }

// Record stores one round-trip and every derived row it implies. It never
// returns an error for a derivation problem: the ops row is the ground truth,
// derived tables are best-effort views over it.
func (l *Log) Record(ctx context.Context, e Entry) (int64, error) {
	if e.TS == "" {
		e.TS = time.Now().UTC().Format(time.RFC3339Nano)
	}
	res, err := l.db.ExecContext(ctx, `INSERT INTO ops(ts, method, path, command, args, status, ok, exit_code, duration_ms, project, timeline, page, error, response)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		e.TS, e.Method, e.Path, e.Command, nullRaw(e.Args), e.Status, boolInt(e.OK), e.ExitCode, e.DurationMS,
		e.Project, e.Timeline, e.Page, e.Error, nullRaw(e.Response))
	if err != nil {
		return 0, err
	}
	id, _ := res.LastInsertId()
	e.ID = id
	l.derive(ctx, e)
	return id, nil
}

// ---------------------------------------------------------------------------
// Derivation: what each sidecar response implies for the history views.
// ---------------------------------------------------------------------------

type envelope struct {
	OK      bool            `json:"ok"`
	Data    json.RawMessage `json:"data"`
	Context *struct {
		Project  string `json:"project"`
		Timeline string `json:"timeline"`
		Page     string `json:"page"`
	} `json:"context"`
	Degraded string `json:"degraded"`
	CLI      string `json:"cli"`
}

func (l *Log) derive(ctx context.Context, e Entry) {
	var env envelope
	if len(e.Response) > 0 {
		_ = json.Unmarshal(e.Response, &env)
	}
	var data map[string]any
	if len(env.Data) > 0 {
		_ = json.Unmarshal(env.Data, &data)
	}
	var args map[string]any
	if len(e.Args) > 0 {
		_ = json.Unmarshal(e.Args, &args)
	}
	cmd := e.Command
	switch {
	case cmd == "status":
		l.deriveEnv(ctx, e, data, env)
	case e.Path == "/healthz":
		l.deriveHealth(ctx, e, env)
	case cmd == "timeline-markers" || cmd == "add-marker" || cmd == "delete-marker":
		l.deriveMarkers(ctx, e, data, str(args["item"]), "", cmd == "delete-marker")
	case cmd == "clip-markers" || cmd == "add-clip-marker" || cmd == "delete-clip-marker":
		l.deriveMarkers(ctx, e, data, "", str(data["clip"]), cmd == "delete-clip-marker")
	case cmd == "add-render-job" || cmd == "start-render" || cmd == "render" || cmd == "quick-export" ||
		cmd == "verify-render" || cmd == "render-job-status" || cmd == "smoke":
		l.deriveRender(ctx, e, data, args)
	case cmd == "color-version" || cmd == "color-info":
		l.deriveGrade(ctx, e, data, args)
	case cmd == "set-title" || cmd == "fusion-set":
		l.deriveTitle(ctx, e, data)
	case cmd == "append" || cmd == "delete-items" || cmd == "insert-generator" || cmd == "add-transition" ||
		cmd == "compound-clip" || cmd == "fusion-clip" || cmd == "link-items" || cmd == "set-item" ||
		cmd == "set-item-properties" || cmd == "create-timeline" || cmd == "delete-timeline" || cmd == "build" ||
		cmd == "multicam" || cmd == "create-timeline-from-clips" || cmd == "import-timeline":
		l.deriveTimelineOp(ctx, e, data, args)
	}
}

func (l *Log) deriveEnv(ctx context.Context, e Entry, data map[string]any, env envelope) {
	if data == nil {
		return
	}
	dbName := ""
	if db, ok := data["database"].(map[string]any); ok {
		dbName = str(db["DbType"]) + ":" + str(db["DbName"])
	}
	_, _ = l.db.ExecContext(ctx, `INSERT INTO env(op_id, ts, product, version, studio, page, project, timeline, database, cli_build, sidecar_ok, degraded)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`,
		e.ID, e.TS, str(data["product"]), str(data["version"]), boolInt(data["studio"] == true), str(data["page"]),
		str(data["project"]), str(data["timeline"]), dbName, str(data["cli"]), boolInt(e.OK), env.Degraded)
}

func (l *Log) deriveHealth(ctx context.Context, e Entry, env envelope) {
	_, _ = l.db.ExecContext(ctx, `INSERT INTO env(op_id, ts, cli_build, sidecar_ok, degraded) VALUES (?,?,?,?,?)`,
		e.ID, e.TS, env.CLI, boolInt(env.OK), env.Degraded)
}

func (l *Log) deriveMarkers(ctx context.Context, e Entry, data map[string]any, item, clip string, deleted bool) {
	rows, _ := data["markers"].([]any)
	if clip == "" {
		clip = str(data["clip"])
	}
	if item == "" {
		if t := str(data["target"]); strings.HasPrefix(t, "V") || strings.HasPrefix(t, "A") {
			item = t
		}
	}
	for _, r := range rows {
		m, ok := r.(map[string]any)
		if !ok {
			continue
		}
		cd := ""
		if v, ok := m["customData"]; ok && v != nil {
			if s, isStr := v.(string); isStr {
				cd = s
			} else if b, err := json.Marshal(v); err == nil {
				cd = string(b)
			}
		}
		_, _ = l.db.ExecContext(ctx, `INSERT INTO markers(op_id, ts, project, timeline, clip, item, frame, color, name, note, custom_data, deleted)
			VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`,
			e.ID, e.TS, e.Project, e.Timeline, clip, item, num(m["frame"]), str(m["color"]), str(m["name"]), str(m["note"]), cd, 0)
	}
	if deleted {
		// A delete answers with the SURVIVING markers; mark the op so the view can
		// prefer the newest observation per (project, timeline/clip, frame).
		_, _ = l.db.ExecContext(ctx, `UPDATE ops SET error = COALESCE(error,'') WHERE id = ?`, e.ID)
	}
}

func (l *Log) deriveRender(ctx context.Context, e Entry, data map[string]any, args map[string]any) {
	ins := func(kind string, cols map[string]any) {
		names := []string{"op_id", "ts", "project", "timeline", "kind"}
		vals := []any{e.ID, e.TS, e.Project, e.Timeline, kind}
		for k, v := range cols {
			names = append(names, k)
			vals = append(vals, v)
		}
		q := "INSERT INTO renders(" + strings.Join(names, ",") + ") VALUES (" + strings.TrimSuffix(strings.Repeat("?,", len(vals)), ",") + ")"
		_, _ = l.db.ExecContext(ctx, q, vals...)
	}
	switch e.Command {
	case "add-render-job":
		ins("queued", map[string]any{"job": str(data["job"]), "out_dir": str(args["out"]), "name": str(args["name"]), "preset": str(args["preset"])})
	case "start-render":
		if statuses, ok := data["status"].(map[string]any); ok {
			for job, st := range statuses {
				sm, _ := st.(map[string]any)
				ins("started", map[string]any{"job": job, "job_status": str(sm["JobStatus"])})
			}
		} else {
			ins("started", map[string]any{"job": fmt.Sprint(data["jobs"])})
		}
	case "render-job-status":
		sm, _ := data["status"].(map[string]any)
		ins("status", map[string]any{"job": str(data["job"]), "job_status": str(sm["JobStatus"])})
	case "render", "smoke":
		ins("rendered", map[string]any{"file": str(data["rendered"]), "job_status": "Complete", "verified": boolInt(str(data["rendered"]) != "")})
	case "quick-export":
		sm, _ := data["status"].(map[string]any)
		settings, _ := data["settings"].(map[string]any)
		ins("quick-export", map[string]any{"preset": str(data["preset"]), "job_status": str(sm["JobStatus"]),
			"out_dir": str(settings["TargetDir"]), "name": str(settings["CustomName"])})
	case "verify-render":
		v, _ := data["video"].(map[string]any)
		audio, _ := data["audio"].([]any)
		problems, _ := json.Marshal(data["problems"])
		ins("verified", map[string]any{"file": str(data["path"]), "bytes": num(data["bytes"]), "codec": str(v["codec"]),
			"width": num(v["width"]), "height": num(v["height"]), "fps": fnum(v["fps"]), "duration": fnum(data["duration"]),
			"audio_streams": len(audio), "verified": boolInt(data["ok"] == true), "problems": string(problems)})
	}
}

func (l *Log) deriveGrade(ctx context.Context, e Entry, data map[string]any, args map[string]any) {
	cv, _ := data["current_version"].(map[string]any)
	if cv == nil {
		return
	}
	item := str(data["item"])
	if item == "" {
		item = str(data["name"])
	}
	_, _ = l.db.ExecContext(ctx, `INSERT INTO grades(op_id, ts, project, timeline, item, track, version, version_type, action) VALUES (?,?,?,?,?,?,?,?,?)`,
		e.ID, e.TS, e.Project, e.Timeline, item, str(data["track"]), str(cv["versionName"]), num(cv["versionType"]), str(args["action"]))
}

func (l *Log) deriveTitle(ctx context.Context, e Entry, data map[string]any) {
	rb, _ := data["readback"].(map[string]any)
	if rb == nil {
		return
	}
	text, ok := rb["StyledText"].(string)
	if !ok {
		return
	}
	inputs, _ := json.Marshal(rb)
	_, _ = l.db.ExecContext(ctx, `INSERT INTO titles(op_id, ts, project, timeline, item, tool, text, inputs) VALUES (?,?,?,?,?,?,?,?)`,
		e.ID, e.TS, e.Project, e.Timeline, str(data["item"]), str(data["tool"]), text, string(inputs))
}

func (l *Log) deriveTimelineOp(ctx context.Context, e Entry, data map[string]any, args map[string]any) {
	timeline := e.Timeline
	if t := str(data["timeline"]); t != "" {
		timeline = t
	}
	if t := str(data["created_timeline"]); t != "" {
		timeline = t
	}
	add := func(item, track string, start, dur int64, detail any) {
		d, _ := json.Marshal(detail)
		_, _ = l.db.ExecContext(ctx, `INSERT INTO timeline_ops(op_id, ts, project, timeline, op, item, track, start, duration, detail) VALUES (?,?,?,?,?,?,?,?,?,?)`,
			e.ID, e.TS, e.Project, timeline, e.Command, item, track, start, dur, string(d))
	}
	switch e.Command {
	case "append", "build":
		placed, _ := data["placed"].([]any)
		for _, p := range placed {
			pm, _ := p.(map[string]any)
			add(str(pm["name"]), str(pm["track"]), num(pm["start"]), num(pm["duration"]), pm)
		}
	case "delete-items":
		deleted, _ := data["deleted"].([]any)
		for _, d := range deleted {
			add(str(d), "", 0, 0, map[string]any{"ripple": data["ripple"]})
		}
	case "insert-generator":
		im, _ := data["item"].(map[string]any)
		add(str(im["name"]), str(im["track"]), num(im["start"]), num(im["duration"]), data)
	case "add-transition":
		tm, _ := data["transition"].(map[string]any)
		add(str(tm["name"]), str(tm["track"]), num(tm["start"]), num(tm["duration"]), data["options"])
	default:
		add(str(data["item"]), str(data["track"]), 0, 0, data)
	}
}

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

// Runs lists the newest entries (optionally one command, optionally failures only).
func (l *Log) Runs(ctx context.Context, command string, failedOnly bool, limit int) ([]Entry, error) {
	q := `SELECT id, ts, method, path, command, status, ok, exit_code, duration_ms, COALESCE(project,''), COALESCE(timeline,''), COALESCE(page,''), COALESCE(error,'') FROM ops WHERE 1=1`
	var a []any
	if command != "" {
		q += " AND command = ?"
		a = append(a, command)
	}
	if failedOnly {
		q += " AND ok = 0"
	}
	q += " ORDER BY id DESC LIMIT ?"
	a = append(a, limit)
	rows, err := l.db.QueryContext(ctx, q, a...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Entry
	for rows.Next() {
		var e Entry
		var ok int
		if err := rows.Scan(&e.ID, &e.TS, &e.Method, &e.Path, &e.Command, &e.Status, &ok, &e.ExitCode, &e.DurationMS, &e.Project, &e.Timeline, &e.Page, &e.Error); err != nil {
			return nil, err
		}
		e.OK = ok == 1
		out = append(out, e)
	}
	return out, rows.Err()
}

// Run returns one entry with its request and response bodies.
func (l *Log) Run(ctx context.Context, id int64) (*Entry, error) {
	row := l.db.QueryRowContext(ctx, `SELECT id, ts, method, path, command, COALESCE(args,''), status, ok, exit_code, duration_ms, COALESCE(project,''), COALESCE(timeline,''), COALESCE(page,''), COALESCE(error,''), COALESCE(response,'') FROM ops WHERE id = ?`, id)
	var e Entry
	var ok int
	var args, resp string
	if err := row.Scan(&e.ID, &e.TS, &e.Method, &e.Path, &e.Command, &args, &e.Status, &ok, &e.ExitCode, &e.DurationMS, &e.Project, &e.Timeline, &e.Page, &e.Error, &resp); err != nil {
		return nil, err
	}
	e.OK = ok == 1
	if args != "" {
		e.Args = json.RawMessage(args)
	}
	if resp != "" {
		e.Response = json.RawMessage(resp)
	}
	return &e, nil
}

// RunStats summarises the log: totals, failures, busy (watchdog) trips, per-command counts.
type RunStats struct {
	Total    int            `json:"total"`
	Failed   int            `json:"failed"`
	Busy     int            `json:"busy_trips"`
	Commands map[string]int `json:"commands"`
	First    string         `json:"first_ts,omitempty"`
	Last     string         `json:"last_ts,omitempty"`
}

func (l *Log) Stats(ctx context.Context) (*RunStats, error) {
	s := &RunStats{Commands: map[string]int{}}
	if err := l.db.QueryRowContext(ctx, `SELECT COUNT(*), COALESCE(SUM(ok=0),0), COALESCE(SUM(exit_code=3),0), COALESCE(MIN(ts),''), COALESCE(MAX(ts),'') FROM ops`).
		Scan(&s.Total, &s.Failed, &s.Busy, &s.First, &s.Last); err != nil {
		return nil, err
	}
	rows, err := l.db.QueryContext(ctx, `SELECT command, COUNT(*) FROM ops GROUP BY command`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var c string
		var n int
		if err := rows.Scan(&c, &n); err != nil {
			return nil, err
		}
		s.Commands[c] = n
	}
	return s, rows.Err()
}

// Marker is one observed marker (newest observation per place wins in SearchMarkers).
type Marker struct {
	Project    string `json:"project"`
	Timeline   string `json:"timeline,omitempty"`
	Clip       string `json:"clip,omitempty"`
	Item       string `json:"item,omitempty"`
	Frame      int64  `json:"frame"`
	Color      string `json:"color"`
	Name       string `json:"name"`
	Note       string `json:"note"`
	CustomData string `json:"custom_data,omitempty"`
	SeenAt     string `json:"seen_at"`
	OpID       int64  `json:"op_id"`
}

// SearchMarkers runs an FTS query over name/note/customData across every project observed.
func (l *Log) SearchMarkers(ctx context.Context, query, project string, limit int) ([]Marker, error) {
	q := `SELECT m.project, COALESCE(m.timeline,''), COALESCE(m.clip,''), COALESCE(m.item,''), m.frame, COALESCE(m.color,''), COALESCE(m.name,''), COALESCE(m.note,''), COALESCE(m.custom_data,''), m.ts, m.op_id
	      FROM markers_fts f JOIN markers m ON m.rowid = f.rowid WHERE markers_fts MATCH ?`
	a := []any{ftsQuery(query)}
	if project != "" {
		q += " AND m.project = ?"
		a = append(a, project)
	}
	q += " ORDER BY m.ts DESC LIMIT ?"
	a = append(a, limit*4)
	rows, err := l.db.QueryContext(ctx, q, a...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	seen := map[string]bool{}
	var out []Marker
	for rows.Next() {
		var m Marker
		if err := rows.Scan(&m.Project, &m.Timeline, &m.Clip, &m.Item, &m.Frame, &m.Color, &m.Name, &m.Note, &m.CustomData, &m.SeenAt, &m.OpID); err != nil {
			return nil, err
		}
		key := m.Project + "|" + m.Timeline + "|" + m.Clip + "|" + m.Item + "|" + strconv.FormatInt(m.Frame, 10)
		if seen[key] {
			continue // an older observation of the same marker
		}
		seen[key] = true
		out = append(out, m)
		if len(out) >= limit {
			break
		}
	}
	return out, rows.Err()
}

// Render is one render-related observation.
type Render struct {
	TS           string  `json:"ts"`
	Project      string  `json:"project"`
	Timeline     string  `json:"timeline,omitempty"`
	Kind         string  `json:"kind"`
	Job          string  `json:"job,omitempty"`
	Preset       string  `json:"preset,omitempty"`
	JobStatus    string  `json:"job_status,omitempty"`
	OutDir       string  `json:"out_dir,omitempty"`
	Name         string  `json:"name,omitempty"`
	File         string  `json:"file,omitempty"`
	Bytes        int64   `json:"bytes,omitempty"`
	Codec        string  `json:"codec,omitempty"`
	Width        int64   `json:"width,omitempty"`
	Height       int64   `json:"height,omitempty"`
	FPS          float64 `json:"fps,omitempty"`
	Duration     float64 `json:"duration,omitempty"`
	AudioStreams int64   `json:"audio_streams,omitempty"`
	Verified     *bool   `json:"verified,omitempty"`
	Problems     string  `json:"problems,omitempty"`
	OpID         int64   `json:"op_id"`
}

func (l *Log) Renders(ctx context.Context, project string, limit int) ([]Render, error) {
	q := `SELECT ts, COALESCE(project,''), COALESCE(timeline,''), kind, COALESCE(job,''), COALESCE(preset,''), COALESCE(job_status,''), COALESCE(out_dir,''), COALESCE(name,''), COALESCE(file,''), COALESCE(bytes,0), COALESCE(codec,''), COALESCE(width,0), COALESCE(height,0), COALESCE(fps,0), COALESCE(duration,0), COALESCE(audio_streams,0), verified, COALESCE(problems,''), op_id FROM renders WHERE 1=1`
	var a []any
	if project != "" {
		q += " AND project = ?"
		a = append(a, project)
	}
	q += " ORDER BY op_id DESC LIMIT ?"
	a = append(a, limit)
	rows, err := l.db.QueryContext(ctx, q, a...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Render
	for rows.Next() {
		var r Render
		var verified sql.NullInt64
		if err := rows.Scan(&r.TS, &r.Project, &r.Timeline, &r.Kind, &r.Job, &r.Preset, &r.JobStatus, &r.OutDir, &r.Name, &r.File, &r.Bytes, &r.Codec, &r.Width, &r.Height, &r.FPS, &r.Duration, &r.AudioStreams, &verified, &r.Problems, &r.OpID); err != nil {
			return nil, err
		}
		if verified.Valid {
			v := verified.Int64 == 1
			r.Verified = &v
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// Grade is the newest grade-version observation for one timeline item.
type Grade struct {
	Project     string `json:"project"`
	Timeline    string `json:"timeline"`
	Item        string `json:"item"`
	Track       string `json:"track,omitempty"`
	Version     string `json:"version"`
	VersionType int64  `json:"version_type"`
	SeenAt      string `json:"seen_at"`
	Latest      string `json:"latest_version_seen"`
	Stale       bool   `json:"stale"`
}

// GradeAudit returns the newest observed version per (project, timeline, item) and
// flags items whose version is not the newest version name observed anywhere in
// that project ("latest" = the version most recently created/loaded/renamed there
// through `color version`; a plain `color info` read never advances it, so an
// old version seen on one item cannot mark every other item stale).
func (l *Log) GradeAudit(ctx context.Context, project string) ([]Grade, error) {
	q := `SELECT g.project, COALESCE(g.timeline,''), g.item, COALESCE(g.track,''), g.version, g.version_type, g.ts
	      FROM grades g JOIN (SELECT project, timeline, item, MAX(op_id) AS mx FROM grades GROUP BY project, timeline, item) n
	      ON n.mx = g.op_id AND n.project = g.project AND COALESCE(n.timeline,'') = COALESCE(g.timeline,'') AND n.item = g.item WHERE 1=1`
	var a []any
	if project != "" {
		q += " AND g.project = ?"
		a = append(a, project)
	}
	q += " ORDER BY g.project, g.timeline, g.item"
	rows, err := l.db.QueryContext(ctx, q, a...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Grade
	for rows.Next() {
		var g Grade
		if err := rows.Scan(&g.Project, &g.Timeline, &g.Item, &g.Track, &g.Version, &g.VersionType, &g.SeenAt); err != nil {
			return nil, err
		}
		out = append(out, g)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	latest := map[string]string{}
	lr, err := l.db.QueryContext(ctx, `SELECT project, version FROM grades WHERE action IN ('add','load','rename') ORDER BY op_id`)
	if err != nil {
		return nil, err
	}
	defer lr.Close()
	for lr.Next() {
		var p, v string
		if err := lr.Scan(&p, &v); err != nil {
			return nil, err
		}
		latest[p] = v
	}
	for i := range out {
		out[i].Latest = latest[out[i].Project]
		out[i].Stale = out[i].Latest != "" && out[i].Latest != out[i].Version
	}
	return out, nil
}

// Title is one observed Text+ write.
type Title struct {
	TS       string `json:"ts"`
	Project  string `json:"project"`
	Timeline string `json:"timeline,omitempty"`
	Item     string `json:"item"`
	Tool     string `json:"tool"`
	Text     string `json:"text"`
	OpID     int64  `json:"op_id"`
}

func (l *Log) SearchTitles(ctx context.Context, query, project string, limit int) ([]Title, error) {
	q := `SELECT t.ts, t.project, COALESCE(t.timeline,''), COALESCE(t.item,''), COALESCE(t.tool,''), t.text, t.op_id FROM titles_fts f JOIN titles t ON t.rowid = f.rowid WHERE titles_fts MATCH ?`
	a := []any{ftsQuery(query)}
	if project != "" {
		q += " AND t.project = ?"
		a = append(a, project)
	}
	q += " ORDER BY t.ts DESC LIMIT ?"
	a = append(a, limit)
	rows, err := l.db.QueryContext(ctx, q, a...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Title
	for rows.Next() {
		var t Title
		if err := rows.Scan(&t.TS, &t.Project, &t.Timeline, &t.Item, &t.Tool, &t.Text, &t.OpID); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

// TimelineOp is one structural change this CLI made to a timeline.
type TimelineOp struct {
	TS       string          `json:"ts"`
	Project  string          `json:"project"`
	Timeline string          `json:"timeline"`
	Op       string          `json:"op"`
	Item     string          `json:"item,omitempty"`
	Track    string          `json:"track,omitempty"`
	Start    int64           `json:"start,omitempty"`
	Duration int64           `json:"duration,omitempty"`
	Detail   json.RawMessage `json:"detail,omitempty"`
	OpID     int64           `json:"op_id"`
}

func (l *Log) TimelineLog(ctx context.Context, timeline, project string, limit int) ([]TimelineOp, error) {
	q := `SELECT ts, COALESCE(project,''), COALESCE(timeline,''), op, COALESCE(item,''), COALESCE(track,''), COALESCE(start,0), COALESCE(duration,0), COALESCE(detail,''), op_id FROM timeline_ops WHERE timeline = ?`
	a := []any{timeline}
	if project != "" {
		q += " AND project = ?"
		a = append(a, project)
	}
	// Page from the NEWEST end so a long trail never hides recent operations
	// behind the limit; the slice is reversed below to read chronologically.
	q += " ORDER BY op_id DESC LIMIT ?"
	a = append(a, limit)
	rows, err := l.db.QueryContext(ctx, q, a...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []TimelineOp
	for rows.Next() {
		var t TimelineOp
		var detail string
		if err := rows.Scan(&t.TS, &t.Project, &t.Timeline, &t.Op, &t.Item, &t.Track, &t.Start, &t.Duration, &detail, &t.OpID); err != nil {
			return nil, err
		}
		if detail != "" {
			t.Detail = json.RawMessage(detail)
		}
		out = append(out, t)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out, nil
}

// Short trims an RFC3339 timestamp to seconds for table output without
// assuming its length (a hand-edited row must not panic a render).
func Short(ts string) string {
	if len(ts) > 19 {
		return ts[:19]
	}
	return ts
}

// Env is one environment observation (a `system status` or `doctor` reaching the sidecar).
type Env struct {
	TS        string   `json:"ts"`
	Product   string   `json:"product,omitempty"`
	Version   string   `json:"version,omitempty"`
	Studio    *bool    `json:"studio,omitempty"`
	Page      string   `json:"page,omitempty"`
	Project   string   `json:"project,omitempty"`
	Timeline  string   `json:"timeline,omitempty"`
	Database  string   `json:"database,omitempty"`
	CLIBuild  string   `json:"sidecar_build,omitempty"`
	SidecarOK bool     `json:"sidecar_ok"`
	Degraded  string   `json:"degraded,omitempty"`
	OpID      int64    `json:"op_id"`
	Changes   []string `json:"changes,omitempty"`
}

func (l *Log) EnvHistory(ctx context.Context, limit int) ([]Env, error) {
	rows, err := l.db.QueryContext(ctx, `SELECT ts, COALESCE(product,''), COALESCE(version,''), studio, COALESCE(page,''), COALESCE(project,''), COALESCE(timeline,''), COALESCE(database,''), COALESCE(cli_build,''), COALESCE(sidecar_ok,0), COALESCE(degraded,''), op_id FROM env ORDER BY op_id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Env
	for rows.Next() {
		var e Env
		var studio sql.NullInt64
		var ok int
		if err := rows.Scan(&e.TS, &e.Product, &e.Version, &studio, &e.Page, &e.Project, &e.Timeline, &e.Database, &e.CLIBuild, &ok, &e.Degraded, &e.OpID); err != nil {
			return nil, err
		}
		if studio.Valid {
			s := studio.Int64 == 1
			e.Studio = &s
		}
		e.SidecarOK = ok == 1
		out = append(out, e)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	// Drift: compare each observation with the previous one (older) that has the field.
	for i := 0; i+1 < len(out); i++ {
		cur, prev := &out[i], out[i+1]
		diff := func(label, a, b string) {
			if a != "" && b != "" && a != b {
				cur.Changes = append(cur.Changes, fmt.Sprintf("%s: %s -> %s", label, b, a))
			}
		}
		diff("resolve", cur.Version, prev.Version)
		diff("sidecar build", cur.CLIBuild, prev.CLIBuild)
		diff("database", cur.Database, prev.Database)
		if cur.Studio != nil && prev.Studio != nil && *cur.Studio != *prev.Studio {
			cur.Changes = append(cur.Changes, fmt.Sprintf("studio: %v -> %v", *prev.Studio, *cur.Studio))
		}
		if cur.SidecarOK != prev.SidecarOK {
			cur.Changes = append(cur.Changes, fmt.Sprintf("sidecar reachable: %v -> %v", prev.SidecarOK, cur.SidecarOK))
		}
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func ftsQuery(q string) string {
	// Quote each term so punctuation in marker text cannot break the FTS grammar.
	var parts []string
	for _, w := range strings.Fields(q) {
		parts = append(parts, `"`+strings.ReplaceAll(w, `"`, `""`)+`"`)
	}
	if len(parts) == 0 {
		return `""`
	}
	return strings.Join(parts, " ")
}

func str(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case float64:
		if t == float64(int64(t)) {
			return strconv.FormatInt(int64(t), 10)
		}
		return strconv.FormatFloat(t, 'f', -1, 64)
	case bool:
		return strconv.FormatBool(t)
	default:
		b, _ := json.Marshal(t)
		return string(b)
	}
}

func num(v any) int64 {
	switch t := v.(type) {
	case float64:
		return int64(t)
	case int64:
		return t
	case int:
		return int64(t)
	case string:
		n, _ := strconv.ParseInt(t, 10, 64)
		return n
	}
	return 0
}

func fnum(v any) float64 {
	switch t := v.(type) {
	case float64:
		return t
	case int64:
		return float64(t)
	case string:
		f, _ := strconv.ParseFloat(t, 64)
		return f
	}
	return 0
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func nullRaw(r json.RawMessage) any {
	if len(r) == 0 {
		return nil
	}
	return string(r)
}
