// mcpctl — MCP Orchestrator CLI
//
// Talks to the orchestrator's REST API at /api/v1/* over the unified
// TLS+ext_authz gateway (port 30444 by default in v1.6.0+).
//
// Authentication: POST /api/v1/auth/login returns a JWT; we save it in
// ~/.mcpctl.json and send it as Bearer on every subsequent request.
package main

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"
)

const version = "3.0.1"

// ─── Config ──────────────────────────────────────────────────────────────────

type Config struct {
	Server   string `json:"server"`
	Token    string `json:"token"`
	Insecure bool   `json:"insecure,omitempty"` // skip TLS verify (self-signed)
	CACert   string `json:"ca_cert,omitempty"`  // path to custom CA bundle
}

// configPath returns the platform-appropriate config path.
// Prefers $XDG_CONFIG_HOME/mcpctl/config.json or ~/.config/mcpctl/config.json,
// falls back to ~/.mcpctl.json for backwards compatibility with v1.0.
func configPath() string {
	// Honor existing legacy config first so users don't lose state on upgrade.
	if home, err := os.UserHomeDir(); err == nil {
		legacy := filepath.Join(home, ".mcpctl.json")
		if _, err := os.Stat(legacy); err == nil {
			return legacy
		}
	}
	dir, err := os.UserConfigDir()
	if err != nil {
		// Last-resort fallback. Should rarely hit on supported platforms.
		home, _ := os.UserHomeDir()
		return filepath.Join(home, ".mcpctl.json")
	}
	return filepath.Join(dir, "mcpctl", "config.json")
}

// loadConfig reads the stored credential, with ENVIRONMENT VARIABLES taking
// precedence over the file.
//
// ⚠ WHY ENV WINS. Three callers need to configure mcpctl without owning the
// developer's home directory:
//
//   - an IDE extension bundling this binary — writing to ~/.config/mcpctl
//     would clobber an existing interactive login, which is rude and
//     surprising;
//   - CI, where a login step is one more thing to get wrong and one more
//     credential written to a disk that outlives the job;
//   - containers, where there is often no home directory worth writing to.
//
// ⚠ Precedence is env-then-file, not file-then-env. An explicit override must
// beat a stale login — the opposite order means a developer who logged into
// the wrong cluster months ago silently keeps submitting there, and nothing
// on screen would say so.
//
// Partial overrides are allowed: MCPCTL_SERVER alone, with the token still
// coming from the file, is a reasonable thing to want.
func loadConfig() Config {
	var c Config

	data, err := os.ReadFile(configPath())
	if err == nil {
		if err := json.Unmarshal(data, &c); err != nil {
			fmt.Fprintf(os.Stderr, "warning: config at %s is malformed: %v\n",
				configPath(), err)
			c = Config{}
		}
	}

	if v := os.Getenv("MCPCTL_SERVER"); v != "" {
		c.Server = v
	}
	if v := os.Getenv("MCPCTL_TOKEN"); v != "" {
		c.Token = v
	}
	if v := os.Getenv("MCPCTL_CA_CERT"); v != "" {
		c.CACert = v
	}
	// ⚠ Only ever turns verification OFF, never on. An env var that could
	// silently RE-ENABLE verification the operator disabled in the file would
	// be a confusing failure; one that disables it is an explicit act.
	if v := os.Getenv("MCPCTL_INSECURE"); v == "1" || v == "true" {
		c.Insecure = true
	}

	return c
}

func loadConfigFileOnly() Config {
	data, err := os.ReadFile(configPath())
	if err != nil {
		return Config{}
	}
	var c Config
	if err := json.Unmarshal(data, &c); err != nil {
		// Corrupted config — surface it rather than silently zeroing.
		fmt.Fprintf(os.Stderr, "warning: config at %s is malformed: %v\n", configPath(), err)
		return Config{}
	}
	return c
}

func saveConfig(c Config) error {
	path := configPath()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return fmt.Errorf("create config dir: %w", err)
	}
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal config: %w", err)
	}
	if err := os.WriteFile(path, data, 0600); err != nil {
		return fmt.Errorf("write config: %w", err)
	}
	return nil
}

// ─── Global flags (parsed from os.Args before subcommand dispatch) ───────────

type globalFlags struct {
	insecure bool
	caCert   string
	jsonOut  bool
	// ⚠ Set ONLY by `login --token`, to prove a candidate credential before it
	// is written to disk. apiRequest honours it in place of loadConfig().
	// Without this the probe would validate the PREVIOUS token and happily save
	// a broken one — the failure this whole change exists to prevent.
	cfgOverride *Config
}

// stripGlobalFlags walks os.Args and pulls out --insecure / -k, --ca-cert,
// and --json wherever they appear. Returns the remaining args (subcommand +
// subcommand args) plus the parsed global flags.
//
// We do this manually rather than using flag.FlagSet because we want global
// flags to work in any position: `mcpctl --insecure servers` and
// `mcpctl servers --insecure` and `mcpctl --insecure deploy ... --port 8080`
// should all work the same. flag.FlagSet's first-non-flag-stops behavior
// makes that awkward.
func stripGlobalFlags(args []string) ([]string, globalFlags) {
	var out []string
	var gf globalFlags
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--insecure", "-k":
			gf.insecure = true
		case "--json":
			gf.jsonOut = true
		case "--ca-cert":
			if i+1 >= len(args) {
				fmt.Fprintln(os.Stderr, "Error: --ca-cert requires a path")
				os.Exit(1)
			}
			gf.caCert = args[i+1]
			i++
		default:
			out = append(out, args[i])
		}
	}
	return out, gf
}

// ─── HTTP client ─────────────────────────────────────────────────────────────

// buildHTTPClient constructs an http.Client honoring TLS settings from the
// saved config and any global flags passed on this invocation.
//
// Precedence: command-line flag > saved config. Saving --insecure into the
// config means the user opts in once; --ca-cert in config means the user
// pinned a CA. This matches kubectl/aws-cli ergonomics.
func buildHTTPClient(cfg Config, gf globalFlags) (*http.Client, error) {
	tlsCfg := &tls.Config{}

	// Insecure: command-line flag wins, otherwise honor saved config.
	if gf.insecure || cfg.Insecure {
		tlsCfg.InsecureSkipVerify = true
	}

	// CA cert: prefer command-line flag, fall back to saved config.
	caCertPath := gf.caCert
	if caCertPath == "" {
		caCertPath = cfg.CACert
	}
	if caCertPath != "" {
		pemBytes, err := os.ReadFile(caCertPath)
		if err != nil {
			return nil, fmt.Errorf("read CA cert %s: %w", caCertPath, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pemBytes) {
			return nil, fmt.Errorf("CA cert %s contains no usable PEM blocks", caCertPath)
		}
		tlsCfg.RootCAs = pool
	}

	transport := &http.Transport{TLSClientConfig: tlsCfg}
	return &http.Client{Timeout: 30 * time.Second, Transport: transport}, nil
}

// apiRequest sends a request and returns the raw response body bytes.
// Callers decode into the appropriate Go type — the previous version
// returned map[string]interface{} which couldn't represent top-level
// arrays (e.g. /api/v1/servers returns a JSON array directly).
// apiRequest issues one request and retries a leader-bounce 503. See
// isNotLeader — mutating ops are leader-only and the Service load-balances, so
// a follower rejects roughly half of them on a multi-replica install.
func apiRequest(gf globalFlags, method, path string, body interface{}) ([]byte, int, error) {
	respBody, status, err := apiRequestOnce(gf, method, path, body)
	if err != nil {
		return respBody, status, err
	}
	if isNotLeader(status, respBody) {
		return apiRequestRetryLeader(gf, method, path, body, nil)
	}
	return respBody, status, nil
}

// apiRequestOnce is the raw request path with NO retry — the retry helper calls
// this, not apiRequest, or it would recurse.
func apiRequestOnce(gf globalFlags, method, path string, body interface{}) ([]byte, int, error) {
	cfg := loadConfig()
	// ⚠ A candidate credential under test — see globalFlags.cfgOverride.
	if gf.cfgOverride != nil {
		cfg = *gf.cfgOverride
	}
	if cfg.Server == "" {
		return nil, 0, errors.New("not configured — run: mcpctl login <server-url> <username> <password>")
	}

	var bodyReader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, 0, fmt.Errorf("marshal request body: %w", err)
		}
		bodyReader = bytes.NewReader(data)
	}

	url := cfg.Server + "/api/v1" + path
	req, err := http.NewRequest(method, url, bodyReader)
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	if cfg.Token != "" {
		req.Header.Set("Authorization", "Bearer "+cfg.Token)
	}

	client, err := buildHTTPClient(cfg, gf)
	if err != nil {
		return nil, 0, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("connection failed: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, fmt.Errorf("read response: %w", err)
	}

	// ⚠ LEADER BOUNCE. Mutating operations are leader-only, and the Service
	// load-balances across replicas — so on a multi-replica install a delete,
	// approve or update lands on a follower about half the time and is
	// rejected with 503 "not the current leader; retry the request".
	//
	// The server sets Retry-After and logs "client should retry". Until now
	// nothing did: an operator saw a raw 503 about pod leadership and clicked
	// again, which usually worked — which is why this was never reported.
	//
	// Retried HERE rather than in each of apiGet/apiPost/apiDelete/apiPut,
	// because all four funnel through this function and a fifth helper would
	// otherwise miss it.
	return respBody, resp.StatusCode, nil
}

// isNotLeader reports whether a response is the orchestrator's leader-bounce
// rejection. Matches on the marker rather than the exact sentence so a reworded
// message does not silently stop retrying.
func isNotLeader(status int, body []byte) bool {
	if status != http.StatusServiceUnavailable {
		return false
	}
	t := strings.ToLower(string(body))
	return strings.Contains(t, "not the current leader") ||
		strings.Contains(t, "not leader") ||
		strings.Contains(t, "not_leader")
}

// apiRequestRetryLeader re-issues a request that hit a follower.
//
// ⚠ Honours Retry-After from the response rather than inventing a backoff —
// the server already states the delay it wants.
//
// ⚠ Bounded. With N replicas the load balancer needs a few attempts to land on
// the leader, but an unbounded loop would hang the CLI during a genuine outage
// (e.g. mid-handover, when NO pod holds the lease). After the last attempt the
// 503 is returned as-is so the operator sees the real message.
func apiRequestRetryLeader(gf globalFlags, method, path string, body interface{},
	first *http.Response) ([]byte, int, error) {

	const maxAttempts = 5

	delay := time.Second
	if first != nil {
		if ra := first.Header.Get("Retry-After"); ra != "" {
			if secs, err := strconv.Atoi(ra); err == nil && secs > 0 && secs <= 30 {
				delay = time.Duration(secs) * time.Second
			}
		}
	}

	var lastBody []byte
	var lastStatus int
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		time.Sleep(delay)

		respBody, status, err := apiRequestOnce(gf, method, path, body)
		if err != nil {
			return nil, status, err
		}
		if !isNotLeader(status, respBody) {
			return respBody, status, nil
		}
		lastBody, lastStatus = respBody, status
	}
	// ⚠ Out of attempts. Return the server's own message — an invented error
	// here would hide which operation was refused and why.
	return lastBody, lastStatus, nil
}

// extractErrorMessage tries to pull a meaningful error message out of an
// error response body. Orchestrator returns either {"error": "..."} or
// occasionally {"detail": "..."}; fall through to the raw body if neither.
// extractLicenseHint returns a human next-step for a 402 Payment Required.
// ⚠ The server sends feature, tier_required and upgrade_url alongside the error;
// printing only the error throws away the two fields that tell someone what to
// actually DO about it.
func extractLicenseHint(body []byte) string {
	var m map[string]interface{}
	if err := json.Unmarshal(body, &m); err != nil {
		return ""
	}
	feat, _ := m["feature"].(string)
	tier, _ := m["tier_required"].(string)
	url, _ := m["upgrade_url"].(string)
	if feat == "" && tier == "" {
		return ""
	}
	out := "\n"
	if feat != "" && tier != "" {
		out += fmt.Sprintf("  '%s' requires the %s tier.\n", feat, strings.Title(tier))
	}
	if url != "" {
		out += fmt.Sprintf("  See %s\n", url)
	}
	return out
}

func extractErrorMessage(body []byte) string {
	var m map[string]interface{}
	if err := json.Unmarshal(body, &m); err == nil {
		if e, ok := m["error"].(string); ok && e != "" {
			return e
		}
		if d, ok := m["detail"].(string); ok && d != "" {
			return d
		}
	}
	if len(body) > 0 && len(body) < 200 {
		return strings.TrimSpace(string(body))
	}
	return "request failed"
}

func apiGet(gf globalFlags, path string) ([]byte, error) {
	body, status, err := apiRequest(gf, "GET", path, nil)
	if err != nil {
		return nil, err
	}
	if status >= 400 {
		return nil, fmt.Errorf("HTTP %d: %s%s", status, extractErrorMessage(body), extractLicenseHint(body))
	}
	return body, nil
}

func apiPost(gf globalFlags, path string, in interface{}) ([]byte, error) {
	body, status, err := apiRequest(gf, "POST", path, in)
	if err != nil {
		return nil, err
	}
	if status >= 400 {
		return nil, fmt.Errorf("HTTP %d: %s%s", status, extractErrorMessage(body), extractLicenseHint(body))
	}
	return body, nil
}

func apiDelete(gf globalFlags, path string) error {
	body, status, err := apiRequest(gf, "DELETE", path, nil)
	if err != nil {
		return err
	}
	if status >= 400 {
		return fmt.Errorf("HTTP %d: %s%s", status, extractErrorMessage(body), extractLicenseHint(body))
	}
	return nil
}

// ─── Commands ────────────────────────────────────────────────────────────────

