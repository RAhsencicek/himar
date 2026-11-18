#!/usr/bin/env bash
# ==========================================================
# Hİ MARKET - macOS .app BUNDLE OLUŞTURUCU (Faz 7.2)
# ==========================================================

set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="HiMarket POS"
APP_DIR="${PROJECT_DIR}/${APP_NAME}.app"
CONTENTS_DIR="${APP_DIR}/Contents"
MACOS_DIR="${CONTENTS_DIR}/MacOS"
RESOURCES_DIR="${CONTENTS_DIR}/Resources"

echo "📦 ${APP_NAME}.app oluşturuluyor..."
rm -rf "${APP_DIR}"
mkdir -p "${MACOS_DIR}" "${RESOURCES_DIR}"

# 1. Simge Kopyala
if [ -f "${PROJECT_DIR}/HiMarket.icns" ]; then
    cp "${PROJECT_DIR}/HiMarket.icns" "${RESOURCES_DIR}/AppIcon.icns"
fi

# 2. Info.plist Oluştur
cat <<EOF > "${CONTENTS_DIR}/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>HiMarketLauncher</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>com.himarket.pos</string>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>2.6.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

# 3. Yürütülebilir Başlatıcı Script
cat <<EOF > "${MACOS_DIR}/HiMarketLauncher"
#!/usr/bin/env bash
cd "${PROJECT_DIR}"
exec /usr/bin/env bash "${PROJECT_DIR}/BASLAT.sh"
EOF

chmod +x "${MACOS_DIR}/HiMarketLauncher"
echo "✅ Başarılı! ${APP_DIR} hazır."

