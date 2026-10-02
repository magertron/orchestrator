#!/usr/bin/env bash
#
# bootstrap.sh — Magertron on a bare Ubuntu box, in one command.
#
#   curl -fsSL https://magertron.com/bootstrap.sh | sudo bash -s -- \
#     --api-public-url https://mcp.acme.com
#
# Installs git, helm and k3s, then hands off to install.sh. Every argument is
# forwarded, so anything install.sh accepts works here.
#
# WHAT THIS IS FOR. install.sh assumes a cluster. This assumes a machine — the
# state a Google Compute VM or a fresh Hetzner box arrives in, where kubectl is
# not a command and helm is not a command and nothing is running.
#
# Every step below is one that bit somebody on a real install:
#
#   * git and helm are absent from a minimal Ubuntu image. install.sh stops on
#     each in turn, one apt install at a time.
#   * k3s writes its kubeconfig root-only, so the first kubectl fails with a
#     permission error that looks like a broken install. Fixed at install time
#     with --write-kubeconfig-mode rather than a chmod that a k3s restart undoes.
#   * k3s bundles Traefik, which takes 443. Nothing routes through it to
#     Magertron, so https://<node-ip> answers with Traefik's own 404 and the
#     dashboard appears to be missing. Disabled at install time; removing it
#     afterwards means stopping k3s, editing config, and deleting three
#     resources.
#
# TESTED ON UBUNTU 24.04 ONLY. It refuses to run anywhere else rather than
# discovering the difference halfway through — see the preflight.

set -euo pipefail

MIN_CORES=8
MIN_MEM_GB=14          # a "16 GB" box reports ~15.6
REPO="https://github.com/magertron/orchestrator.git"
CLONE_DIR="${CLONE_DIR:-/opt/magertron}"

# ── Output ───────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  B=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RST=$'\e[0m'
else
  B=""; DIM=""; RED=""; GRN=""; YEL=""; RST=""
fi
step()  { printf '\n%s──%s %s\n' "$B" "$RST" "$1"; }
ok()    { printf '%s  ✓%s %s\n' "$GRN" "$RST" "$1"; }
warn()  { printf '%s  ⚠%s %s\n' "$YEL" "$RST" "$1"; }
die()   { printf '\n%s  ✗ %s%s\n\n' "$RED" "$1" "$RST" >&2; exit 1; }
note()  { printf '%s    %s%s\n' "$DIM" "$1" "$RST"; }

# ── Preflight ────────────────────────────────────────────────────────────────
# Refusing early with a clear reason beats failing at step four with an apt
# error. Everything here is cheap to check and expensive to discover late.
step "Checking this machine"

[ "$(id -u)" -eq 0 ] || die "Run with sudo — installing k3s and packages needs root."

# Ubuntu 24 is what this has been tested on. Others may well work; they have
# not been tried, and saying so is more use than a confident failure later.
if [ -r /etc/os-release ]; then
  . /etc/os-release
  if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "24.04" ]; then
    warn "Tested on Ubuntu 24.04; this is ${PRETTY_NAME:-unknown}."
    note "It may work. If it does not, install k3s and helm yourself and run"
    note "install.sh directly — that path has no distribution assumptions."
    if [ -t 0 ]; then
      read -r -p "    Continue anyway? [y/N] " a
      case "$a" in [yY]*) ;; *) die "Stopped." ;; esac
    else
      die "Refusing to guess on a non-Ubuntu-24.04 box in a non-interactive shell.
     Re-run interactively, or install the prerequisites yourself."
    fi
  else
    ok "Ubuntu 24.04"
  fi
else
  warn "No /etc/os-release — cannot identify this distribution."
fi

CORES="$(nproc)"
MEM_GB=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))

# ⚠ 4 cores is not enough, measured rather than guessed. The platform requests
# just under 4 cores before you deploy anything of your own — orchestrator,
# three Envoy replicas, two PostgreSQL instances, inventory, and the two bundled
# MCP servers. On a 4-core node the first server you deploy sits Pending on
# "Insufficient cpu", which reads like a broken install and is not.
if [ "$CORES" -lt "$MIN_CORES" ]; then
  warn "${CORES} cores. The platform reserves nearly 4 before you deploy anything."
  note "Expect your first server to sit Pending on Insufficient cpu."
  note "${MIN_CORES} cores is the practical floor."
  if [ -t 0 ]; then
    read -r -p "    Continue anyway? [y/N] " a
    case "$a" in [yY]*) ;; *) die "Stopped." ;; esac
  fi
else
  ok "${CORES} cores"
fi

if [ "$MEM_GB" -lt "$MIN_MEM_GB" ]; then
  warn "${MEM_GB} GB of memory. 16 GB is the tested floor."
else
  ok "${MEM_GB} GB memory"
fi

