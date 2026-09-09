#!/bin/sh
# Install codebase-brain: CLI on PATH, Claude Code skill, optional MCP server.
set -e
REPO=$(cd "$(dirname "$0")" && pwd)
BIN=${BIN_DIR:-$HOME/.local/bin}
SKILLS=${SKILLS_DIR:-$HOME/.claude/skills}

mkdir -p "$BIN" "$SKILLS"
ln -sfn "$REPO/bin/idxg" "$BIN/idxg"
ln -sfn "$REPO/bin/idxg-build" "$BIN/idxg-build"
ln -sfn "$REPO/bin/idxg-history" "$BIN/idxg-history"
rm -f "$SKILLS/ios-codebase-indexer"
ln -sfn "$REPO/skill/codebase-brain" "$SKILLS/codebase-brain"
echo "linked idxg, idxg-build, idxg-history -> $BIN"
echo "linked skill           -> $SKILLS/codebase-brain"

if command -v claude >/dev/null 2>&1; then
    claude mcp remove --scope user ios-codebase-indexer >/dev/null 2>&1 || true
    if claude mcp list 2>/dev/null | grep -q "^codebase-brain:"; then
        echo "mcp server already registered"
    else
        claude mcp add --scope user codebase-brain -- python3 "$REPO/src/mcp_server.py" \
            && echo "registered mcp server (restart Claude Code to pick it up)"
    fi
else
    echo "claude CLI not found; skipping MCP registration"
fi

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "note: $BIN is not on your PATH" ;;
esac

echo
echo "next: cd into an indexed Swift project and run"
echo "  idxg-build --jobs 8 && idxg status"
