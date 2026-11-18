# Hİ MARKET — Kurumsal Kasa (POS), Stok ve Market Yönetim Otomasyonu

**Hİ MARKET**, perakende satış noktaları (süpermarket, şarküteri, manav, kuruyemiş ve yerel zincir marketler) için sıfırdan tasarlanmış; **Python / Django** mimarisi, **SQLite WAL (Write-Ahead Logging)** eşzamanlı veritabanı modu, **atomik stok kilitleme (`select_for_update` & `F()`)**, **vardiya kapanışında otomatik veritabanı yedekleme (`backup_database`)** ve **ESC/POS sessiz termal fiş baskısı & RJ11 para çekmecesi tetiği** özelliklerine sahip uçtan uca bir Kasa Satış (POS) ve Market Yönetim Sistemidir.

---

## 📌 İçindekiler

1. [Mimari ve Teknoloji Yığını](#-mimari-ve-teknoloji-yığını)
2. [Temel Özellikler ve Modüller](#-temel-özellikler-ve-modüller)
3. [Veri Güvenliği, Eşzamanlılık ve Otomatik Yedekleme](#-veri-güvenliği-eşzamanlılık-ve-otomatik-yedekleme)
4. [Proje Dizin Yapısı](#-proje-dizin-yapısı)
5. [Kurulum ve Çalıştırma](#-kurulum-ve-çalıştırma)
6. [Varsayılan Giriş Bilgileri ve Rol Yapısı](#-varsayılan-giriş-bilgileri-ve-rol-yapısı)
7. [Kasa (POS) Klavye Kısayolları](#-kasa-pos-klavye-kısayolları)
8. [Test Paketi ve Doğrulama](#-test-paketi-ve-doğrulama)

---

## 🏗️ Mimari ve Teknoloji Yığını

| Katman | Teknoloji / Bileşen | Açıklama |
| :--- | :--- | :--- |
| **Backend & İş Mantığı** | **Python 3.10+ / Django 5.2+** | Tek doğruluk kaynağı (Single Source of Truth) olarak tüm POS satış, iade, vardiya, stok, cari ve yetkilendirme süreçlerini yönetir. |
| **Veritabanı & Eşzamanlılık** | **SQLite 3 (`WAL` Modu) / PostgreSQL** | `settings.py` ve `pos_app/apps.py` üzerinden `PRAGMA journal_mode=WAL;`, `synchronous=NORMAL` ve `timeout=30` ile çoklu kasa okuma/yazma kilitlenmelerini önler. `.env` üzerinden tek satırla PostgreSQL'e geçirilebilir. |
| **Atomik Stok & Bakiye** | **`transaction.atomic()` + `select_for_update()` + `F()`** | İki kasa aynı saniyede aynı ürünü okutup sattığında stok kaymasını (Race Condition) ve stokların eksiye düşmesini veritabanı satır kilidiyle engeller. |
| **Otomatik Yedekleme** | **`backup_database()` & Admin Yedek İndirme** | Z Raporu (vardiya kapanışı) alındığı anda `backups/` klasörüne zaman damgalı veritabanı yedeği alır; yönetici panelindeki **`Yedek İndir`** butonuyla tek tıkla indirilir. |
| **Donanım & Fiş Baskısı** | **ESC/POS Sessiz Termal Yazıcı Servisi** | `pos_app/printer_service.py` üzerinden tarayıcı `Ctrl+P` penceresine gerek kalmadan arka planda termal fiş basar (`GS V` otomatik kağıt kesme) ve nakit ödemede RJ11 para çekmecesini (`ESC p`) tetikler. |
| **Arayüz (Frontend)** | **Bootstrap 5.3 + Vanilla JS (ES6+)** | Dokunmatik POS monitörlerine, sayısal PIN tuş takımına (Numpad), el terminallerine ve masaüstü ekranlara tam uyumlu hızlı arayüz. |
| **Güvenlik** | **`.env` + Django CSRF + RBAC** | Tüm gizli anahtarlar `.env` dosyasındadır; tüm `POST` API uçlarında `X-CSRFToken` doğrulaması ve rol tabanlı erişim kontrolü zorunludur. |

---

## ✨ Temel Özellikler ve Modüller

### 1. Yüzde Yüz Bağımsız Çift Kasa Terminali (`KASA-01` & `KASA-02`)
* **Bağımsız Vardiya ve Z Raporu:** `KASA-01` ve `KASA-02` birbirinden tamamen bağımsız vardiyalarla çalışır. `KASA-01` gün sonu Z raporu alıp vardiyasını kapatsa bile `KASA-02` açık kalmaya ve kesintisiz satış yapmaya devam eder.
* **Sıfır Gecikmeli Barkod & Koli Barkodu Okuma:** Tekil ürün barkodları (`barcode`) ve ikincil koli barkodları (`secondary_barcode`) yerel veritabanından `< 1 ms` sürede sorgulanır; koli barkodu okutulduğunda koli içi adet (`package_quantity`) kadar sepete otomatik eklenir.
* **Terazi / Tartılı Ürün Barkodu Desteği (EAN-13):** `27` veya `28` ile başlayan 13 haneli terazi barkodlarını otomatik ayrıştırır (`27` + `5 hane ürün kodu` + `5 hane gramaj` + `kontrol hanesi`), ağırlığı kilograma çevirip birim fiyatla çarparak sepete ekler.
* **Dokunmatik Hızlı PIN Girişi (`1234`):** Giriş ekranındaki dokunmatik Numpad üzerinden kasa seçimi (`KASA-01` / `KASA-02`) yapılarak 4 haneli PIN kodu ile saniyeler içinde oturum açılır.

### 2. Kusursuz İade (`F8`) ve Kasa Mutabakatı
* **Stok ve Kasa Senkronizasyonu:** Müşteri iadesi alındığında iade edilen ürün miktarı otomatik olarak rafa (stoğa) geri eklenir.
* **Nakit / Kart Ayrımı:** Nakit iadeler aktif vardiyanın nakit toplamından, kredi kartı iadeleri ise POS kart toplamından düşülür; Z raporunda net ciro (`Toplam Satış - İadeler`) kuruşu kuruşuna doğru hesaplanır.
* **Mükerrer İade Koruması:** İade edilmiş bir fişin tekrar iade edilmesi sistem tarafından otomatik engellenir.

### 3. Dört Katmanlı Stok ve Toptancı (Cari) Yönetimi
1. **Tedarikçi Carileri (`Supplier`):** Toptancı bilgileri, fatura/irsaliye kaynaklı borçlanma, yapılan ödemeler (`SupplierPayment`) ve anlık bakiye takibi.
2. **Mal Kabul & İrsaliye Girişi (`StockEntry`):** `Liste Fiyatı × (1 - İskonto%) × (1 + KDV%)` formülüyle net birim maliyet hesaplama, otomatik stok artışı ve toptancı cari bakiyesinin atomik güncellenmesi.
3. **Ürün Kataloğu (`Product`):** Alış maliyeti, KDV oranı, satış fiyatı, kâr marjı (`%`), kritik stok eşiği ve hızlı POS buton (`is_quick_menu`) ayarları.
4. **Hızlı Fiyat Güncelleme & Etiket:** Tek ekrandan toplu veya tekil alış/satış fiyatı güncelleme ve raf etiketi yazdırma.

### 4. Sadeleştirilmiş 2 Rollü Personel Yönetimi (RBAC)
* **Kasiyer Rolü:** Yalnızca POS satış ekranına, barkod aramaya, satış ve iade işlemlerine erişebilir. Ürün alış maliyetlerini, kâr marjlarını, toptancı borçlarını ve yönetici ayarlarını göremez.
* **Yönetici Rolü:** Canlı Kasa 1 / Kasa 2 takip kartlarına, Z raporlarına, mal kabul ve cari işlemlere, personel yönetimine, işlem loglarına ve veritabanı yedek indirme ekranına tam erişime sahiptir.

### 5. Kasa ve Personel Filtreli Denetim Logları (`SystemLog`)
* Sistemdeki her satış, iade, vardiya açılış/kapanışı, stok girişi ve yedekleme işlemi `[KASA-01]` veya `[KASA-02]` etiketi, personel adı ve zaman damgasıyla kaydedilir; yönetici panelinden kasaya veya işlem tipine göre filtrelenebilir.

---

## 🛡️ Veri Güvenliği, Eşzamanlılık ve Otomatik Yedekleme

1. **SQLite WAL (Write-Ahead Logging) Modu:**
   - Veritabanı bağlantısı kurulduğu anda otomatik olarak şu direktifler çalıştırılır:
     ```sql
     PRAGMA journal_mode=WAL;
     PRAGMA synchronous=NORMAL;
     PRAGMA busy_timeout=30000;
     PRAGMA foreign_keys=ON;
     ```
   - Bu sayede Kasa 1 satış yaparken Kasa 2 barkod okuttuğunda veya içeriden mal kabul girildiğinde veritabanı kilitlenmez.
2. **Z Raporu Kapanışında Otomatik Yedekleme (`backup_database()`):**
   - Herhangi bir kasada Z Raporu kapatıldığı anda `backups/` dizinine `himarket_backup_YYYYMMDD_HHMMSS_KASA-XX.sqlite3` formatında zaman damgalı tam yedek otomatik kaydedilir (en güncel 50 yedek saklanır).
3. **Admin Panelinden Tek Tıkla `Yedek İndir`:**
   - Market sahibi veya yönetici, Admin panelinin sağ üst köşesindeki veya Dashboard üzerindeki **`Yedek İndir`** butonuna tıklayarak o anki canlı veritabanı yedeğini doğrudan kendi bilgisayarına veya USB belleğe indirebilir.

---

## 📂 Proje Dizin Yapısı

```text
Hİ MARKET/
├── backups/                     # Z Raporu kapanışlarında alınan otomatik zaman damgalı .sqlite3 yedekleri
├── himarket_project/            # Django ana proje yapılandırması
│   ├── settings.py              # .env, SQLite WAL / PostgreSQL, güvenlik ve uygulama ayarları
│   ├── urls.py                  # Ana URL yönlendirmeleri ve statik/medya dosya sunumu
│   └── wsgi.py / asgi.py        # Sunucu giriş noktaları
├── pos_app/                     # Ana POS ve Market Yönetim uygulaması
│   ├── apps.py                  # SQLite WAL modu ve PRAGMA sinyallerinin otomatik aktivasyonu
│   ├── models.py                # Product, Supplier, StockEntry, Sale, SaleItem,CashierShift, SystemLog
│   ├── views.py                 # POS, İade, Z Raporu, Yedekleme, Cari, Stok ve RBAC görünüm/API fonksiyonları
│   ├── printer_service.py       # ESC/POS sessiz termal fiş baskısı ve RJ11 para çekmecesi tetikleme servisi
│   ├── admin.py                 # Özelleştirilmiş Django Admin yapılandırması
│   ├── urls.py                  # Uygulama içi sayfa ve API uç noktaları
│   └── tests.py                 # 27 senaryoluk kapsamlı otomatik test paketi
├── templates/                   # HTML Şablonları (POS, Login PIN Pad, Z Raporu, Yönetim ve Admin sayfaları)
├── static/                      # CSS, JavaScript, ikonlar ve yerel kütüphaneler
├── .env.example                 # Örnek ortam değişkenleri şablonu (SECRET_KEY, DEBUG, Yazıcı, DB ayarları)
├── BASLAT.bat                   # Tek tıkla kurulum, veritabanı hazırlama ve sunucu başlatma betiği
├── manage.py                    # Django yönetim komutu aracı
└── requirements.txt             # Python bağımlılıkları
```

---

## 💻 Kurulum ve Çalıştırma

### Yöntem 1: Tek Tıkla Başlatma (Önerilen - Windows)
1. Bilgisayarınızda **Python 3.10 veya üzeri** sürümün yüklü olduğundan emin olun.
2. Proje ana dizinindeki **`BASLAT.bat`** dosyasına çift tıklayın.
   - Betik ilk çalıştırmada `requirements.txt` paketlerini kontrol eder, veritabanı tablolarını ve SQLite WAL modunu hazırlar, varsayılan hesapları oluşturur ve sunucuyu `8000` portunda başlatır.
3. Tarayıcınız otomatik olarak `http://127.0.0.1:8000/login/` adresini açacaktır.

### Yöntem 2: Terminal Üzerinden Manuel Çalıştırma
```powershell
# 1. Bağımlılıkları yükleyin
pip install -r requirements.txt

# 2. Ortam değişkenleri dosyasını (.env) oluşturun (isteğe bağlı, mevcut değilse .env.example'dan kopyalayın)
copy .env.example .env

# 3. Veritabanı tablolarını oluşturun
python manage.py migrate

# 4. Sunucuyu başlatın (Aynı ağdaki diğer kasa/cihazların erişimi için 0.0.0.0:8000)
python manage.py runserver 0.0.0.0:8000
```

---

## 🔑 Varsayılan Giriş Bilgileri ve Rol Yapısı

| Rol | Giriş Yöntemi | Kullanıcı Adı / PIN | Şifre | Yetki Kapsamı |
| :--- | :--- | :--- | :--- | :--- |
| **Kasiyer** | Dokunmatik PIN Pad veya Standart Giriş | PIN: **`1234`** (veya `kasiyer`) | **`1234`** | Kasa 1 / Kasa 2 seçimi, barkodlu satış, nakit/kart/parçalı tahsilat, müşteri iadesi (`F8`). |
| **Yönetici (Admin)** | Standart Giriş / Yönetim Paneli | **`admin`** | **`admin123`** | Tüm POS özellikleri + Canlı Kasa 1/2 Dashboard, Z Raporu & Vardiya Kapatma, Mal Kabul, Tedarikçi Cari, Fiyat Güncelleme, Personel Yönetimi, **`Yedek İndir`**. |

---

## ⌨️ Kasa (POS) Klavye Kısayolları

| Kısayol Tuşu | İşlev |
| :---: | :--- |
| **`F2` / Barkod Okuyucu** | Barkod arama kutusuna odaklanır ve okutulan ürünü anında sepete ekler. |
| **`F8`** | **Müşteri İade / Fiş İptal** penceresini açar (Fiş numarası ile tek tıkla iade). |
| **`F9`** | Sepeti **Nakit** ödeme ile tamamlar, sessiz fiş basar ve para çekmecesini açar. |
| **`F10`** | Sepeti **Kredi Kartı (POS)** ödeme ile tamamlar ve sessiz fiş basar. |
| **`ESC`** | Açık olan ödeme veya iade pencerelerini kapatır. |

---

## 🧪 Test Paketi ve Doğrulama

Projede çoklu kasa izolasyonu, atomik stok kilidi, iade-kasa-stok mutabakatı, SQLite WAL modu, Z raporu otomatik yedekleme, Admin yedek indirme, ESC/POS fiş üretimi, CSRF güvenliği, dokunmatik PIN girişi ve RBAC yetkilendirmesini doğrulayan **27 adet uçtan uca otomatik test** bulunmaktadır:

```powershell
# Sistem bütünlük kontrolü
python manage.py check

# 27 otomatik testin tamamını çalıştırma
python manage.py test pos_app -v 2
```

---

## 🔒 Lisans
Bu proje **Hİ MARKET** için özel olarak geliştirilmiştir. Tüm hakları saklıdır.
