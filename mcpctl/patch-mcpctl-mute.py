#!/usr/bin/env python3
"""
mcpctl mute / unmute / mutes — the kill switch from a terminal.

  mcpctl mute server <ns> <name> [--reason "..."]
  mcpctl unmute server <ns> <name>
  mcpctl mute principal <subject> [--reason "..."]
  mcpctl unmute principal <subject>
  mcpctl mutes

Needs the orchestrator built with patch-mute.py (PUT .../mute, GET /mutes).
Bumps the minor version. Run from ~/GetHub/orchestrator/mcpctl. Writes a .bak.
"""
import datetime, re, shutil, sys

P = "main.go"
s = open(P, encoding="utf-8").read()
if "func cmdMute(" in s:
    sys.exit("mute commands already present — nothing written")

def once(anchor, label):
    n = s.count(anchor)
    if n != 1:
        sys.exit(f"[{label}] anchor found {n}x — nothing written")

# ── dispatch ────────────────────────────────────────────────────────────────
A = '\tcase "disable":\n\t\tcmdDisable(gf, cmdArgs)\n'
once(A, "dispatch")
s = s.replace(A, A +
    '\tcase "mute":\n\t\tcmdMute(gf, cmdArgs, true)\n'
    '\tcase "unmute":\n\t\tcmdMute(gf, cmdArgs, false)\n'
    '\tcase "mutes":\n\t\tcmdMutes(gf, cmdArgs)\n', 1)

# ── help ────────────────────────────────────────────────────────────────────
m = re.search(r'^  disable <ns> <name>( +)Disable an external MCP server.*\n', s, re.M)
if not m:
    sys.exit("[help] disable line not found — nothing written")
col = len("  disable <ns> <name>") + len(m.group(1))
def row(cmd, text):
    return cmd.ljust(col) + text + "\n"
s = s[:m.end()] + (
    row("  mute server <ns> <name>", "Refuse every call to a server (403, audited)")
  + row("  mute principal <subject>", "Refuse a user/service account, as caller or on-behalf-of")
  + row("  unmute server|principal ...", "Lift a mute; grants and tokens were never touched")
  + row("  mutes", "List everything currently muted")
) + s[m.end():]

