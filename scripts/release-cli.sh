#!/bin/bash
# release-cli.sh — End-to-end mcpctl release automation.
#
# Run from the orchestrator repo root.
#
# Usage:
#   ./scripts/release-cli.sh <version> [options]
#
# Examples:
#   ./scripts/release-cli.sh 2.0.2
#   ./scripts/release-cli.sh 2.0.2 --dry-run
#   ./scripts/release-cli.sh 2.0.2 --skip-tap     # don't update Homebrew Formula
#   ./scripts/release-cli.sh 2.0.2 --notes "Brief release notes go here"
#
# What this script does:
#   1. Verifies orchestrator + homebrew-tap working trees are clean
#   2. Bumps the version constant in mcpctl/main.go
#   3. Builds 4 platform binaries via `make dist`
#   4. Builds .deb + .rpm packages via `make packages`
#   5. Creates a GitHub release at github.com/magertron/orchestrator
#   6. Regenerates the Homebrew Formula with new SHA256s
#   7. Commits + pushes the orchestrator change (version bump)
#   8. Commits + pushes the homebrew-tap change (Formula update)
#   9. Verifies the release is reachable + Formula is queryable
#
# Safety:
#   - Refuses to run with uncommitted changes (use --force to override)
#   - Refuses to release a version lower than or equal to current
#   - --dry-run prints what would happen without making any changes
#
# Recovery:
#   If a step fails partway through, the script prints the failure clearly.
#   The earlier steps are non-destructive (working in dist/, no commits yet)
#   so you can fix and rerun. Once a GitHub release is created (step 5),
#   re-running the script will fail at "release already exists" — delete
#   the release manually if you need to redo it.

set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────────

ORCH_REPO="$(cd "$(dirname "$0")/.." && pwd)"
TAP_REPO="${TAP_REPO:-$ORCH_REPO/../homebrew-tap}"
MCPCTL_DIR="$ORCH_REPO/mcpctl"

# Defaults
DRY_RUN=0
SKIP_TAP=0
FORCE=0
NOTES=""
AUTO_DETECT=0   # Session 2.12: set by --auto-detect mode (bootstrap.sh hand-off)

# ─── Polish helpers (mirror install.sh's style) ──────────────────────────────

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RESET=$'\033[0m'  C_BOLD=$'\033[1m'  C_DIM=$'\033[2m'
    C_RED=$'\033[31m'   C_GREEN=$'\033[32m'  C_YELLOW=$'\033[33m'  C_CYAN=$'\033[36m'
else
    C_RESET="" C_BOLD="" C_DIM="" C_RED="" C_GREEN="" C_YELLOW="" C_CYAN=""
fi

case "${LANG:-}${LC_ALL:-}" in
    *UTF-8*|*utf8*|*UTF8*) G_CHECK="✓" G_CROSS="✗" G_WARN="⚠" G_ARROW="→" ;;
    *)                     G_CHECK="OK" G_CROSS="X" G_WARN="!" G_ARROW=">" ;;
esac

STEP=0
section() {
    STEP=$((STEP + 1))
    echo ""
    printf "${C_CYAN}${C_BOLD}[%d] %s${C_RESET}\n" "$STEP" "$1"
    printf "${C_DIM}%s${C_RESET}\n" "$(printf '%.0s─' $(seq 1 60))"
}
ok()   { printf "  ${C_GREEN}${G_CHECK}${C_RESET} %s\n" "$*"; }
warn() { printf "  ${C_YELLOW}${G_WARN}${C_RESET} %s\n" "$*"; }
err()  { printf "  ${C_RED}${G_CROSS}${C_RESET} %s\n" "$*" >&2; }
info() { printf "  ${C_DIM}%s${C_RESET}\n" "$*"; }
fatal() { err "$*"; exit 1; }

# ─── Argument parsing ────────────────────────────────────────────────────────

