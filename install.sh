#!/data/data/com.termux/files/usr/bin/bash
# Patch Google's Antigravity CLI to run natively in Termux, without glibc.
#
#   ./install.sh                 fetch and patch the default version
#   ./install.sh --version 1.2.7 pin a version
#   ./install.sh --keep-download keep the downloaded tarball
#   ./install.sh --from-binary F patch F instead of downloading (use this with
#                                wallentx's VA39-patched engine; see README)
#
# Nothing here redistributes Google's binary: this downloads their published
# release, applies 20 bytes of patches, and builds a small shim beside it.
#
# Not "#!/usr/bin/env bash": Android has no /usr/bin/env.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LIBDIR="$HERE/lib"
VERSION=1.2.7
KEEP=0
FROM_BINARY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --version) shift; VERSION="${1:?--version needs a value}" ;;
    --from-binary) shift; FROM_BINARY="${1:?--from-binary needs a path}" ;;
    --keep-download) KEEP=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { echo "install.sh: $*" >&2; exit 1; }

# --- preflight ---------------------------------------------------------------
[ "$(uname -m)" = "aarch64" ] || die "this targets aarch64; found $(uname -m)"
command -v curl    >/dev/null || die "curl is required (pkg install curl)"
command -v tar     >/dev/null || die "tar is required"
command -v cc      >/dev/null || die "a compiler is required (pkg install clang)"
command -v python3 >/dev/null || die "python3 is required (pkg install python)"

: "${PREFIX:=/data/data/com.termux/files/usr}"
LOADER="$PREFIX/lib/ld-musl-aarch64.so.1"
[ -x "$LOADER" ] || die "no musl loader at $LOADER
  Install it first — claude-code-termux-musl's install.sh fetches it from Alpine:
  https://github.com/Aarstad/claude-code-termux-musl"

# TCMalloc assumes a 48-bit virtual address space and aborts before main() on a
# 39-bit-VA kernel, which is most Android devices. That fix is not implemented
# here (see patch.py); refuse to build a binary that cannot start rather than
# leaving the user with a confusing TCMalloc abort.
if [ -z "$FROM_BINARY" ]; then
  top="$(awk 'END{split($1,a,"-"); print a[2]}' /proc/self/maps 2>/dev/null)"
  if [ -n "$top" ] && [ "${#top}" -le 10 ]; then
    die "this kernel uses a 39-bit user VA, where the stock binary aborts in
  TCMalloc before anything else runs. That patch is not implemented here.
  Start from wallentx's already-patched engine instead:

    curl -fsSL -o agy.tar.gz \\
      https://github.com/wallentx/antigravity-cli-termux/releases/download/v$VERSION/antigravity-termux-standalone.tar.gz
    tar xzf agy.tar.gz agy.va39
    ./install.sh --from-binary agy.va39"
  fi
fi


# --- fetch -------------------------------------------------------------------
ASSET="agy_cli_linux_arm64.tar.gz"
URL="https://github.com/google-antigravity/antigravity-cli/releases/download/$VERSION/$ASSET"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

if [ -n "$FROM_BINARY" ]; then
  [ -f "$FROM_BINARY" ] || die "no such file: $FROM_BINARY"
  say "using the binary given with --from-binary"
  cp "$FROM_BINARY" "$tmp/antigravity"
elif [ -f "$HERE/$ASSET" ]; then
  say "using the tarball already here"
  cp "$HERE/$ASSET" "$tmp/$ASSET"
else
  say "fetching Antigravity CLI $VERSION — about 58MB"
  curl -fsSL --retry 3 -o "$tmp/$ASSET" "$URL" \
    || die "could not download $URL"
  [ "$KEEP" = 1 ] && cp "$tmp/$ASSET" "$HERE/$ASSET"
fi

if [ -z "$FROM_BINARY" ]; then
  tar xzf "$tmp/$ASSET" -C "$tmp" || die "could not extract $ASSET"
fi
[ -f "$tmp/antigravity" ] || die "no 'antigravity' binary to patch"

# --- patch -------------------------------------------------------------------
# The google_find_phdr load-bias fix: 20 bytes, in place, file size unchanged.
# See FINDINGS.md and UPSTREAM-REPORT.md.
#
# NOTE: TCMalloc's 48-bit VA assumption is a separate problem and is NOT patched
# here. On a 39-bit-VA kernel (most Android devices) the stock binary aborts in
# TCMalloc before any of this matters. Until that patch is reimplemented, start
# from wallentx's already-VA39-patched engine:
#   https://github.com/wallentx/antigravity-cli-termux/releases
# and pass it with --from-binary.
say "patching google_find_phdr load bias"
# Build into a temp file and rename at the end: overwriting agy.bin in place
# fails with ETXTBSY if a copy is still running.
NEW="$HERE/.agy.bin.new.$$"
trap 'rm -rf "$tmp" "$NEW"' EXIT
cp "$tmp/antigravity" "$NEW"
chmod +x "$NEW"

python3 "$HERE/patch.py" "$NEW" || die "patching failed"

# --- musl linkage ------------------------------------------------------------
say "repointing the interpreter at musl"
command -v patchelf >/dev/null || pkg install -y patchelf >/dev/null 2>&1 \
  || die "patchelf is required (pkg install patchelf)"

patchelf --set-interpreter "$LOADER" "$NEW"
for n in libresolv.so.2 libpthread.so.0 libm.so.6 libdl.so.2 librt.so.1; do
  patchelf --remove-needed "$n" "$NEW" 2>/dev/null || true
done
patchelf --replace-needed libc.so.6 libc.musl-aarch64.so.1 "$NEW"

# --- shim --------------------------------------------------------------------
# Ten glibc-only symbols musl does not provide. Built against the musl loader,
# not bionic: a normal build links to bionic and dies on __register_atfork.
say "building the compatibility shim"
mkdir -p "$LIBDIR"
cc -shared -fPIC -O2 -nostdlib -o "$LIBDIR/libagyshim.so" "$HERE/shim.c" "$LOADER" \
  || die "could not build the shim"
patchelf --add-needed libagyshim.so "$NEW"

mv -f "$NEW" "$HERE/agy.bin" || die "could not install agy.bin (is a copy running?)"

# --- DNS proxy ---------------------------------------------------------------
# musl resolves through /etc/resolv.conf, which Android does not have, so DNS
# inside the process hangs. This proxy runs on the bionic side, where it works.
if [ -f "$HERE/dns-proxy.c" ]; then
  say "building the DNS proxy"
  cc -O2 -o "$HERE/dns-proxy" "$HERE/dns-proxy.c" || die "could not build the DNS proxy"
fi

# --- verify ------------------------------------------------------------------
v="$("$HERE/agy" --version 2>&1 || true)"
case "$v" in
  *[0-9].[0-9]*) say "installed: agy $v" ;;
  *) die "patched, but agy did not report a version (said: ${v:-nothing})" ;;
esac

rm -rf "$tmp"; trap - EXIT

echo
echo "  run it with:   $HERE/agy"
echo "  on your PATH:  ln -sf $HERE/agy ~/.local/bin/agy"
echo "  log in:        agy       (then follow the browser prompts)"
