#!/bin/zsh
# Builds "Schedule I Launcher.app" from ScheduleILauncher.swift and installs it in /Applications. The app uses the
# repository it's built from as its porting tool.
set -e
cd "${0:A:h}"
REPO=${0:A:h:h}
APP="build/Schedule I Launcher.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
xcrun swiftc -O -swift-version 5 -parse-as-library -target arm64-apple-macos14 \
    -o "$APP/Contents/MacOS/Schedule I Launcher" ScheduleILauncher.swift
ICON=$REPO/work/icon/ScheduleI.icns  # made by the port's ./build.sh
[[ -f $ICON ]] && cp "$ICON" "$APP/Contents/Resources/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Schedule I Launcher</string>
    <key>CFBundleDisplayName</key><string>Schedule I Launcher</string>
    <key>CFBundleIdentifier</key><string>local.scheduleI.launcher</string>
    <key>CFBundleExecutable</key><string>Schedule I Launcher</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF
/usr/libexec/PlistBuddy -c "Add :MacPortRepository string $REPO" "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"
rm -rf "/Applications/Schedule I Launcher.app"
cp -R "$APP" /Applications/
echo "installed /Applications/Schedule I Launcher.app"
