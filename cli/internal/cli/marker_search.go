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

type markerSearchView struct {
	Query   string         `json:"query"`
	Markers []oplog.Marker `json:"markers"`
	Note    string         `json:"note,omitempty"`
}

func newNovelMarkerSearchCmd(flags *rootFlags) *cobra.Command {
	var limit int
	var project string
	cmd := &cobra.Command{
		Use:   "search [query]",
		Short: "Find a marker by text across every project this CLI has touched, not just the one open in Resolve",
		Long: strings.TrimSpace(`
Full-text search over marker names, notes and customData observed whenever this
CLI read or wrote markers (timeline markers, clip markers, add-marker...), across
ALL projects. Resolve's own marker search is single-project. Only markers this CLI
has seen are indexed: run 'timeline markers' / 'clip markers' on a project to index it.`),
		Example: strings.Trim(`
  davinci-resolve-pp-cli marker search "pickup shot" --json
  davinci-resolve-pp-cli marker search approved --project "Client Q3" --limit 20
`, "\n"),
		Annotations: historyAnnotations("query=marker"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 0 && cmd.Flags().NFlag() == 0 {
				return cmd.Help()
			}
			if dryRunOK(flags) {
				return writeDryRun(cmd.OutOrStdout(), flags, "marker search")
			}
			if len(args) < 1 || strings.TrimSpace(args[0]) == "" {
				_ = cmd.Usage()
				return usageErr(fmt.Errorf("a search query is required"))
			}
			if limit <= 0 {
				limit = 25
			}
			l, ok, err := openHistory(cmd, flags, markerSearchView{Query: args[0], Markers: []oplog.Marker{}})
			if !ok || err != nil {
				return err
			}
			defer l.Close()
			rows, err := l.SearchMarkers(cmd.Context(), args[0], project, limit)
			if err != nil {
				return err
			}
			view := markerSearchView{Query: args[0], Markers: rows}
			if len(rows) == 0 {
				view.Markers = []oplog.Marker{}
				view.Note = "no indexed marker matches; markers are indexed when this CLI reads or writes them (timeline markers, clip markers, add-marker)"
			}
			if wantsHumanTable(cmd.OutOrStdout(), flags) {
				w := newTabWriter(cmd.OutOrStdout())
				fmt.Fprintln(w, "PROJECT\tTIMELINE/CLIP\tFRAME\tCOLOR\tNAME\tNOTE\tSEEN")
				for _, m := range rows {
					where := m.Timeline
					if m.Clip != "" {
						where = "clip:" + m.Clip
					}
					fmt.Fprintf(w, "%s\t%s\t%d\t%s\t%s\t%s\t%s\n", m.Project, where, m.Frame, m.Color, m.Name, truncate(m.Note, 40), oplog.Short(m.SeenAt))
				}
				return w.Flush()
			}
			return printJSONFiltered(cmd.OutOrStdout(), view, flags)
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 25, "maximum markers to return")
	cmd.Flags().StringVar(&project, "project", "", "restrict to one project name")
	return cmd
}
