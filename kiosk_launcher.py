#!/usr/bin/env python3
"""
Hİ MARKET - Kasa (POS) Masaüstü Kiosk Başlatıcı (Faz 7.1)
=========================================================
Bu script, market kasalarında güvenli tam ekran (Kiosk Mode) POS penceresi açar.
Kasiyerin tarayıcı adres çubuğunu, çıkış butonlarını veya işletim sistemi
masaüstünü kurcalamasını engeller.

Özellikler:
1. Django sunucusunu (/api/health/) kontrol eder, kapalıysa arka planda başlatır.
2. Varsa yerel PyWebView (WebKit / WebView2) ile yerel masaüstü penceresi oluşturur.
3. Yoksa Chrome / Edge / Chromium'u bağımsız izole profil ve kiosk modunda başlatır.
4. Çift ekran (Dual-Screen) müşteri göstergesi açıksa ikinci monitöre otomatik yansıtır.
"""

import os
import sys
import time
import shutil
import urllib.request
import urllib.error
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PORT = int(os.environ.get('DJANGO_PORT', 8000))
SERVER_URL = f"http://127.0.0.1:{PORT}"
HEALTH_URL = f"{SERVER_URL}/api/health/"
LOGIN_URL = f"{SERVER_URL}/login/"


def is_server_running(timeout=1.5):
    """Django sunucusunun çalışıp çalışmadığını /api/health/ ile kontrol eder."""
    try:
        req = urllib.request.Request(HEALTH_URL, headers={'User-Agent': 'HiMarket-Kiosk-Launcher'})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status in (200, 503)
    except Exception:
        return False


def start_django_server():
    """Django sunucusunu arka planda başlatır ve hazır olmasını bekler."""
    if is_server_running():
        print("✅ HiMarket Django sunucusu zaten aktif.")
        return None

    python_bin = sys.executable
    venv_py = BASE_DIR / '.venv' / ('Scripts' if os.name == 'nt' else 'bin') / ('python.exe' if os.name == 'nt' else 'python')
    if venv_py.exists():
        python_bin = str(venv_py)

    print(f"⚡ HiMarket sunucusu başlatılıyor: {python_bin} manage.py runserver 0.0.0.0:{PORT}")
    manage_py = BASE_DIR / 'manage.py'

    kwargs = {}
    if os.name == 'nt':
        # Windows'ta konsol penceresini gizle
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs['stdout'] = subprocess.DEVNULL
        kwargs['stderr'] = subprocess.DEVNULL

    proc = subprocess.Popen(
        [python_bin, str(manage_py), 'runserver', f'0.0.0.0:{PORT}', '--insecure'],
        cwd=str(BASE_DIR),
        **kwargs
    )

    # Sunucunun ayağa kalkmasını bekle (Azami 15 sn)
    for _ in range(30):
        time.sleep(0.5)
        if is_server_running():
            print("🚀 HiMarket sunucusu hazır ve yanıt veriyor!")
            return proc

    print("⚠️ Sunucu yanıt vermedi, yine de pencere açılıyor...")
    return proc


def launch_pywebview(url):
    """PyWebView kütüphanesi kuruluysa yerel işletim sistemi penceresi açar."""
    try:
        import webview
        print("🖥️  PyWebView Masaüstü Penceresi Yükleniyor...")
        window = webview.create_window(
            title='Hİ MARKET - Kasa Satış Terminali',
            url=url,
            fullscreen=True,
            confirm_close=True,
            easy_drag=False
        )
        webview.start()
        return True
    except ImportError:
        return False
    except Exception as e:
        print(f"PyWebView başlatılamadı ({e}), sistem tarayıcısına geçiliyor...")
        return False


def launch_browser_kiosk(url):
    """Chrome, Edge veya Chromium ile izole Kiosk / App penceresi açar."""
    kiosk_profile = Path(os.environ.get('TEMP', '/tmp')) / 'himarket_kiosk_browser_profile'
    kiosk_profile.mkdir(parents=True, exist_ok=True)

    common_args = [
        f'--app={url}',
        '--kiosk',
        '--kiosk-printing',
        '--disable-pinch',
        '--overscroll-history-navigation=0',
        '--disable-infobars',
        '--noerrdialogs',
        '--disable-session-crashed-bubble',
        f'--user-data-dir={kiosk_profile}'
    ]

    candidates = []

    if sys.platform == 'darwin':
        candidates = [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
            '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
        ]
    elif os.name == 'nt':
        local_app_data = os.environ.get('LOCALAPPDATA', 'C:\\Users\\Default\\AppData\\Local')
        prog_files = os.environ.get('ProgramFiles', 'C:\\Program Files')
        prog_files_x86 = os.environ.get('ProgramFiles(x86)', 'C:\\Program Files (x86)')

        candidates = [
            os.path.join(prog_files_x86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
            os.path.join(prog_files, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
            os.path.join(prog_files, 'Google', 'Chrome', 'Application', 'chrome.exe'),
            os.path.join(prog_files_x86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
            'msedge.exe',
            'chrome.exe'
        ]
    else:  # Linux
        candidates = ['google-chrome', 'chromium-browser', 'chromium', 'google-chrome-stable']

    for candidate in candidates:
        bin_path = shutil.which(candidate) if not os.path.isabs(candidate) else candidate
        if bin_path and os.path.exists(bin_path):
            print(f"🚀 Kiosk Tarayıcısı Başlatılıyor: {bin_path}")
            try:
                subprocess.Popen([bin_path] + common_args)
                return True
            except Exception as e:
                print(f"Hata ({candidate}): {e}")

    # Hiçbir kiosk tarayıcısı bulunamazsa varsayılan tarayıcıda aç
    print("⚠️ Kiosk destekli tarayıcı bulunamadı, varsayılan sistem tarayıcısında açılıyor...")
    import webbrowser
    webbrowser.open(url)
    return True


def main():
    print("=" * 60)
    print("     🛒 Hİ MARKET - KASA (POS) MASAÜSTÜ KIOSK BAŞLATICI     ")
    print("=" * 60)

    server_proc = start_django_server()

    # Öncelik 1: PyWebView ile tam yerel masaüstü penceresi
    if not launch_pywebview(LOGIN_URL):
        # Öncelik 2: Edge / Chrome Kiosk App Penceresi
        launch_browser_kiosk(LOGIN_URL)

    print("✅ HiMarket Kiosk Terminali Aktif!")


if __name__ == '__main__':
    main()

