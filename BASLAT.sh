#!/bin/bash

# ==========================================================
# Hİ MARKET - Kasa (POS) & Stok Yönetim Sistemi Başlatıcı
# ==========================================================

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

clear
echo "=========================================================="
echo "          🛒 Hİ MARKET - POS & STOK OTOMASYONU           "
echo "=========================================================="
echo ""

PROJECT_DIR="/Users/mac/HiMarket"
DJANGO_PORT=8000
API_PORT=5000
DJANGO_URL="http://127.0.0.1:${DJANGO_PORT}/login/"
SWAGGER_URL="http://127.0.0.1:${API_PORT}/swagger"

if [ ! -d "$PROJECT_DIR" ]; then
    echo "❌ Hata: Proje dizini bulunamadı: $PROJECT_DIR"
    echo "Lütfen klasör yolunun doğru olduğundan emin olun."
    echo ""
    read -p "Çıkmak için Enter'a basın..."
    exit 1
fi

cd "$PROJECT_DIR"

# 1. Python Sanal Ortam (.venv) Kontrolü
PYTHON="$PROJECT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "📦 Python sanal ortamı (.venv) oluşturuluyor..."
    python3 -m venv "$PROJECT_DIR/.venv" || { echo "❌ venv oluşturulamadı!"; exit 1; }
fi

# 2. Python Paketleri Kontrolü
if ! "$PYTHON" -c 'import django, requests, PIL' >/dev/null 2>&1; then
    echo "📦 Gerekli Python paketleri kuruluyor..."
    "$PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt" || { echo "❌ Paketler yüklenemedi!"; exit 1; }
fi

# 3. Veritabanı Migration Kontrolü
echo "🔄 Veritabanı kontrol ediliyor..."
"$PYTHON" "$PROJECT_DIR/manage.py" migrate --noinput >/dev/null 2>&1

# 4. C# .NET API Kontrolü (Opsiyonel / Arka Plan Servisi)
if lsof -i :$API_PORT >/dev/null 2>&1; then
    echo "✅ C# .NET API zaten aktif (Port $API_PORT)"
else
    if command -v dotnet >/dev/null 2>&1; then
        echo "⚡ C# .NET 8 API arka planda başlatılıyor (Port $API_PORT)..."
        (
            cd "$PROJECT_DIR"
            dotnet run --project "$PROJECT_DIR/src/HiMarket.API/HiMarket.API.csproj" --urls "http://0.0.0.0:${API_PORT}" >/dev/null 2>&1 &
        )
    fi
fi

# 5. Yerel Ağ IP Tespiti (Tablet & Mobil Terminal için)
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "127.0.0.1")

# 6. Django POS Sunucusu Kontrolü
if lsof -i :$DJANGO_PORT >/dev/null 2>&1; then
    echo "✅ HiMarket sunucusu zaten aktif!"
    echo "🌐 Web arayüzü açılıyor: $DJANGO_URL"
    open "$DJANGO_URL"
    echo ""
    echo "----------------------------------------------------------"
    echo "Pencereyi kapatabilir veya arka planda tutabilirsiniz."
    echo "----------------------------------------------------------"
else
    echo "⚡ HiMarket POS ve Yönetim Sunucusu başlatılıyor..."
    echo ""
    echo "=========================================================="
    echo "  🚀 Sunucu Aktif!"
    echo "  🖥️  Bilgisayar:   $DJANGO_URL"
    echo "  📱 Tablet/Telefon: http://${LOCAL_IP}:${DJANGO_PORT}/login/"
    echo "  📋 C# API / Doc:  $SWAGGER_URL"
    echo ""
    echo "  🔑 Kasa Girişi:   KASA-01 | PIN: 1234"
    echo "  🔑 Admin Girişi:  admin   | Şifre: admin123"
    echo "=========================================================="
    echo ""

    # 2 saniye sonra otomatik tarayıcıyı aç
    (sleep 2 && open "$DJANGO_URL") &

    # Django sunucusunu tüm arayüzlere açarak başlat (Tabletler bağlanabilsin diye 0.0.0.0)
    "$PYTHON" "$PROJECT_DIR/manage.py" runserver 0.0.0.0:$DJANGO_PORT
fi
