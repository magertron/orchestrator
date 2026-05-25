#!/bin/bash
# build-rpm.sh — build .rpm packages for mcpctl.
#
# Currently builds x86_64 only — aarch64 cross-packaging on amd64 hosts
# requires extra rpmbuild config. Add aarch64 when there's customer demand.
#
# Reads version from main.go (single source of truth).
# Builds packages into ./dist/ alongside the existing binaries.
#
# Usage: ./scripts/build-rpm.sh
# Requires: rpmbuild (apt install rpm on Ubuntu)
# Requires: the cross-compiled binaries in ./dist/

set -euo pipefail

cd "$(dirname "$0")/.."

# Read the version from main.go.
VERSION=$(grep -E '^const version = "' main.go | sed -E 's/.*"(.+)".*/\1/')
if [ -z "$VERSION" ]; then
    echo "ERROR: could not parse version from main.go" >&2
    exit 1
fi

echo "Building .rpm packages for mcpctl v${VERSION}"

# We only build x86_64 RPMs for now. aarch64 RPM cross-packaging from an
# amd64 host needs additional rpmbuild configuration we'll add when there's
# a customer ask.
ARCHES_TO_BUILD="amd64"

# Verify binaries exist.
for ARCH in $ARCHES_TO_BUILD; do
    BIN="dist/mcpctl-linux-${ARCH}"
    if [ ! -f "$BIN" ]; then
        echo "ERROR: $BIN not found. Run 'make dist' first." >&2
        exit 1
    fi
done

# rpmbuild requires a specific directory structure. We'll create a per-build
# temp tree inside dist/ to keep everything self-contained.
RPM_TOP="$(pwd)/dist/rpm-build"
rm -rf "$RPM_TOP"
mkdir -p "$RPM_TOP/BUILD" "$RPM_TOP/RPMS" "$RPM_TOP/SOURCES" "$RPM_TOP/SPECS" "$RPM_TOP/SRPMS"

for ARCH in $ARCHES_TO_BUILD; do
    RPM_ARCH="x86_64"
    [ "$ARCH" = "arm64" ] && RPM_ARCH="aarch64"

    # Copy the binary into SOURCES with the canonical name.
    cp "dist/mcpctl-linux-${ARCH}" "$RPM_TOP/SOURCES/mcpctl"

    # Write the .spec file.
    SPEC_FILE="$RPM_TOP/SPECS/mcpctl.spec"
    cat > "$SPEC_FILE" <<SPEC
Name:           mcpctl
Version:        ${VERSION}
Release:        1
Summary:        Magertron MCP Orchestrator CLI

License:        ASL 2.0
URL:            https://magertron.com
Source0:        mcpctl

BuildArch:      ${RPM_ARCH}
AutoReqProv:    no

%description
mcpctl is the command-line interface for managing a Magertron MCP
Orchestrator deployment. Authenticate, deploy MCP servers, manage
service accounts, review audit logs, and inspect cluster state.

Requires a running Magertron orchestrator (see https://magertron.com
for installation).

%prep
# Nothing to prep — Source0 is a prebuilt binary.

%build
# Nothing to build — Source0 is already compiled.

%install
mkdir -p %{buildroot}/usr/local/bin
install -m 755 %{SOURCE0} %{buildroot}/usr/local/bin/mcpctl

%files
/usr/local/bin/mcpctl

%changelog
* $(date '+%a %b %d %Y') Magertron <support@magertron.com> - ${VERSION}-1
- Release ${VERSION}
SPEC

    # Build the RPM.
    rpmbuild \
        --define "_topdir $RPM_TOP" \
        --target "$RPM_ARCH" \
        -bb "$SPEC_FILE"

    # Find and move the resulting RPM into ./dist/ with a clean name.
    BUILT_RPM=$(find "$RPM_TOP/RPMS" -name "mcpctl-${VERSION}-1.${RPM_ARCH}.rpm" -type f | head -1)
    if [ -z "$BUILT_RPM" ] || [ ! -f "$BUILT_RPM" ]; then
        echo "ERROR: rpmbuild succeeded but couldn't find output file." >&2
        echo "  Expected: mcpctl-${VERSION}-1.${RPM_ARCH}.rpm under $RPM_TOP/RPMS/" >&2
        exit 1
    fi

    OUT_RPM="dist/mcpctl-${VERSION}-1.${RPM_ARCH}.rpm"
    cp "$BUILT_RPM" "$OUT_RPM"
    echo "  ✓ Built $OUT_RPM"
done

# Clean up the rpmbuild scratch tree.
rm -rf "$RPM_TOP"

echo ""
echo "Done. Packages in ./dist/:"
ls -lh dist/*.rpm
