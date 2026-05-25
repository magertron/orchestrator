#!/bin/bash
# build-deb.sh — build .deb packages for mcpctl across architectures.
#
# Reads version from main.go (single source of truth).
# Builds packages into ./dist/ alongside the existing binaries.
#
# Usage: ./scripts/build-deb.sh
# Requires: dpkg-deb, the cross-compiled binaries in ./dist/

set -euo pipefail

cd "$(dirname "$0")/.."

# Read the version from main.go.
VERSION=$(grep -E '^const version = "' main.go | sed -E 's/.*"(.+)".*/\1/')
if [ -z "$VERSION" ]; then
    echo "ERROR: could not parse version from main.go" >&2
    exit 1
fi

echo "Building .deb packages for mcpctl v${VERSION}"

# Verify binaries are present.
for ARCH in amd64 arm64; do
    BIN="dist/mcpctl-linux-${ARCH}"
    if [ ! -f "$BIN" ]; then
        echo "ERROR: $BIN not found. Run 'make dist' first." >&2
        exit 1
    fi
done

# Build a .deb for each architecture.
for ARCH in amd64 arm64; do
    PKG_DIR="dist/mcpctl_${VERSION}_${ARCH}"
    rm -rf "$PKG_DIR"

    # Standard Debian package layout:
    #   DEBIAN/control      — package metadata
    #   usr/local/bin/      — where the binary goes
    mkdir -p "$PKG_DIR/DEBIAN"
    mkdir -p "$PKG_DIR/usr/local/bin"

    # Copy + rename the binary
    cp "dist/mcpctl-linux-${ARCH}" "$PKG_DIR/usr/local/bin/mcpctl"
    chmod 755 "$PKG_DIR/usr/local/bin/mcpctl"

    # Write the control file.
    # Description: first line is a short summary, indented lines are long desc.
    cat > "$PKG_DIR/DEBIAN/control" <<EOF
Package: mcpctl
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Maintainer: Magertron <support@magertron.com>
Homepage: https://magertron.com
Description: Magertron MCP Orchestrator CLI
 mcpctl is the command-line interface for managing a Magertron MCP
 Orchestrator deployment. Authenticate, deploy MCP servers, manage
 service accounts, review audit logs, and inspect cluster state.
 .
 Requires a running Magertron orchestrator (see https://magertron.com
 for installation).
EOF

    # Build the .deb.
    DEB_FILE="dist/mcpctl_${VERSION}_${ARCH}.deb"
    dpkg-deb --build --root-owner-group "$PKG_DIR" "$DEB_FILE" >/dev/null

    # Clean up the staging directory.
    rm -rf "$PKG_DIR"

    echo "  ✓ Built $DEB_FILE"
done

echo ""
echo "Done. Packages in ./dist/:"
ls -lh dist/*.deb
