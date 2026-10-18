#!/usr/bin/env bash
# Apply the committed Kahin patch series to a Camoufox recipe source tree,
# build it, package the COMPLETE distribution (fonts/properties/fontconfig),
# and install it under $KAHIN_HOME/engines.
set -euo pipefail

SRC=${1:?usage: build-camoufox-patched.sh <camoufox-source-dir>}
SRC=$(cd "$SRC" && pwd)
RECIPE=$(cd "$SRC/.." && pwd)
HERE=$(cd "$(dirname "$0")" && pwd)
PATCH_DIR=$HERE/../patches/camoufox
KAHIN_HOME=${KAHIN_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/kahin}
ENGINES=$KAHIN_HOME/engines
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/camoufox

[ -d "$PATCH_DIR" ] || { echo "patch dir not found: $PATCH_DIR" >&2; exit 1; }

echo "== dry-run patches against $SRC"
for patch in "$PATCH_DIR"/0*.patch; do
  echo "-- $(basename "$patch")"
  patch -p1 -d "$SRC" --dry-run --forward <"$patch"
done

echo "== apply patches"
for patch in "$PATCH_DIR"/0*.patch; do
  patch -p1 -d "$SRC" --forward <"$patch"
done

echo "== build"
(cd "$SRC" && ./mach build)

OBJ=$(cd "$SRC" && printf '%s\n' obj-*/ | head -1)
OBJ=${OBJ%/}
APP_INI="$SRC/$OBJ/dist/bin/application.ini"
FULL=$(sed -n 's/^Version=//p' "$APP_INI" | head -1)
VERSION=${FULL%%-*}
BUILD=${FULL#*-}

if [ -f "$RECIPE/scripts/package.py" ]; then
  echo "== package (Camoufox recipe: fonts, properties.json, fontconfig)"
  (cd "$RECIPE" && python3 scripts/package.py linux \
    --includes settings/chrome.css settings/camoucfg.jvv settings/properties.json bundle/fontconfig \
    --version "$VERSION" --release "$BUILD" --arch "$(uname -m)" \
    --fonts windows macos linux)
  ARCHIVE=$(ls -t "$RECIPE"/camoufox-"$VERSION"-"$BUILD"-*.zip | head -1)
else
  echo "== package (mach; WARNING: Camoufox assets may be missing)"
  (cd "$SRC" && ./mach package)
  ARCHIVE=$(ls -t "$SRC/$OBJ"/dist/*.tar.* | head -1)
fi
[ -f "$ARCHIVE" ] || { echo "package not found" >&2; exit 1; }

STAMP=$(date -u +%Y%m%d%H%M%S)
DEST="$ENGINES/$VERSION-$BUILD-$STAMP"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
case "$ARCHIVE" in
  *.zip) 7z x -y -o"$TMP/pkg" "$ARCHIVE" >/dev/null ;;
  *)     mkdir -p "$TMP/pkg" && tar -xf "$ARCHIVE" -C "$TMP/pkg" ;;
esac
PKG_ROOT=$(printf '%s\n' "$TMP"/pkg/*/ "$TMP"/pkg/ | head -1)
PKG_ROOT=${PKG_ROOT%/}

mkdir -p "$ENGINES"
[ ! -e "$DEST" ] || { echo "refusing to overwrite $DEST" >&2; exit 1; }
cp -a "$PKG_ROOT" "$DEST"
printf '{"version":"%s","build":"%s","local_build":true,"source":"%s"}\n' \
  "$VERSION" "$BUILD" "$(basename "$ARCHIVE")" >"$DEST/version.json"
touch "$DEST/.0.5_FLAG"

# Camoufox's launch_options resolves properties.json/fontconfig through its
# INSTALL_DIR (~/.cache/camoufox). Keep only metadata + symlinks there; the
# browser itself stays in $KAHIN_HOME/engines.
mkdir -p "$CACHE/addons"
touch "$CACHE/.0.5_FLAG"
cp "$DEST/version.json" "$CACHE/version.json"
ln -sfn "$DEST/properties.json" "$CACHE/properties.json"
ln -sfn "$DEST/fontconfig" "$CACHE/fontconfig"
ln -sfn "$DEST/fonts" "$CACHE/fonts"

ln -sfn "$(basename "$DEST")" "$ENGINES/current"
echo "installed: $DEST"
echo "selected:  $ENGINES/current"
"$ENGINES/current/camoufox-bin" --version
