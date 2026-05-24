.PHONY: build install clean dist all

BINARY=mcpctl
VERSION=2.0.0-dev

all: build

build:
	go build -ldflags="-s -w -X main.version=$(VERSION)" -o $(BINARY) .

install: build
	cp $(BINARY) /usr/local/bin/$(BINARY)

# Cross-compile for distribution. Output goes into ./dist/.
dist:
	mkdir -p dist
	GOOS=darwin GOARCH=arm64 go build -ldflags="-s -w -X main.version=$(VERSION)" -o dist/$(BINARY)-darwin-arm64 .
	GOOS=darwin GOARCH=amd64 go build -ldflags="-s -w -X main.version=$(VERSION)" -o dist/$(BINARY)-darwin-amd64 .
	GOOS=linux  GOARCH=amd64 go build -ldflags="-s -w -X main.version=$(VERSION)" -o dist/$(BINARY)-linux-amd64  .
	GOOS=linux  GOARCH=arm64 go build -ldflags="-s -w -X main.version=$(VERSION)" -o dist/$(BINARY)-linux-arm64  .
	cd dist && shasum -a 256 $(BINARY)-* > SHA256SUMS

clean:
	rm -f $(BINARY)
	rm -rf dist/