usage() {
    cat <<EOF
Usage: $(basename "$0") <version> [options]

Required:
  <version>           New version (e.g. 2.0.2). Must be higher than current.

Options:
  --dry-run           Show what would happen without making changes.
  --skip-tap          Don't update or push the Homebrew Formula.
  --force             Allow running with uncommitted changes (risky).
  --notes <text>      Release notes for the GitHub release.
                      Default: "Release v<version>"
  -h, --help          Show this help.

Environment:
  TAP_REPO            Path to homebrew-tap clone (default: ../homebrew-tap)
  GPG_KEY             GPG key ID to sign APT/YUM repos with
                      (default: 7D435C1D166D3BAF — Magertron Packages)
  NO_COLOR            Disable colored output.

Auto-detect mode (called from bootstrap.sh):
  --auto-detect       Inspect mcpctl/ + tags and decide whether to release.
                      Exit codes:
                        0  = nothing to do (caller continues)
                        1  = hard failure (uncommitted changes, etc.)
                        2  = release happened (caller sees this as success)
EOF
}

# ─── Auto-detect mode (Session 2.12) ─────────────────────────────────────────
#
# Called by bootstrap.sh in the mc-platform-private repo to keep the chart
# release and the mcpctl release in lockstep without forcing the operator
# to manually pick versions every time.
#
# Semantics:
#   (a) Uncommitted changes in orchestrator/mcpctl/ → HARD FAIL (exit 1)
#       Refusing to release stale code prevents shipping binaries that don't
#       match a tagged commit.
#   (b) Commits in mcpctl/ since highest v* tag, but version constant unchanged
#       → WARN + exit 0. Caller proceeds (chart-only release). Operator must
#       explicitly bump const version in main.go to opt in to releasing.
#   (c) Version constant in main.go > highest v* tag → PROCEED with release
#       using the version from main.go.
#   (d) Version constant matches highest tag, no commits since → exit 0
#       (nothing to do, caller continues).
#
# No --auto-detect flag = normal explicit-version mode (unchanged behavior).

