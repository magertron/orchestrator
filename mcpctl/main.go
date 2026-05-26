// mcpctl — MCP Orchestrator CLI
//
// Talks to the orchestrator's REST API at /api/v1/* over the unified
// TLS+ext_authz gateway (port 30443 by default in v1.6.0+).
//
// Authentication: POST /api/v1/auth/login returns a JWT; we save it in
// ~/.mcpctl.json and send it as Bearer on every subsequent request.
package main

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
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

const version = "2.0.4"

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

func loadConfig() Config {
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
func apiRequest(gf globalFlags, method, path string, body interface{}) ([]byte, int, error) {
	cfg := loadConfig()
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

	return respBody, resp.StatusCode, nil
}

// extractErrorMessage tries to pull a meaningful error message out of an
// error response body. Orchestrator returns either {"error": "..."} or
// occasionally {"detail": "..."}; fall through to the raw body if neither.
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
		return nil, fmt.Errorf("HTTP %d: %s", status, extractErrorMessage(body))
	}
	return body, nil
}

func apiPost(gf globalFlags, path string, in interface{}) ([]byte, error) {
	body, status, err := apiRequest(gf, "POST", path, in)
	if err != nil {
		return nil, err
	}
	if status >= 400 {
		return nil, fmt.Errorf("HTTP %d: %s", status, extractErrorMessage(body))
	}
	return body, nil
}

func apiDelete(gf globalFlags, path string) error {
	body, status, err := apiRequest(gf, "DELETE", path, nil)
	if err != nil {
		return err
	}
	if status >= 400 {
		return fmt.Errorf("HTTP %d: %s", status, extractErrorMessage(body))
	}
	return nil
}

// ─── Commands ────────────────────────────────────────────────────────────────

func cmdLogin(gf globalFlags, args []string) {
	if len(args) < 3 {
		fmt.Println("Usage: mcpctl login <server-url> <username> <password>")
		fmt.Println("Example: mcpctl login https://localhost:30443 admin admin --insecure")
		fmt.Println()
		fmt.Println("Notes:")
		fmt.Println("  Default Magertron gateway is HTTPS on port 30443.")
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

	token, _ := result["token"].(string)
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

	roles, _ := result["roles"].([]interface{})
	roleStrs := make([]string, len(roles))
	for i, r := range roles {
		roleStrs[i], _ = r.(string)
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
		fmt.Println("                                  oauth2-client-credentials, mtls")
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
	validAuthTypes := map[string]bool{
		"none":                      true,
		"bearer":                    true,
		"api-key":                   true,
		"oauth2-client-credentials": true,
		"mtls":                      true,
	}
	if !validAuthTypes[authType] {
		fmt.Fprintf(os.Stderr,
			"Error: --auth-type must be one of: none, bearer, api-key, oauth2-client-credentials, mtls (got '%s')\n",
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
  login <url> <user> <pass>    Login to orchestrator (e.g. https://localhost:30443)
  logout                       Clear saved credentials
  status                       Show connection status

SERVERS:
  servers                              List all deployed servers
  deploy <name> <ns> <image> [opts]    Deploy a new MCP server (--wait to block on Running)
  register-external [opts]             Register an external (vendor-hosted) MCP server proxy
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
  mcpctl login https://localhost:30443 admin admin --insecure
  mcpctl deploy fast-time mcp-prod ghcr.io/ibm/fast-time-server \
    --upstream-path /http --port 8080 --wait
  mcpctl servers --json | jq '.[] | select(.state=="Running")'

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
  Default Magertron gateway is HTTPS on port 30443 (TLS+ext_authz unified
  in v1.6.0). Plain HTTP listeners on 8080/30080 were removed in that
  release. Use --insecure for self-signed certs typical of fresh helm
  installs, or --ca-cert to pin the CA bundle.

  mcpctl v2.x targets Magertron platform v2.x (sync-at-majors).
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
		return nil, fmt.Errorf("HTTP %d: %s", status, extractErrorMessage(body))
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
			Name             string  `json:"name"`
			RowCount         int64   `json:"row_count"`
			SizeBytes        int64   `json:"size_bytes"`
			OldestRowUnixSec *int64  `json:"oldest_row_unix_sec"`
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
	case "register-external", "register":
		cmdRegisterExternal(gf, cmdArgs)
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
