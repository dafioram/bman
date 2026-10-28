#!/bin/sh
# Install bman: the launcher in PREFIX/bin, the program in PREFIX/lib/bman, and a
# default config in CONF_DIR (only when none exists). Never touches Btrfs or its data.
#
#   ./install.sh          ask before installing
#   ./install.sh --yes    no questions
#
# BMAN_PREFIX (default /usr/local) and BMAN_CONF_DIR (default /etc/bman) change
# where it goes, e.g. to test without root.
set -eu

APP=bman
PREFIX=${BMAN_PREFIX:-/usr/local}
CONF_DIR=${BMAN_CONF_DIR:-/etc/bman}
BIN="$PREFIX/bin/$APP"
LIB="$PREFIX/lib/$APP"
CONF="$CONF_DIR/$APP.conf"
MARKER=".$APP-install-marker"
TAG="# bman-launcher: installed by bman install.sh; uninstall.sh removes it"
SRC=$(cd "$(dirname "$0")" && pwd)

YES=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=1 ;;
        --help|-h)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "install.sh: unknown option $arg" >&2; exit 2 ;;
    esac
done

die() { echo "install.sh: $*" >&2; exit 1; }

ask() {
    # ask "question" default(Y|N) -> 0 for yes
    [ "$YES" -eq 1 ] && return 0
    printf '%s ' "$1"
    read -r answer || answer=""
    case "$answer" in
        [Yy]*) return 0 ;;
        [Nn]*) return 1 ;;
        "") [ "$2" = "Y" ] && return 0 || return 1 ;;
        *) return 1 ;;
    esac
}

command -v python3 >/dev/null 2>&1 || die "python3 is required (apt install python3)."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 3.9 or newer is required."
[ -f "$SRC/bman.py" ] && [ -f "$SRC/VERSION" ] && [ -d "$SRC/bmanlib" ] \
    || die "run it from the bman source folder."
VERSION=$(cat "$SRC/VERSION")

# Refuse to touch anything at our locations that we did not put there.
if [ -L "$LIB" ]; then die "$LIB is a symlink; not touching it."; fi
if [ -e "$LIB" ] && [ ! -f "$LIB/$MARKER" ]; then
    die "$LIB exists but was not installed by bman; not touching it."
fi
if [ -L "$BIN" ]; then die "$BIN is a symlink; not touching it."; fi
if [ -e "$BIN" ] && ! grep -qxF "$TAG" "$BIN" 2>/dev/null; then
    die "$BIN exists but is not bman's launcher; not touching it."
fi
if [ -L "$CONF" ] || [ -L "$CONF_DIR" ]; then die "$CONF is a symlink; not touching it."; fi

if [ -e "$LIB" ]; then
    old=$(cat "$LIB/VERSION" 2>/dev/null || echo "?")
    ask "Replace bman $old in $PREFIX with bman $VERSION? [Y/n]" Y || { echo "Nothing was changed."; exit 0; }
else
    ask "Install bman $VERSION to $PREFIX (config in $CONF_DIR)? [Y/n]" Y || { echo "Nothing was changed."; exit 0; }
fi

mkdir -p "$PREFIX/bin" "$PREFIX/lib" || die "cannot create folders in $PREFIX (run with sudo?)"
[ -w "$PREFIX/lib" ] && [ -w "$PREFIX/bin" ] || die "no write access to $PREFIX (run with sudo?)"

# Stage next to the target, check it runs, then swap it in.
STAGE=$(mktemp -d "$PREFIX/lib/.$APP-stage.XXXXXX")
OLD=""
cleanup() {
    rm -rf "$STAGE"
    if [ -n "$OLD" ] && [ -d "$OLD" ]; then rm -rf "$OLD"; fi
}
trap cleanup EXIT

cp "$SRC/bman.py" "$SRC/VERSION" "$STAGE/"
(cd "$SRC" && find bmanlib -type f -name '*.py') | while read -r f; do
    mkdir -p "$STAGE/$(dirname "$f")"
    cp "$SRC/$f" "$STAGE/$f"
done
echo "bman installed this folder; uninstall.sh removes it." > "$STAGE/$MARKER"
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
chmod 755 "$STAGE"

got=$(PYTHONDONTWRITEBYTECODE=1 python3 "$STAGE/bman.py" --version 2>&1) || die "the staged copy does not run: $got"
[ "$got" = "bman $VERSION" ] || die "the staged copy reports '$got', expected 'bman $VERSION'."

if [ -e "$LIB" ]; then
    OLD="$PREFIX/lib/.$APP-old.$$"
    mv "$LIB" "$OLD"
fi
mv "$STAGE" "$LIB"

LAUNCH=$(mktemp "$PREFIX/bin/.$APP-launcher.XXXXXX")
cat > "$LAUNCH" <<EOF
#!/bin/sh
$TAG
exec python3 $LIB/bman.py "\$@"
EOF
chmod 755 "$LAUNCH"
mv "$LAUNCH" "$BIN"

if [ ! -e "$CONF" ]; then
    mkdir -p "$CONF_DIR"
    python3 - "$SRC" "$CONF" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
sys.dont_write_bytecode = True
from bmanlib.config import DEFAULT_TEXT
with open(sys.argv[2], "w") as f:
    f.write(DEFAULT_TEXT)
EOF
    chmod 644 "$CONF"
    echo "Wrote the default config to $CONF."
else
    echo "Kept the existing config $CONF."
fi

echo "Installed bman $VERSION. Run: $APP"