func cmdLogin(gf globalFlags, args []string) {
	// ── Service-account login ────────────────────────────────────────────
	// ⚠ A service account holds a signed JWT, not a password. There is nothing
	// to POST to /auth/login — the credential IS the session. So this path
	// skips the login endpoint entirely and stores the token.
	//
	// ⚠ MFA is therefore never reached, and that is correct rather than a
	// loophole: MFA is a control on human authentication. A machine identity
	// has no second factor to present; its protection is that the credential is
	// scoped, attributable and revocable.
	for i, a := range args {
		if a == "--token" {
			if i+1 >= len(args) || len(args) < 1 {
				fmt.Fprintln(os.Stderr, "Error: --token requires a value")
				os.Exit(1)
			}
			if i == 0 {
				fmt.Fprintln(os.Stderr, "Error: server URL must come first")
				fmt.Fprintln(os.Stderr, "  mcpctl login <server-url> --token <token>")
				os.Exit(1)
			}
			cmdLoginToken(gf, strings.TrimRight(args[0], "/"), args[i+1])
			return
		}
	}

	if len(args) < 3 {
		fmt.Println("Usage: mcpctl login <server-url> --token <service-account-token>")
		fmt.Println("        Mint the token in the console: Users -> Service Accounts -> Create.")
		fmt.Println("Example: mcpctl login https://localhost:30444 --token eyJhbGc... --insecure")
		fmt.Println(" ")
		fmt.Println()
		fmt.Println("Notes:")
		fmt.Println("  Default Magertron gateway is HTTPS on port 30444.")
		fmt.Println("  Use --insecure (or -k) for self-signed certs (typical for fresh helm installs).")
		fmt.Println("  Use --ca-cert <path> to pin a CA bundle.")
		os.Exit(1)
	}

	server := strings.TrimRight(args[0], "/")
	username := args[1]
	password := args[2]

	body := map[string]string{"username": username, "password": password}
	data, err := json.Marshal(body)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}

	url := server + "/api/v1/auth/login"
	req, err := http.NewRequest("POST", url, bytes.NewReader(data))
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	req.Header.Set("Content-Type", "application/json")

	// For login we don't have a saved config yet, so build the client from
	// command-line flags only. This is the one place we can't read cfg.
	tlsCfg := &tls.Config{}
	if gf.insecure {
		tlsCfg.InsecureSkipVerify = true
	}
	if gf.caCert != "" {
		pemBytes, err := os.ReadFile(gf.caCert)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error reading CA cert: %v\n", err)
			os.Exit(1)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pemBytes) {
			fmt.Fprintf(os.Stderr, "Error: CA cert %s contains no usable PEM blocks\n", gf.caCert)
			os.Exit(1)
		}
		tlsCfg.RootCAs = pool
	}
	client := &http.Client{
		Timeout:   30 * time.Second,
		Transport: &http.Transport{TLSClientConfig: tlsCfg},
	}

	resp, err := client.Do(req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		// Common case: TLS verify fail against self-signed cert. Hint the fix.
		if strings.Contains(err.Error(), "x509") || strings.Contains(err.Error(), "certificate") {
			fmt.Fprintln(os.Stderr, "\nHint: if the server uses a self-signed cert, retry with --insecure")
		}
		os.Exit(1)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)
	var result map[string]interface{}
	if err := json.Unmarshal(respBody, &result); err != nil {
		fmt.Fprintf(os.Stderr, "Login failed: malformed response (HTTP %d)\n", resp.StatusCode)
		os.Exit(1)
	}

	if resp.StatusCode != 200 {
		msg, _ := result["error"].(string)
		if msg == "" {
			msg = extractErrorMessage(respBody)
		}
		fmt.Fprintf(os.Stderr, "Login failed: %s\n", msg)
		os.Exit(1)
	}

	// ⚠ A 200 WITHOUT A TOKEN IS NOT A LOGIN. Previously the comma-ok was
	// discarded here, so an absent token became "", the config was written, and
	// the CLI printed a tick — leaving every later command to fail with an
	// unexplained 401.
	token, ok := result["token"].(string)
	if !ok || token == "" {
		// The case that actually occurs: two-phase MFA. The server answers 200
		// with {"mfa_required":true,"pending":"..."} because authentication has
		// only STARTED — it completes through the IdP in a browser.
		if mfa, _ := result["mfa_required"].(bool); mfa {
			fmt.Fprintf(os.Stderr, "Login failed: %s requires multi-factor authentication.\n\n", username)
			fmt.Fprintln(os.Stderr, "  MFA completes through your identity provider in a browser, so it")
			fmt.Fprintln(os.Stderr, "  cannot be finished from the command line.")
			fmt.Fprintln(os.Stderr)
			fmt.Fprintln(os.Stderr, "  The CLI is intended for SERVICE ACCOUNTS. Create one in the console")
			fmt.Fprintln(os.Stderr, "  (Users -> Service Accounts) and log in with its credentials — that is")
			fmt.Fprintln(os.Stderr, "  also the right identity for automation: it is attributable, scoped,")
			fmt.Fprintln(os.Stderr, "  and revocable without touching a person's account.")
		} else {
			// ⚠ Any other tokenless 200. Do not guess at a cause — say exactly
			// what was and was not received.
			fmt.Fprintf(os.Stderr, "Login failed: the server accepted the request (HTTP %d) but returned no session token.\n", resp.StatusCode)
			if msg, _ := result["error"].(string); msg != "" {
				fmt.Fprintf(os.Stderr, "  Server said: %s\n", msg)
			}
			fmt.Fprintln(os.Stderr, "  Nothing has been saved; you are not logged in.")
		}
		// ⚠ Exit BEFORE saveConfig. Writing an empty token is what turned a
		// clear failure into a confusing one.
		os.Exit(1)
	}

	cfg := Config{
		Server:   server,
		Token:    token,
		Insecure: gf.insecure,
		CACert:   gf.caCert,
	}

	// License-tier gating — CLI requires Pro or Enterprise.
	if license, ok := result["license"].(map[string]interface{}); ok {
		cliAllowed, _ := license["cli_allowed"].(bool)
		tier, _ := license["tier"].(string)
		if !cliAllowed {
			fmt.Fprintf(os.Stderr, "\n⚠  CLI access requires a Pro or Enterprise license (current tier: %s)\n", tier)
			fmt.Fprintf(os.Stderr, "   Upgrade at https://magertron.com/#pricing\n\n")
			os.Exit(1)
		}
	}

	if err := saveConfig(cfg); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}

	// ⚠ Printed as "Logged in as admin ()" when roles were absent — an empty
	// bracket reads like a bug rather than an absence. Omit it instead.
	roles, _ := result["roles"].([]interface{})
	roleStrs := make([]string, 0, len(roles))
	for _, r := range roles {
		if rs, ok := r.(string); ok && rs != "" {
			roleStrs = append(roleStrs, rs)
		}
	}

	fmt.Printf("✓ Logged in as %s (%s)\n", username, strings.Join(roleStrs, ", "))
	fmt.Printf("  Server: %s\n", server)
	fmt.Printf("  Config: %s\n", configPath())
	if gf.insecure {
		fmt.Println("  TLS:    insecure (skipping cert verification)")
	}
}

func cmdLogout() {
	if err := os.Remove(configPath()); err != nil && !os.IsNotExist(err) {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	fmt.Println("✓ Logged out")
}

func cmdStatus(gf globalFlags) {
	cfg := loadConfig()
	if cfg.Server == "" {
		fmt.Println("Not logged in. Run: mcpctl login <server-url> <username> <password>")
		return
	}

	fmt.Printf("Server:  %s\n", cfg.Server)
	fmt.Printf("Config:  %s\n", configPath())
	if cfg.Insecure {
		fmt.Println("TLS:     insecure")
	}

	body, err := apiGet(gf, "/version")
	if err != nil {
		fmt.Printf("Status:  offline (%v)\n", err)
		return
	}
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		fmt.Printf("Status:  connected (malformed /version response)\n")
		return
	}
	fmt.Printf("Version: %v\n", result["version"])
	fmt.Println("Status:  connected")
}

// ── Servers ──────────────────────────────────────────────────────────────────

// listServers fetches /api/v1/servers and decodes the JSON array directly.
// v1.6.0+ returns a top-level array; we no longer try map-then-fallback.
func listServers(gf globalFlags) ([]map[string]interface{}, error) {
	body, err := apiGet(gf, "/servers")
	if err != nil {
		return nil, err
	}
	var arr []map[string]interface{}
	if err := json.Unmarshal(body, &arr); err != nil {
		return nil, fmt.Errorf("decode servers list: %w", err)
	}
	return arr, nil
}

func cmdListServers(gf globalFlags) {
	servers, err := listServers(gf)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		emitJSON(servers)
		return
	}
	if len(servers) == 0 {
		fmt.Println("No servers deployed.")
		return
	}

	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	// v2.0.4: type-aware columns. External rows have no image / replicas /
	// gateway_url; they have endpoint_url. Show the appropriate TARGET per
	// row so the table is meaningful for all three deployment patterns.
	// Pre-2.0.4: hard-coded image + gateway columns made External rows
	// read as broken (REPLICAS '0/0', IMAGE blank) rather than as proxy
	// registrations with their own shape.
	fmt.Fprintln(w, "NAME\tNAMESPACE\tTYPE\tSTATE\tREPLICAS\tTARGET")
	for _, s := range servers {
		name, _ := s["name"].(string)
		ns, _ := s["namespace"].(string)
		state, _ := s["state"].(string)
		serverType, _ := s["server_type"].(string)
		if serverType == "" {
			// Backward-compat for pre-2.4.24 server rows where the column
			// hadn't been added yet (defaulted to internal at the DB
			// level, but the API response may still omit it).
			serverType = "internal"
		}

		var replicas, target string
		if serverType == "external" {
			// External: no pod, no replicas counter is meaningful.
			// endpoint_url is what the operator wants to see.
			replicas = "-"
			target, _ = s["endpoint_url"].(string)
		} else {
			// Internal/Hybrid: image is the deploy target; replicas count
			// reflects actual pods. gateway_url is the in-cluster URL
			// operators hit for testing/debugging — append it if present.
			ready, _ := s["ready_replicas"].(float64)
			total, _ := s["replicas"].(float64)
			replicas = fmt.Sprintf("%.0f/%.0f", ready, total)
			image, _ := s["image"].(string)
			tag, _ := s["image_tag"].(string)
			if tag != "" && tag != "latest" {
				target = image + ":" + tag
			} else {
				target = image
			}
			if gw, _ := s["gateway_url"].(string); gw != "" {
				// Two-space separator keeps the column count consistent
				// across rows; tabwriter aligns the columns to the left.
				target = target + "  " + gw
			}
		}

		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\n",
			name, ns, serverType, state, replicas, target)
	}
	w.Flush()
}

func cmdDeploy(gf globalFlags, args []string) {
	if len(args) < 3 {
		fmt.Println("Usage: mcpctl deploy <name> <namespace> <image> [options]")
		fmt.Println()
		fmt.Println("Options:")
		fmt.Println("  --tag <tag>             Image tag (default: latest)")
		fmt.Println("  --port <port>           MCP port (default: 8080)")
		fmt.Println("  --replicas <n>          Replicas (default: 1)")
		fmt.Println("  --transport <type>      streamable_http|sse|stdio (default: streamable_http)")
		fmt.Println("  --upstream-path <path>  Path the MCP server listens on (default: /).")
		fmt.Println("                          Set to /http for IBM fast-time-server, /mcp for ContextForge,")
		fmt.Println("                          /sse for SSE-transport servers.")
		fmt.Println("  --cpu-limit <limit>     CPU limit in cores (default: 1.0)")
		fmt.Println("  --memory <mb>           Memory limit in MB (default: 512)")
		fmt.Println("  --team <name>           Team label")
		fmt.Println("  --wait                  Block until server reaches Running or ProbeFailed")
		fmt.Println()
		fmt.Println("Example:")
		fmt.Println("  mcpctl deploy fast-time mcp-prod ghcr.io/ibm/fast-time-server \\")
		fmt.Println("    --upstream-path /http --port 8080 --wait")
		os.Exit(1)
	}

	spec := map[string]interface{}{
		"name":            args[0],
		"namespace":       args[1],
		"image":           args[2],
		"image_tag":       "latest",
		"mcp_port":        8080,
		"replicas":        1,
		"transport":       "streamable_http",
		"upstream_path":   "/",
		"cpu_limit":       1.0,
		"memory_limit_mb": 512,
		"labels":          map[string]string{},
	}

	wait := false

	// Parse options. parseIntFlag/parseFloatFlag report errors with command
	// context so users see "invalid --port: not a number" not silent zero.
	for i := 3; i < len(args); i++ {
		switch args[i] {
		case "--tag":
			i++
			if i >= len(args) {
				flagError("--tag requires a value")
			}
			spec["image_tag"] = args[i]
		case "--port":
			i++
			if i >= len(args) {
				flagError("--port requires a value")
			}
			spec["mcp_port"] = parseIntFlag("--port", args[i])
		case "--replicas":
			i++
			if i >= len(args) {
				flagError("--replicas requires a value")
			}
			spec["replicas"] = parseIntFlag("--replicas", args[i])
		case "--transport":
			i++
			if i >= len(args) {
				flagError("--transport requires a value")
			}
			spec["transport"] = args[i]
		case "--upstream-path":
			i++
			if i >= len(args) {
				flagError("--upstream-path requires a value")
			}
			path := args[i]
			if !strings.HasPrefix(path, "/") {
				path = "/" + path
			}
			spec["upstream_path"] = path
		case "--cpu-limit":
			i++
			if i >= len(args) {
				flagError("--cpu-limit requires a value")
			}
			spec["cpu_limit"] = parseFloatFlag("--cpu-limit", args[i])
		case "--memory":
			i++
			if i >= len(args) {
				flagError("--memory requires a value")
			}
			spec["memory_limit_mb"] = parseIntFlag("--memory", args[i])
		case "--team":
			i++
			if i >= len(args) {
				flagError("--team requires a value")
			}
			spec["labels"] = map[string]string{"team": args[i]}
		case "--wait":
			wait = true
		default:
			flagError("unknown option: " + args[i])
		}
	}

	fmt.Printf("Deploying %s to %s...\n", args[0], args[1])

	body, err := apiPost(gf, "/servers", spec)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		fmt.Fprintf(os.Stderr, "Error: malformed deploy response: %v\n", err)
		os.Exit(1)
	}

	state, _ := result["state"].(string)
	fmt.Printf("✓ Server accepted (state: %s)\n", state)

	if !wait {
		fmt.Println("  Use 'mcpctl servers' to check status, or pass --wait next time.")
		return
	}

	// ── --wait: poll until terminal state ────────────────────────────────
	//
	// v1.6.0 introduced post-deploy MCP-handshake verification. States:
	//   Pending → Deploying → Probing → Running       (success)
	//                                  → ProbeFailed   (failure with reason)
	// We poll every 2 seconds until we hit Running or ProbeFailed, or hit
	// a 5-minute hard ceiling. The probe itself runs with internal retries
	// (5x1s by default) so 5 minutes is generous.
	ns, _ := args[1], args[0]
	name := args[0]
	deadline := time.Now().Add(5 * time.Minute)
	lastState := state
	fmt.Print("  Waiting for Running")
	for time.Now().Before(deadline) {
		time.Sleep(2 * time.Second)
		body, err := apiGet(gf, fmt.Sprintf("/servers/%s/%s", ns, name))
		if err != nil {
			fmt.Printf("\n  Poll error: %v\n", err)
			continue
		}
		var s map[string]interface{}
		if err := json.Unmarshal(body, &s); err != nil {
			continue
		}
		curState, _ := s["state"].(string)
		if curState != lastState {
			fmt.Printf("\n  → %s", curState)
			lastState = curState
		} else {
			fmt.Print(".")
		}
		switch curState {
		case "Running":
			fmt.Println("\n✓ Server is Running")
			if gw, ok := s["gateway_url"].(string); ok && gw != "" {
				fmt.Printf("  Gateway URL: %s\n", gw)
			}
			return
		case "ProbeFailed":
			reason, _ := s["error_reason"].(string)
			fmt.Printf("\n✗ Probe failed: %s\n", reason)
			os.Exit(1)
		}
	}
	fmt.Println("\n✗ Timed out waiting for Running state (5 min). Use 'mcpctl servers' to check.")
	os.Exit(1)
}

