@echo off
setlocal enabledelayedexpansion
title HI MARKET - POS VE STOK YONETIM SISTEMI
cd /d "%~dp0"

echo ==========================================================
echo          HI MARKET - KASA VE YONETIM SISTEMI
echo          (Django SSOT + SQLite WAL + Otomatik Yedek)
echo ==========================================================
echo.

:: 1. Python kontrol ediliyor
set "PY_CMD="
where python >nul 2>&1
if !errorlevel! equ 0 (
    set "PY_CMD=python"
) else (
    where py >nul 2>&1
    if !errorlevel! equ 0 (
        set "PY_CMD=py"
    )
)

if "%PY_CMD%"=="" (
    echo.
    echo [HATA] Bilgisayarinizda Python kurulu bulunamadi!
    echo Lutfen https://www.python.org/downloads/ adresinden Python kurunuz.
    echo Kurulum yaparken "Add Python to PATH" secenegini isaretlemeyi unutmayiniz.
    echo.
    pause
    exit /b 1
)

:: 2. Python paketleri ve veritabani kontrolu
%PY_CMD% -c "import django" >nul 2>&1
if !errorlevel! neq 0 (
    echo.
    echo [BILGI] Ilk calistirma tespit edildi. Gerekli paketler yukleniyor...
    %PY_CMD% -m pip install -r "%~dp0requirements.txt"
)

echo 1. Veritabani tablolari ve SQLite WAL modu kontrol ediliyor...
%PY_CMD% "%~dp0manage.py" migrate --noinput >nul 2>&1

:: 3. Tarayici acilisi hazirlaniyor
echo 2. Tarayici acilisi hazirlaniyor...
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8000/login/'"

:: 4. Wi-Fi Yerel IP Adresi Tespiti
set "LOCAL_IP="
for /f "usebackq tokens=*" %%a in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object InterfaceAlias -notlike '*Loopback*' | Where-Object IPAddress -like '192.168.*' | Select-Object -ExpandProperty IPAddress -First 1)"`) do set "LOCAL_IP=%%a"
if "%LOCAL_IP%"=="" set "LOCAL_IP=127.0.0.1"

echo.
echo 3. HI MARKET POS Sunucusu baslatiliyor (Port: 8000)...
echo.
echo ==========================================================
echo   Sunucu aktif ediliyor!
echo   Bilgisayar Ekrani: http://127.0.0.1:8000/login/
echo   Telefon / Wi-Fi:   http://%LOCAL_IP%:8000/login/
echo.
echo   Kasiyer Hizli PIN: 1234
echo   Yonetici Giris:    admin / admin123
echo.
echo   BU PENCEREYI KAPATMAYINIZ!
echo ==========================================================
echo.

%PY_CMD% "%~dp0manage.py" runserver 0.0.0.0:8000 --insecure

if !errorlevel! neq 0 (
    echo.
    echo [HATA] Sunucu baslatilamadi!
    echo Lutfen yukaridaki hata mesajini kontrol ediniz.
    pause
)
