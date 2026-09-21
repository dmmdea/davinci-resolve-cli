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

type timelineLogView struct {
	Timeline string             `json:"timeline"`
	Project  string             `json:"project,omitempty"`
	Ops      []oplog.TimelineOp `json:"ops"`
	Note     string             `json:"note,omitempty"`
}

func newNovelTimelineLogCmd(flags *rootFlags) *cobra.Command {
	var limit int
	var project string
	cmd := &cobra.Command{
		Use:   "log [timeline]",
		Short: "The chronological append/delete/insert/transition trail this CLI left on one timeline -- Resolve keeps no undo history",
		Long: strings.TrimSpace(`
AppendToTimeline is the only placement primitive the scripting API exposes and
there is no undo/history endpoint, so this local trail is the only record of
what actually changed: every append (with the read-back start/duration),
delete, generator/title insert, transition, compound/fusion clip and multicam
operation this CLI performed on the named timeline.`),
		Example: strings.Trim(`
  davinci-resolve-pp-cli timeline log "Episode 12 v3" --json
  davinci-resolve-pp-cli timeline log "Episode 12 v3" --project "Weekly Show" --limit 200
`, "\n"),
		Annotations: historyAnnotations("timeline=example"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 0 && cmd.Flags().NFlag() == 0 {
				return cmd.Help()
			}
			if dryRunOK(flags) {
				return writeDryRun(cmd.OutOrStdout(), flags, "timeline log")
			}
			if len(args) < 1 || strings.TrimSpace(args[0]) == "" {
				_ = cmd.Usage()
				return usageErr(fmt.Errorf("a timeline name is required"))
			}
			if limit <= 0 {
				limit = 500
			}
			l, ok, err := openHistory(cmd, flags, timelineLogView{Timeline: args[0], Project: project, Ops: []oplog.TimelineOp{}})
			if !ok || err != nil {
				return err
			}
			defer l.Close()
			rows, err := l.TimelineLog(cmd.Context(), args[0], project, limit)
			if err != nil {
				return err
			}
			view := timelineLogView{Timeline: args[0], Project: project, Ops: rows}
			if len(rows) == 0 {
				// Exit 3: nothing recorded under that name. A typo and an untouched
				// timeline look the same to the local log, so the message says so.
				return notFoundErr(fmt.Errorf("no operations recorded for timeline %q by this CLI (check the name with `timeline list`, or drive it once so the log has rows)", args[0]))
			}
			if wantsHumanTable(cmd.OutOrStdout(), flags) {
				w := newTabWriter(cmd.OutOrStdout())
				fmt.Fprintln(w, "WHEN\tOP\tTRACK\tITEM\tSTART\tDURATION")
				for _, t := range rows {
					fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%d\t%d\n", oplog.Short(t.TS), t.Op, t.Track, truncate(t.Item, 40), t.Start, t.Duration)
				}
				return w.Flush()
			}
			return printJSONFiltered(cmd.OutOrStdout(), view, flags)
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 500, "maximum operations to return (oldest first)")
	cmd.Flags().StringVar(&project, "project", "", "restrict to one project name (timeline names repeat across projects)")
	return cmd
}