// cmdRegisterExternal registers an external (vendor-hosted) MCP server.
//
// Unlike cmdDeploy (which builds a pod in-cluster), this records a proxy
// registration: Magertron will forward MCP traffic to the vendor endpoint
// using credentials from a referenced K8s Secret, applying RBAC, audit,
// and (future) DLP policy at the boundary.
//
// The row lands in 'Registered' state — operator must approve via the
// approval endpoint (mcpctl servers approve, forthcoming) before the
// proxy code path forwards any traffic.
func cmdRegisterExternal(gf globalFlags, args []string) {
	if len(args) < 1 || args[0] == "--help" || args[0] == "-h" {
		fmt.Println("Usage: mcpctl register-external --name <name> --namespace <ns> \\")
		fmt.Println("                                --endpoint-url <url> --auth-type <type> \\")
		fmt.Println("                                [--credential-secret <secret-name>]")
		fmt.Println()
		fmt.Println("Registers an external (vendor-hosted) MCP server as a proxy registration.")
		fmt.Println("Magertron forwards MCP traffic to the endpoint using the referenced K8s Secret.")
		fmt.Println()
		fmt.Println("Required:")
		fmt.Println("  --name <name>                   Server name (unique per namespace)")
		fmt.Println("  --namespace <ns>                Tenant namespace (e.g. mcp-prod)")
		fmt.Println("  --endpoint-url <url>            HTTPS URL of the vendor MCP endpoint")
		fmt.Println("  --auth-type <type>              How Magertron authenticates to the vendor.")
		fmt.Println("                                  One of: none, bearer, api-key,")
		fmt.Println("                                  oauth2-client-credentials,")
		fmt.Println("                                  oauth2-authorization-code, mtls")
		fmt.Println()
		fmt.Println("Conditional:")
		fmt.Println("  --credential-secret <name>      Name of the K8s Secret holding credentials.")
		fmt.Println("                                  Required unless --auth-type is 'none'.")
		fmt.Println()
		fmt.Println("Example:")
		fmt.Println("  mcpctl register-external \\")
		fmt.Println("    --name nitro-prod --namespace mcp-prod \\")
		fmt.Println("    --endpoint-url https://api.nitro.cloud/mcp \\")
		fmt.Println("    --auth-type bearer \\")
		fmt.Println("    --credential-secret nitro-bearer-token")
		fmt.Println()
		fmt.Println("After registration, the server lands in 'Registered' state. An operator")
		fmt.Println("with the appropriate role must approve it before traffic is proxied.")
		os.Exit(1)
	}

	var name, namespace, endpointURL, authType, credentialSecret string

	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--name":
			i++
			if i >= len(args) {
				flagError("--name requires a value")
			}
			name = args[i]
		case "--namespace":
			i++
			if i >= len(args) {
				flagError("--namespace requires a value")
			}
			namespace = args[i]
		case "--endpoint-url":
			i++
			if i >= len(args) {
				flagError("--endpoint-url requires a value")
			}
			endpointURL = args[i]
		case "--auth-type":
			i++
			if i >= len(args) {
				flagError("--auth-type requires a value")
			}
			authType = args[i]
		case "--credential-secret":
			i++
			if i >= len(args) {
				flagError("--credential-secret requires a value")
			}
			credentialSecret = args[i]
		default:
			flagError("unknown option: " + args[i])
		}
	}

	// Required-field validation. The orchestrator also validates server-side
	// (and the DB CHECK constraint is the third line of defense), but
	// catching obvious omissions client-side gives faster feedback.
	if name == "" {
		fmt.Fprintln(os.Stderr, "Error: --name is required")
		os.Exit(1)
	}
	if namespace == "" {
		fmt.Fprintln(os.Stderr, "Error: --namespace is required")
		os.Exit(1)
	}
	if endpointURL == "" {
		fmt.Fprintln(os.Stderr, "Error: --endpoint-url is required")
		os.Exit(1)
	}
	if authType == "" {
		fmt.Fprintln(os.Stderr, "Error: --auth-type is required")
		os.Exit(1)
	}

	// Allow-list. Server also enforces this; client check is for usability
	// (fail fast with a helpful message rather than waiting for HTTP 400).
	//
	// Stay in lock-step with secret_shapes() in src/api/rest_server.cpp.
	// Adding a new auth_type there → add it here as well.
	validAuthTypes := map[string]bool{
		"none":                      true,
		"bearer":                    true,
		"api-key":                   true,
		"oauth2-client-credentials": true,
		"oauth2-authorization-code": true,
		"mtls":                      true,
	}
	if !validAuthTypes[authType] {
		fmt.Fprintf(os.Stderr,
			"Error: --auth-type must be one of: none, bearer, api-key, "+
				"oauth2-client-credentials, oauth2-authorization-code, mtls (got '%s')\n",
			authType)
		os.Exit(1)
	}
	if authType != "none" && credentialSecret == "" {
		fmt.Fprintf(os.Stderr,
			"Error: --credential-secret is required when --auth-type is '%s'\n", authType)
		os.Exit(1)
	}

	spec := map[string]interface{}{
		"type":         "external",
		"name":         name,
		"namespace":    namespace,
		"endpoint_url": endpointURL,
		"auth_type":    authType,
	}
	if credentialSecret != "" {
		spec["credential_secret_ref"] = credentialSecret
	}

	fmt.Printf("Registering external MCP server %s in namespace %s...\n", name, namespace)

	body, err := apiPost(gf, "/servers", spec)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		fmt.Fprintf(os.Stderr, "Error: malformed registration response: %v\n", err)
		os.Exit(1)
	}

	state, _ := result["state"].(string)
	fmt.Printf("✓ External server registered (state: %s)\n", state)
	fmt.Println("  Next: an operator must approve this registration before traffic is proxied.")
	fmt.Println("        (Approval endpoint: forthcoming.)")
}

func cmdUndeploy(gf globalFlags, args []string) {
	if len(args) < 2 {
		fmt.Println("Usage: mcpctl undeploy <namespace> <name>")
		os.Exit(1)
	}
	ns, name := args[0], args[1]
	fmt.Printf("Undeploying %s/%s...\n", ns, name)
	if err := apiDelete(gf, fmt.Sprintf("/servers/%s/%s", ns, name)); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("✓ Server %s/%s undeployed\n", ns, name)
}

// cmdApprove flips an External MCP server registration to 'Active'.
//
// Backed by POST /api/v1/servers/{ns}/{name}/approve (Session 2.13, Step 3.5).
// State machine: Registered → Active, or Inactive → Active.
// Idempotent: approving an already-Active server succeeds with no DB write.
// Rejects: 404 if not found; 400 if the server is not server_type='external'.
func cmdApprove(gf globalFlags, args []string) {
	if len(args) < 2 {
		fmt.Println("Usage: mcpctl approve <namespace> <name>")
		fmt.Println()
		fmt.Println("Approves an External MCP server registration so Magertron's proxy")
		fmt.Println("starts forwarding traffic to its vendor endpoint.")
		fmt.Println()
		fmt.Println("Only valid for External (vendor-hosted) MCP servers. Internal and")
		fmt.Println("Hybrid servers are deployed directly via 'mcpctl deploy'.")
		fmt.Println()
		fmt.Println("Example:")
		fmt.Println("  mcpctl approve mcp-prod nitro-prod")
		os.Exit(1)
	}
	ns, name := args[0], args[1]
	fmt.Printf("Approving external server %s/%s...\n", ns, name)
	body, err := apiPost(gf, fmt.Sprintf("/servers/%s/%s/approve", ns, name), nil)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		fmt.Fprintf(os.Stderr, "Error: malformed approve response: %v\n", err)
		os.Exit(1)
	}
	state, _ := result["state"].(string)
	fmt.Printf("✓ External server %s/%s state: %s\n", ns, name, state)
}

// cmdDisable flips an External MCP server registration to 'Inactive'.
//
// Backed by POST /api/v1/servers/{ns}/{name}/disable (Session 2.13, Step 3.5).
// State machine: Active → Inactive, or Registered → Inactive ("park" an
// unapproved registration without deleting it).
// Idempotent: disabling an already-Inactive server succeeds with no DB write.
// Rejects: 404 if not found; 400 if the server is not server_type='external'.
//
// While a server is Inactive, the proxy code path refuses to forward traffic
// (returns 503 to clients). The registration row is preserved — call approve
// again to re-enable.
func cmdDisable(gf globalFlags, args []string) {
	if len(args) < 2 {
		fmt.Println("Usage: mcpctl disable <namespace> <name>")
		fmt.Println()
		fmt.Println("Disables an External MCP server registration, blocking proxy traffic")
		fmt.Println("to its vendor endpoint. The registration is preserved; call approve")
		fmt.Println("to re-enable.")
		fmt.Println()
		fmt.Println("Only valid for External (vendor-hosted) MCP servers. To remove an")
		fmt.Println("Internal or Hybrid server, use 'mcpctl undeploy'.")
		fmt.Println()
		fmt.Println("Example:")
		fmt.Println("  mcpctl disable mcp-prod nitro-prod")
		os.Exit(1)
	}
	ns, name := args[0], args[1]
	fmt.Printf("Disabling external server %s/%s...\n", ns, name)
	body, err := apiPost(gf, fmt.Sprintf("/servers/%s/%s/disable", ns, name), nil)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		fmt.Fprintf(os.Stderr, "Error: malformed disable response: %v\n", err)
		os.Exit(1)
	}
	state, _ := result["state"].(string)
	fmt.Printf("✓ External server %s/%s state: %s\n", ns, name, state)
}

func cmdScale(gf globalFlags, args []string) {
	if len(args) < 3 {
		fmt.Println("Usage: mcpctl scale <namespace> <name> <replicas>")
		os.Exit(1)
	}
	ns, name := args[0], args[1]
	replicas := parseIntFlag("<replicas>", args[2])
	fmt.Printf("Scaling %s/%s to %d replicas...\n", ns, name, replicas)
	if _, err := apiPost(gf, fmt.Sprintf("/servers/%s/%s/scale", ns, name),
		map[string]int{"replicas": replicas}); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("✓ Scaled to %d replicas\n", replicas)
}

func cmdRestart(gf globalFlags, args []string) {
	if len(args) < 2 {
		fmt.Println("Usage: mcpctl restart <namespace> <name>")
		os.Exit(1)
	}
	ns, name := args[0], args[1]
	fmt.Printf("Restarting %s/%s...\n", ns, name)
	if _, err := apiPost(gf, fmt.Sprintf("/servers/%s/%s/restart", ns, name), nil); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	fmt.Println("✓ Restart initiated")
}

func cmdLogs(gf globalFlags, args []string) {
	if len(args) < 2 {
		fmt.Println("Usage: mcpctl logs <namespace> <name>")
		os.Exit(1)
	}
	ns, name := args[0], args[1]
	body, err := apiGet(gf, fmt.Sprintf("/servers/%s/%s/logs", ns, name))
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		// /logs may also return raw text — print it.
		fmt.Println(string(body))
		return
	}
	logs, _ := result["logs"].(string)
	fmt.Println(logs)
}

