# mcpctl

The official command-line interface for the Magertron MCP Orchestrator.

`mcpctl` is the client; the orchestrator is the server. The CLI talks to a
running orchestrator over HTTPS to deploy MCP servers, manage users and service
accounts, run governance evaluations, and inspect cluster state.

If you don't have a Magertron orchestrator deployed yet, see the [platform
install guide](https://magertron.com#install) — `./install.sh` deploys to a
Kubernetes cluster in about 5 minutes.

## Install

Pick the path that matches your platform. All paths install the same binary
(`mcpctl`) — the version is determined by which release is current.

### Homebrew (macOS or Linux)

```bash
brew install magertron/tap/mcpctl
```

### curl one-liner (macOS or Linux)

```bash
curl -fsSL https://magertron.com/install-mcpctl.sh | sh
```

Pinning a version:

```bash
curl -fsSL https://magertron.com/install-mcpctl.sh | MCPCTL_VERSION=v2.0.1 sh
```

### apt (Debian or Ubuntu)

One-time repo setup:

```bash
curl -fsSL https://magertron.com/apt/magertron-archive-keyring.gpg \
    | sudo tee /etc/apt/trusted.gpg.d/magertron-archive-keyring.gpg > /dev/null

echo "deb [signed-by=/etc/apt/trusted.gpg.d/magertron-archive-keyring.gpg] https://magertron.com/apt stable main" \
    | sudo tee /etc/apt/sources.list.d/magertron.list

sudo apt update
```

Then install (and upgrade in future):

```bash
sudo apt install mcpctl
sudo apt upgrade mcpctl
```

### dnf / yum (RHEL, Fedora, Rocky, AlmaLinux)

One-time repo setup:

```bash
sudo curl -fsSL https://magertron.com/yum/magertron.repo \
    -o /etc/yum.repos.d/magertron.repo
```

Then install:

```bash
sudo dnf install mcpctl
```

### Direct binary download

Pre-built binaries for the four supported platforms are attached to every
[GitHub release](https://github.com/magertron/orchestrator/releases/latest):

- `mcpctl-darwin-arm64` — macOS Apple Silicon
- `mcpctl-darwin-amd64` — macOS Intel
- `mcpctl-linux-amd64` — Linux x86_64
- `mcpctl-linux-arm64` — Linux ARM64

Each release also publishes a `SHA256SUMS` file for integrity checking.

## First-time setup

After installing, point `mcpctl` at your Magertron orchestrator and log in:

```bash
mcpctl login https://<your-orchestrator-host>:30443 admin
```

If you used `./install.sh` to deploy, the URL is printed in the post-install
summary. The bootstrap password is in a Kubernetes secret — retrieve it with:

```bash
kubectl get secret -n mcp-system mcp-orchestrator-secrets \
    -o jsonpath='{.data.MCP_SEED_ADMIN_PASSWORD}' | base64 -d
```

Change the password from the UI on first login.

## Commands

Run `mcpctl --help` for the full list. Common commands:

```
Authentication
  mcpctl login <url> <user>          Log in and cache credentials
  mcpctl logout                       Forget cached credentials
  mcpctl status                       Show current login state

Servers
  mcpctl servers                      List deployed MCP servers
  mcpctl deploy <name> <ns> <image>   Deploy a new MCP server
  mcpctl undeploy <name> <ns>         Remove an MCP server
  mcpctl scale <name> <ns> <count>    Scale replicas
  mcpctl restart <name> <ns>          Restart a server
  mcpctl logs <name> <ns>             Stream logs
  mcpctl history                      Deployment history

Identity
  mcpctl users list                   List users
  mcpctl users update-email <user> <email>
  mcpctl orgs list                    List organizations
  mcpctl service-accounts (sa) ...    Service account lifecycle

Governance & audit
  mcpctl governance list
  mcpctl governance evaluate <spec>
  mcpctl governance export
  mcpctl audit                        Recent audit events

Other
  mcpctl url                          Print orchestrator URL
  mcpctl version                      Show CLI version
```

## Configuration

`mcpctl` stores its config at `~/.mcpctl/config.yaml`. The file is created on
first `login` and contains the orchestrator URL and a cached auth token. Delete
it to start fresh.

## License

Apache 2.0. See [LICENSE](LICENSE). The CLI is open source and free for all
users, including customers on the Free tier of the orchestrator. Commands that
hit Pro or Enterprise features will return a clear license error unless your
orchestrator is licensed for them.

## Source + contributing

This repository contains the full mcpctl source. It's a single-file Go program
(`main.go`) plus build tooling. To build from source:

```bash
cd mcpctl
make build       # produces ./mcpctl
make install     # installs to /usr/local/bin/mcpctl
make dist        # cross-compiles for all four platforms
make packages    # builds .deb and .rpm packages
```

The version is defined as a const in `main.go` (single source of truth) — no
build flags needed.
