// Copyright 2026 dmmdea and contributors. Licensed under Apache-2.0. See LICENSE.
// Hand-authored (preserved across generate --force): records every sidecar
// round-trip into the local history (internal/oplog) by wrapping the HTTP
// transport of every client the CLI builds. The seven history commands read
// what this writes. Recording never changes a command's outcome: a history
// failure is reported on stderr only when DAVINCI_RESOLVE_HISTORY_DEBUG=1.

package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/spf13/cobra"

	"github.com/dmmdea/davinci-resolve-cli/cli/internal/client"
	"github.com/dmmdea/davinci-resolve-cli/cli/internal/oplog"
)

func init() {
	// A control surface, not a data API: every read answers the live Resolve
	// state and a local mirror of it is stale the moment it is written, so the
	// default data source is `live` (the generated auto/local modes stay
	// selectable with --data-source). This also stops the generated read-through
	// cache from trying to key envelope responses that carry no id.
	registerNovelCommand(func(root *cobra.Command, flags *rootFlags) {
		flags.dataSource = "live"
		if f := root.PersistentFlags().Lookup("data-source"); f != nil {
			f.DefValue = "live"
			_ = f.Value.Set("live")
		}
	})
	registerClientHook(func(c *client.Client) error {
		if c == nil || c.HTTPClient == nil || c.DryRun {
			return nil
		}
		if _, already := c.HTTPClient.Transport.(*recordingTransport); already {
			return nil
		}
		if os.Getenv("DAVINCI_RESOLVE_HISTORY") == "off" {
			return nil
		}
		c.HTTPClient.Transport = &recordingTransport{next: c.HTTPClient.Transport}
		return nil
	})
}

var (
	historyOnce sync.Once
	historyLog  *oplog.Log
	historyErr  error
)

// history opens the log once per process (lazily, on the first recorded call).
func history() (*oplog.Log, error) {
	historyOnce.Do(func() {
		path, err := oplog.DefaultPath()
		if err != nil {
			historyErr = err
			return
		}
		historyLog, historyErr = oplog.Open(context.Background(), path)
	})
	return historyLog, historyErr
}

// historyPath is the file the query commands read (without opening it).
func historyPath() string {
	p, err := oplog.DefaultPath()
	if err != nil {
		return "history.db"
	}
	return p
}

type recordingTransport struct {
	next http.RoundTripper
}

func (t *recordingTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	next := t.next
	if next == nil {
		next = http.DefaultTransport
	}
	var reqBody []byte
	if req.Body != nil && req.Body != http.NoBody {
		reqBody, _ = io.ReadAll(req.Body)
		req.Body = io.NopCloser(bytes.NewReader(reqBody))
	}
	start := time.Now()
	resp, err := next.RoundTrip(req)
	elapsed := time.Since(start)
	entry := oplog.Entry{
		TS:         start.UTC().Format(time.RFC3339Nano),
		Method:     req.Method,
		Path:       req.URL.Path,
		Command:    commandFromPath(req.URL.Path),
		DurationMS: elapsed.Milliseconds(),
	}
	if len(reqBody) > 0 && json.Valid(reqBody) {
		entry.Args = json.RawMessage(reqBody)
	} else if req.URL.RawQuery != "" {
		q := map[string]any{}
		for k, v := range req.URL.Query() {
			if len(v) == 1 {
				q[k] = v[0]
			} else {
				q[k] = v
			}
		}
		if b, merr := json.Marshal(q); merr == nil {
			entry.Args = b
		}
	}
	if err != nil {
		entry.Error = err.Error()
		entry.ExitCode = 1
		recordHistory(entry)
		return nil, err
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	resp.Body = io.NopCloser(bytes.NewReader(body))
	entry.Status = resp.StatusCode
	if json.Valid(body) {
		entry.Response = json.RawMessage(body)
		var env struct {
			OK       bool   `json:"ok"`
			ExitCode int    `json:"exit_code"`
			Error    string `json:"error"`
			Context  *struct {
				Project  string `json:"project"`
				Timeline string `json:"timeline"`
				Page     string `json:"page"`
			} `json:"context"`
		}
		if json.Unmarshal(body, &env) == nil {
			entry.OK = env.OK
			entry.ExitCode = env.ExitCode
			entry.Error = env.Error
			if env.Context != nil {
				entry.Project, entry.Timeline, entry.Page = env.Context.Project, env.Context.Timeline, env.Context.Page
			}
		}
	} else {
		entry.OK = resp.StatusCode >= 200 && resp.StatusCode < 300
	}
	recordHistory(entry)
	return resp, nil
}

func recordHistory(e oplog.Entry) {
	l, err := history()
	if err == nil {
		_, err = l.Record(context.Background(), e)
	}
	if err != nil && os.Getenv("DAVINCI_RESOLVE_HISTORY_DEBUG") == "1" {
		fmt.Fprintf(os.Stderr, "history: %v\n", err)
	}
}

func commandFromPath(p string) string {
	if p == "/healthz" {
		return "healthz"
	}
	return strings.TrimPrefix(strings.TrimPrefix(p, "/v1/"), "/")
}