func cmdHistory(gf globalFlags, args []string) {
	if len(args) < 2 {
		fmt.Println("Usage: mcpctl history <namespace> <name>")
		os.Exit(1)
	}
	ns, name := args[0], args[1]
	body, err := apiGet(gf, fmt.Sprintf("/servers/%s/%s/history", ns, name))
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		fmt.Fprintf(os.Stderr, "Error: malformed history response: %v\n", err)
		os.Exit(1)
	}
	items, _ := result["items"].([]interface{})
	if gf.jsonOut {
		emitJSON(items)
		return
	}
	if len(items) == 0 {
		fmt.Println("No deployment history.")
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "TIME\tEVENT\tACTOR\tDETAIL")
	for _, item := range items {
		ev, _ := item.(map[string]interface{})
		ts, _ := ev["timestamp"].(string)
		kind, _ := ev["kind"].(string)
		actor, _ := ev["actor"].(string)
		detail, _ := ev["detail"].(string)
		if len(detail) > 60 {
			detail = detail[:57] + "..."
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\n", ts, kind, actor, detail)
	}
	w.Flush()
}

// cmdURL is a small convenience: print the gateway URL for a server.
// Useful for shell pipelines: `URL=$(mcpctl url mcp-prod fast-time)`.
func cmdURL(gf globalFlags, args []string) {
	if len(args) < 2 {
		fmt.Println("Usage: mcpctl url <namespace> <name>")
		os.Exit(1)
	}
	ns, name := args[0], args[1]
	body, err := apiGet(gf, fmt.Sprintf("/servers/%s/%s", ns, name))
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	var s map[string]interface{}
	if err := json.Unmarshal(body, &s); err != nil {
		fmt.Fprintf(os.Stderr, "Error: malformed response: %v\n", err)
		os.Exit(1)
	}
	gw, _ := s["gateway_url"].(string)
	if gw == "" {
		fmt.Fprintln(os.Stderr, "Server has no gateway_url (may not be Running yet)")
		os.Exit(1)
	}
	fmt.Println(gw)
}

// ── Governance ───────────────────────────────────────────────────────────────

func cmdGovList(gf globalFlags) {
	body, err := apiGet(gf, "/governance/policies")
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		fmt.Fprintf(os.Stderr, "Error: malformed response: %v\n", err)
		os.Exit(1)
	}
	items, _ := result["items"].([]interface{})
	if gf.jsonOut {
		emitJSON(items)
		return
	}
	if len(items) == 0 {
		fmt.Println("No governance policies.")
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "NAME\tENABLED\tTEMPLATE\tRULES\tNAMESPACES")
	for _, item := range items {
		p, _ := item.(map[string]interface{})
		name, _ := p["name"].(string)
		enabled, _ := p["enabled"].(bool)
		tmpl, _ := p["is_template"].(bool)
		rules, _ := p["rules"].([]interface{})
		nsList, _ := p["applies_to_namespaces"].([]interface{})
		nsStrs := make([]string, len(nsList))
		for i, n := range nsList {
			nsStrs[i], _ = n.(string)
		}
		enabledStr := "✓"
		if !enabled {
			enabledStr = "✗"
		}
		tmplStr := ""
		if tmpl {
			tmplStr = "yes"
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%d\t%s\n",
			name, enabledStr, tmplStr, len(rules), strings.Join(nsStrs, ","))
	}
	w.Flush()
}

func cmdGovEvaluate(gf globalFlags, args []string) {
	if len(args) < 1 {
		fmt.Println("Usage: mcpctl governance evaluate <spec-file.json> [--namespace <ns>]")
		os.Exit(1)
	}
	data, err := os.ReadFile(args[0])
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error reading file: %v\n", err)
		os.Exit(1)
	}
	var spec interface{}
	if err := json.Unmarshal(data, &spec); err != nil {
		fmt.Fprintf(os.Stderr, "Error parsing JSON: %v\n", err)
		os.Exit(1)
	}
	body := map[string]interface{}{"spec": spec}
	for i := 1; i < len(args); i++ {
		if args[i] == "--namespace" && i+1 < len(args) {
			body["namespace"] = args[i+1]
			i++
		}
	}

	respBody, err := apiPost(gf, "/governance/evaluate", body)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	var result map[string]interface{}
	if err := json.Unmarshal(respBody, &result); err != nil {
		fmt.Fprintf(os.Stderr, "Error: malformed response: %v\n", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		emitJSON(result)
		return
	}

	allowed, _ := result["allowed"].(bool)
	if allowed {
		fmt.Println("✓ ALLOWED")
	} else {
		fmt.Println("✗ BLOCKED")
	}

	violations, _ := result["violations"].([]interface{})
	if len(violations) > 0 {
		fmt.Println()
		w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
		fmt.Fprintln(w, "SEVERITY\tPOLICY\tFIELD\tMESSAGE")
		for _, v := range violations {
			viol, _ := v.(map[string]interface{})
			sev, _ := viol["severity"].(string)
			policy, _ := viol["policy"].(string)
			field, _ := viol["field"].(string)
			msg, _ := viol["message"].(string)
			fmt.Fprintf(w, "%s\t%s\t%s\t%s\n", strings.ToUpper(sev), policy, field, msg)
		}
		w.Flush()
	}
	warnings, _ := result["warnings"].([]interface{})
	if len(warnings) > 0 && len(violations) == 0 {
		fmt.Println()
		for _, w := range warnings {
			warn, _ := w.(map[string]interface{})
			msg, _ := warn["message"].(string)
			fmt.Printf("  ⚠ %s\n", msg)
		}
	}
	if !allowed {
		os.Exit(1)
	}
}

func cmdGovExport(gf globalFlags, args []string) {
	body, err := apiGet(gf, "/governance/export")
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	// Round-trip through generic decode so we re-pretty-print consistently.
	var result interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		fmt.Fprintf(os.Stderr, "Error: malformed response: %v\n", err)
		os.Exit(1)
	}
	out, _ := json.MarshalIndent(result, "", "  ")
	if len(args) > 0 {
		if err := os.WriteFile(args[0], out, 0644); err != nil {
			fmt.Fprintf(os.Stderr, "Error writing file: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("✓ Exported to %s\n", args[0])
	} else {
		fmt.Println(string(out))
	}
}

// ── Audit ────────────────────────────────────────────────────────────────────

func cmdAudit(gf globalFlags, args []string) {
	path := "/audit"
	if len(args) > 0 {
		path += "?size=" + args[0]
	}
	body, err := apiGet(gf, path)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		fmt.Fprintf(os.Stderr, "Error: malformed response: %v\n", err)
		os.Exit(1)
	}
	items, _ := result["items"].([]interface{})
	if gf.jsonOut {
		emitJSON(items)
		return
	}
	if len(items) == 0 {
		fmt.Println("No audit events.")
		return
	}
	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "TIME\tEVENT\tSERVER\tNAMESPACE\tACTOR\tDETAIL")
	for _, item := range items {
		ev, _ := item.(map[string]interface{})
		ts, _ := ev["occurred_at"].(string)
		kind, _ := ev["kind"].(string)
		server, _ := ev["server_name"].(string)
		ns, _ := ev["namespace"].(string)
		actor, _ := ev["actor"].(string)
		detail, _ := ev["detail"].(string)
		if len(detail) > 50 {
			detail = detail[:47] + "..."
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\t%s\t%s\n", ts, kind, server, ns, actor, detail)
	}
	w.Flush()
}

// ── Users ────────────────────────────────────────────────────────────────────

func cmdListUsers(gf globalFlags) {
	body, err := apiGet(gf, "/users")
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		fmt.Fprintf(os.Stderr, "Error: malformed response: %v\n", err)
		os.Exit(1)
	}
	items, _ := result["items"].([]interface{})
	if gf.jsonOut {
		emitJSON(items)
		return
	}
	if len(items) == 0 {
		fmt.Println("No users.")
		return
	}
	// v2.0.2: surface email + provisioning provenance so operators can see
	// at-a-glance what they just configured via `users update-email` AND
	// which users are managed externally (sso_jit / scim) vs. password-
	// authenticated. Provisioned column matters before you go disabling
	// SSO and accidentally lock yourself out.
	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "USERNAME\tEMAIL\tPROVISIONED\tROLES")
	for _, item := range items {
		u, _ := item.(map[string]interface{})
		username, _ := u["username"].(string)
		email, _ := u["email"].(string)
		// provisioned_via can be password / sso_jit / scim / null (legacy
		// rows seeded before the column existed). Display "-" for missing
		// so the column doesn't collapse and confuse the eye.
		provisioned, _ := u["provisioned_via"].(string)
		if provisioned == "" {
			provisioned = "-"
		}
		if email == "" {
			email = "-"
		}
		roles, _ := u["roles"].([]interface{})
		roleStrs := make([]string, len(roles))
		for i, r := range roles {
			roleStrs[i], _ = r.(string)
		}
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\n",
			username, email, provisioned, strings.Join(roleStrs, ", "))
	}
	w.Flush()
}

// ── Helpers ──────────────────────────────────────────────────────────────────

func parseIntFlag(name, value string) int {
	n, err := strconv.Atoi(value)
	if err != nil {
		flagError(fmt.Sprintf("invalid %s: %q is not an integer", name, value))
	}
	return n
}

func parseFloatFlag(name, value string) float64 {
	f, err := strconv.ParseFloat(value, 64)
	if err != nil {
		flagError(fmt.Sprintf("invalid %s: %q is not a number", name, value))
	}
	return f
}

func flagError(msg string) {
	fmt.Fprintln(os.Stderr, "Error: "+msg)
	os.Exit(1)
}

func emitJSON(v interface{}) {
	out, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error encoding JSON: %v\n", err)
		os.Exit(1)
	}
	fmt.Println(string(out))
}

func printHelp() {
	fmt.Printf(`mcpctl v%s — MCP Orchestrator CLI

USAGE:
  mcpctl [global-flags] <command> [arguments]

GLOBAL FLAGS:
  --insecure, -k        Skip TLS certificate verification (self-signed certs)
  --ca-cert <path>      Path to a CA bundle to trust
  --json                Emit JSON instead of tables (where applicable)

CONNECTION:
  login <url> --token <jwt>    Log in with a service-account token
  logout                       Clear saved credentials
  status                       Show connection status

SERVERS:
  servers                              List all deployed servers
  deploy <name> <ns> <image> [opts]    Deploy a new MCP server (--wait to block on Running)
  register-external [opts]             Register an external (vendor-hosted) MCP server proxy
  approve <ns> <name>                  Approve an external MCP server registration (→ Active)
  disable <ns> <name>                  Disable an external MCP server (block proxy traffic)
  undeploy <ns> <name>                 Remove a server
  scale <ns> <name> <replicas>         Scale server replicas
  restart <ns> <name>                  Rolling restart
  logs <ns> <name>                     View server pod logs
  history <ns> <name>                  View deployment history
  url <ns> <name>                      Print just the gateway URL (pipeline-friendly)

ORGS:
  orgs list                            List organizations
  orgs get <id>                        Get org details

SERVICE ACCOUNTS (alias: sa):
  sa list --org <id>                   List SAs in an org
  sa get <id>                          Get SA details
  sa create --name <n> --subject <s> --org <id> \
            --role <r>... [--scope <s>...] \
            [--ttl-days <N>] [--owner-email <e>] [--description <text>]
                                       Create SA (prints JWT once — save it)
  sa update <id> [--name <n>] [--description <d>] \
            [--owner-email <e>] [--clear-owner-email] \
            [--role <r>...] [--scope <s>...]
                                       Update SA fields
  sa rotate <id>                       Rotate SA — old is revoked, new JWT printed
  sa revoke <id>                       Revoke SA (soft delete)
  sa expiring [--threshold-days 30]    List SAs expiring within N days

USERS:
  users list                           List all users
  users update-email <user> <email>    Set user's email address
  users delete <user>                  Delete a user

WEBHOOKS:
  webhooks list                        List configured webhooks
  webhooks get <id>                    Get webhook details
  webhooks create --name <n> --url <u> [--secret <s>] [--namespace <ns>]
  webhooks create --from-file <spec.json>
                                       Create webhook from full JSON spec
  webhooks update <id> --from-file <spec.json>
                                       Replace webhook (PUT semantics)
  webhooks delete <id>                 Delete a webhook
  webhooks test <id>                   Send a test notification

RETENTION:
  retention status                     Show retention policy + per-table stats

GOVERNANCE:
  governance list                      List all policies
  governance evaluate <file>           Evaluate spec against policies
  governance export [file]             Export policies as JSON

OBSERVABILITY:
  audit [limit]                        View audit log

EXAMPLES:
  mcpctl login https://localhost:30444 --token eyJhbGc... --insecure
  mcpctl deploy fast-time mcp-prod ghcr.io/ibm/fast-time-server \
    --upstream-path /http --port 8080 --wait
  mcpctl servers | grep Running

  mcpctl orgs list
  mcpctl sa list --org 11111111-1111-1111-1111-111111111111
  mcpctl sa create --name ci-bot --subject ci_bot \
    --org 11111111-1111-1111-1111-111111111111 \
    --role system:platform-admin --ttl-days 90 \
    --owner-email ops@yourco.com
  mcpctl sa rotate <id>
  mcpctl sa expiring --threshold-days 14
  # (--subject must be lowercase a-z, 0-9, '-', or '_' only)

  mcpctl webhooks create --name slack-ops \
    --url https://hooks.slack.com/services/... --secret s3cret
  mcpctl webhooks test <id>

  mcpctl retention status
  mcpctl users update-email alice alice@yourco.com

NOTES:
  The CLI authenticates as a SERVICE ACCOUNT, not as a person. A human login
 hits multi-factor authentication, which completes in a browser and cannot
 finish at a command line — and a service account is the right identity for
 automation anyway: attributable, scoped, and revocable without touching
 anyone's account. Create one in the console (Users -> Service Accounts); the
 JWT is shown once, at creation.

 After a successful login the token is saved to ~/.config/mcpctl/config.json,
 so later commands need no flag.

 Default Magertron gateway is HTTPS on port 30444 (TLS+ext_authz unified
  in v1.6.0). Plain HTTP listeners on 8080/30080 were removed in that
  release. Use --insecure for self-signed certs typical of fresh helm
  installs, or --ca-cert to pin the CA bundle.

  mcpctl v3.x is tested against Magertron platform v3.x. Older platform
  versions may work and are not tested; if a command fails against one,
  the platform version is the first thing to check.
  Some commands require Enterprise features (webhooks); the orchestrator
  will return a clear error if the feature isn't licensed.
`, version)
}

