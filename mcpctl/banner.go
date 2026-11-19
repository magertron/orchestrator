package main

// The Magertron(TM) mark, printed by `mcpctl login` (2026-11-19) - the same braille banner the
// czartup CLI shows. It goes to stderr and only when a person is watching (stderr is a terminal),
// so scripts and CI that capture output never see it. Blue unless NO_COLOR is set
// (https://no-color.org). The first 9 characters of each line are the ▸ mark; the rest is the name.

import (
	"fmt"
	"os"
)

var magertronBanner = []string{
	"⢰⣄⡀",
	"⢸⣿⣿⣶⣄⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢰⣶",
	"⢸⣿⣿⣿⣿⣿⣶⣄⡀⠀⠀⠀⠈⣿⡗⠉⣿⡖⠉⣿⡆⠀⢞⣉⣹⣶⠀⣴⡏⠑⣿⡏⠀⣴⡏⠉⣷⡄⠈⣿⡗⠉⠃⢹⣿⠉⠀⢹⣿⠊⠙⣴⡏⠉⣷⡄⠀⢹⣿⠊⢹⣷",
	"⢸⣿⣿⣿⣿⣿⠿⠋⠁⠀⠀⠀⠀⣿⡇⠀⣿⡇⠀⣿⡇⢸⣿⠀⢸⣿⠀⢿⡇⠀⣿⡇⠀⢿⣏⠉⢉⠅⠀⣿⡇⠀⠀⢸⣿⠀⡀⢸⣿⠀⠀⢿⡇⠀⣿⠇⠀⢸⣿⠀⢸⣿",
	"⢸⣿⣿⠿⠋⠁⠀⠀⠀⠀⠀⠀⠈⠉⠉⠈⠉⠉⠈⠉⠉⠀⠉⠉⠉⠉⠁⢠⡉⢉⣿⠇⠀⠀⠉⠉⠁⠀⠈⠉⠉⠀⠀⠀⠉⠉⠀⠉⠉⠁⠀⠀⠉⠉⠁⠀⠀⠉⠉⠁⠉⠉⠁",
	"⠸⠋⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠉⠁",
}

func printBanner() {
	fi, err := os.Stderr.Stat()
	if err != nil || fi.Mode()&os.ModeCharDevice == 0 {
		return
	}
	blue, reset := "\033[38;5;75m", "\033[0m"
	if os.Getenv("NO_COLOR") != "" {
		blue, reset = "", ""
	}
	for _, line := range magertronBanner {
		r := []rune(line)
		n := 9
		if len(r) < n {
			n = len(r)
		}
		fmt.Fprintf(os.Stderr, "%s%s%s%s\n", blue, string(r[:n]), reset, string(r[n:]))
	}
	fmt.Fprintln(os.Stderr)
}
