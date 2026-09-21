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

type doctorHistoryView struct {
	Snapshots []oplog.Env `json:"snapshots"`
	Drift     []string    `json:"drift"`
	Note      string      `json:"note,omitempty"`
}

func newNovelDoctorHistoryCmd(flags *rootFlags) *cobra.Command {
	var limit int
	var record bool
	cmd := &cobra.Command{
		Use:   "history",
		Short: "How this machine's Resolve/sidecar environment drifted over time: version, Studio, database, sidecar build, reachability",
		Long: strings.TrimSpace(`
Time-series of environment snapshots taken whenever this CLI reached the
sidecar's health or status endpoints ('doctor', 'system status'), with the
differences between consecutive snapshots called out: Resolve version, Studio
licence, project database, sidecar build, page-null state, reachability.
The bundled CPython the scripting bridge binds to differed per machine on
21.0.4; drift like that shows up here before it costs a morning.`),
		Example: strings.Trim(`
  davinci-resolve-pp-cli doctor history --json
  davinci-resolve-pp-cli doctor history --record --limit 5
`, "\n"),
		Annotations: historyAnnotations("--limit=5"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if dryRunOK(flags) {
				return writeDryRun(cmd.OutOrStdout(), flags, "doctor history")
			}
			if limit <= 0 {
				limit = 20
			}
			var recordNote string
			if record {
				// One live status read through the recording client adds a fresh snapshot.
				c, err := flags.newClient()
				if err == nil {
					ctx, cancel := boundCtx(cmd.Context(), flags)
					_, err = c.GetNoCache(ctx, "/v1/status", nil)
					cancel()
				}
				if err != nil {
					recordNote = "snapshot NOT recorded: " + err.Error()
					fmt.Fprintf(cmd.ErrOrStderr(), "warning: --record could not reach the sidecar: %v\n", err)
				}
			}
			l, ok, err := openHistory(cmd, flags, doctorHistoryView{Snapshots: []oplog.Env{}, Drift: []string{}})
			if !ok || err != nil {
				return err
			}
			defer l.Close()
			rows, err := l.EnvHistory(cmd.Context(), limit)
			if err != nil {
				return err
			}
			view := doctorHistoryView{Snapshots: rows, Drift: []string{}}
			for _, r := range rows {
				for _, ch := range r.Changes {
					view.Drift = append(view.Drift, oplog.Short(r.TS)+" "+ch)
				}
			}
			if len(rows) == 0 {
				view.Snapshots = []oplog.Env{}
				view.Note = "no environment snapshots yet; run 'doctor' or 'system status' (or add --record)"
			}
			if recordNote != "" {
				view.Note = strings.TrimSpace(view.Note + " " + recordNote)
			}
			if wantsHumanTable(cmd.OutOrStdout(), flags) {
				w := newTabWriter(cmd.OutOrStdout())
				fmt.Fprintln(w, "WHEN\tRESOLVE\tSTUDIO\tPAGE\tPROJECT\tDATABASE\tSIDECAR\tCHANGES")
				for _, r := range rows {
					studio := ""
					if r.Studio != nil {
						studio = strconv.FormatBool(*r.Studio)
					}
					fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\t%v\t%s\n", oplog.Short(r.TS), r.Version, studio, r.Page, r.Project, r.Database, r.SidecarOK, strings.Join(r.Changes, "; "))
				}
				return w.Flush()
			}
			return printJSONFiltered(cmd.OutOrStdout(), view, flags)
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 20, "newest N snapshots")
	cmd.Flags().BoolVar(&record, "record", false, "take a fresh snapshot first (one live 'system status' call)")
	return cmd
}
