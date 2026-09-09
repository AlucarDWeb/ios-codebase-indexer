#!/bin/sh
# Install ios-codebase-indexer: CLI on PATH, Claude Code skill, optional MCP server.
set -e
REPO=$(cd "$(dirname "$0")" && pwd)
BIN=${BIN_DIR:-$HOME/.local/bin}
SKILLS=${SKILLS_DIR:-$HOME/.claude/skills}

mkdir -p "$BIN" "$SKILLS"
ln -sfn "$REPO/bin/idxg" "$BIN/idxg"
ln -sfn "$REPO/bin/idxg-build" "$BIN/idxg-build"
ln -sfn "$REPO/bin/idxg-history" "$BIN/idxg-history"
ln -sfn "$REPO/skill/ios-codebase-indexer" "$SKILLS/ios-codebase-indexer"
echo "linked idxg, idxg-build, idxg-history -> $BIN"
echo "linked skill           -> $SKILLS/ios-codebase-indexer"

if command -v claude >/dev/null 2>&1; then
    if claude mcp list 2>/dev/null | grep -q "^ios-codebase-indexer:"; then
        echo "mcp server already registered"
    else
        claude mcp add --scope user ios-codebase-indexer -- python3 "$REPO/src/mcp_server.py" \
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
