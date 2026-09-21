// Copyright 2026 dmmdea and contributors. Licensed under Apache-2.0. See LICENSE.
// Novel command: IMPLEMENTED (hand-authored body over internal/oplog, preserved across
// generate --force). Reads the local operation log written by oplog_hook.go.
// pp:data-source local

package cli

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"github.com/dmmdea/davinci-resolve-cli/cli/internal/oplog"
)

type renderHistoryView struct {
	Project string         `json:"project,omitempty"`
	Renders []oplog.Render `json:"renders"`
	Note    string         `json:"note,omitempty"`
}

func newNovelRenderHistoryCmd(flags *rootFlags) *cobra.Command {
	var limit int
	var project string
	cmd := &cobra.Command{
		Use:   "history",
		Short: "Every render this CLI queued, started or verified, across all projects, with the ffprobe verdict on the output file",
		Long: strings.TrimSpace(`
Persisted render outcomes (queued job, start/poll status, quick exports, the
bridge's render/smoke proofs) joined with 'render verify-render' results:
codec, size, fps, duration, audio streams and any spec problems. Resolve has
no render-settings readback API and GetRenderJobStatus sometimes answers with
a bare error string; the FILE is the only proof, and this is where it is kept.`),
		Example: strings.Trim(`
  davinci-resolve-pp-cli render history --json --select project,file,codec,verified
  davinci-resolve-pp-cli render history --project "Client Q3" --agent
`, "\n"),
		Annotations: historyAnnotations("--limit=5"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if dryRunOK(flags) {
				return writeDryRun(cmd.OutOrStdout(), flags, "render history")
			}
			if limit <= 0 {
				limit = 50
			}
			l, ok, err := openHistory(cmd, flags, renderHistoryView{Project: project, Renders: []oplog.Render{}})
			if !ok || err != nil {
				return err
			}
			defer l.Close()
			rows, err := l.Renders(cmd.Context(), project, limit)
			if err != nil {
				return err
			}
			view := renderHistoryView{Project: project, Renders: rows}
			if len(rows) == 0 {
				view.Renders = []oplog.Render{}
				view.Note = "no renders recorded yet (render add-job / start / quick-export / verify-render populate this)"
			}
			if wantsHumanTable(cmd.OutOrStdout(), flags) {
				w := newTabWriter(cmd.OutOrStdout())
				fmt.Fprintln(w, "WHEN\tPROJECT\tKIND\tJOB/FILE\tSTATUS\tCODEC\tSIZE\tVERIFIED")
				for _, r := range rows {
					ref := r.File
					if ref == "" {
						ref = r.Job
					}
					verified := ""
					if r.Verified != nil {
						verified = strconv.FormatBool(*r.Verified)
					}
					fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\t%dx%d\t%s\n", oplog.Short(r.TS), r.Project, r.Kind, truncate(ref, 48), r.JobStatus, r.Codec, r.Width, r.Height, verified)
				}
				return w.Flush()
			}
			return printJSONFiltered(cmd.OutOrStdout(), view, flags)
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 50, "newest N render observations")
	cmd.Flags().StringVar(&project, "project", "", "restrict to one project name")
	return cmd
}
