#!/bin/bash
# publish-repos.sh — regenerate APT + YUM hosted repos.
#
# Run after building new mcpctl .deb/.rpm packages. Copies them into the
# repo pools, prunes old versions (keeps last 5), regenerates the
# indices, and re-signs everything.
#
# Usage:
#   ./scripts/publish-repos.sh [options]
#
# Options:
#   --keep N            Number of versions to keep (default: 5)
#   --gpg-key <id>      GPG key ID to sign with (default: read from env or fail)
#   --no-sign           Skip signing (for testing only — repos won't work for customers!)
#
# Requires:
#   - dist/*.deb and dist/*.rpm already built (run `make packages` first)
#   - GPG key matching --gpg-key in your keyring
#   - apt-ftparchive, createrepo_c, gpg

set -euo pipefail

cd "$(dirname "$0")/.."

# ─── Defaults + arg parsing ──────────────────────────────────────────────────
KEEP="${KEEP:-5}"
GPG_KEY="${GPG_KEY:-7D435C1D166D3BAF}"   # Magertron Packages key
SIGN=1

while [ $# -gt 0 ]; do
    case "$1" in
        --keep)     KEEP="$2"; shift 2 ;;
        --gpg-key)  GPG_KEY="$2"; shift 2 ;;
        --no-sign)  SIGN=0; shift ;;
        -h|--help)
            grep -E "^#" "$0" | head -25 | sed 's|^# \?||'
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ─── Polish (mirror release-cli.sh style) ────────────────────────────────────
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RESET=$'\033[0m'  C_BOLD=$'\033[1m'  C_DIM=$'\033[2m'
    C_GREEN=$'\033[32m'  C_YELLOW=$'\033[33m'  C_RED=$'\033[31m'  C_CYAN=$'\033[36m'
else
    C_RESET="" C_BOLD="" C_DIM="" C_GREEN="" C_YELLOW="" C_RED="" C_CYAN=""
fi

section() { echo ""; printf "${C_CYAN}${C_BOLD}» %s${C_RESET}\n" "$1"; }
ok()      { printf "  ${C_GREEN}✓${C_RESET} %s\n" "$*"; }
warn()    { printf "  ${C_YELLOW}⚠${C_RESET} %s\n" "$*"; }
info()    { printf "  ${C_DIM}%s${C_RESET}\n" "$*"; }
fatal()   { printf "  ${C_RED}✗${C_RESET} %s\n" "$*" >&2; exit 1; }

# ─── Sanity checks ───────────────────────────────────────────────────────────
section "Sanity checks"

for tool in apt-ftparchive createrepo_c gpg gzip; do
    command -v "$tool" >/dev/null 2>&1 || fatal "Required tool not found: $tool"
done
ok "Tools present"

APT_ROOT="docs/apt"
YUM_ROOT="docs/yum"

[ -d "$APT_ROOT" ] || fatal "APT repo root not found: $APT_ROOT (run from orchestrator/)"
[ -d "$YUM_ROOT" ] || fatal "YUM repo root not found: $YUM_ROOT"
[ -d "mcpctl/dist" ] || fatal "mcpctl/dist not found (run 'make packages' first)"
ok "Directory layout valid"

if [ "$SIGN" = "1" ]; then
    if ! gpg --list-secret-keys "$GPG_KEY" >/dev/null 2>&1; then
        fatal "GPG key $GPG_KEY not found in keyring"
    fi
    ok "GPG key $GPG_KEY available"
else
    warn "Skipping signing (--no-sign) — repos will NOT be valid for customers"
fi

# ─── APT: add new packages, prune old ────────────────────────────────────────
section "APT: pool updates"

APT_POOL="$APT_ROOT/pool/main/m/mcpctl"
mkdir -p "$APT_POOL"

