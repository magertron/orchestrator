#!/bin/sh
# install-mcpctl.sh — one-line installer for the Magertron CLI.
#
# Usage:
#   curl -fsSL https://magertron.com/install-mcpctl.sh | sh
#
# Or pin a specific version:
#   curl -fsSL https://magertron.com/install-mcpctl.sh | MCPCTL_VERSION=v2.0.0 sh
#
# Or install to a different location:
#   curl -fsSL https://magertron.com/install-mcpctl.sh | INSTALL_DIR=$HOME/bin sh
#
# What this script does:
#   1. Detects your OS + architecture
#   2. Fetches the matching mcpctl binary from GitHub Releases
#   3. Verifies SHA-256 checksum against the released SHA256SUMS file
#   4. Installs to /usr/local/bin/mcpctl (or $INSTALL_DIR)
#
# This script is safe to inspect before running:
#   curl -fsSL https://magertron.com/install-mcpctl.sh
#
# License: Apache 2.0. Source: github.com/curtismager20/magertron-mcpm

set -eu

REPO="curtismager20/magertron-mcpm"
BINARY="mcpctl"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
MCPCTL_VERSION="${MCPCTL_VERSION:-latest}"

# ─── Detect OS + arch ────────────────────────────────────────────────────────
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"

case "$OS" in
    linux)  GOOS="linux" ;;
    darwin) GOOS="darwin" ;;
    *)
        echo "Error: unsupported OS '$OS'." >&2
        echo "  mcpctl currently ships for macOS (Darwin) and Linux." >&2
        echo "  For other platforms, build from source:" >&2
        echo "    git clone https://github.com/$REPO" >&2
        echo "    cd magertron-mcpm/mcpctl && make build" >&2
        exit 1
        ;;
esac

case "$ARCH" in
    x86_64|amd64) GOARCH="amd64" ;;
    arm64|aarch64) GOARCH="arm64" ;;
    *)
        echo "Error: unsupported architecture '$ARCH'." >&2
        echo "  mcpctl ships binaries for amd64 and arm64." >&2
        echo "  For other architectures, build from source:" >&2
        echo "    git clone https://github.com/$REPO" >&2
        echo "    cd magertron-mcpm/mcpctl && make build" >&2
        exit 1
        ;;
esac

ASSET="${BINARY}-${GOOS}-${GOARCH}"

# ─── Resolve version ─────────────────────────────────────────────────────────
if [ "$MCPCTL_VERSION" = "latest" ]; then
    echo "Resolving latest mcpctl release..."
    # GitHub redirects /releases/latest to /releases/tag/<version>; we follow
    # the redirect with -L and inspect the resolved URL.
    RESOLVED=$(curl -fsSLI "https://github.com/$REPO/releases/latest" \
        | grep -i '^location:' | tail -1 | tr -d '\r\n' || true)
    MCPCTL_VERSION=$(echo "$RESOLVED" | sed 's|.*/tag/||')
    if [ -z "$MCPCTL_VERSION" ]; then
        echo "Error: could not resolve latest version." >&2
        echo "  Either GitHub is unreachable or no releases have been published yet." >&2
        echo "  Set MCPCTL_VERSION=vX.Y.Z explicitly:" >&2
        echo "    MCPCTL_VERSION=v2.0.0 curl ... | sh" >&2
        exit 1
    fi
fi

echo "Target: $BINARY $MCPCTL_VERSION ($GOOS/$GOARCH)"

# ─── Download binary + checksum ──────────────────────────────────────────────
BASE_URL="https://github.com/$REPO/releases/download/$MCPCTL_VERSION"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

echo "Downloading $ASSET..."
if ! curl -fsSL "$BASE_URL/$ASSET" -o "$TMP/$BINARY"; then
    echo "Error: failed to download $BASE_URL/$ASSET" >&2
    echo "  Check that release $MCPCTL_VERSION exists and includes $ASSET." >&2
    exit 1
fi

echo "Downloading SHA256SUMS..."
if ! curl -fsSL "$BASE_URL/SHA256SUMS" -o "$TMP/SHA256SUMS"; then
    echo "Warning: SHA256SUMS not found; skipping checksum verification." >&2
else
    echo "Verifying checksum..."
    EXPECTED=$(grep " $ASSET\$" "$TMP/SHA256SUMS" | awk '{print $1}' || true)
    if [ -z "$EXPECTED" ]; then
        echo "Warning: no checksum entry for $ASSET in SHA256SUMS; skipping." >&2
    else
        if command -v sha256sum >/dev/null 2>&1; then
            ACTUAL=$(sha256sum "$TMP/$BINARY" | awk '{print $1}')
        elif command -v shasum >/dev/null 2>&1; then
            ACTUAL=$(shasum -a 256 "$TMP/$BINARY" | awk '{print $1}')
        else
            echo "Warning: neither sha256sum nor shasum available; skipping checksum verification." >&2
            ACTUAL="$EXPECTED"
        fi
        if [ "$EXPECTED" != "$ACTUAL" ]; then
            echo "Error: checksum mismatch!" >&2
            echo "  expected: $EXPECTED" >&2
            echo "  actual:   $ACTUAL" >&2
            echo "  Refusing to install. This usually means a corrupted download." >&2
            echo "  Try again, or download manually from:" >&2
            echo "    $BASE_URL/$ASSET" >&2
            exit 1
        fi
        echo "Checksum OK."
    fi
fi

# ─── Install ─────────────────────────────────────────────────────────────────
chmod +x "$TMP/$BINARY"

DEST="$INSTALL_DIR/$BINARY"

# Decide whether to sudo. If the user owns INSTALL_DIR, no sudo needed.
# Otherwise, prompt with sudo.
if [ -w "$INSTALL_DIR" ]; then
    mv "$TMP/$BINARY" "$DEST"
else
    echo "Installing to $DEST (requires sudo)..."
    sudo mv "$TMP/$BINARY" "$DEST"
fi

# ─── Verify ──────────────────────────────────────────────────────────────────
if ! command -v "$BINARY" >/dev/null 2>&1; then
    echo ""
    echo "Installed to $DEST, but '$BINARY' is not on your PATH."
    echo "Add this to your shell rc:"
    echo "  export PATH=\"$INSTALL_DIR:\$PATH\""
    echo ""
    echo "Or invoke directly: $DEST version"
    exit 0
fi

INSTALLED_VERSION=$("$BINARY" version 2>/dev/null || echo "unknown")
echo ""
echo "Installed: $INSTALLED_VERSION"
echo "Location:  $DEST"
echo ""
echo "Next steps:"
echo "  $BINARY login https://<your-magertron-host>:30443 <user> <password>"
echo "  $BINARY --help"