# ── commands ────────────────────────────────────────────────────────────────
A = "// cmdDisable flips an External MCP server registration to 'Inactive'."
once(A, "commands")
CMDS = '''// cmdMute mutes or unmutes a server or a principal.
//
// Mute is the incident-response kill switch: every call is refused with a 403
// that says "muted", and an audit row records who pressed it and why. It is
// NOT revoke — grants, roles, tokens and the server's lifecycle state are left
// alone, so unmute puts everything back exactly as it was.
//
// Backed by PUT /api/v1/servers/{ns}/{name}/mute and PUT /api/v1/principals/mute.
func cmdMute(gf globalFlags, args []string, mute bool) {
	verb := "mute"
	if !mute {
		verb = "unmute"
	}
	reason := ""
	var pos []string
	for i := 0; i < len(args); i++ {
		switch {
		case args[i] == "--reason" && i+1 < len(args):
			reason = args[i+1]
			i++
		case strings.HasPrefix(args[i], "--reason="):
			reason = strings.TrimPrefix(args[i], "--reason=")
		default:
			pos = append(pos, args[i])
		}
	}
	usage := func() {
		fmt.Printf("Usage: mcpctl %s server <namespace> <name>%s\\n", verb, map[bool]string{true: " [--reason \\"...\\"]", false: ""}[mute])
		fmt.Printf("       mcpctl %s principal <subject>%s\\n", verb, map[bool]string{true: " [--reason \\"...\\"]", false: ""}[mute])
		fmt.Println()
		fmt.Println("A muted server refuses every call. A muted principal is refused as the")
		fmt.Println("calling agent and as the person an agent acts on behalf of.")
		fmt.Println("Grants and tokens are not touched; unmute restores everything.")
		fmt.Println()
		fmt.Println("Examples:")
		fmt.Println("  mcpctl mute server mcp-prod payments-agent --reason \\"suspicious transfers\\"")
		fmt.Println("  mcpctl mute principal svc-payout-bot")
		fmt.Println("  mcpctl unmute server mcp-prod payments-agent")
		os.Exit(1)
	}
	if len(pos) < 2 {
		usage()
	}
	payload := map[string]interface{}{"muted": mute}
	if reason != "" {
		payload["reason"] = reason
	}
	switch pos[0] {
	case "server":
		if len(pos) < 3 {
			usage()
		}
		ns, name := pos[1], pos[2]
		body, err := apiPut(gf, fmt.Sprintf("/servers/%s/%s/mute", ns, name), payload)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %v\\n", err)
			os.Exit(1)
		}
		var r map[string]interface{}
		_ = json.Unmarshal(body, &r)
		changed, _ := r["changed"].(bool)
		switch {
		case mute && changed:
			fmt.Printf("✓ Muted %s/%s. Every call to it is now refused.\\n", ns, name)
		case mute:
			fmt.Printf("%s/%s was already muted. Nothing changed.\\n", ns, name)
		case changed:
			fmt.Printf("✓ Unmuted %s/%s. Calls are allowed again.\\n", ns, name)
		default:
			fmt.Printf("%s/%s was not muted. Nothing changed.\\n", ns, name)
		}
	case "principal", "user", "service-account", "sa":
		subject := pos[1]
		payload["subject"] = subject
		body, err := apiPut(gf, "/principals/mute", payload)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %v\\n", err)
			os.Exit(1)
		}
		var r map[string]interface{}
		_ = json.Unmarshal(body, &r)
		changed, _ := r["changed"].(bool)
		switch {
		case mute && changed:
			fmt.Printf("✓ Muted %s. Refused as a caller and as an on-behalf-of principal.\\n", subject)
		case mute:
			fmt.Printf("%s was already muted. Nothing changed.\\n", subject)
		case changed:
			fmt.Printf("✓ Unmuted %s.\\n", subject)
		default:
			fmt.Printf("%s was not muted. Nothing changed.\\n", subject)
		}
	default:
		usage()
	}
}

// cmdMutes lists everything currently muted, newest first.
// Backed by GET /api/v1/mutes.
func cmdMutes(gf globalFlags, args []string) {
	body, err := apiGet(gf, "/mutes")
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\\n", err)
		os.Exit(1)
	}
	var r struct {
		Mutes []struct {
			Kind    string `json:"kind"`
			Key     string `json:"key"`
			Label   string `json:"label"`
			MutedBy string `json:"muted_by"`
			Reason  string `json:"reason"`
			MutedAt string `json:"muted_at"`
		} `json:"mutes"`
	}
	if err := json.Unmarshal(body, &r); err != nil {
		fmt.Fprintf(os.Stderr, "Error: malformed mutes response: %v\\n", err)
		os.Exit(1)
	}
	if len(r.Mutes) == 0 {
		fmt.Println("Nothing is muted.")
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "KIND\\tWHAT\\tMUTED BY\\tSINCE\\tREASON")
	for _, m := range r.Mutes {
		what := m.Label
		if what == "" {
			what = m.Key
		}
		fmt.Fprintf(w, "%s\\t%s\\t%s\\t%s\\t%s\\n", m.Kind, what, m.MutedBy, m.MutedAt, m.Reason)
	}
	w.Flush()
}

'''
s = s.replace(A, CMDS + A, 1)

# ── version ─────────────────────────────────────────────────────────────────
vm = re.search(r'const version = "(\d+)\.(\d+)\.(\d+)"', s)
if not vm:
    sys.exit("[version] const version not found — nothing written")
new_v = f"{vm.group(1)}.{int(vm.group(2)) + 1}.0"
s = s[:vm.start()] + f'const version = "{new_v}"' + s[vm.end():]

stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy(P, f"{P}.bak.{stamp}")
open(P, "w", encoding="utf-8").write(s)
print(f"patched {P} → version {new_v}  (backup: {P}.bak.{stamp})")
print("then: gofmt -l . && go build -o mcpctl .")
