#!/bin/sh
# install → reinstall → uninstall into a temporary prefix, plus the refusals.
# Needs no root and touches nothing outside its temporary folder.
set -eu

SRC=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
export BMAN_PREFIX="$WORK/prefix" BMAN_CONF_DIR="$WORK/etc/bman"
export PYTHONDONTWRITEBYTECODE=1

fail() { echo "FAIL: $*" >&2; exit 1; }
check() { "$@" || fail "$*"; }

# 1. Fresh install.
sh "$SRC/install.sh" --yes >/dev/null
check test -x "$BMAN_PREFIX/bin/bman"
check test -f "$BMAN_PREFIX/lib/bman/.bman-install-marker"
check test -f "$BMAN_CONF_DIR/bman.conf"
v=$("$BMAN_PREFIX/bin/bman" --version)
[ "$v" = "bman $(cat "$SRC/VERSION")" ] || fail "version after install: $v"

# The installed copy must not depend on the source folder.
copy="$WORK/src"
cp -R "$SRC" "$copy"
rm -rf "$copy/.git"
next=$(( $(cat "$SRC/VERSION") + 1 ))
echo "$next" > "$copy/VERSION"
echo "# kept" >> "$BMAN_CONF_DIR/bman.conf"

# 2. Reinstall a newer version in place, from another folder; the config is kept.
sh "$copy/install.sh" --yes >/dev/null
v=$("$BMAN_PREFIX/bin/bman" --version)
[ "$v" = "bman $next" ] || fail "version after reinstall: $v"
grep -q "# kept" "$BMAN_CONF_DIR/bman.conf" || fail "reinstall replaced the config"
rm -rf "$copy"
v=$("$BMAN_PREFIX/bin/bman" --version)
[ "$v" = "bman $next" ] || fail "installed copy depends on its source folder"
for leftover in "$BMAN_PREFIX"/lib/.bman-stage.* "$BMAN_PREFIX"/lib/.bman-old.*; do
    [ -e "$leftover" ] && fail "staging folder left behind: $leftover"
done

# 3. Not a terminal → exit 2 with the standard message.
set +e
msg=$("$BMAN_PREFIX/bin/bman" </dev/null 2>&1 >/dev/null)
rc=$?
set -e
[ "$rc" -eq 2 ] || fail "non-terminal run exited $rc"
[ "$msg" = "bman is interactive; run it in a terminal." ] || fail "non-terminal message: $msg"

# 4. Uninstall with --purge removes everything it made.
sh "$SRC/uninstall.sh" --yes --purge >/dev/null
[ -e "$BMAN_PREFIX/bin/bman" ] && fail "launcher left behind"
[ -e "$BMAN_PREFIX/lib/bman" ] && fail "program left behind"
[ -e "$BMAN_CONF_DIR/bman.conf" ] && fail "config left behind"

# 5. Refuse to overwrite or remove files bman did not create.
mkdir -p "$BMAN_PREFIX/bin"
echo "#!/bin/sh" > "$BMAN_PREFIX/bin/bman"
echo "echo someone else's bman" >> "$BMAN_PREFIX/bin/bman"
if sh "$SRC/install.sh" --yes >/dev/null 2>&1; then fail "install overwrote a foreign launcher"; fi
grep -q "someone else" "$BMAN_PREFIX/bin/bman" || fail "foreign launcher was changed"
if sh "$SRC/uninstall.sh" --yes >/dev/null 2>&1; then fail "uninstall removed a foreign launcher"; fi
check test -f "$BMAN_PREFIX/bin/bman"
rm "$BMAN_PREFIX/bin/bman"

mkdir -p "$BMAN_PREFIX/lib/bman"
echo "data" > "$BMAN_PREFIX/lib/bman/important"
if sh "$SRC/install.sh" --yes >/dev/null 2>&1; then fail "install overwrote a foreign folder"; fi
check test -f "$BMAN_PREFIX/lib/bman/important"
rm -rf "$BMAN_PREFIX/lib/bman"

ln -s /tmp "$BMAN_PREFIX/lib/bman"
if sh "$SRC/install.sh" --yes >/dev/null 2>&1; then fail "install followed a symlink"; fi
rm "$BMAN_PREFIX/lib/bman"

# 6. A config written by someone else is kept even with --purge.
sh "$SRC/install.sh" --yes >/dev/null
echo "[bman]" > "$BMAN_CONF_DIR/bman.conf"
sh "$SRC/uninstall.sh" --yes --purge >/dev/null
check test -f "$BMAN_CONF_DIR/bman.conf"

echo "install cycle: ok"