# Copy any new .deb files into the pool.
new_debs=0
for deb in mcpctl/dist/*.deb; do
    [ -f "$deb" ] || continue
    target="$APT_POOL/$(basename "$deb")"
    if [ -f "$target" ]; then
        # Same version already in pool — check if content matches (might be a rebuild).
        if cmp -s "$deb" "$target"; then
            info "Already present: $(basename "$deb")"
        else
            info "Updating: $(basename "$deb")"
            cp "$deb" "$target"
            new_debs=$((new_debs + 1))
        fi
    else
        cp "$deb" "$target"
        ok "Added: $(basename "$deb")"
        new_debs=$((new_debs + 1))
    fi
done

# Prune old versions in the APT pool.
# Filenames look like:  mcpctl_2.0.1_amd64.deb
# Extract the version, sort -V, keep top $KEEP, delete the rest.
versions_in_pool=$(ls "$APT_POOL" 2>/dev/null | grep -oE '_[0-9]+\.[0-9]+\.[0-9]+_' | tr -d '_' | sort -u -V)
total_versions=$(echo "$versions_in_pool" | wc -l)

if [ "$total_versions" -gt "$KEEP" ]; then
    to_delete=$(echo "$versions_in_pool" | head -n "-$KEEP")
    info "Pruning $total_versions → $KEEP versions"
    while IFS= read -r v; do
        [ -z "$v" ] && continue
        for f in "$APT_POOL"/mcpctl_${v}_*.deb; do
            [ -f "$f" ] && rm -v "$f" | sed 's|^|    |'
        done
    done <<< "$to_delete"
else
    info "$total_versions version(s) in pool (≤ $KEEP, no pruning needed)"
fi

ok "Pool contents:"
ls "$APT_POOL" | sed 's|^|    |'

# ─── APT: regenerate indices ─────────────────────────────────────────────────
section "APT: regenerate Packages indices"

for arch in amd64 arm64; do
    dir="$APT_ROOT/dists/stable/main/binary-${arch}"
    mkdir -p "$dir"
    apt-ftparchive --arch="$arch" packages "$APT_ROOT/pool/" > "$dir/Packages.tmp"

    # Rewrite Filename: paths to be repo-root-relative (apt-ftparchive uses
    # the path it was given, which would otherwise be "$APT_ROOT/pool/...").
    sed "s|^Filename: ${APT_ROOT}/|Filename: |" "$dir/Packages.tmp" > "$dir/Packages"
    rm "$dir/Packages.tmp"

    gzip -kf "$dir/Packages"
    pkg_count=$(grep -c '^Package: ' "$dir/Packages" || echo 0)
    ok "$arch: $pkg_count packages indexed"
done

# ─── APT: regenerate + sign Release ──────────────────────────────────────────
section "APT: regenerate + sign Release"

cat > /tmp/apt-release.conf <<EOF
APT::FTPArchive::Release::Origin "Magertron";
APT::FTPArchive::Release::Label "Magertron";
APT::FTPArchive::Release::Suite "stable";
APT::FTPArchive::Release::Codename "stable";
APT::FTPArchive::Release::Architectures "amd64 arm64";
APT::FTPArchive::Release::Components "main";
APT::FTPArchive::Release::Description "Magertron official APT repository";
EOF

# Delete existing Release files so the new manifest doesn't hash them.
rm -f "$APT_ROOT/dists/stable/Release" \
      "$APT_ROOT/dists/stable/Release.gpg" \
      "$APT_ROOT/dists/stable/InRelease"

apt-ftparchive -c /tmp/apt-release.conf release "$APT_ROOT/dists/stable/" \
    > "$APT_ROOT/dists/stable/Release"
ok "Release manifest written ($(wc -c < "$APT_ROOT/dists/stable/Release") bytes)"

if [ "$SIGN" = "1" ]; then
    gpg --batch --yes --default-key "$GPG_KEY" \
        --detach-sign --armor \
        --output "$APT_ROOT/dists/stable/Release.gpg" \
        "$APT_ROOT/dists/stable/Release" 2>/dev/null
    ok "Signed: Release.gpg"

    gpg --batch --yes --default-key "$GPG_KEY" \
        --clear-sign \
        --output "$APT_ROOT/dists/stable/InRelease" \
        "$APT_ROOT/dists/stable/Release" 2>/dev/null
    ok "Signed: InRelease"
else
    warn "Skipping APT signing (--no-sign)"
fi

# ─── YUM: add new packages, prune old ────────────────────────────────────────
section "YUM: pool updates"

YUM_PKGS="$YUM_ROOT/packages"
mkdir -p "$YUM_PKGS"

for rpm in mcpctl/dist/*.rpm; do
    [ -f "$rpm" ] || continue
    target="$YUM_PKGS/$(basename "$rpm")"
    if [ -f "$target" ]; then
        if cmp -s "$rpm" "$target"; then
            info "Already present: $(basename "$rpm")"
        else
            info "Updating: $(basename "$rpm")"
            cp "$rpm" "$target"
        fi
    else
        cp "$rpm" "$target"
        ok "Added: $(basename "$rpm")"
    fi
done

# Prune old .rpm versions.
# Filenames: mcpctl-2.0.1-1.x86_64.rpm
versions_in_yum=$(ls "$YUM_PKGS" 2>/dev/null | grep -oE '^mcpctl-[0-9]+\.[0-9]+\.[0-9]+-' | sed 's|^mcpctl-||; s|-$||' | sort -u -V)
total_yum=$(echo "$versions_in_yum" | wc -l)

if [ "$total_yum" -gt "$KEEP" ]; then
    to_delete=$(echo "$versions_in_yum" | head -n "-$KEEP")
    info "Pruning $total_yum → $KEEP versions"
    while IFS= read -r v; do
        [ -z "$v" ] && continue
        for f in "$YUM_PKGS"/mcpctl-${v}-*.rpm; do
            [ -f "$f" ] && rm -v "$f" | sed 's|^|    |'
        done
    done <<< "$to_delete"
else
    info "$total_yum version(s) in packages (≤ $KEEP, no pruning needed)"
fi

ok "Packages contents:"
ls "$YUM_PKGS" | sed 's|^|    |'

# ─── YUM: regenerate repodata + sign ─────────────────────────────────────────
section "YUM: regenerate repodata"

createrepo_c "$YUM_ROOT" 2>&1 | sed 's|^|    |'
ok "repodata regenerated"

if [ "$SIGN" = "1" ]; then
    gpg --batch --yes --default-key "$GPG_KEY" \
        --detach-sign --armor \
        --output "$YUM_ROOT/repodata/repomd.xml.asc" \
        "$YUM_ROOT/repodata/repomd.xml" 2>/dev/null
    ok "Signed: repomd.xml.asc"
else
    warn "Skipping YUM signing (--no-sign)"
fi

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
ok "${C_BOLD}Both repos regenerated.${C_RESET}"
info "Next: git add docs/apt docs/yum && git commit && git push"
