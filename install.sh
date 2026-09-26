#!/bin/sh
# Installs what the email skill needs, at user level, no sudo:
#   himalaya v2 (pinned) into ~/.local/bin, the markdown package for rich-text drafts,
#   and the skill into ~/.claude/skills/email (skip with --deps-only, e.g. after a plugin install).
# Idempotent: re-running skips whatever is already in place.
set -eu

HIMALAYA_VERSION="${HIMALAYA_VERSION:-v2.1.0}"
PREFIX="${PREFIX:-$HOME/.local}"
SKILLS_DIR="${SKILLS_DIR:-$HOME/.claude/skills}"
HERE=$(cd "$(dirname "$0")" && pwd)

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) die "native Windows is untested; run this inside WSL (wsl --install), where the Linux path is tested" ;;
esac

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
    || die "python 3.11+ required (mail.py uses tomllib)"

if command -v himalaya >/dev/null 2>&1 && himalaya --version | grep -q '^himalaya v2'; then
    echo "himalaya: $(himalaya --version | head -1), kept"
else
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64) target=x86_64-linux ;;
        Linux-aarch64|Linux-arm64) target=aarch64-linux ;;
        Darwin-x86_64) target=x86_64-darwin ;;
        Darwin-arm64) target=aarch64-darwin ;;
        *) die "no prebuilt himalaya for $(uname -sm); see https://github.com/pimalaya/himalaya#installation" ;;
    esac
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    curl -fsSL -o "$tmp/h.tgz" \
        "https://github.com/pimalaya/himalaya/releases/download/$HIMALAYA_VERSION/himalaya.$target.tgz"
    tar -xzf "$tmp/h.tgz" -C "$tmp"
    mkdir -p "$PREFIX/bin"
    cp -f "$tmp/himalaya" "$PREFIX/bin/himalaya"
    echo "himalaya: $("$PREFIX/bin/himalaya" --version | head -1) -> $PREFIX/bin"
    case ":$PATH:" in *":$PREFIX/bin:"*) ;; *) echo "note: add $PREFIX/bin to PATH" ;; esac
fi

if python3 -c 'import markdown' 2>/dev/null; then
    echo "markdown: present, kept"
else
    python3 -m pip install --user --quiet markdown 2>/dev/null \
        || python3 -m pip install --user --quiet --break-system-packages markdown \
        || die "could not install markdown; try: sudo apt install python3-markdown"
    echo "markdown: installed"
fi

if [ "${1:-}" != "--deps-only" ]; then
    mkdir -p "$SKILLS_DIR/email"
    cp -f "$HERE/skills/email/SKILL.md" "$HERE/skills/email/mail.py" "$SKILLS_DIR/email/"
    chmod +x "$SKILLS_DIR/email/mail.py"
    echo "skill: $SKILLS_DIR/email"
fi

mkdir -p "$HOME/.config/mail" && chmod 700 "$HOME/.config/mail"
[ -f "$HOME/.config/himalaya/config.toml" ] \
    || echo "next: copy config.sample.toml to ~/.config/himalaya/config.toml (chmod 600) and add your mailbox"
