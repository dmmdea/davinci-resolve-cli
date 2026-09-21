// Copyright 2026 dmmdea and contributors. Licensed under Apache-2.0. See LICENSE.
// Hand-authored (preserved across generate --force): the seven history
// commands over the local operation log (internal/oplog) -- the data DaVinci
// Resolve itself never keeps. Registered through registerNovelCommand so they
// replace the generated TODO scaffolds of the same names.

package cli

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"github.com/dmmdea/davinci-resolve-cli/cli/internal/oplog"
)

func init() {
	registerNovelCommand(func(root *cobra.Command, flags *rootFlags) {
		if runs := findChildCommand(root, "runs"); runs != nil {
			addNovelCommandIfAbsent(runs, newHistoryRunsShowCmd(flags))
		}
	})
}

func findChildCommand(parent *cobra.Command, name string) *cobra.Command {
	for _, c := range parent.Commands() {
		if c.Name() == name {
			return c
		}
	}
	return nil
}

// openHistory opens the log read-only; a missing log answers with (nil, false)
// after printing the hint, so every command can emit an empty result and exit 0.
func openHistory(cmd *cobra.Command, flags *rootFlags, empty any) (*oplog.Log, bool, error) {
	path := historyPath()
	l, err := oplog.OpenReadOnly(cmd.Context(), path)
	if err == oplog.ErrNoHistory {
		fmt.Fprintf(cmd.ErrOrStderr(), "no history at %s\n%s\n", path, oplog.ErrNoHistory.Error())
		if !wantsHumanTable(cmd.OutOrStdout(), flags) {
			return nil, false, printJSONFiltered(cmd.OutOrStdout(), empty, flags)
		}
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("opening history %s: %w", path, err)
	}
	return l, true, nil
}

func historyAnnotations(happy string) map[string]string {
	return map[string]string{
		"mcp:read-only":       "true",
		"pp:data-source":      "local",
		"pp:happy-args":       happy,
		"pp:typed-exit-codes": "0,3",
		"pp:history-command":  "true",
	}
}

func newHistoryRunsShowCmd(flags *rootFlags) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "show [id]",
		Short: "One recorded run in full: request arguments and the sidecar's response",
		Example: strings.Trim(`
  davinci-resolve-pp-cli runs show 42 --json
  davinci-resolve-pp-cli runs show 42 --json --select command,args,response
`, "\n"),
		Annotations: historyAnnotations("id=1"),
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 0 && cmd.Flags().NFlag() == 0 {
				return cmd.Help()
			}
			if dryRunOK(flags) {
				return writeDryRun(cmd.OutOrStdout(), flags, "runs show")
			}
			if len(args) < 1 {
				_ = cmd.Usage()
				return usageErr(fmt.Errorf("a run id is required (see 'runs list')"))
			}
			id, err := strconv.ParseInt(args[0], 10, 64)
			if err != nil {
				return usageErr(fmt.Errorf("run id must be an integer, got %q", args[0]))
			}
			l, ok, err := openHistory(cmd, flags, map[string]any{})
			if !ok || err != nil {
				return err
			}
			defer l.Close()
			e, err := l.Run(cmd.Context(), id)
			if err != nil {
				if errors.Is(err, sql.ErrNoRows) {
					return notFoundErr(fmt.Errorf("run %d not found in %s", id, l.Path()))
				}
				return fmt.Errorf("reading run %d from %s: %w", id, l.Path(), err)
			}
			return printJSONFiltered(cmd.OutOrStdout(), e, flags)
		},
	}
	return cmd
}

// keep the compiler honest about unused imports when the parent lookup changes
var _ = context.Background
var _ = os.Getenv