// ============================================================================
// v2.0 ADDITIONS BELOW
// ============================================================================
//
// Session 2.11+ additions: Service accounts, Orgs, Webhooks, Retention status,
// Users update-email + delete. Bumps mcpctl from v1.6 to v2.0 — sync-at-majors
// versioning means v2.x talks to platform v2.x.
//
// Layout: each domain has its own section divider below. All commands follow
// the same patterns as the v1 commands (apiGet/apiPost/etc., emitJSON honored,
// table output otherwise).

// ─── PUT and PATCH helpers (new in v2.0) ─────────────────────────────────────

func apiPut(gf globalFlags, path string, in interface{}) ([]byte, error) {
	body, status, err := apiRequest(gf, "PUT", path, in)
	if err != nil {
		return nil, err
	}
	if status >= 400 {
		return nil, fmt.Errorf("HTTP %d: %s%s", status, extractErrorMessage(body), extractLicenseHint(body))
	}
	return body, nil
}

func apiPatch(gf globalFlags, path string, in interface{}) ([]byte, error) {
	body, status, err := apiRequest(gf, "PATCH", path, in)
	if err != nil {
		return nil, err
	}
	if status >= 400 {
		return nil, fmt.Errorf("HTTP %d: %s", status, extractErrorMessage(body))
	}
	return body, nil
}

// flagValue extracts --name <value> or --name=<value> from args.
// Returns the value (whitespace-trimmed) and the args with that flag pair
// removed. Multiple occurrences keep the last one (idiomatic for CLI flags).
//
// Whitespace trimming protects against paste-from-terminal trailing-space
// or trailing-newline bugs, which the orchestrator's validators reject
// with vague messages like "must be UUID" or "must be lowercase a-z...".
func flagValue(args []string, name string) (string, []string) {
	out := make([]string, 0, len(args))
	value := ""
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--"+name {
			if i+1 < len(args) {
				value = args[i+1]
				i++ // skip the value
			}
			continue
		}
		if strings.HasPrefix(a, "--"+name+"=") {
			value = strings.TrimPrefix(a, "--"+name+"=")
			continue
		}
		out = append(out, a)
	}
	return strings.TrimSpace(value), out
}

// flagValuesMulti extracts repeating --name <value> (e.g. --role r1 --role r2).
// Returns all collected values (whitespace-trimmed) and args minus those flag
// pairs.
func flagValuesMulti(args []string, name string) ([]string, []string) {
	out := make([]string, 0, len(args))
	values := []string{}
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--"+name {
			if i+1 < len(args) {
				values = append(values, strings.TrimSpace(args[i+1]))
				i++
			}
			continue
		}
		if strings.HasPrefix(a, "--"+name+"=") {
			values = append(values, strings.TrimSpace(strings.TrimPrefix(a, "--"+name+"=")))
			continue
		}
		out = append(out, a)
	}
	return values, out
}

// flagPresent returns true if --name appears in args (boolean flag), and
// returns args with that flag removed.
func flagPresent(args []string, name string) (bool, []string) {
	out := make([]string, 0, len(args))
	present := false
	for _, a := range args {
		if a == "--"+name {
			present = true
			continue
		}
		out = append(out, a)
	}
	return present, out
}

// ─── orgs ────────────────────────────────────────────────────────────────────

func cmdOrgs(gf globalFlags, args []string) {
	if len(args) == 0 {
		fmt.Println("Usage: mcpctl orgs <list|get>")
		os.Exit(1)
	}
	switch args[0] {
	case "list", "ls":
		cmdOrgsList(gf)
	case "get":
		if len(args) < 2 {
			fmt.Println("Usage: mcpctl orgs get <id>")
			os.Exit(1)
		}
		cmdOrgsGet(gf, args[1])
	default:
		fmt.Printf("Unknown orgs subcommand: %s\n", args[0])
		os.Exit(1)
	}
}

func cmdOrgsList(gf globalFlags) {
	body, err := apiGet(gf, "/orgs")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	var resp struct {
		Items []struct {
			ID   string `json:"id"`
			Slug string `json:"slug"`
			Name string `json:"name"`
		} `json:"items"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		fmt.Fprintln(os.Stderr, "Error parsing response:", err)
		os.Exit(1)
	}
	if len(resp.Items) == 0 {
		fmt.Println("No organizations.")
		return
	}
	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "ID\tSLUG\tNAME")
	for _, o := range resp.Items {
		fmt.Fprintf(tw, "%s\t%s\t%s\n", o.ID, o.Slug, o.Name)
	}
	tw.Flush()
}

func cmdOrgsGet(gf globalFlags, id string) {
	body, err := apiGet(gf, "/orgs/"+id)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	// Generic pretty print — orgs response is small enough to dump
	fmt.Println(string(body))
}

// ─── service-accounts ────────────────────────────────────────────────────────

func cmdServiceAccounts(gf globalFlags, args []string) {
	if len(args) == 0 {
		fmt.Println("Usage: mcpctl service-accounts <list|get|create|update|rotate|revoke|expiring>")
		fmt.Println("Alias: mcpctl sa <...>")
		os.Exit(1)
	}
	sub := args[0]
	rest := args[1:]
	switch sub {
	case "list", "ls":
		cmdSaList(gf, rest)
	case "get":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl service-accounts get <id>")
			os.Exit(1)
		}
		cmdSaGet(gf, rest[0])
	case "create":
		cmdSaCreate(gf, rest)
	case "update":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl service-accounts update <id> [flags]")
			os.Exit(1)
		}
		cmdSaUpdate(gf, rest[0], rest[1:])
	case "rotate":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl service-accounts rotate <id>")
			os.Exit(1)
		}
		cmdSaRotate(gf, rest[0])
	case "revoke":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl service-accounts revoke <id>")
			os.Exit(1)
		}
		cmdSaRevoke(gf, rest[0])
	case "expiring":
		cmdSaExpiring(gf, rest)
	default:
		fmt.Printf("Unknown service-accounts subcommand: %s\n", sub)
		os.Exit(1)
	}
}

func cmdSaList(gf globalFlags, args []string) {
	orgID, args := flagValue(args, "org")
	_ = args
	orgID = strings.TrimSpace(orgID)
	if orgID == "" {
		fmt.Fprintln(os.Stderr, "Error: --org <uuid> is required.")
		fmt.Fprintln(os.Stderr, "  Use 'mcpctl orgs list' to find the UUID (NOT the org name).")
		os.Exit(1)
	}
	body, err := apiGet(gf, "/service-accounts?organization_id="+orgID)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	var resp struct {
		Items []struct {
			ID         string   `json:"id"`
			Name       string   `json:"name"`
			Subject    string   `json:"subject"`
			OwnerEmail string   `json:"owner_email"`
			RoleIDs    []string `json:"role_ids"`
			CreatedAt  string   `json:"created_at"`
			ExpiresAt  string   `json:"expires_at"`
			RevokedAt  string   `json:"revoked_at"`
			LastUsedAt string   `json:"last_used_at"`
		} `json:"items"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		fmt.Fprintln(os.Stderr, "Error parsing response:", err)
		os.Exit(1)
	}
	if len(resp.Items) == 0 {
		fmt.Println("No service accounts in this org.")
		return
	}
	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "ID\tSUBJECT\tOWNER\tROLES\tEXPIRES\tSTATUS")
	for _, sa := range resp.Items {
		status := "active"
		if sa.RevokedAt != "" {
			status = "revoked"
		}
		owner := sa.OwnerEmail
		if owner == "" {
			owner = "—"
		}
		exp := sa.ExpiresAt
		if len(exp) > 10 {
			exp = exp[:10] // YYYY-MM-DD
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\t%s\t%s\n",
			sa.ID[:8], sa.Subject, owner, strings.Join(sa.RoleIDs, ","), exp, status)
	}
	tw.Flush()
	fmt.Printf("\n%d service account(s). IDs shown as first 8 chars; use 'sa get <id>' with full UUID for detail.\n", len(resp.Items))
}

func cmdSaGet(gf globalFlags, id string) {
	body, err := apiGet(gf, "/service-accounts/"+id)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	// Pretty-print as YAML-ish key/value list
	var m map[string]interface{}
	if err := json.Unmarshal(body, &m); err != nil {
		fmt.Println(string(body))
		return
	}
	keys := []string{"id", "name", "subject", "owner_email", "organization_id",
		"role_ids", "scopes", "created_by", "created_at", "expires_at",
		"revoked_at", "last_used_at", "description"}
	for _, k := range keys {
		if v, ok := m[k]; ok && v != nil {
			fmt.Printf("%-18s %v\n", k+":", v)
		}
	}
}

func cmdSaCreate(gf globalFlags, args []string) {
	name, args := flagValue(args, "name")
	subject, args := flagValue(args, "subject")
	orgID, args := flagValue(args, "org")
	ownerEmail, args := flagValue(args, "owner-email")
	description, args := flagValue(args, "description")
	ttlDays, args := flagValue(args, "ttl-days")
	roles, args := flagValuesMulti(args, "role")
	scopes, args := flagValuesMulti(args, "scope")
	_ = args

	// Trim whitespace from all string flag values. Paste-from-terminal often
	// brings a trailing space or newline, and the orchestrator rejects those
	// with vague "must be UUID" / "must be lowercase a-z..." errors.
	name = strings.TrimSpace(name)
	subject = strings.TrimSpace(subject)
	orgID = strings.TrimSpace(orgID)
	ownerEmail = strings.TrimSpace(ownerEmail)
	description = strings.TrimSpace(description)
	ttlDays = strings.TrimSpace(ttlDays)
	for i, r := range roles {
		roles[i] = strings.TrimSpace(r)
	}
	for i, s := range scopes {
		scopes[i] = strings.TrimSpace(s)
	}

	if name == "" || subject == "" || orgID == "" {
		fmt.Println("Usage: mcpctl service-accounts create \\")
		fmt.Println("         --name <name> --subject <sub> --org <org-uuid> \\")
		fmt.Println("         [--role <role>...] [--scope <scope>...] \\")
		fmt.Println("         [--ttl-days <N>] [--owner-email <email>] \\")
		fmt.Println("         [--description <text>]")
		fmt.Println()
		fmt.Println("Notes:")
		fmt.Println("  --org takes the organization UUID (use 'mcpctl orgs list' to find it),")
		fmt.Println("    NOT the org name or slug.")
		fmt.Println("  --subject must be lowercase a-z, 0-9, '-', or '_' (no '@', spaces, etc.)")
		os.Exit(1)
	}
	if len(roles) == 0 {
		fmt.Fprintln(os.Stderr, "Error: at least one --role is required")
		os.Exit(1)
	}
	ttl := 90
	if ttlDays != "" {
		ttl = parseIntFlag("ttl-days", ttlDays)
		if ttl < 1 || ttl > 365 {
			flagError("ttl-days must be 1-365")
		}
	}

	payload := map[string]interface{}{
		"organization_id": orgID,
		"name":            name,
		"subject":         subject,
		"role_ids":        roles,
		"scopes":          scopes,
		"ttl_seconds":     ttl * 86400,
	}
	if ownerEmail != "" {
		payload["owner_email"] = ownerEmail
	}
	if description != "" {
		payload["description"] = description
	}

	body, err := apiPost(gf, "/service-accounts", payload)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	// Show ID + token. Token is shown once — make it obvious.
	var resp map[string]interface{}
	if err := json.Unmarshal(body, &resp); err != nil {
		fmt.Println(string(body))
		return
	}
	id, _ := resp["id"].(string)
	token, _ := resp["token"].(string)
	fmt.Printf("Service account created.\n")
	fmt.Printf("  ID:      %s\n", id)
	fmt.Printf("  Subject: %s\n", subject)
	if ownerEmail != "" {
		fmt.Printf("  Owner:   %s\n", ownerEmail)
	}
	fmt.Printf("\n")
	fmt.Printf("JWT (save this NOW — not shown again):\n")
	fmt.Printf("%s\n", token)
}

func cmdSaUpdate(gf globalFlags, id string, args []string) {
	name, args := flagValue(args, "name")
	description, args := flagValue(args, "description")
	ownerEmail, args := flagValue(args, "owner-email")
	clearOwnerEmail, args := flagPresent(args, "clear-owner-email")
	roles, args := flagValuesMulti(args, "role")
	scopes, args := flagValuesMulti(args, "scope")
	_ = args

	patch := map[string]interface{}{}
	if name != "" {
		patch["name"] = name
	}
	if description != "" {
		patch["description"] = description
	}
	if clearOwnerEmail {
		patch["owner_email"] = ""
	} else if ownerEmail != "" {
		patch["owner_email"] = ownerEmail
	}
	if len(roles) > 0 {
		patch["role_ids"] = roles
	}
	if len(scopes) > 0 {
		patch["scopes"] = scopes
	}
	if len(patch) == 0 {
		fmt.Fprintln(os.Stderr, "Error: nothing to update — pass at least one of --name, --description, --owner-email, --clear-owner-email, --role, --scope")
		os.Exit(1)
	}

	body, err := apiPatch(gf, "/service-accounts/"+id, patch)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	fmt.Printf("Service account updated: %s\n", id)
}

