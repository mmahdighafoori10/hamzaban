#!/bin/sh
# Build on macOS with Xcode Command Line Tools and Python 3.10+.
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output=${1:-"$repo/dist"}
version=$(cd "$repo" && python3 -c 'import pathlib,re; text=(pathlib.Path("engine")/"hamzaban"/"__init__.py").read_text(); print(re.search(r"^VERSION = \"([^\"]+)\"", text, re.M).group(1))')
app="$output/Hamzaban.app"
iconset="$output/.Hamzaban.iconset"
stage="$output/.dmg-stage"
dmg="$output/Hamzaban-$version-macOS.dmg"

mkdir -p "$output"
rm -rf "$app" "$iconset" "$stage"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/payload" "$iconset" "$stage"
cp "$repo/macos/Info.plist" "$app/Contents/Info.plist"
plutil -lint "$app/Contents/Info.plist"
swiftc -O -framework AppKit -framework WebKit "$repo/macos/HamzabanApp.swift" -o "$app/Contents/MacOS/Hamzaban"
ditto "$repo/engine" "$app/Contents/Resources/payload/engine"
ditto "$repo/skills" "$app/Contents/Resources/payload/skills"
ditto "$repo/install.py" "$app/Contents/Resources/payload/install.py"

source_icon="$repo/engine/assets/hamzaban-256.png"
for size in 16 32 64 128 256; do
  sips -s format png -z "$size" "$size" "$source_icon" --out "$iconset/icon_${size}x${size}.png" >/dev/null
done
for size in 16 32 64 128 256; do
  doubled=$((size * 2))
  sips -s format png -z "$doubled" "$doubled" "$source_icon" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$app/Contents/Resources/Hamzaban.icns"
codesign --force --sign - "$app"
codesign --verify --strict --verbose=2 "$app"
ditto "$app" "$stage/Hamzaban.app"
ln -s /Applications "$stage/Applications"
hdiutil create -volname "Hamzaban" -srcfolder "$stage" -ov -format UDZO "$dmg"
hdiutil verify "$dmg"
printf 'DMG: %s\n' "$dmg"
