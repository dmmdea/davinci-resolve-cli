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

type colorAuditView struct {
	Project string        `json:"project,omitempty"`
	Items   []oplog.Grade `json:"items"`
	Stale   int           `json:"stale_count"`
	Note    string        `json:"note,omitempty"`
}

func newNovelColorAuditCmd(flags *rootFlags) *cobra.Command {
	var project string
	cmd := &cobra.Command{
		Use:   "audit",
		Short: "Which timeline items are on which grade version across every project this CLI has graded, and which are behind",
		Long: strings.TrimSpace(`
Cross-project join of every grade-version observation this CLI made ('color
version add/load/rename', 'color info') against the newest version name seen
in each project. An item whose last observed version differs from the
project's latest is flagged stale. No Resolve endpoint answers this question;
it needs every project opened in turn.`),
		Example: strings.Trim(`
  davinci-resolve-pp-cli color audit --json
  davinci-resolve-pp-cli color audit --project "Season 2" --json --select item,version,stale
`, "\n"),
		Annotations: historyAnnotations("--project=example"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if dryRunOK(flags) {
				return writeDryRun(cmd.OutOrStdout(), flags, "color audit")
			}
			l, ok, err := openHistory(cmd, flags, colorAuditView{Project: project, Items: []oplog.Grade{}})
			if !ok || err != nil {
				return err
			}
			defer l.Close()
			rows, err := l.GradeAudit(cmd.Context(), project)
			if err != nil {
				return err
			}
			view := colorAuditView{Project: project, Items: rows}
			for _, g := range rows {
				if g.Stale {
					view.Stale++
				}
			}
			if len(rows) == 0 {
				view.Items = []oplog.Grade{}
				view.Note = "no grade versions observed yet ('color info' / 'color version' populate this)"
			}
			if wantsHumanTable(cmd.OutOrStdout(), flags) {
				w := newTabWriter(cmd.OutOrStdout())
				fmt.Fprintln(w, "PROJECT\tTIMELINE\tITEM\tVERSION\tLATEST\tSTALE\tSEEN")
				for _, g := range rows {
					fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%v\t%s\n", g.Project, g.Timeline, g.Item, g.Version, g.Latest, g.Stale, oplog.Short(g.SeenAt))
				}
				return w.Flush()
			}
			return printJSONFiltered(cmd.OutOrStdout(), view, flags)
		},
	}
	cmd.Flags().StringVar(&project, "project", "", "restrict to one project name")
	return cmd
}
