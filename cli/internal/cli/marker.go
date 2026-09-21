// Copyright 2026 dmmdea and contributors. Licensed under Apache-2.0. See LICENSE.
// Novel command scaffold. Implement the RunE body before shipping.
// generate --force preserves implemented bodies; untouched TODO scaffolds may refresh.
// pp:data-source auto
// Supported strategies: auto, local, live, or computed. Change this default deliberately.

package cli

import (
	"github.com/spf13/cobra"
)

func newNovelMarkerCmd(flags *rootFlags) *cobra.Command {

	cmd := &cobra.Command{
		Use:         "marker",
		Short:       "Cross-project marker index",
		Example:     "  davinci-resolve-pp-cli marker search 'pickup shot' --json",
		Annotations: map[string]string{"mcp:read-only": "true", "pp:data-source": "auto"},
		RunE:        parentNoSubcommandRunE(flags),
	}
	addNovelCommandIfAbsent(cmd, newNovelMarkerSearchCmd(flags))
	return cmd
}
