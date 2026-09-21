// Copyright 2026 dmmdea and contributors. Licensed under Apache-2.0. See LICENSE.
// Novel command scaffold. Implement the RunE body before shipping.
// generate --force preserves implemented bodies; untouched TODO scaffolds may refresh.
// pp:data-source auto
// Supported strategies: auto, local, live, or computed. Change this default deliberately.

package cli

import (
	"github.com/spf13/cobra"
)

func newNovelFusionTitlesCmd(flags *rootFlags) *cobra.Command {

	cmd := &cobra.Command{
		Use:         "titles",
		Short:       "Title text catalog",
		Example:     "  davinci-resolve-pp-cli fusion titles search 'episode 12' --json",
		Annotations: map[string]string{"mcp:read-only": "true", "pp:data-source": "auto"},
		RunE:        parentNoSubcommandRunE(flags),
	}
	addNovelCommandIfAbsent(cmd, newNovelFusionTitlesSearchCmd(flags))
	return cmd
}