detect_release_intent() {
    cd "$ORCH_REPO"

    local mcpctl_relpath="mcpctl"

    # (a) Uncommitted changes in mcpctl/?
    if ! git diff --quiet -- "$mcpctl_relpath" 2>/dev/null; then
        err "orchestrator/mcpctl has uncommitted changes at: $MCPCTL_DIR"
        info "Modified files:"
        git -C "$ORCH_REPO" status --short mcpctl/ 2>/dev/null | sed 's/^/    /'
        info ""
        info "Refusing to auto-release stale source. To fix:"
        info "    cd $ORCH_REPO"
        info "    git add mcpctl/<file>..."
        info "    git commit -m 'mcpctl <version>: <what changed>'"
        info "    git push"
        info ""
        info "Then re-run ./bootstrap.sh."
        return 1
    fi
    # Also check staged-but-not-committed.
    if ! git diff --cached --quiet -- "$mcpctl_relpath" 2>/dev/null; then
        err "orchestrator/mcpctl has staged-but-uncommitted changes at: $MCPCTL_DIR"
        info "Staged files:"
        git -C "$ORCH_REPO" status --short mcpctl/ 2>/dev/null | sed 's/^/    /'
        info ""
        info "Commit and push them:"
        info "    cd $ORCH_REPO"
        info "    git commit -m 'mcpctl <version>: <what changed>'"
        info "    git push"
        return 1
    fi

    # Unpushed commits would create a GitHub release that points at a SHA
    # github.com doesn't yet have — broken state. Refuse and tell the
    # operator to push first.
    #
    # We check the current branch's upstream. If no upstream is configured
    # (detached HEAD, branch never pushed) we conservatively bail — release
    # pipelines should run from a tracked branch.
    local upstream
    upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo "")
    if [ -z "$upstream" ]; then
        err "orchestrator repo has no upstream configured for the current branch."
        info "Refusing to release: GitHub release would point at a SHA that"
        info "github.com cannot resolve."
        info ""
        info "To fix:"
        info "    cd $ORCH_REPO"
        info "    git push -u origin \$(git symbolic-ref --short HEAD)"
        info ""
        info "Then re-run ./bootstrap.sh."
        return 1
    fi
    local unpushed
    unpushed=$(git log "$upstream..HEAD" --oneline 2>/dev/null | wc -l | tr -d ' ')
    if [ "$unpushed" != "0" ] && [ -n "$unpushed" ]; then
        err "orchestrator repo at $ORCH_REPO has $unpushed unpushed commit(s) on $(git symbolic-ref --short HEAD)."
        info "Unpushed commits:"
        git -C "$ORCH_REPO" log "$upstream..HEAD" --oneline 2>/dev/null | sed 's/^/    /'
        info ""
        info "Refusing to release: GitHub release would point at SHAs that"
        info "github.com hasn't seen yet."
        info ""
        info "To fix:"
        info "    cd $ORCH_REPO"
        info "    git push"
        info ""
        info "Then re-run ./bootstrap.sh."
        return 1
    fi

    # Current version from main.go.
    local current_version
    current_version=$(grep -E '^const version = "' "$MCPCTL_DIR/main.go" | sed -E 's/.*"(.+)".*/\1/')
    [ -z "$current_version" ] && { err "Could not parse 'const version' from main.go"; return 1; }

    # Highest v* tag (semver-sorted).
    local highest_tag
    highest_tag=$(git tag -l 'v*' | sed 's/^v//' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)
    [ -z "$highest_tag" ] && highest_tag="0.0.0"

    # (c) Version bumped explicitly?
    if [ "$current_version" != "$highest_tag" ]; then
        # Sanity: current must be GREATER than highest. A regression (current < highest)
        # is a sign of operator error and we refuse silently.
        local cmp_higher
        cmp_higher=$(printf '%s\n%s\n' "$current_version" "$highest_tag" | sort -V | tail -1)
        if [ "$cmp_higher" != "$current_version" ]; then
            err "main.go has version=$current_version but tag v$highest_tag is higher."
            info "Refusing to release backwards. Fix main.go or delete the bad tag."
            return 1
        fi
        info "Auto-detect: version bumped ($highest_tag → $current_version) — releasing."
        VERSION="$current_version"
        return 2
    fi

    # Versions match. Are there commits in mcpctl/ since the tag?
    local commits_since
    commits_since=$(git log "v${highest_tag}..HEAD" --oneline -- "$mcpctl_relpath" 2>/dev/null | wc -l | tr -d ' ')

    if [ "$commits_since" != "0" ] && [ -n "$commits_since" ]; then
        # (b) Commits exist but version unchanged. Warn, don't act.
        warn "mcpctl/ has $commits_since commits since v$highest_tag but const version unchanged."
        info "To release, bump 'const version' in $MCPCTL_DIR/main.go and re-run."
        info "Continuing without mcpctl release."
        return 0
    fi

    # (d) Truly nothing to do.
    info "mcpctl already at v$highest_tag and no new commits — nothing to release."
    return 0
}

if [ $# -lt 1 ]; then
    usage
    exit 1
fi

# Handle --help / -h before treating first arg as version.
if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    usage
    exit 0
fi

# Handle --auto-detect: scan repo state and decide whether to release.
# This branch sets VERSION (if release proceeds) or exits.
if [ "$1" = "--auto-detect" ]; then
    AUTO_DETECT=1
    shift
    echo ""
    printf "  ${C_CYAN}${C_BOLD}▌${C_RESET} ${C_BOLD}MAGERTRON${C_RESET}  ${C_DIM}CLI Release · auto-detect${C_RESET}\n"
    echo ""
    section "Auto-detect: should we release?"

    # Capture detect_release_intent's exit code WITHOUT letting set -e
    # abort the script on a non-zero return. We use return codes 1 and 2
    # as semantic signals (hard-fail / proceed-with-release), not errors.
    # The `|| true` here disables pipefail for this one call; the case
    # statement below interprets the code.
    set +e
    detect_release_intent
    intent_code=$?
    set -e
    case $intent_code in
        0) ok "Nothing to release — caller continues."; exit 0 ;;
        1) err "Auto-detect refused."; exit 1 ;;
        2) ok "Proceeding with release of v$VERSION" ;;
        *) err "Unexpected exit code from detect_release_intent: $intent_code"; exit 1 ;;
    esac
    # Fall through to the existing flow with VERSION already set.
