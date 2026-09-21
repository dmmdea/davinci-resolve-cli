// Copyright 2026 dmmdea and contributors. Licensed under Apache-2.0. See LICENSE.
// cli-printing-press: novel-scaffold-test
// Novel command scaffold tests. Keep the wiring smoke test and add behavior cases as needed.

package cli

import (
	"bytes"
	"strings"
	"testing"

	"github.com/dmmdea/davinci-resolve-cli/cli/internal/cliutil/testenv"
)

// TestNovelFusionTitlesSearchHelpWires smoke-tests that the fusion titles search command
// resolves at runtime and renders useful --help output. Catches wiring
// regressions (missing AddCommand, panicking RunE on --help, etc.) before
// review. Keep this smoke test when adding behavior-specific cases.
func TestNovelFusionTitlesSearchHelpWires(t *testing.T) {
	testenv.Isolate(t)
	cmd := RootCmd()
	cmd.SetArgs([]string{"fusion", "titles", "search", "--help"})
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&out)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("fusion titles search --help error = %v (novel command not wired correctly?)", err)
	}
	help := out.String()
	for _, want := range []string{"Usage:", "search"} {
		if !strings.Contains(help, want) {
			t.Fatalf("fusion titles search --help missing %q in output:\n%s", want, help)
		}
	}
}
