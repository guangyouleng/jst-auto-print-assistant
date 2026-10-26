#!/bin/zsh
set -euo pipefail

ROOT_DIR="${0:A:h}"
cd "$ROOT_DIR"

VERSION="0.5.25"
PYTHON_BIN="${JST_MAC_BUILD_PYTHON:-$ROOT_DIR/.mac-build-venv/bin/python}"
DIST_ROOT="$ROOT_DIR/dist_macos"
BUILD_OUTPUT="$DIST_ROOT/pyinstaller-output"
WORK_DIR="$ROOT_DIR/.mac-build-work"
SPEC_DIR="$ROOT_DIR/.mac-build-spec"
STAGE_DIR="$DIST_ROOT/JSTAutoPrint_Mac_arm64_V${VERSION}"
ZIP_PATH="$DIST_ROOT/JSTAutoPrint_Mac_arm64_V${VERSION}.zip"
APP_NAME="聚水潭安全打单助手"
APP_PATH="$BUILD_OUTPUT/${APP_NAME}.app"
SELF_TEST_JSON="$DIST_ROOT/macos_self_test_V${VERSION}.json"

if [[ ! -f "$ROOT_DIR/jst_auto_print_app.py" || ! -f "$ROOT_DIR/jst_operator_config.json" ]]; then
  print -u2 "build root is incomplete: $ROOT_DIR"
  exit 2
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  print -u2 "missing build Python: $PYTHON_BIN"
  exit 2
fi
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  print -u2 "this package must be built on Apple Silicon macOS"
  exit 2
fi

rm -rf "$BUILD_OUTPUT" "$WORK_DIR" "$SPEC_DIR" "$STAGE_DIR"
rm -f "$ZIP_PATH" "$ZIP_PATH.sha256" "$SELF_TEST_JSON"
mkdir -p "$BUILD_OUTPUT" "$WORK_DIR" "$SPEC_DIR" "$STAGE_DIR"

"$PYTHON_BIN" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --collect-all tzdata \
  --name "$APP_NAME" \
  --osx-bundle-identifier "cn.erp321.jst-safe-auto-print" \
  --add-data "$ROOT_DIR/jst_operator_config.json:." \
  --distpath "$BUILD_OUTPUT" \
  --workpath "$WORK_DIR" \
  --specpath "$SPEC_DIR" \
  "$ROOT_DIR/jst_auto_print_app.py"

if [[ ! -d "$APP_PATH" ]]; then
  print -u2 "PyInstaller did not produce: $APP_PATH"
  exit 2
fi

find "$APP_PATH" -name jst_operator_config.json -type f -exec chmod 600 {} +
codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"

"$APP_PATH/Contents/MacOS/$APP_NAME" \
  --self-test \
  --self-test-output "$SELF_TEST_JSON"

"$PYTHON_BIN" - "$SELF_TEST_JSON" "$VERSION" <<'PY'
import json
import pathlib
import platform
import sys

path = pathlib.Path(sys.argv[1])
version = sys.argv[2]
payload = json.loads(path.read_text(encoding="utf-8"))
assert payload.get("version") == version, payload
assert payload.get("tkinter") is True, payload
assert payload.get("native_cdp") is True, payload
assert payload.get("self_test") == "PASS", payload
assert payload.get("errors") == [], payload
assert platform.machine() == "arm64", platform.machine()
PY

cp -R "$APP_PATH" "$STAGE_DIR/"
cp "$ROOT_DIR/聚水潭安全打单助手_使用说明.md" "$STAGE_DIR/使用说明_V${VERSION}.md"
cp "$ROOT_DIR/聚水潭安全打单助手_V${VERSION}_运行逻辑与流程图.md" \
  "$STAGE_DIR/运行逻辑与流程图_V${VERSION}.md"
cp "$ROOT_DIR/Mac版使用说明.md" "$STAGE_DIR/Mac版使用说明.md"
cp "$SELF_TEST_JSON" "$STAGE_DIR/离线自检结果.json"

ditto -c -k --sequesterRsrc --keepParent "$STAGE_DIR" "$ZIP_PATH"
shasum -a 256 "$ZIP_PATH" > "$ZIP_PATH.sha256"

print "MAC_APP=$STAGE_DIR/${APP_NAME}.app"
print "MAC_ZIP=$ZIP_PATH"
print "MAC_SHA256=$(awk '{print $1}' "$ZIP_PATH.sha256")"