else
    AUTO_DETECT=0
    VERSION="$1"
    shift
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   DRY_RUN=1; shift ;;
        --skip-tap)  SKIP_TAP=1; shift ;;
        --force)     FORCE=1; shift ;;
        --notes)     NOTES="$2"; shift 2 ;;
        -h|--help)   usage; exit 0 ;;
        *)           fatal "Unknown option: $1" ;;
    esac
done

# Version sanity check.
if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    fatal "Version must be MAJOR.MINOR.PATCH format (e.g. 2.0.2). Got: $VERSION"
fi

[ -z "$NOTES" ] && NOTES="Release v${VERSION}"

# ─── Banner ──────────────────────────────────────────────────────────────────

echo ""
printf "  ${C_CYAN}${C_BOLD}▌${C_RESET} ${C_BOLD}MAGERTRON${C_RESET}  ${C_DIM}CLI Release · v${VERSION}${C_RESET}\n"
echo ""

[ "$DRY_RUN" = "1" ] && warn "${C_BOLD}DRY RUN${C_RESET} — no changes will be made."

# ─── Step 1: Preflight checks ────────────────────────────────────────────────

section "Preflight checks"

# 1a: Verify directories exist.
[ -d "$ORCH_REPO/.git" ] || fatal "Not a git repo: $ORCH_REPO"
[ -d "$MCPCTL_DIR" ] || fatal "mcpctl directory not found: $MCPCTL_DIR"

if [ "$SKIP_TAP" = "0" ]; then
    [ -d "$TAP_REPO/.git" ] || fatal "homebrew-tap repo not found at $TAP_REPO (set TAP_REPO or --skip-tap)"
    [ -f "$TAP_REPO/Formula/mcpctl.rb" ] || fatal "Formula/mcpctl.rb not found in $TAP_REPO"
fi
ok "Repo layout valid"

# 1b: Verify required tools.
for tool in go make gh git sha256sum dpkg-deb rpmbuild apt-ftparchive createrepo_c gpg; do
    command -v "$tool" >/dev/null 2>&1 || fatal "Required tool not found: $tool"
done
ok "Required tools present"

# Verify GPG signing key is in the keyring (for APT/YUM repo signing).
GPG_KEY="${GPG_KEY:-7D435C1D166D3BAF}"
if ! gpg --list-secret-keys "$GPG_KEY" >/dev/null 2>&1; then
    fatal "GPG signing key $GPG_KEY not found in keyring (needed for APT/YUM repo signing). Set GPG_KEY=<id> or generate one."
fi
ok "GPG signing key $GPG_KEY available"

# 1c: Verify gh is authenticated.
if ! gh auth status >/dev/null 2>&1; then
    fatal "gh is not authenticated. Run: gh auth login"
fi
ok "gh authenticated"

# 1d: Verify working trees are clean (unless --force).
#
# These error messages are the WHOLE POINT of having this check. The script
# exists to remind the developer of the steps they forgot — commit, push,
# bump version. So when these fire, print the exact repo path, the exact
# files, and the exact commands to copy-paste. No vague "uncommitted changes"
# scolding; that just makes the dev hunt for what's wrong.
remind_commit_steps() {
    local repo_path="$1"
    local repo_label="$2"
    err "$repo_label has uncommitted changes at: $repo_path"
    info "Modified files:"
    git -C "$repo_path" status --short 2>/dev/null | sed 's/^/    /'
    info ""
    info "To fix:"
    info "    cd $repo_path"
    info "    git status                     # review what changed"
    info "    git add <file>...              # stage what you want shipped"
    info "    git commit -m '...'"
    info "    git push                       # release-cli refuses unpushed commits"
    info ""
    info "Then re-run ./bootstrap.sh from mc-platform-private."
}