func cmdSaRotate(gf globalFlags, id string) {
	body, err := apiPost(gf, "/service-accounts/"+id+"/rotate", nil)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	var resp map[string]interface{}
	if err := json.Unmarshal(body, &resp); err != nil {
		fmt.Println(string(body))
		return
	}
	newID, _ := resp["id"].(string)
	token, _ := resp["token"].(string)
	fmt.Printf("Service account rotated.\n")
	fmt.Printf("  Old ID (now revoked): %s\n", id)
	fmt.Printf("  New ID:               %s\n", newID)
	fmt.Printf("\nNew JWT (save this NOW — not shown again):\n")
	fmt.Printf("%s\n", token)
}

func cmdSaRevoke(gf globalFlags, id string) {
	if err := apiDelete(gf, "/service-accounts/"+id); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	fmt.Printf("Service account revoked: %s\n", id)
}

func cmdSaExpiring(gf globalFlags, args []string) {
	thresholdStr, args := flagValue(args, "threshold-days")
	_ = args
	threshold := 30
	if thresholdStr != "" {
		threshold = parseIntFlag("threshold-days", thresholdStr)
		if threshold < 1 || threshold > 365 {
			flagError("threshold-days must be 1-365")
		}
	}

	// Client-side implementation: list all orgs, list SAs in each, filter
	// locally on expires_at. The orchestrator doesn't currently expose a
	// dedicated "list expiring SAs" endpoint, but the expiry-reminder
	// worker reads the same inventory data we can reach by enumerating.
	orgsBody, err := apiGet(gf, "/orgs")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error listing orgs:", err)
		os.Exit(1)
	}
	var orgsResp struct {
		Items []struct {
			ID   string `json:"id"`
			Slug string `json:"slug"`
			Name string `json:"name"`
		} `json:"items"`
	}
	if err := json.Unmarshal(orgsBody, &orgsResp); err != nil {
		fmt.Fprintln(os.Stderr, "Error parsing orgs response:", err)
		os.Exit(1)
	}

	cutoff := time.Now().UTC().Add(time.Duration(threshold) * 24 * time.Hour)

	type expiringSA struct {
		OrgSlug    string
		ID         string
		Subject    string
		OwnerEmail string
		ExpiresAt  string
		DaysLeft   int
	}
	var expiring []expiringSA

	for _, org := range orgsResp.Items {
		saBody, err := apiGet(gf, "/service-accounts?organization_id="+org.ID)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Warning: failed to list SAs in org %s: %v\n", org.Slug, err)
			continue
		}
		var saResp struct {
			Items []struct {
				ID         string `json:"id"`
				Subject    string `json:"subject"`
				OwnerEmail string `json:"owner_email"`
				ExpiresAt  string `json:"expires_at"`
				RevokedAt  string `json:"revoked_at"`
			} `json:"items"`
		}
		if err := json.Unmarshal(saBody, &saResp); err != nil {
			continue
		}
		for _, sa := range saResp.Items {
			if sa.RevokedAt != "" {
				continue
			}
			if sa.ExpiresAt == "" {
				continue
			}
			exp, err := time.Parse(time.RFC3339, sa.ExpiresAt)
			if err != nil {
				// Try date-only form
				exp, err = time.Parse("2006-01-02T15:04:05Z", sa.ExpiresAt)
				if err != nil {
					continue
				}
			}
			if exp.After(cutoff) {
				continue
			}
			days := int(time.Until(exp).Hours() / 24)
			expiring = append(expiring, expiringSA{
				OrgSlug:    org.Slug,
				ID:         sa.ID,
				Subject:    sa.Subject,
				OwnerEmail: sa.OwnerEmail,
				ExpiresAt:  sa.ExpiresAt,
				DaysLeft:   days,
			})
		}
	}

	if gf.jsonOut {
		emitJSON(expiring)
		return
	}
	if len(expiring) == 0 {
		fmt.Printf("No service accounts expire within %d days.\n", threshold)
		return
	}
	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "ORG\tID\tSUBJECT\tOWNER\tEXPIRES\tDAYS LEFT")
	for _, e := range expiring {
		owner := e.OwnerEmail
		if owner == "" {
			owner = "—"
		}
		exp := e.ExpiresAt
		if len(exp) > 10 {
			exp = exp[:10]
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\t%s\t%d\n",
			e.OrgSlug, e.ID[:8], e.Subject, owner, exp, e.DaysLeft)
	}
	tw.Flush()
	fmt.Printf("\n%d service account(s) expire within %d days.\n", len(expiring), threshold)
}

// ─── users (additions) ───────────────────────────────────────────────────────

func cmdUsers(gf globalFlags, args []string) {
	// New subcommand style: mcpctl users <list|update-email|delete>
	// Backward compat: bare `mcpctl users` falls through to list.
	if len(args) == 0 {
		cmdListUsers(gf)
		return
	}
	switch args[0] {
	case "list", "ls":
		cmdListUsers(gf)
	case "update-email":
		if len(args) < 3 {
			fmt.Println("Usage: mcpctl users update-email <username> <new-email>")
			os.Exit(1)
		}
		cmdUserUpdateEmail(gf, args[1], args[2])
	case "delete":
		if len(args) < 2 {
			fmt.Println("Usage: mcpctl users delete <username>")
			os.Exit(1)
		}
		cmdUserDelete(gf, args[1])
	default:
		// Could be a username (old `mcpctl users <action>` pattern) — error out
		fmt.Printf("Unknown users subcommand: %s\n", args[0])
		fmt.Println("Available: list, update-email, delete")
		os.Exit(1)
	}
}

func cmdUserUpdateEmail(gf globalFlags, username, email string) {
	if _, err := apiPut(gf, "/users/"+username+"/email", map[string]string{"email": email}); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	fmt.Printf("Email updated for user '%s': %s\n", username, email)
}

func cmdUserDelete(gf globalFlags, username string) {
	if err := apiDelete(gf, "/users/"+username); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	fmt.Printf("User deleted: %s\n", username)
}

// ─── webhooks ────────────────────────────────────────────────────────────────

func cmdWebhooks(gf globalFlags, args []string) {
	if len(args) == 0 {
		fmt.Println("Usage: mcpctl webhooks <list|get|create|update|delete|test>")
		os.Exit(1)
	}
	sub := args[0]
	rest := args[1:]
	switch sub {
	case "list", "ls":
		cmdWebhooksList(gf)
	case "get":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl webhooks get <id>")
			os.Exit(1)
		}
		cmdWebhooksGet(gf, rest[0])
	case "create":
		cmdWebhooksCreate(gf, rest)
	case "update":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl webhooks update <id> [flags]")
			os.Exit(1)
		}
		cmdWebhooksUpdate(gf, rest[0], rest[1:])
	case "delete":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl webhooks delete <id>")
			os.Exit(1)
		}
		cmdWebhooksDelete(gf, rest[0])
	case "test":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl webhooks test <id>")
			os.Exit(1)
		}
		cmdWebhooksTest(gf, rest[0])
	default:
		fmt.Printf("Unknown webhooks subcommand: %s\n", sub)
		os.Exit(1)
	}
}

func cmdWebhooksList(gf globalFlags) {
	body, err := apiGet(gf, "/webhooks")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	// Pretty-print whatever fields are there. We don't know the exact Webhook
	// shape from the CLI's POV — handle gracefully.
	var resp struct {
		Items []map[string]interface{} `json:"items"`
		Total int                      `json:"total"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		fmt.Println(string(body))
		return
	}
	if len(resp.Items) == 0 {
		fmt.Println("No webhooks configured.")
		return
	}
	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "NAME\tURL\tENABLED\tNAMESPACE")
	for _, w := range resp.Items {
		name := fmt.Sprintf("%v", w["name"])
		url := fmt.Sprintf("%v", w["url"])
		if len(url) > 60 {
			url = url[:57] + "..."
		}
		enabled := fmt.Sprintf("%v", w["enabled"])
		ns := fmt.Sprintf("%v", w["namespace"])
		if ns == "<nil>" || ns == "" {
			ns = "*"
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\n", name, url, enabled, ns)
	}
	tw.Flush()
}

func cmdWebhooksGet(gf globalFlags, id string) {
	body, err := apiGet(gf, "/webhooks/"+id)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	// Pretty print JSON (we don't know the full shape)
	var raw interface{}
	if err := json.Unmarshal(body, &raw); err == nil {
		out, _ := json.MarshalIndent(raw, "", "  ")
		fmt.Println(string(out))
	} else {
		fmt.Println(string(body))
	}
}

func cmdWebhooksCreate(gf globalFlags, args []string) {
	name, args := flagValue(args, "name")
	url, args := flagValue(args, "url")
	secret, args := flagValue(args, "secret")
	namespace, args := flagValue(args, "namespace")
	fromFile, args := flagValue(args, "from-file")
	_ = args

	var payload map[string]interface{}
	if fromFile != "" {
		// Customer provided a full JSON spec — pass through verbatim so they can
		// set fields the CLI doesn't expose explicitly.
		data, err := os.ReadFile(fromFile)
		if err != nil {
			fmt.Fprintln(os.Stderr, "Error reading", fromFile, ":", err)
			os.Exit(1)
		}
		if err := json.Unmarshal(data, &payload); err != nil {
			fmt.Fprintln(os.Stderr, "Error parsing JSON in", fromFile, ":", err)
			os.Exit(1)
		}
	} else {
		if name == "" || url == "" {
			fmt.Println("Usage: mcpctl webhooks create --name <name> --url <url> \\")
			fmt.Println("         [--secret <shared-secret>] [--namespace <ns-filter>]")
			fmt.Println("Or:    mcpctl webhooks create --from-file <spec.json>")
			os.Exit(1)
		}
		payload = map[string]interface{}{
			"name":    name,
			"url":     url,
			"enabled": true,
		}
		if secret != "" {
			payload["secret"] = secret
		}
		if namespace != "" {
			payload["namespace"] = namespace
		}
	}

	body, err := apiPost(gf, "/webhooks", payload)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	var resp map[string]interface{}
	_ = json.Unmarshal(body, &resp)
	fmt.Printf("Webhook created: %v\n", resp["name"])
	fmt.Println("(Use 'mcpctl webhooks list' to find the ID for update/delete/test.)")
}

func cmdWebhooksUpdate(gf globalFlags, id string, args []string) {
	fromFile, args := flagValue(args, "from-file")
	_ = args

	var payload map[string]interface{}
	if fromFile != "" {
		data, err := os.ReadFile(fromFile)
		if err != nil {
			fmt.Fprintln(os.Stderr, "Error reading", fromFile, ":", err)
			os.Exit(1)
		}
		if err := json.Unmarshal(data, &payload); err != nil {
			fmt.Fprintln(os.Stderr, "Error parsing JSON in", fromFile, ":", err)
			os.Exit(1)
		}
	} else {
		fmt.Fprintln(os.Stderr, "Error: --from-file <spec.json> is required for update")
		fmt.Fprintln(os.Stderr, "(The webhook PUT replaces the entire record; provide a complete spec)")
		os.Exit(1)
	}

	if _, err := apiPut(gf, "/webhooks/"+id, payload); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	fmt.Printf("Webhook updated: %s\n", id)
}

func cmdWebhooksDelete(gf globalFlags, id string) {
	if err := apiDelete(gf, "/webhooks/"+id); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	fmt.Printf("Webhook deleted: %s\n", id)
}

func cmdWebhooksTest(gf globalFlags, id string) {
	body, err := apiPost(gf, "/webhooks/"+id+"/test", nil)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		fmt.Fprintln(os.Stderr, "(Check the webhook URL is reachable from inside the cluster, and that the secret matches what the receiver expects.)")
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	fmt.Printf("Test notification sent. Check the webhook receiver.\n")
}

// ─── retention ───────────────────────────────────────────────────────────────

func cmdRetention(gf globalFlags, args []string) {
	if len(args) == 0 || args[0] == "status" {
		cmdRetentionStatus(gf)
		return
	}
	switch args[0] {
	case "status":
		cmdRetentionStatus(gf)
	default:
		fmt.Printf("Unknown retention subcommand: %s\n", args[0])
		fmt.Println("Available: status")
		fmt.Println("Note: retention policy is set in Helm values (retention.*), not via API.")
		os.Exit(1)
	}
}

func cmdRetentionStatus(gf globalFlags) {
	body, err := apiGet(gf, "/admin/database/health")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	var resp struct {
		RetentionEnabled bool   `json:"retention_enabled"`
		NextRunAt        string `json:"next_run_at"`
		Tables           []struct {
			Name             string `json:"name"`
			RowCount         int64  `json:"row_count"`
			SizeBytes        int64  `json:"size_bytes"`
			OldestRowUnixSec *int64 `json:"oldest_row_unix_sec"`
			RetentionPolicy  *struct {
				Enabled       bool `json:"enabled"`
				RetentionDays int  `json:"retention_days"`
			} `json:"retention_policy"`
			LastPrune *struct {
				RanAt       string `json:"ran_at"`
				RowsDeleted int64  `json:"rows_deleted"`
				DurationMs  int64  `json:"duration_ms"`
				Status      string `json:"status"`
			} `json:"last_prune"`
		} `json:"tables"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		fmt.Fprintln(os.Stderr, "Error parsing response:", err)
		fmt.Println(string(body))
		return
	}
	fmt.Printf("Retention worker:  %s\n", map[bool]string{true: "enabled", false: "disabled"}[resp.RetentionEnabled])
	if resp.NextRunAt != "" {
		fmt.Printf("Next run at (UTC): %s\n", resp.NextRunAt)
	}
	fmt.Println()

	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "TABLE\tROWS\tSIZE\tRETENTION\tLAST PRUNE")
	for _, t := range resp.Tables {
		policy := "—"
		if t.RetentionPolicy != nil && t.RetentionPolicy.Enabled {
			policy = fmt.Sprintf("%dd", t.RetentionPolicy.RetentionDays)
		}
		lastPrune := "—"
		if t.LastPrune != nil {
			if t.LastPrune.RanAt != "" && len(t.LastPrune.RanAt) >= 10 {
				lastPrune = fmt.Sprintf("%s (%d rows, %s)",
					t.LastPrune.RanAt[:10], t.LastPrune.RowsDeleted, t.LastPrune.Status)
			}
		}
		fmt.Fprintf(tw, "%s\t%d\t%s\t%s\t%s\n",
			t.Name, t.RowCount, humanBytes(t.SizeBytes), policy, lastPrune)
	}
	tw.Flush()
}

