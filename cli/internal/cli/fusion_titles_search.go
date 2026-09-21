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

type titlesSearchView struct {
	Query  string        `json:"query"`
	Titles []oplog.Title `json:"titles"`
	Note   string        `json:"note,omitempty"`
}

func newNovelFusionTitlesSearchCmd(flags *rootFlags) *cobra.Command {
	var limit int
	var project string
	cmd := &cobra.Command{
		Use:   "search [query]",
		Short: "Find what text a past Text+ title carried, across every project, without reopening it",
		Long: strings.TrimSpace(`
Catalog of every Text+ StyledText this CLI wrote ('fusion set-title', 'fusion
set' with StyledText), searchable by words. No Fusion call returns historical
title text across projects.`),
		Example: strings.Trim(`
  davinci-resolve-pp-cli fusion titles search "welcome back" --json
  davinci-resolve-pp-cli fusion titles search episode --project "Weekly Show" --limit 10
`, "\n"),
		Annotations: historyAnnotations("query=title"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 0 && cmd.Flags().NFlag() == 0 {
				return cmd.Help()
			}
			if dryRunOK(flags) {
				return writeDryRun(cmd.OutOrStdout(), flags, "fusion titles search")
			}
			if len(args) < 1 || strings.TrimSpace(args[0]) == "" {
				_ = cmd.Usage()
				return usageErr(fmt.Errorf("a search query is required"))
			}
			if limit <= 0 {
				limit = 25
			}
			l, ok, err := openHistory(cmd, flags, titlesSearchView{Query: args[0], Titles: []oplog.Title{}})
			if !ok || err != nil {
				return err
			}
			defer l.Close()
			rows, err := l.SearchTitles(cmd.Context(), args[0], project, limit)
			if err != nil {
				return err
			}
			view := titlesSearchView{Query: args[0], Titles: rows}
			if len(rows) == 0 {
				view.Titles = []oplog.Title{}
				view.Note = "no indexed title matches; titles are indexed when this CLI writes them (fusion set-title / fusion set)"
			}
			if wantsHumanTable(cmd.OutOrStdout(), flags) {
				w := newTabWriter(cmd.OutOrStdout())
				fmt.Fprintln(w, "WHEN\tPROJECT\tTIMELINE\tITEM\tTEXT")
				for _, t := range rows {
					fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\n", oplog.Short(t.TS), t.Project, t.Timeline, t.Item, truncate(t.Text, 60))
				}
				return w.Flush()
			}
			return printJSONFiltered(cmd.OutOrStdout(), view, flags)
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 25, "maximum titles to return")
	cmd.Flags().StringVar(&project, "project", "", "restrict to one project name")
	return cmd
}