# ── Packages ─────────────────────────────────────────────────────────────────
step "Prerequisites"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
for pkg in git curl ca-certificates; do
  case "$pkg" in
    ca-certificates) have=$(dpkg -s "$pkg" >/dev/null 2>&1 && echo yes || echo no) ;;
    *)               have=$(command -v "$pkg" >/dev/null 2>&1 && echo yes || echo no) ;;
  esac
  if [ "$have" = yes ]; then
    ok "$pkg already present"
  else
    apt-get install -y -qq --reinstall "$pkg" >/dev/null
    command -v "$pkg" >/dev/null 2>&1 || [ "$pkg" = ca-certificates ] \
      || die "installed $pkg but the command is still missing"
    ok "installed $pkg"
  fi
done

if command -v helm >/dev/null 2>&1; then
  ok "helm already present ($(helm version --short 2>/dev/null || echo 'version unknown'))"
else
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash >/dev/null 2>&1 \
    || die "helm install failed. Install it yourself and re-run:
     https://helm.sh/docs/intro/install/"
  ok "installed helm ($(helm version --short 2>/dev/null || echo 'version unknown'))"
fi

if helm repo list 2>/dev/null | grep -q '^magertron[[:space:]]'; then
  ok "helm repo 'magertron' already configured"
else
  helm repo add magertron https://magertron.com/charts >/dev/null 2>&1 \
    || die "could not add the magertron helm repo — check network access to magertron.com"
  ok "added helm repo 'magertron'"
fi
helm repo update magertron >/dev/null 2>&1 || warn "helm repo update failed — using the cached index"

# ── k3s ──────────────────────────────────────────────────────────────────────
step "Kubernetes (k3s)"

if command -v k3s >/dev/null 2>&1 && systemctl is-active --quiet k3s; then
  ok "k3s already running — leaving it alone"
  # ⚠ NOT reconfigured. An existing cluster may be serving something, and
  # disabling its Traefik or rewriting its kubeconfig mode without being asked
  # would be a rude thing for an installer to do.
  if kubectl get svc -n kube-system traefik >/dev/null 2>&1; then
    warn "Traefik is running and holds ports 80 and 443 on every node (svclb)."
    note "Nothing routes through it to Magertron, so https://<node-ip> will"
    note "answer with Traefik's 404 rather than the dashboard, and a local"
    note "port-forward to :80 on any node is silently captured. Reach the UI"
    note "on :30444, or remove Traefik (on the server node), then re-run:"
    note "  echo 'disable: [traefik]' | sudo tee -a /etc/rancher/k3s/config.yaml"
    note "  sudo systemctl restart k3s      # k3s uninstalls Traefik + svclb itself"
  fi
else
  # ⚠ Both flags at INSTALL time, not afterwards.
  #
  # --write-kubeconfig-mode: k3s writes /etc/rancher/k3s/k3s.yaml root-only, and
  # its own kubectl reads that path regardless of what you copy elsewhere. A
  # chmod works until the next k3s restart rewrites the file.
  #
  # --disable traefik: k3s bundles Traefik and gives it 443. Removing it later
  # means stopping k3s, adding a line to config.yaml, restarting, and deleting a
  # HelmChart, a Deployment and a Service.
  curl -sfL https://get.k3s.io | \
    INSTALL_K3S_EXEC="--write-kubeconfig-mode 644 --disable traefik" sh - >/dev/null 2>&1 \
    || die "k3s install failed. See https://docs.k3s.io/quick-start"
  ok "installed k3s (Traefik disabled, kubeconfig world-readable)"
fi

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

printf '    waiting for the node'
for _ in $(seq 1 60); do
  if kubectl get nodes 2>/dev/null | grep -q ' Ready '; then
    printf '\n'; ok "node Ready"; break
  fi
  printf '.'; sleep 2
done
kubectl get nodes 2>/dev/null | grep -q ' Ready ' \
  || die "The node did not become Ready in two minutes.
     journalctl -u k3s -n 50   will say why."

# ── Magertron ────────────────────────────────────────────────────────────────
step "Magertron"

if [ -d "$CLONE_DIR/.git" ]; then
  git -C "$CLONE_DIR" pull --ff-only -q || warn "could not update $CLONE_DIR — using what is there"
  ok "updated $CLONE_DIR"
else
  mkdir -p "$(dirname "$CLONE_DIR")"
  git clone -q "$REPO" "$CLONE_DIR" || die "git clone failed — check network access to github.com"
  ok "cloned to $CLONE_DIR"
fi

cd "$CLONE_DIR"
[ -x install.sh ] || chmod +x install.sh

# Every argument goes straight through. This script deliberately knows nothing
# about install.sh's options — adding one there should not mean editing here.
#
# ⚠ install.sh PROMPTS for the public URL when it is not given and the shell is
# interactive. Piped from curl there is no stdin, so pass --api-public-url (or
# --non-interactive to make it fail loudly rather than hang).
step "Handing off to install.sh"
note "$# argument(s) forwarded"
echo
exec ./install.sh "$@"
