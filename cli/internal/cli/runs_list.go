// Copyright 2026 dmmdea and contributors. Licensed under Apache-2.0. See LICENSE.
// Novel command: IMPLEMENTED (hand-authored body over internal/oplog, preserved across
// generate --force). Reads the local operation log written by oplog_hook.go.
// pp:data-source local

package cli

import (
	"fmt"
	"strings"

	"github.com/spf13/cobra"

	"github.com/dmmdea/davinci-resolve-cli/cli/internal/oplog"
)

type runsListView struct {
	History string          `json:"history_db"`
	Stats   *oplog.RunStats `json:"stats,omitempty"`
	Runs    []oplog.Entry   `json:"runs"`
	Note    string          `json:"note,omitempty"`
}

func newNovelRunsListCmd(flags *rootFlags) *cobra.Command {
	var limit int
	var command string
	var failed bool
	cmd := &cobra.Command{
		Use:   "list",
		Short: "Every command this CLI has run against the sidecar: outcome, exit code, watchdog busy-trips, duration",
		Long: strings.TrimSpace(`
Local log of every sidecar round-trip this CLI issued (build, render, colour,
Fusion writes, doctor checks), with success/failure, the bridge's typed exit
code (3 = Resolve busy behind a modal and abandoned by the watchdog), timing
and the project/timeline it acted on. Resolve exposes none of this.

Use it for a general audit of what ran. For render delivery outcomes use
'render history'; for one timeline's structural edit trail use 'timeline log';
for environment drift use 'doctor history'.`),
		Example: strings.Trim(`
  davinci-resolve-pp-cli runs list --json --select id,command,ok,exit_code,duration_ms
  davinci-resolve-pp-cli runs list --failed --limit 20
  davinci-resolve-pp-cli runs list --command append --json
`, "\n"),
		Annotations: historyAnnotations("--limit=5"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if dryRunOK(flags) {
				return writeDryRun(cmd.OutOrStdout(), flags, "runs list")
			}
			if limit <= 0 {
				limit = 50
			}
			l, ok, err := openHistory(cmd, flags, runsListView{History: historyPath(), Runs: []oplog.Entry{}})
			if !ok || err != nil {
				return err
			}
			defer l.Close()
			runs, err := l.Runs(cmd.Context(), command, failed, limit)
			if err != nil {
				return err
			}
			stats, _ := l.Stats(cmd.Context())
			view := runsListView{History: l.Path(), Stats: stats, Runs: runs}
			if len(runs) == 0 {
				view.Runs = []oplog.Entry{}
				view.Note = "no matching runs recorded yet"
			}
			if wantsHumanTable(cmd.OutOrStdout(), flags) {
				w := newTabWriter(cmd.OutOrStdout())
				fmt.Fprintln(w, "ID\tWHEN\tCOMMAND\tOK\tEXIT\tMS\tPROJECT\tTIMELINE\tERROR")
				for _, r := range runs {
					fmt.Fprintf(w, "%d\t%s\t%s\t%v\t%d\t%d\t%s\t%s\t%s\n", r.ID, oplog.Short(r.TS), r.Command, r.OK, r.ExitCode, r.DurationMS, r.Project, r.Timeline, truncate(r.Error, 60))
				}
				return w.Flush()
			}
			return printJSONFiltered(cmd.OutOrStdout(), view, flags)
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 50, "newest N runs")
	cmd.Flags().StringVar(&command, "command", "", "only this sidecar command (e.g. append, render, add-marker)")
	cmd.Flags().BoolVar(&failed, "failed", false, "only runs that did not succeed (includes exit 3 busy-trips)")
	return cmd
}