if [ "$FORCE" = "0" ]; then
    # SCOPED check: release-cli only owns mcpctl/. The orchestrator repo
    # also holds helm/orchestrator/ (owned by bootstrap.sh in the private
    # repo, which modifies Chart.yaml then commits at its Phase 1.5) and
    # docs/charts /docs/apt /docs/yum (also bootstrap-owned). Checking the
    # whole tree would put us in an infinite loop with bootstrap:
    #
    #    bootstrap modifies Chart.yaml → calls release-cli
    #    release-cli sees Chart.yaml uncommitted → refuses
    #    operator resets Chart.yaml, re-runs bootstrap
    #    bootstrap modifies Chart.yaml again → same crash
    #
    # So we ONLY check mcpctl/ and Formula files we touch ourselves.
    # Everything else in the repo is somebody else's concern.
    if ! git -C "$ORCH_REPO" diff --quiet -- mcpctl/ 2>/dev/null; then
        remind_commit_steps "$ORCH_REPO/mcpctl" "orchestrator/mcpctl"
        exit 1
    fi
    if ! git -C "$ORCH_REPO" diff --cached --quiet -- mcpctl/ 2>/dev/null; then
        remind_commit_steps "$ORCH_REPO/mcpctl" "orchestrator/mcpctl (staged-but-uncommitted)"
        exit 1
    fi
    if [ "$SKIP_TAP" = "0" ]; then
        if ! git -C "$TAP_REPO" diff --quiet HEAD 2>/dev/null; then
            remind_commit_steps "$TAP_REPO" "homebrew-tap repo"
            exit 1
        fi
        if ! git -C "$TAP_REPO" diff --cached --quiet 2>/dev/null; then
            remind_commit_steps "$TAP_REPO" "homebrew-tap repo (staged-but-uncommitted)"
            exit 1
        fi
    fi
    ok "Working trees clean (mcpctl/ + tap)"
else
    warn "--force: skipping clean-tree check"
fi

# 1e: Verify version makes sense — current must be lower than new.
CURRENT_VERSION=$(grep -E '^const version = "' "$MCPCTL_DIR/main.go" | sed -E 's/.*"(.+)".*/\1/')
[ -n "$CURRENT_VERSION" ] || fatal "Could not read current version from main.go"

if [ "$AUTO_DETECT" = "1" ]; then
    # In auto-detect mode, main.go IS the source of truth — the dev already
    # bumped the constant and committed it. CURRENT_VERSION == VERSION is
    # the correct state, not an error.
    info "Auto-detect: main.go at v${VERSION} (matches detected target)"
    ok "Version validated by auto-detect"
else
    if [ "$CURRENT_VERSION" = "$VERSION" ]; then
        fatal "main.go already at version $VERSION. Nothing to release."
    fi

    # Crude version comparison via sort -V. If new version sorts after current, we're good.
    HIGHEST=$(printf '%s\n%s\n' "$CURRENT_VERSION" "$VERSION" | sort -V | tail -1)
    if [ "$HIGHEST" != "$VERSION" ]; then
        fatal "New version $VERSION is not higher than current $CURRENT_VERSION"
    fi
    info "Current: $CURRENT_VERSION  →  New: $VERSION"
    ok "Version bump is valid"
fi

# 1f: Verify release doesn't already exist on GitHub.
if gh release view "v${VERSION}" -R magertron/orchestrator >/dev/null 2>&1; then
    fatal "GitHub release v${VERSION} already exists. Delete it first if you want to redo."
fi
ok "No conflicting GitHub release"

# ─── Step 2: Bump version in main.go ─────────────────────────────────────────

section "Bumping version in main.go"

if [ "$AUTO_DETECT" = "1" ]; then
    # In auto-detect mode, main.go is already at the target version (the
    # dev's explicit version-bump commit IS the release trigger). Skip the
    # sed-mutation; verify the state instead and move on.
    info "Auto-detect: main.go already at v${VERSION} (no edit needed)"
    ok "Skipped (dev already bumped)"
