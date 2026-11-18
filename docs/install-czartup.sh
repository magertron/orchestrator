#!/bin/sh
# Install the Czartup(TM) CLI - the same way as mcpctl:
#   curl -fsSL https://magertron.com/install-czartup.sh | sh
# Debian/Ubuntu with the Magertron apt repo: apt install czartup (updates come with apt upgrade).
# Anything else (macOS, other Linux): the single-file CLI into /usr/local/bin, or ~/.local/bin without sudo.
# Needs python3. Then: czartup login https://your-magertron
set -eu
BASE="${MAGERTRON_DOWNLOADS:-https://magertron.com}"

command -v python3 >/dev/null 2>&1 || { echo "czartup needs python3 - install it first" >&2; exit 1; }

if command -v apt-get >/dev/null 2>&1 && [ -f /etc/apt/sources.list.d/magertron.list ]; then
    echo "Installing czartup from the Magertron apt repo..."
    sudo apt-get update -qq
    sudo apt-get install -y czartup
else
    TMP=$(mktemp)
    curl -fsSL "$BASE/czartup/czartup" -o "$TMP"
    head -1 "$TMP" | grep -q python3 || { echo "the download is not the czartup CLI - nothing installed" >&2; rm -f "$TMP"; exit 1; }
    chmod 0755 "$TMP"
    if [ -w /usr/local/bin ] || command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        DEST=/usr/local/bin/czartup
        if [ -w /usr/local/bin ]; then mv "$TMP" "$DEST"; else sudo mv "$TMP" "$DEST"; fi
    else
        mkdir -p "$HOME/.local/bin"
        DEST="$HOME/.local/bin/czartup"
        mv "$TMP" "$DEST"
        case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to your PATH to run czartup." ;; esac
    fi
    echo "Installed $DEST"
fi
# The /czartup-... commands for any IDE found (Cursor, Claude Code, Codex) - harmless when none is.
czartup commands >/dev/null 2>&1 && echo "Added the /czartup-... commands to your IDE(s)." || true
echo "Next: czartup login https://your-magertron"
