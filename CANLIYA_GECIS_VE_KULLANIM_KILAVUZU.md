# 🛒 Hİ MARKET - CANLIYA GEÇİŞ, KURULUM VE İŞLETİM KILAVUZU
**Sürüm:** 2.6.0 Enterprise | **Yasal Uyumluluk:** VUK 507, GİB e-Arşiv Fatura, ÖKC Mevzuatı  
**Hedef Kitle:** Market Sahipleri, Mağaza Müdürleri, Muhasebe Yetkilileri ve Kasiyerler

---

## 📌 İÇİNDEKİLER
1. [Sistem Mimarisi ve Yasal Uyumluluk](#1-sistem-mimarisi-ve-yasal-uyumluluk)
2. [Kurulum ve İlk Çalıştırma](#2-kurulum-ve-ilk-çalıştırma)
3. [Kasa Operasyonları Rehberi (Kasiyer Eğitimi)](#3-kasa-operasyonları-rehberi-kasiyer-eğitimi)
4. [Depo, Stok ve Envanter Yönetimi](#4-depo-stok-ve-envanter-yönetimi)
5. [Akşam Gün Sonu (Z Raporu) ve Kasa Kapanışı](#5-akşam-gün-sonu-z-raporu-ve-kasa-kapanışı)
6. [Donanım ve Çevre Birimleri Bağlantı Rehberi](#6-donanım-ve-çevre-birimleri-bağlantı-rehberi)
7. [Yedekleme, Güvenlik ve Felaket Kurtarma](#7-yedekleme-güvenlik-ve-felaket-kurtarma)
8. [Sıkça Sorulan Sorular ve Sorun Giderme](#8-sıkça-sorulan-sorular-ve-sorun-giderme)

---

## 1. SİSTEM MİMARİSİ VE YASAL UYUMLULUK

HiMarket, Türkiye perakende mevzuatına (%1, %10, %20 KDV dilimleri, VUK 507 güvenli mali satış formatı, GİB e-Arşiv karekod ve ETTN UUID standartları) %100 uyumlu, yüksek performanslı ve çift veritabanı motoru (SQLite WAL & PostgreSQL) destekleyen profesyonel bir süpermarket otomasyonudur.

- **Tek Doğruluk Kaynağı (SSOT):** Tüm satışlar, stok hareketleri, terazi tartımları ve veresiye hareketleri eşzamanlı olarak merkezi Django çekirdeğinde işlenir.
- **Çoklu Kasa Mimarisi:** Mağazada aynı anda Kasa 1, Kasa 2, El Terminali ve Arka Ofis Yönetim bilgisayarı eşzamanlı çalışır. SQLite WAL modu sayesinde `database is locked` hatası sıfırlanmıştır.
- **Dinamik İşletme Ayarları:** Vergi no, unvan, fiş altı mesajları, terazi portları ve kör sayım tercihleri kod yazmadan arayüzden yönetilir.

---

## 2. KURULUM VE İLK ÇALIŞTIRMA

### A. Windows Sistemler (Market POS PC)
1. Proje ana klasöründeki **`BASLAT.bat`** dosyasına çift tıklayınız.
2. Script arka planda Python paketlerini, veritabanı migrasyonlarını kontrol eder ve sistemi başlatır.
3. Tarayıcı tam ekran kiosk modunda Kasa Giriş ekranını (`http://127.0.0.1:8000/login/`) açacaktır.
4. *İsteğe bağlı:* `kiosk_launcher.py` veya `deployment/himarket_setup.iss` ile Windows başlangıcına otomatik açılış eklenebilir.

### B. macOS Sistemler
1. `HiMarket POS.app` uygulamasına çift tıklayarak doğrudan başlatabilirsiniz.
2. Veya terminalden:
   ```bash
   ./BASLAT.sh
   ```

### C. Linux / Dokunmatik POS Terminalleri (Ubuntu / Debian / Raspberry Pi)
1. `deployment/himarket.service` dosyasını `/etc/systemd/system/` altına kopyalayınız:
   ```bash
   sudo cp deployment/himarket.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now himarket.service
   ```
2. Masaüstü kısayollarını `~/.local/share/applications/` altına ekleyerek dokunmatik masaüstünden tek tıkla açabilirsiniz.

### Varsayılan Giriş Bilgileri:
- **Kasa / Kasiyer Girişi:** `KASA-01` | Hızlı PIN: `1234`
- **Yönetici Girişi:** Kullanıcı: `admin` | Şifre: `admin123`

---

## 3. KASA OPERASYONLARI REHBERİ (KASİYER EĞİTİMİ)

### 3.1. Hızlı Satış ve Barkod Okutma
- El tipi barkod okuyucu veya masaüstü lazer okuyucu ile ürün barkodunu okutunuz. Ürün sepete anında eklenir.
- **Miktar Değiştirme:** Klavyeden `*` veya dokunmatik ekrandaki miktar butonuna tıklayarak adet belirleyebilirsiniz (Örn: `6 * [Barkod]` = 6 koli).
- **Ağırlıklı / Manav Ürünü Tartımı:**
  - RS232 bağlantılı elektronik terazi varsa, ürünü terazinin üzerine koyup ekrandaki **"Terazi Tart [F5]"** butonuna basınız. Darası düşülmüş net gramaj doğrudan sepete aktarılır.
  - Terazi bağlı değilse dokunmatik ekrandan gramajı el ile yazabilirsiniz.

### 3.2. Ödeme Alma Türleri
1. **Nakit Ödeme:**
   - Müşteriden alınan parayı giriniz (Hazır `10`, `20`, `50`, `100`, `200 TL` veya `Tam Para` butonları).
   - Sistem para üstünü dev mavi ekranda gösterir, para çekmecesini otomatik fırlatır ve fişi termal yazıcıdan basar.
2. **Kredi Kartı:**
   - POS cihazından çekim yapıldıktan sonra "Kredi Kartı" butonuna basılır.
3. **Parçalı Ödeme (Split Payment):**
   - Tutarın bir kısmı nakit, kalanı kredi kartı ile ödenecekse "Parçalı Ödeme" penceresi açılır. Kalan bakiye anında hesaplanır.
4. **Veresiye Satış (Müşteri Defteri / Cari):**
   - "Veresiye / Cari" butonuna basılır. Müşteri adı veya telefonuyla aranır.
   - Müşterinin bakiye ve kredi limiti ekranda görünür. Limit aşımı yoksa satış onaylanır ve borç hesabına yazılır.

### 3.3. Çoklu Müşteri / Fiş Askıya Alma (F6)
- Sıradaki müşteri cüzdanını unuttuysa veya ek ürün almaya gittiyse:
  - **"Askıya Al [F6]"** butonuna basınız. Fiş güvenle beklemeye alınır.
  - Yeni müşterinin satışını tamamlayınız.
  - Ardından **"Askıdaki Fişler"** butonundan önceki sepeti tek tıkla geri çağırıp kaldığınız yerden devam ediniz.

### 3.4. Yetkili Onayı (Supervisor Override)
- Kasiyer sepetteki bir satırı silmek istediğinde, sepeti tamamen iptal etmek istediğinde veya iade moduna geçmek istediğinde:
  - Ekrana kırmızı renkli **"Yetkili Onayı Zorunlu"** modalı açılır.
  - Mağaza müdürü PIN / şifresini tuşlayarak işlemi onaylar.
  - Tüm yetkili onayları güvenlik loglarına kaydedilir.

### 3.5. Çevrimdışı Kasa Güvencesi (Offline-First)
- Mağazanın ana sunucusu veya Wi-Fi ağı kopsa dahi kasa kilitlenmez!
- Satış yerel belleğe (`OFFLINE-BUFFER`) kaydedilir, sıradaki müşteriye geçilir.
- Ağ bağlantısı sağlandığında sistem arkada kuyruktaki satışları otomatik olarak ana veritabanına iletir ve stokları günceller.

---

## 4. DEPO, STOK VE ENVANTER YÖNETİMİ

### 4.1. Toptancı Mal Kabulü & İrsaliye Girişi
- **Yol:** `Stok & İrsaliye` $\rightarrow$ `Yeni Mal Kabul / İade / Fire`
- Toptancı cari hesabı seçilir, fatura/irsaliye numarası girilir.
- Barkod okuyucuyla mallar sepete eklenir; alış fiyatı, KDV oranı ve iskonto girilir.
- Onaylandığında ürün stokları artar, birim maliyet güncellenir ve toptancı carisine borç otomatik yazılır.

### 4.2. Fire / Hasar / SKT Zayiat Çıkışı
- Kırılan, bozulan veya tarihi geçmiş ürünler için işlem türü **"Fire / Zayi"** seçilir.
- Ürünler stoktan düşer. **Toptancı bakiyesine kesinlikle borç yazılmaz;** işletme zayiat gideri olarak kaydedilir.

### 4.3. El Terminali ile Barkodlu Stok Sayımı
- **Yol:** `Stok & İrsaliye` $\rightarrow$ `Sayım Fişleri (El Terminali)`
- Reyon bazlı (Örn: "Meşrubat Reyonu Sayımı") fiş açılır.
- El terminali ile raflardaki ürünler seri şekilde okutulur. Web Audio API sayesinde her başarılı okutmada yüksek sesli teyit bipi çalar.
- Ekran canlı olarak:
  - Sistem Stoğu vs Sayılan Fiziki Stoğu kıyaslar.
  - Fazla çıkanları yeşil (+), eksik çıkanları kırmızı (-) rozetle ve maliyet tutarıyla gösterir.
- **Sayımı Onayla ve Eşitle:** Yönetici butona bastığında sistemdeki stok miktarları fiziki sayıma eşitlenir ve mali denetim için otomatik **`SAYIM_DUZELTME`** fişi kesilir.

### 4.4. Kritik Stok & WhatsApp Sipariş Motoru
- **Yol:** `Stok & İrsaliye` $\rightarrow$ `Kritik Stok & Sipariş`
- Asgari stok seviyesinin altına düşen ürünler toptancılarına göre otomatik gruplanır.
- Önerilen sipariş miktarı formülüyle eksikler belirlenir.
- **Tek Tıkla WhatsApp Siparişi:** Toptancının cep telefonuna hazır sipariş metni gönderilir.
- **A4 Satın Alma Sipariş Formu:** Resmi kağıt veya PDF çıktısı alınabilir.

---

## 5. AKŞAM GÜN SONU (Z RAPORU) VE KASA KAPANISI

### 5.1. Kör Kasa Sayımı (Blind Shift Closing)
- Kasiyer akşam vardiyayı kapatırken beklenen nakit tutarı ve kasa farkını göremez (Hırsızlık ve manipülasyon önleme).
- Kasiyer çekmecedeki parayı banknot sayacına (200, 100, 50, 20, 10, 5 TL ve madeni para adetleri) göre sayıp girer.
- Kasiyer "Vardiyayı Kapat" dediğinde kasa kapanır.
- Gerçek kasa farkını (Açık / Fazla) yalnızca **Mağaza Müdürü** kendi ekranında görür.

### 5.2. Otomatik Yedekleme
- Z raporu kapandığı saniyede sistem otomatik olarak `backups/` klasörüne zaman damgalı, sıkıştırılmış (GZIP) ve SHA-256 imzalı veritabanı yedeği kaydeder.

---

## 6. DONANIM VE ÇEVRE BİRİMLERİ BAĞLANTI REHBERİ

| Donanım | Bağlantı Türü | Desteklenen Standart / Protokol |
| :--- | :--- | :--- |
| **Termal Fiş Yazıcı** | USB / Ethernet / Wi-Fi | 80mm ESC/POS (Port 9100 / Raw Spool) |
| **Elektronik Terazi** | RS232 COM / USB-Seri | CAS PD-II, TEM, Toledo (9600 baud, 8N1) |
| **Müşteri Ekranı (2. Ekran)** | HDMI / VGA / Dual Screen | Canlı Web BroadcastChannel senkronizasyonu |
| **Barkod / Raf Etiketi** | USB Termal Barkod Yazıcı | TSPL (Argox/TSC) ve ZPL (Zebra) 58x40 mm |
| **Barkod Okuyucu** | USB HID / Kablosuz 2.4G | EAN-13, EAN-8, Code 128, QR Code |
| **Nakit Para Çekmecesi** | RJ11 / RJ12 (Yazıcı Portu) | 24V Pin 2 kick darbeli otomatik fırlatma |

---

## 7. YEDEKLEME, GÜVENLİK VE FELAKET KURTARMA

### 7.1. Bağımsız Yönetim Komutu ile Yedek Alma
İstediğiniz zaman terminalden veya zamanlanmış görevden tek satırla yedek alabilirsiniz:
```bash
python manage.py backup_database --compress --keep 30 --reason MANUEL_YEDEK
```
- `--compress`: Dosyayı GZIP ile sıkıştırır (.gz).
- `--keep 30`: En son 30 yedeği saklar, eskileri temizler.

### 7.2. Felaket Kurtarma (Veritabanı Geri Yükleme)
Bilgisayar veya disk arızası durumunda:
1. `backups/` klasöründeki en güncel `.sqlite3` veya `.sqlite3.gz` dosyasını bulunuz.
2. Dosya `.gz` ise arşivden çıkartınız.
3. Dosya adını `db.sqlite3` olarak değiştirip proje ana dizinine yerleştiriniz.
4. `python manage.py check` komutuyla doğrulayıp sistemi başlatınız. Tüm satışlar, bakiyeler ve stoklar eksiksiz geri gelir.

---

## 8. SIKÇA SORULAN SORULAR VE SORUN GİDERME

**S1: Fiş yazıcıdan çıktı vermiyor veya çekmece açılmıyor, ne yapmalıyım?**  
- `Yönetim Paneli` $\rightarrow$ `İşletme & Donanım Ayarları` sekmesine gidiniz.
- `ESC/POS Yazıcı Aktif mi?` seçeneğinin açık olduğunu ve yazıcı IP/Port veya Windows Yazıcı Adının doğru yazıldığını kontrol ediniz.
- "Test Fişi Yazdır" butonuna basarak yazıcıyı test ediniz.

**S2: Terazi tartım yapmıyor, ekranda ağırlık 0.000 KG kalıyor?**  
- USB-RS232 dönüştürücü kablonun takılı olduğu COM portunu (Örn: `COM3` veya `/dev/ttyUSB0`) ayarlar ekranından seçiniz.
- Terazinin kendi ayarlarında haberleşme modunun `Baud Rate: 9600, Mode: Stream / Command` olduğunu teyit ediniz.

**S3: Kasiyer şifresini unuttu, nasıl sıfırlarım?**  
- Yönetici paneline giriniz: `Kullanıcı & Personel Yetkilendirme` $\rightarrow$ İlgili kasiyerin yanındaki **"Şifre Sıfırla"** butonuna basıp yeni PIN/şifre belirleyiniz.

**S4: Sistem sağlığını nasıl kontrol edebilirim?**  
- Tarayıcıdan `http://127.0.0.1:8000/api/health/` adresini açınız. Veritabanı bağlantı hızı, disk boş alanı ve açık vardiya durumunu anında görebilirsiniz.

---
**Tebrikler!** HiMarket kurulumu ve operasyonel eğitimi tamamlanmıştır. Marketinizde bol ve bereketli satışlar dileriz! 🛒🇹🇷