elif [ "$DRY_RUN" = "0" ]; then
    sed -i.bak \
        "s|^const version = \"${CURRENT_VERSION}\"|const version = \"${VERSION}\"|" \
        "$MCPCTL_DIR/main.go"
    rm -f "$MCPCTL_DIR/main.go.bak"

    # Verify the change actually happened.
    UPDATED=$(grep -E '^const version = "' "$MCPCTL_DIR/main.go" | sed -E 's/.*"(.+)".*/\1/')
    [ "$UPDATED" = "$VERSION" ] || fatal "sed didn't update main.go (still shows $UPDATED)"
    ok "main.go updated: const version = \"$VERSION\""
else
    info "Would update: const version = \"$VERSION\""
fi

# ─── Step 3: Build binaries ──────────────────────────────────────────────────

section "Cross-compiling binaries"

if [ "$DRY_RUN" = "0" ]; then
    cd "$MCPCTL_DIR"
    rm -rf dist
    make dist 2>&1 | sed 's/^/    /'
    ok "Built 4 platform binaries"
    info "Sizes:"
    ls -lh dist/mcpctl-* | awk '{printf "    %s  %s\n", $5, $NF}' | sed "s|$MCPCTL_DIR/||"
else
    info "Would run: cd $MCPCTL_DIR && make dist"
fi

# ─── Step 4: Build packages ──────────────────────────────────────────────────

section "Building .deb + .rpm packages"

