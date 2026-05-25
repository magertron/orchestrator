.PHONY: build install clean dist all package-deb package-rpm packages

BINARY=mcpctl

# Version is defined as a const in main.go (single source of truth).
# If you want to bump version, edit main.go directly:
#   const version = "X.Y.Z"

all: build

build:
	go build -ldflags="-s -w" -o $(BINARY) .

install: build
	cp $(BINARY) /usr/local/bin/$(BINARY)

# Cross-compile for distribution. Output goes into ./dist/.
dist:
	mkdir -p dist
	GOOS=darwin GOARCH=arm64 go build -ldflags="-s -w" -o dist/$(BINARY)-darwin-arm64 .
	GOOS=darwin GOARCH=amd64 go build -ldflags="-s -w" -o dist/$(BINARY)-darwin-amd64 .
	GOOS=linux  GOARCH=amd64 go build -ldflags="-s -w" -o dist/$(BINARY)-linux-amd64  .
	GOOS=linux  GOARCH=arm64 go build -ldflags="-s -w" -o dist/$(BINARY)-linux-arm64  .
	cd dist && shasum -a 256 $(BINARY)-* > SHA256SUMS

# Build Debian/Ubuntu .deb packages for amd64 + arm64.
# Requires: dpkg-deb, prior `make dist`.
package-deb: dist
	./scripts/build-deb.sh

# Build RHEL/Fedora .rpm packages for amd64 + arm64.
# Requires: rpmbuild (apt install rpm), prior `make dist`.
package-rpm: dist
	./scripts/build-rpm.sh

# Build all package formats.
packages: package-deb package-rpm

clean:
	rm -f $(BINARY)
	rm -rf dist/
