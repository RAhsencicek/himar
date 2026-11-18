#!/usr/bin/env bash
# ==========================================================
# Hİ MARKET - KIOSK TAM EKRAN BAŞLATICI (macOS / Linux)
# Kasiyerin tarayıcı adres çubuğunu veya işletim sistemini kurcalamasını engeller.
# ==========================================================

POS_URL="http://127.0.0.1:8000/pos/"

# Google Chrome veya Chromium tespiti
if [[ "$OSTYPE" == "darwin"* ]]; then
    CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
else
    CHROME_BIN=$(which google-chrome || which chromium-browser || which chromium)
fi

if [ -x "$CHROME_BIN" ]; then
    echo "🚀 Chrome Kiosk Modunda Başlatılıyor: $POS_URL"
    "$CHROME_BIN" \
        --kiosk \
        --kiosk-printing \
        --app="$POS_URL" \
        --disable-pinch \
        --overscroll-history-navigation=0 \
        --disable-infobars \
        --noerrdialogs \
        --user-data-dir="/tmp/himarket_kiosk_profile" &
else
    echo "⚠️ Chrome bulunamadı, varsayılan tarayıcıda açılıyor..."
    if command -v open >/dev/null 2>&1; then
        open "$POS_URL"
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$POS_URL"
    fi
fi