func humanBytes(b int64) string {
	const unit = 1024
	if b < unit {
		return fmt.Sprintf("%dB", b)
	}
	div, exp := int64(unit), 0
	for n := b / unit; n >= unit; n /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f%cB", float64(b)/float64(div), "KMGTPE"[exp])
}

// ============================================================================
// END v2.0 ADDITIONS
// ============================================================================

// ─── Main ────────────────────────────────────────────────────────────────────

func main() {
	args, gf := stripGlobalFlags(os.Args[1:])

	if len(args) == 0 {
		printHelp()
		os.Exit(0)
	}

	cmd := args[0]
	cmdArgs := args[1:]

	switch cmd {
	case "login":
		cmdLogin(gf, cmdArgs)
	case "logout":
		cmdLogout()
	case "status":
		cmdStatus(gf)
	case "servers", "list":
		cmdListServers(gf)
	case "deploy":
		cmdDeploy(gf, cmdArgs)
	// ⚠ submit is NOT deploy. It records that a server EXISTS so the platform
	// team can review it; it creates nothing routable. See submit.go.
	case "submit":
		cmdSubmit(gf, cmdArgs)
	case "register-external", "register":
		cmdRegisterExternal(gf, cmdArgs)
	case "approve":
		cmdApprove(gf, cmdArgs)
	case "disable":
		cmdDisable(gf, cmdArgs)
	case "undeploy":
		cmdUndeploy(gf, cmdArgs)
	case "scale":
		cmdScale(gf, cmdArgs)
	case "restart":
		cmdRestart(gf, cmdArgs)
	case "logs":
		cmdLogs(gf, cmdArgs)
	case "history":
		cmdHistory(gf, cmdArgs)
	case "url":
		cmdURL(gf, cmdArgs)
	case "governance", "gov":
		if len(cmdArgs) == 0 {
			fmt.Println("Usage: mcpctl governance <list|evaluate|export>")
			os.Exit(1)
		}
		switch cmdArgs[0] {
		case "list", "ls":
			cmdGovList(gf)
		case "evaluate", "eval":
			cmdGovEvaluate(gf, cmdArgs[1:])
		case "export":
			cmdGovExport(gf, cmdArgs[1:])
		default:
			fmt.Printf("Unknown governance command: %s\n", cmdArgs[0])
			os.Exit(1)
		}
	case "audit":
		cmdAudit(gf, cmdArgs)
	case "users":
		cmdUsers(gf, cmdArgs)
	case "orgs":
		cmdOrgs(gf, cmdArgs)
	case "service-accounts", "sa":
		cmdServiceAccounts(gf, cmdArgs)
	case "inventory", "inv":
		cmdInventory(gf, cmdArgs)
	case "cost", "billing":
		cmdCost(gf, cmdArgs)
	case "departments", "depts":
		cmdDepartments(gf, cmdArgs)
	case "routing", "routes":
		cmdRouting(gf, cmdArgs)
	case "webhooks":
		cmdWebhooks(gf, cmdArgs)
	case "retention":
		cmdRetention(gf, cmdArgs)
	case "version":
		fmt.Printf("mcpctl v%s\n", version)
	case "help", "-h", "--help":
		printHelp()
	default:
		fmt.Printf("Unknown command: %s\nRun 'mcpctl help' for usage.\n", cmd)
		os.Exit(1)
	}
}

// str safely extracts a string from a map[string]interface{} value.
// ⚠ Returns "" for nil or non-string rather than panicking — API responses
// carry nulls for joined columns whose target no longer exists.
func str(v interface{}) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprintf("%v", v)
}

// ─── Model routing policies ──────────────────────────────────────────────────
//
// ⚠ Routing is a CONTROL, not a convenience. A caller with no policy is refused;
// there is no silent default. A caller naming a different model than policy
// allows is refused rather than substituted — an answer attributed to a model
// that never saw the prompt is worse than no answer.
//
// Pro tier. A Free licence gets 402 with the tier and upgrade URL.

func cmdRouting(gf globalFlags, args []string) {
	if len(args) == 0 {
		fmt.Println("Usage: mcpctl routing <list|get|create|delete> [args]")
		os.Exit(1)
	}
	sub := args[0]
	rest := args[1:]

	switch sub {
	case "list", "ls":
		cmdRoutingList(gf)
	case "get":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl routing get <id>")
			os.Exit(1)
		}
		cmdRoutingGet(gf, rest[0])
	case "create":
		cmdRoutingCreate(gf, rest)
	case "delete", "rm":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl routing delete <id>")
			os.Exit(1)
		}
		cmdRoutingDelete(gf, rest[0])
	default:
		fmt.Printf("Unknown routing subcommand: %s\n", sub)
		os.Exit(1)
	}
}

func cmdRoutingList(gf globalFlags) {
	body, err := apiGet(gf, "/llm/routing-policies")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	var resp struct {
		Items []map[string]interface{} `json:"items"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		fmt.Println(string(body))
		return
	}
	if len(resp.Items) == 0 {
		// ⚠ Say what the absence MEANS. No policies is not an empty list, it is
		// a posture: every unnamed call is refused.
		fmt.Println("No routing policies. Callers must name a model server explicitly;")
		fmt.Println("a call to /api/v1/llm/messages without one is refused.")
		return
	}
	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "NAME\tCALLER\tTARGET\tMODEL\tENABLED")
	for _, r := range resp.Items {
		model := str(r["target_model"])
		if model == "" {
			// ⚠ A target with no model label cannot serve a routed call — the
			// proxy has nothing to fill in. Worth flagging, not blanking.
			model = "(none — routed calls fail)"
		}
		fmt.Fprintf(tw, "%s\t%s\t%s/%s\t%s\t%v\n",
			str(r["name"]), str(r["match_subject"]),
			str(r["target_namespace"]), str(r["target_server_name"]),
			model, r["enabled"])
	}
	tw.Flush()
}

func cmdRoutingGet(gf globalFlags, id string) {
	body, err := apiGet(gf, "/llm/routing-policies")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	var resp struct {
		Items []map[string]interface{} `json:"items"`
	}
	_ = json.Unmarshal(body, &resp)
	for _, r := range resp.Items {
		if str(r["id"]) == id || str(r["name"]) == id {
			if gf.jsonOut {
				emitJSON(r)
				return
			}
			tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
			fmt.Fprintf(tw, "Name:\t%s\n", str(r["name"]))
			fmt.Fprintf(tw, "Caller:\t%s\n", str(r["match_subject"]))
			fmt.Fprintf(tw, "Target:\t%s/%s\n", str(r["target_namespace"]), str(r["target_server_name"]))
			fmt.Fprintf(tw, "Model:\t%s\n", str(r["target_model"]))
			fmt.Fprintf(tw, "Enabled:\t%v\n", r["enabled"])
			fmt.Fprintf(tw, "Description:\t%s\n", str(r["description"]))
			fmt.Fprintf(tw, "Created by:\t%s\n", str(r["created_by"]))
			fmt.Fprintf(tw, "Created:\t%s\n", str(r["created_at"]))
			fmt.Fprintf(tw, "Id:\t%s\n", str(r["id"]))
			tw.Flush()
			return
		}
	}
	fmt.Fprintf(os.Stderr, "No routing policy matching %q\n", id)
	os.Exit(1)
}

func cmdRoutingCreate(gf globalFlags, args []string) {
	var subject, target, name, desc string
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--caller", "--subject":
			if i+1 < len(args) {
				subject = args[i+1]
				i++
			}
		case "--target", "--server-id":
			if i+1 < len(args) {
				target = args[i+1]
				i++
			}
		case "--name":
			if i+1 < len(args) {
				name = args[i+1]
				i++
			}
		case "--description", "--desc":
			if i+1 < len(args) {
				desc = args[i+1]
				i++
			}
		}
	}
	if subject == "" || target == "" {
		fmt.Println("Usage: mcpctl routing create --caller <subject> --target <server-id> [--name <n>] [--description <why>]")
		fmt.Println()
		fmt.Println("  --target takes a model server ID. Find one with: mcpctl servers")
		os.Exit(1)
	}
	if name == "" {
		name = subject + " route"
	}
	payload := map[string]interface{}{
		"name":             name,
		"match_subject":    subject,
		"target_server_id": target,
	}
	// ⚠ Omit rather than send "" — the description is the only place the REASON
	// for a route is recorded, and an empty string is not the same as absent.
	if desc != "" {
		payload["description"] = desc
	}
	if _, err := apiPost(gf, "/llm/routing-policies", payload); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	fmt.Printf("Routing policy created: %s -> %s\n", subject, target)
	if desc == "" {
		fmt.Println("  (no description — consider --description, it is the only record of why)")
	}
}

func cmdRoutingDelete(gf globalFlags, id string) {
	// ⚠ Deleting a route is a TRAFFIC change, not a tidy-up: the caller it
	// served has no route afterwards and its next unnamed call is refused.
	if err := apiDelete(gf, "/llm/routing-policies/"+id); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	fmt.Printf("Routing policy %s deleted.\n", id)
	fmt.Println("  That caller now has no model route; its next unnamed call will be refused.")
}

// ─── Cost & chargeback ───────────────────────────────────────────────────────
//
// Pro tier (COST_MANAGEMENT). Reads rated_costs — the rating engine's output,
// not a recompute — so a CLI figure and the dashboard figure are the same
// number by construction rather than by coincidence.

func cmdCost(gf globalFlags, args []string) {
	if len(args) == 0 {
		fmt.Println("Usage: mcpctl cost report [--view <v>] [--from YYYY-MM-01] [--to YYYY-MM-01] [--server-id <id>]")
		fmt.Println()
		fmt.Println("  --view  full | vendor | department | user | service_account   (default: department)")
		fmt.Println("  --from  inclusive month start   (default: current month)")
		fmt.Println("  --to    EXCLUSIVE month start   (default: next month)")
		os.Exit(1)
	}
	switch args[0] {
	case "report", "rpt":
		cmdCostReport(gf, args[1:])
	default:
		fmt.Printf("Unknown cost subcommand: %s\n", args[0])
		os.Exit(1)
	}
}

func cmdCostReport(gf globalFlags, args []string) {
	view, from, to, serverID := "", "", "", ""
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--view":
			if i+1 < len(args) {
				view = args[i+1]
				i++
			}
		case "--from":
			if i+1 < len(args) {
				from = args[i+1]
				i++
			}
		case "--to":
			if i+1 < len(args) {
				to = args[i+1]
				i++
			}
		case "--server-id", "--vendor":
			if i+1 < len(args) {
				serverID = args[i+1]
				i++
			}
		}
	}

	// ⚠ THE HALF-OPEN RANGE TRAP. --from 2026-08-01 --to 2026-08-01 is an EMPTY
	// interval and returns zero — which reads as "we spent nothing" rather than
	// "you asked for nothing". Catch it here; the server cannot tell the
	// difference between a deliberate empty range and a mistake.
	if from != "" && to != "" && from == to {
		fmt.Fprintf(os.Stderr,
			"Error: --from and --to are identical (%s), which is an EMPTY range.\n"+
				"  The range is half-open: --to is EXCLUSIVE.\n"+
				"  For the month of %s, use --to %s\n",
			from, from[:7], nextMonth(from))
		os.Exit(1)
	}

	q := []string{}
	if view != "" {
		q = append(q, "view="+view)
	}
	if from != "" {
		q = append(q, "from="+from)
	}
	if to != "" {
		q = append(q, "to="+to)
	}
	if serverID != "" {
		q = append(q, "server_id="+serverID)
	}
	path := "/billing/report"
	if len(q) > 0 {
		path += "?" + strings.Join(q, "&")
	}

	body, err := apiGet(gf, path)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}

	var resp map[string]interface{}
	if err := json.Unmarshal(body, &resp); err != nil {
		fmt.Println(string(body))
		return
	}
	// ⚠ Echo the bounds the SERVER used, not the ones asked for. It defaults
	// when they are omitted, and a statement whose period is implicit is a
	// statement somebody will misread.
	// ⚠ period is a nested object, not two top-level keys.
	if pd, ok := resp["period"].(map[string]interface{}); ok {
		fmt.Printf("Period: %v to %v (exclusive)\n\n", pd["from"], pd["to"])
	}
	rows, _ := resp["rows"].([]interface{})
	if len(rows) == 0 {
		fmt.Println("No rated costs in this period.")
		fmt.Println("  Costs are rated periodically — very recent usage may not have settled yet.")
		return
	}
	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	printed := false
	for _, r := range rows {
		m, ok := r.(map[string]interface{})
		if !ok {
			continue
		}
		if !printed {
			fmt.Fprintln(tw, "SUBJECT\tSERVER\tCOST CENTRE\tCOST (USD)")
			printed = true
		}
		label := str(m["dept_name"])
		for _, k := range []string{"caller_display", "server_name", "namespace"} {
			if label == "" {
				label = str(m[k])
			}
		}
		if label == "" {
			label = "(unattributed)"
		}
		// ⚠ cost_usd, not amount_usd. And cost_center is worth showing — it is
		// the code a finance system actually keys on.
		// ⚠ The department view returns one row PER SERVER per department, so
		// a name repeats legitimately. Without the server column it reads as
		// duplicate rows rather than a breakdown.
		srv := str(m["server_name"])
		if srv == "" {
			srv = "—"
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\t%v\n", label, srv, str(m["cost_center"]), m["cost_usd"])
	}
	tw.Flush()
	// ⚠ The API returns no total — sum the rows rather than leaving the reader
	// to add up a chargeback statement by hand.
	var total float64
	for _, r := range rows {
		if m, ok := r.(map[string]interface{}); ok {
			if v, ok := m["cost_usd"].(float64); ok {
				total += v
			}
		}
	}
	fmt.Printf("\nTotal: %.2f USD\n", total)
}

// nextMonth turns YYYY-MM-01 into the following YYYY-MM-01, for the half-open
// hint above.
func nextMonth(p string) string {
	if len(p) < 7 {
		return p
	}
	var y, m int
	if _, err := fmt.Sscanf(p[:7], "%d-%d", &y, &m); err != nil {
		return p
	}
	if m == 12 {
		y, m = y+1, 1
	} else {
		m++
	}
	return fmt.Sprintf("%04d-%02d-01", y, m)
}

// ─── Departments ─────────────────────────────────────────────────────────────
//
// ⚠ Departments are what make a chargeback report readable. Without them every
// figure lands in "(unattributed)" — which is a working report that tells
// nobody anything.

func cmdDepartments(gf globalFlags, args []string) {
	if len(args) == 0 {
		fmt.Println("Usage: mcpctl departments <list|get|create|members> [args]")
		os.Exit(1)
	}
	sub := args[0]
	rest := args[1:]
	switch sub {
	case "list", "ls":
		cmdDepartmentsList(gf)
	case "get":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl departments get <id>")
			os.Exit(1)
		}
		cmdDepartmentsGet(gf, rest[0])
	case "create":
		cmdDepartmentsCreate(gf, rest)
	case "members":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl departments members <id>")
			os.Exit(1)
		}
		cmdDepartmentsMembers(gf, rest[0])
	default:
		fmt.Printf("Unknown departments subcommand: %s\n", sub)
		os.Exit(1)
	}
}

func cmdDepartmentsList(gf globalFlags) {
	body, err := apiGet(gf, "/departments")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	// ⚠ The key is "departments", not "items" — the guess would have printed
	// "No departments defined" while five existed.
	var resp struct {
		Departments []map[string]interface{} `json:"departments"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		fmt.Println(string(body))
		return
	}
	if len(resp.Departments) == 0 {
		fmt.Println("No departments defined.")
		fmt.Println("  Every cost in a chargeback report will show as (unattributed) until")
		fmt.Println("  departments exist and callers are assigned to them.")
		return
	}
	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "NAME\tNAMESPACE\tCOST CENTRE\tMEMBERS")
	for _, d := range resp.Departments {
		cc := str(d["cost_center"])
		if cc == "" {
			cc = "—"
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\t%v\n",
			str(d["name"]), str(d["namespace"]), cc, d["member_count"])
	}
	tw.Flush()
}

