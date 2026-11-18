@echo off
:: ==========================================================
:: Hİ MARKET - WINDOWS KIOSK TAM EKRAN BAŞLATICI
:: Kasiyerin adres çubuğunu veya Windows masaüstünü kurcalamasını engeller.
:: ==========================================================

set POS_URL=http://127.0.0.1:8000/pos/

echo [BILGI] Kasa Ekrani Kiosk Tam Ekran Modunda Aciliyor...

:: Microsoft Edge Kiosk
start msedge.exe --kiosk "%POS_URL%" --edge-kiosk-type=fullscreen --no-first-run --kiosk-printing

:: Alternatif: Chrome Kiosk (Edge yoksa)
if errorlevel 1 (
    start chrome.exe --kiosk --kiosk-printing --app="%POS_URL%" --disable-pinch
)