if [ "$DRY_RUN" = "0" ]; then
    cd "$MCPCTL_DIR"
    make packages 2>&1 | sed 's/^/    /' | tail -20
    ok "Built packages"
    info "Package list:"
    ls -lh dist/*.deb dist/*.rpm 2>/dev/null | awk '{printf "    %s  %s\n", $5, $NF}' | sed "s|$MCPCTL_DIR/||"
else
    info "Would run: cd $MCPCTL_DIR && make packages"
fi

# ─── Step 5: Capture SHA256s for the Homebrew Formula ────────────────────────

section "Capturing SHA256s"

if [ "$DRY_RUN" = "0" ]; then
    cd "$MCPCTL_DIR"
    DARWIN_AMD64_SHA=$(sha256sum dist/mcpctl-darwin-amd64 | awk '{print $1}')
    DARWIN_ARM64_SHA=$(sha256sum dist/mcpctl-darwin-arm64 | awk '{print $1}')
    LINUX_AMD64_SHA=$(sha256sum dist/mcpctl-linux-amd64 | awk '{print $1}')
    LINUX_ARM64_SHA=$(sha256sum dist/mcpctl-linux-arm64 | awk '{print $1}')

    [ -n "$DARWIN_AMD64_SHA" ] || fatal "Couldn't read darwin-amd64 SHA"
    [ -n "$DARWIN_ARM64_SHA" ] || fatal "Couldn't read darwin-arm64 SHA"
    [ -n "$LINUX_AMD64_SHA" ]  || fatal "Couldn't read linux-amd64 SHA"
    [ -n "$LINUX_ARM64_SHA" ]  || fatal "Couldn't read linux-arm64 SHA"

    ok "All 4 SHA256s captured"
    info "darwin-amd64: ${DARWIN_AMD64_SHA:0:16}..."
    info "darwin-arm64: ${DARWIN_ARM64_SHA:0:16}..."
    info "linux-amd64:  ${LINUX_AMD64_SHA:0:16}..."
    info "linux-arm64:  ${LINUX_ARM64_SHA:0:16}..."
else
    info "Would capture sha256sum of each binary in dist/"
    DARWIN_AMD64_SHA="<dry-run>"
    DARWIN_ARM64_SHA="<dry-run>"
    LINUX_AMD64_SHA="<dry-run>"
    LINUX_ARM64_SHA="<dry-run>"
fi

# ─── Step 6: Create GitHub release ───────────────────────────────────────────

section "Creating GitHub release v${VERSION}"

if [ "$DRY_RUN" = "0" ]; then
    cd "$MCPCTL_DIR"
    # Collect all release assets in one glob.
    ASSETS=(dist/mcpctl-darwin-amd64 dist/mcpctl-darwin-arm64
            dist/mcpctl-linux-amd64  dist/mcpctl-linux-arm64
            dist/SHA256SUMS)
    # Packages may or may not exist depending on the build target.
    for f in dist/*.deb dist/*.rpm; do
        [ -f "$f" ] && ASSETS+=("$f")
    done

    gh release create "v${VERSION}" \
        --repo magertron/orchestrator \
        --title "mcpctl v${VERSION}" \
        --notes "$NOTES" \
        "${ASSETS[@]}" 2>&1 | sed 's/^/    /'

    RELEASE_URL="https://github.com/magertron/orchestrator/releases/tag/v${VERSION}"
    ok "Release published: $RELEASE_URL"
else
    info "Would: gh release create v${VERSION} --title 'mcpctl v${VERSION}' --notes '$NOTES' <assets>"
fi

# ─── Step 7: Regenerate APT + YUM repos ──────────────────────────────────────

section "Regenerating APT + YUM hosted repos"

if [ "$DRY_RUN" = "0" ]; then
    if [ -x "$ORCH_REPO/scripts/publish-repos.sh" ]; then
        cd "$ORCH_REPO"
        ./scripts/publish-repos.sh 2>&1 | sed 's/^/    /'
        ok "Repos regenerated under docs/apt and docs/yum"
    else
        warn "publish-repos.sh not found at $ORCH_REPO/scripts/publish-repos.sh"
        warn "APT/YUM repos will NOT be updated for this release."
        warn "Customers running 'apt install mcpctl' will get the previous version."
    fi
else
    info "Would run: ./scripts/publish-repos.sh"
fi

# ─── Step 8: Update Homebrew Formula ─────────────────────────────────────────

if [ "$SKIP_TAP" = "1" ]; then
    section "Skipping Homebrew Formula update (--skip-tap)"
else
    section "Regenerating Homebrew Formula"

    FORMULA="$TAP_REPO/Formula/mcpctl.rb"

    if [ "$DRY_RUN" = "0" ]; then
        # Generate a fresh Formula. We rewrite the whole thing to avoid
        # fragile in-place edits of multiple URL+SHA pairs.
        cat > "$FORMULA" <<FORMULA_EOF
class Mcpctl < Formula
  desc "Magertron MCP Orchestrator CLI"
  homepage "https://magertron.com"
  version "${VERSION}"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/magertron/orchestrator/releases/download/v#{version}/mcpctl-darwin-arm64"
      sha256 "${DARWIN_ARM64_SHA}"
    end
    on_intel do
      url "https://github.com/magertron/orchestrator/releases/download/v#{version}/mcpctl-darwin-amd64"
      sha256 "${DARWIN_AMD64_SHA}"
    end
  end

  on_linux do
    on_arm do
      url "https://github.com/magertron/orchestrator/releases/download/v#{version}/mcpctl-linux-arm64"
      sha256 "${LINUX_ARM64_SHA}"
    end
    on_intel do
      url "https://github.com/magertron/orchestrator/releases/download/v#{version}/mcpctl-linux-amd64"
      sha256 "${LINUX_AMD64_SHA}"
    end
  end

  def install
    binary = Dir.glob("mcpctl-*").first
    odie "Could not locate downloaded mcpctl binary" if binary.nil?
    bin.install binary => "mcpctl"
  end

  test do
    assert_match "mcpctl v#{version}", shell_output("#{bin}/mcpctl version")
  end
end
FORMULA_EOF
        ok "Formula rewritten with v${VERSION} + new SHAs"
    else
        info "Would rewrite $FORMULA with version $VERSION + new SHAs"
    fi
fi

# ─── Step 9: Commit + push orchestrator changes ──────────────────────────────

section "Committing orchestrator (version bump + regenerated repos)"

if [ "$DRY_RUN" = "0" ]; then
    cd "$ORCH_REPO"
    git add mcpctl/main.go docs/apt docs/yum
    if git diff --cached --quiet; then
        warn "Nothing staged to commit (versions match? repos already current?)"
    else
        if [ "$AUTO_DETECT" = "1" ]; then
            # Auto-detect mode: main.go was already at v${VERSION}, so only
            # docs/apt + docs/yum are the actual changes here.
            git commit -m "release: APT + YUM repos for mcpctl v${VERSION}

- regenerated APT repo (docs/apt) for v${VERSION}
- regenerated YUM repo (docs/yum) for v${VERSION}

(version constant in mcpctl/main.go was bumped in the trigger commit
that activated bootstrap's Phase 1.7 auto-release)" 2>&1 | sed 's/^/    /'
        else
            git commit -m "mcpctl: v${VERSION}

- bumped version constant in main.go
- regenerated APT repo (docs/apt) for v${VERSION}
- regenerated YUM repo (docs/yum) for v${VERSION}" 2>&1 | sed 's/^/    /'
        fi
        git push 2>&1 | sed 's/^/    /'
        ok "orchestrator pushed"
    fi
else
    info "Would: git commit + push mcpctl/main.go, docs/apt, docs/yum"
fi

# ─── Step 10: Commit + push homebrew-tap ──────────────────────────────────────

if [ "$SKIP_TAP" = "1" ]; then
    section "Skipping homebrew-tap commit (--skip-tap)"
else
    section "Committing homebrew-tap (Formula update)"

    if [ "$DRY_RUN" = "0" ]; then
        cd "$TAP_REPO"
        git add Formula/mcpctl.rb
        if git diff --cached --quiet; then
            warn "Formula has no changes to commit"
        else
            git commit -m "Bump mcpctl to v${VERSION}" 2>&1 | sed 's/^/    /'
            git push 2>&1 | sed 's/^/    /'
            ok "homebrew-tap pushed"
        fi
    else
        info "Would: git commit + push Formula/mcpctl.rb in $TAP_REPO"
    fi
fi

# ─── Step 11: Verify ─────────────────────────────────────────────────────────

section "Verifying release"

if [ "$DRY_RUN" = "0" ]; then
    # GitHub eventually-consistent; small sleep before checking.
    sleep 2

    if curl -fsSLI "https://github.com/magertron/orchestrator/releases/download/v${VERSION}/mcpctl-linux-amd64" \
       | head -1 | grep -q "200\|302"; then
        ok "Release asset is downloadable"
    else
        warn "Release asset not yet reachable (may need a few seconds for GitHub to propagate)"
    fi

    if [ "$SKIP_TAP" = "0" ]; then
        # Verify the formula is fetchable via raw GitHub
        if curl -fsSL "https://raw.githubusercontent.com/magertron/homebrew-tap/main/Formula/mcpctl.rb" \
           | grep -q "version \"${VERSION}\""; then
            ok "Formula on GitHub shows v${VERSION}"
        else
            warn "Formula on GitHub not yet showing v${VERSION} (push propagation)"
        fi
    fi
else
    info "Would verify the release URL + Formula visibility"
fi

# ─── Done ────────────────────────────────────────────────────────────────────

echo ""
printf "  ${C_GREEN}${C_BOLD}${G_CHECK} Released mcpctl v${VERSION}${C_RESET}\n"
echo ""
echo "  Release URL:  ${C_BOLD}https://github.com/magertron/orchestrator/releases/tag/v${VERSION}${C_RESET}"
echo ""
echo "  ${C_BOLD}Customer install paths now point at v${VERSION}:${C_RESET}"
echo "    ${G_ARROW} brew upgrade mcpctl    (existing customers)"
echo "    ${G_ARROW} brew install magertron/tap/mcpctl    (new customers)"
echo "    ${G_ARROW} curl -fsSL https://magertron.com/install-mcpctl.sh | sh"
echo "    ${G_ARROW} wget .../v${VERSION}/mcpctl_${VERSION}_amd64.deb && sudo dpkg -i ..."
echo ""

if [ "$DRY_RUN" = "1" ]; then
    warn "(dry-run mode — nothing was actually changed)"
fi