func cmdDepartmentsGet(gf globalFlags, id string) {
	body, err := apiGet(gf, "/departments/"+id)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	fmt.Println(string(body))
}

func cmdDepartmentsMembers(gf globalFlags, id string) {
	body, err := apiGet(gf, "/departments/"+id+"/members")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	var resp struct {
		Items []map[string]interface{} `json:"items"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		fmt.Println(string(body))
		return
	}
	if len(resp.Items) == 0 {
		fmt.Println("No members. Costs for this department will be empty.")
		return
	}
	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "SUBJECT\tFROM\tTO")
	for _, m := range resp.Items {
		to := str(m["effective_to"])
		if to == "" {
			to = "(current)"
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\n", str(m["subject"]), str(m["effective_from"]), to)
	}
	tw.Flush()
}

func cmdDepartmentsCreate(gf globalFlags, args []string) {
	var name, costCenter string
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--name":
			if i+1 < len(args) {
				name = args[i+1]
				i++
			}
		case "--cost-center", "--cost-centre":
			if i+1 < len(args) {
				costCenter = args[i+1]
				i++
			}
		}
	}
	if name == "" {
		fmt.Println("Usage: mcpctl departments create --name <name> [--cost-center <cc>]")
		os.Exit(1)
	}
	payload := map[string]interface{}{"name": name}
	if costCenter != "" {
		payload["cost_center"] = costCenter
	}
	if _, err := apiPost(gf, "/departments", payload); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	fmt.Printf("Department created: %s\n", name)
}

// cmdLoginToken stores a pre-minted service-account token, after proving it works.
//
// ⚠ VALIDATE BEFORE SAVING. Writing an unverified token reproduces the exact
// failure this change follows: a config file that looks like a session, and
// every later command returning 401 with nothing to explain it. One call to a
// cheap authenticated endpoint settles it.
func cmdLoginToken(gf globalFlags, server, token string) {
	if strings.TrimSpace(token) == "" {
		fmt.Fprintln(os.Stderr, "Error: empty token")
		os.Exit(1)
	}

	probe := Config{
		Server:   server,
		Token:    token,
		Insecure: gf.insecure,
		CACert:   gf.caCert,
	}
	gfProbe := gf
	gfProbe.cfgOverride = &probe

	body, err := apiGet(gfProbe, "/servers")
	if err != nil {
		fmt.Fprintf(os.Stderr, "Token rejected: %v\n\n", err)
		fmt.Fprintln(os.Stderr, "  Nothing has been saved. Check that:")
		fmt.Fprintln(os.Stderr, "    - the token was copied whole (they are long, and truncation is silent)")
		fmt.Fprintln(os.Stderr, "    - the service account has not been revoked or expired")
		fmt.Fprintln(os.Stderr, "    - the server URL is the one that issued it")
		os.Exit(1)
	}

	if err := saveConfig(probe); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}

	subject := ""
	var me map[string]interface{}
	if json.Unmarshal(body, &me) == nil {
		subject = str(me["subject"])
		if subject == "" {
			subject = str(me["username"])
		}
	}
	if subject == "" {
		subject = "service account"
	}
	fmt.Printf("✓ Authenticated as %s\n", subject)
	fmt.Printf("  Server: %s\n", server)
	fmt.Printf("  Config: %s\n", configPath())
	if gf.insecure {
		fmt.Println("  TLS:    insecure (skipping cert verification)")
	}
	// ⚠ Say when it dies. A token that silently expires mid-pipeline is a CI
	// failure nobody can read; knowing the date turns it into a calendar entry.
	if exp := jwtExpiry(token); exp != "" {
		fmt.Printf("  Expires: %s\n", exp)
	}
}

// jwtExpiry reads `exp` from a JWT payload without verifying the signature —
// ⚠ display only. The server is the only thing that decides whether a token is
// valid; this is a courtesy, not a check.
func jwtExpiry(token string) string {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return ""
	}
	seg := parts[1]
	if m := len(seg) % 4; m != 0 {
		seg += strings.Repeat("=", 4-m)
	}
	raw, err := base64.URLEncoding.DecodeString(seg)
	if err != nil {
		return ""
	}
	var claims map[string]interface{}
	if json.Unmarshal(raw, &claims) != nil {
		return ""
	}
	exp, ok := claims["exp"].(float64)
	if !ok {
		return ""
	}
	return time.Unix(int64(exp), 0).UTC().Format("2006-01-02 15:04 UTC")
}

// ─── Inventory / tool-definition drift ───────────────────────────────────────
//
// ⚠ Two statuses that look similar and mean different things:
//
//   pending  — first sighting of a tool. Nobody has approved it yet. Expected
//              when a server is deployed; it is new surface, not a betrayal.
//   drifted  — an APPROVED tool whose definition hash changed. This is the
//              rug-pull: something you vetted is no longer what you vetted.
//
// The default list returns both, because both need a human. But a CI gate that
// fails on `pending` fails every time anyone deploys anything, and a gate that
// cries wolf gets removed. So --fail-on-drift means drifted only.

func cmdInventory(gf globalFlags, args []string) {
	if len(args) == 0 {
		fmt.Println("Usage: mcpctl inventory <list|probe|approve|reject|quarantine> [args]")
		fmt.Println()
		fmt.Println("  list       tools awaiting review (pending + drifted by default)")
		fmt.Println("  probe      force a drift probe for one server, now")
		fmt.Println("  approve    accept the observed definition as the new baseline")
		fmt.Println("  reject     refuse the observed definition")
		fmt.Println("  quarantine kill switch — block the tool outright")
		os.Exit(1)
	}
	sub := args[0]
	rest := args[1:]

	switch sub {
	case "list", "ls":
		cmdInventoryList(gf, rest)
	case "probe":
		if len(rest) < 1 {
			fmt.Println("Usage: mcpctl inventory probe <namespace>/<server>")
			os.Exit(1)
		}
		cmdInventoryProbe(gf, rest[0])
	case "approve":
		cmdInventoryAction(gf, rest, "approve",
			"accepted as the new baseline")
	case "reject":
		cmdInventoryAction(gf, rest, "reject",
			"rejected")
	case "quarantine":
		cmdInventoryAction(gf, rest, "quarantine",
			"QUARANTINED — the tool is blocked estate-wide")
	default:
		fmt.Printf("Unknown inventory subcommand: %s\n", sub)
		os.Exit(1)
	}
}

func cmdInventoryList(gf globalFlags, args []string) {
	status := ""
	failOnDrift, failOnPending := false, false
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--status":
			if i+1 < len(args) {
				status = args[i+1]
				i++
			}
		case "--drifted":
			status = "drifted"
		case "--fail-on-drift":
			failOnDrift = true
		case "--fail-on-pending":
			failOnPending = true
		}
	}

	path := "/inventory/tools"
	if status != "" {
		path += "?status=" + status
	}
	body, err := apiGet(gf, path)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		// ⚠ Still honour the exit code in --json mode: a pipeline that parses
		// the output should not also have to interpret it.
		exitOnDrift(body, failOnDrift, failOnPending)
		return
	}

	var resp struct {
		Items []map[string]interface{} `json:"items"`
		Tools []map[string]interface{} `json:"tools"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		fmt.Println(string(body))
		return
	}
	rows := resp.Items
	if len(rows) == 0 {
		rows = resp.Tools
	}
	if len(rows) == 0 {
		if status == "" {
			fmt.Println("Nothing awaiting review — no pending or drifted tool definitions.")
		} else {
			fmt.Printf("No tool definitions with status %q.\n", status)
		}
		return
	}

	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "STATUS\tSERVER\tTOOL\tID")
	for _, t := range rows {
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\n",
			str(t["status"]), str(t["server_name"]), str(t["tool_name"]), str(t["id"]))
	}
	tw.Flush()

	// ⚠ Say what the operator should DO. A list of drifted tools with no next
	// step is a list somebody looks at twice and then stops looking at.
	fmt.Println()
	fmt.Println("  approve <id>     accept the observed definition as the new baseline")
	fmt.Println("  reject <id>      refuse it")
	fmt.Println("  quarantine <id>  block the tool outright")

	exitOnDrift(body, failOnDrift, failOnPending)
}

// exitOnDrift is the CI gate.
//
// ⚠ DRIFTED ONLY BY DEFAULT. `pending` fires whenever anyone deploys a server
// carrying a tool nobody has approved yet — which is routine. A gate that fails
// on routine events gets disabled, and then it catches nothing. Failing on
// `drifted` alone keeps the signal worth acting on.
func exitOnDrift(body []byte, failOnDrift, failOnPending bool) {
	if !failOnDrift && !failOnPending {
		return
	}
	var resp struct {
		Items []map[string]interface{} `json:"items"`
		Tools []map[string]interface{} `json:"tools"`
	}
	if json.Unmarshal(body, &resp) != nil {
		return
	}
	rows := resp.Items
	if len(rows) == 0 {
		rows = resp.Tools
	}
	drifted, pending := 0, 0
	for _, t := range rows {
		switch str(t["status"]) {
		case "drifted":
			drifted++
		case "pending":
			pending++
		}
	}
	fail := (failOnDrift && drifted > 0) || (failOnPending && pending > 0)
	if !fail {
		return
	}
	fmt.Fprintln(os.Stderr)
	if drifted > 0 && failOnDrift {
		fmt.Fprintf(os.Stderr, "FAIL: %d approved tool definition(s) have DRIFTED.\n", drifted)
		fmt.Fprintln(os.Stderr, "  A tool you vetted is no longer what you vetted.")
	}
	if pending > 0 && failOnPending {
		fmt.Fprintf(os.Stderr, "FAIL: %d tool definition(s) are PENDING review.\n", pending)
	}
	os.Exit(2)
}

func cmdInventoryProbe(gf globalFlags, target string) {
	parts := strings.SplitN(target, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		fmt.Fprintln(os.Stderr, "Error: expected <namespace>/<server>, e.g. mcp-prod/exa-search")
		os.Exit(1)
	}
	// ⚠ Probing is a WRITE in effect — it reaches out to the upstream server and
	// records what it finds, which can flip a tool to `drifted`.
	body, err := apiPost(gf, "/drift/servers/"+parts[0]+"/"+parts[1]+"/probe-now", map[string]interface{}{})
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	if gf.jsonOut {
		var raw interface{}
		_ = json.Unmarshal(body, &raw)
		emitJSON(raw)
		return
	}
	fmt.Printf("Probe requested for %s.\n", target)
	fmt.Println("  Results land asynchronously — re-run `mcpctl inventory list` to see them.")
}

func cmdInventoryAction(gf globalFlags, args []string, action, past string) {
	if len(args) < 1 {
		fmt.Printf("Usage: mcpctl inventory %s <tool-id>\n", action)
		fmt.Println("  Find ids with: mcpctl inventory list")
		os.Exit(1)
	}
	id := args[0]
	if _, err := apiPost(gf, "/inventory/tools/"+id+"/"+action, map[string]interface{}{}); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	fmt.Printf("Tool %s %s.\n", id, past)
	if action == "quarantine" {
		// ⚠ Quarantine is estate-wide and immediate. Say so — it is the one
		// action here that changes live traffic.
		fmt.Println("  Every caller reaching this tool is now refused, in every namespace.")
	}
}
