#!/usr/bin/env bash
# Build-time step: overlay the private strategy on the public stand-in, when a read-only token is provided.
#
#   PRIVATE_REPO_TOKEN       fine-grained, read-only GitHub token for the private repo (Render build env, or a
#                            Docker build secret via PRIVATE_REPO_TOKEN_FILE). Unset/empty = demo-only build.
#   PRIVATE_REPO_REF         branch, tag or commit to build from (default: main)
#   TARGET_DIR               tree to overlay (default: the repo root this script lives in)
#   OVERLAY_ALLOW_GIT_WORKTREE=1  only for a throwaway deploy clone (set by render_build.sh); otherwise a git tree is refused
#   PRIVATE_REPO_URL         clone URL (default: the owner's private repo; override only for local tests)
#
# The token is handed to git through a throwaway GIT_ASKPASS script: it is never put in a URL, a command line,
# .git/config or the log, and the clone is deleted on exit (success or failure). No `set -x` in this file.
# Exit 0: overlay done, or no token (stand-in kept, one plain line says so). Non-zero: token set but the overlay
# did not complete (the build must fail, never silently continue with the stand-in).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${TARGET_DIR:-$(dirname "$HERE")}"
REF="${PRIVATE_REPO_REF:-main}"
URL="${PRIVATE_REPO_URL:-https://github.com/calebabr/tradingBot.git}"
PY="$(command -v python3 || command -v python || true)"

TOKEN="${PRIVATE_REPO_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -n "${PRIVATE_REPO_TOKEN_FILE:-}" ] && [ -s "$PRIVATE_REPO_TOKEN_FILE" ]; then
  TOKEN="$(tr -d '\r\n' < "$PRIVATE_REPO_TOKEN_FILE")"
fi
unset PRIVATE_REPO_TOKEN

if [ -z "$TOKEN" ]; then
  echo "demo-only build: no private strategy token, the public stand-in is installed (live mode will be refused)."
  exit 0
fi

fail() { echo "private overlay FAILED: $1" >&2; exit 1; }

[ -n "$PY" ] || fail "python not found"
case "$REF" in
  ""|-*|*..*|*[!A-Za-z0-9._/-]*) fail "PRIVATE_REPO_REF has characters that are not allowed" ;;
esac

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 700 "$WORK"

# Askpass helper: answers git's username/password prompts from the environment. It contains no secret itself.
cat > "$WORK/askpass.sh" <<'ASK'
#!/bin/sh
case "$1" in
  Username*) echo "x-access-token" ;;
  *) printf '%s\n' "$KALSHI_FETCH_TOKEN" ;;
esac
ASK
chmod 700 "$WORK/askpass.sh"

CLONE="$WORK/private"
mkdir "$CLONE"
git_private() {
  KALSHI_FETCH_TOKEN="$TOKEN" GIT_ASKPASS="$WORK/askpass.sh" GIT_TERMINAL_PROMPT=0 \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    git -c credential.helper= -c core.askPass="$WORK/askpass.sh" -C "$CLONE" "$@"
}

echo "fetching the private strategy (ref: $REF)..."
{
  git -C "$CLONE" init -q &&
  git -C "$CLONE" remote add origin "$URL" &&
  git_private fetch -q --depth 1 --no-tags origin "$REF" &&
  git -C "$CLONE" -c advice.detachedHead=false checkout -q FETCH_HEAD
} >"$WORK/git.log" 2>&1 || fail "could not fetch the private repo (check that the token is valid, not expired, and has read access to Contents; check PRIVATE_REPO_REF)"

# Traceability (SC-7): print the exact private commit built (a hash only, nothing private).
echo "private commit: $(git -C "$CLONE" rev-parse HEAD 2>/dev/null || echo unknown)"

args=(--private "$CLONE" --target "$TARGET")
# A git work tree is refused unless the caller opts in. Only render_build.sh (a throwaway deploy clone) sets this.
[ "${OVERLAY_ALLOW_GIT_WORKTREE:-}" = "1" ] && args+=(--allow-git-worktree)
"$PY" "$HERE/overlay_private.py" "${args[@]}" || fail "overlay_private.py rejected the private checkout"

rm -rf "$CLONE"

# Static assertion (no private code is imported, nothing private is printed).
"$PY" - "$TARGET" <<'PYEOF' || fail "post-build assertion: the stand-in is still active"
import re, sys
from pathlib import Path
t = Path(sys.argv[1]) / "kalshi_bot"
marker = t / "_overlay.py"
ok = marker.is_file() and re.search(r"^IS_STANDIN\s*=\s*False\b", marker.read_text(encoding="utf-8"), re.M) \
    and not re.search(r"^\s*IS_STANDIN\s*(?::[^=]*)?=\s*True\b",
                      (t / "maker_strategy.py").read_text(encoding="utf-8", errors="replace"), re.M)
print("IS_STANDIN=" + ("False" if ok else "True"))
sys.exit(0 if ok else 1)
PYEOF
echo "private strategy installed (live mode can be enabled by the app's own gates)."
