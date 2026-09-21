#!/data/data/com.termux/files/usr/bin/bash
# File the load-bias report upstream, then leave a pointer on #9.
#
#   ./FILE-UPSTREAM.sh --dry-run   print what would happen, change nothing
#   ./FILE-UPSTREAM.sh             create the issue, then prompt before commenting
#
# Two steps, deliberately separate: the new issue carries the full report, and
# #9 gets one line pointing at it rather than a wall of text in someone else's
# thread.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="google-antigravity/antigravity-cli"
REPORT="$HERE/UPSTREAM-REPORT.md"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

command -v gh >/dev/null || { echo "gh is required" >&2; exit 1; }
[ -f "$REPORT" ] || { echo "no $REPORT" >&2; exit 1; }

# The title is the report's H1, minus the leading "# ".
TITLE="$(head -1 "$REPORT" | sed 's/^# //')"
# The body is everything after the H1.
BODY="$(tail -n +2 "$REPORT")"

if [ "$DRY" = 1 ]; then
  echo "=== would create in $REPO ==="
  echo "title: $TITLE"
  echo
  echo "--- first 20 lines of the body (preview only; the real run sends all"
  echo "    $(printf '%s' "$BODY" | wc -l) lines / $(printf '%s' "$BODY" | wc -c) bytes) ---"
  echo "$BODY" | head -20
  echo "--- end of preview; $(( $(printf '%s' "$BODY" | wc -l) - 20 )) more lines follow in the real run ---"
  echo
  echo "To read the whole thing as it will be posted:  less UPSTREAM-REPORT.md"
  echo
  echo "=== would then comment on #9 (after confirmation) ==="
  echo "For anyone working on or past the TCMalloc issue: there is an independent"
  echo "load-bias crash at 0x5be0 in google_find_phdr, with root cause and a fix,"
  echo "in #<new issue>."
  exit 0
fi

echo "Filing the report as a new issue on $REPO ..."
URL="$(printf '%s' "$BODY" | gh issue create -R "$REPO" -t "$TITLE" -F -)"
echo "created: $URL"

NUM="${URL##*/}"
echo
echo "Now leave a one-line pointer on #9? This posts publicly as you."
read -r -p "comment on #9 [y/N] " ans
case "$ans" in
  [yY]*)
    gh issue comment 9 -R "$REPO" -b "For anyone working on or past the TCMalloc issue: there is an independent load-bias crash at \`0x5be0\` in \`google_find_phdr\`, with root cause and a fix, in #$NUM."
    echo "commented on #9"
    ;;
  *) echo "skipped; the comment for #9 would have been:"
     echo "  For anyone working on or past the TCMalloc issue: there is an independent"
     echo "  load-bias crash at \`0x5be0\` in \`google_find_phdr\`, with root cause and a"
     echo "  fix, in #$NUM." ;;
esac
