# HİMARKET SAAS & POS — MASTER FAZ YOL HARİTASI VE GÜNCEL DURUM RAPORU
**Doküman Kodu:** ROADMAP-HM-2026-V2  
**Tarih:** 19 Kasım 2026  
**Aktif Sürüm:** HiMarket Enterprise v2.8.0 (130/130 Birim Testi Başarılı - %100 Yeşil)  
**Canlı Sistem:** `70.40.138.174` (`https://himarket.hayatisleri.com.tr/`)  
**Statü:** Resmi Yol Haritası & Canlı Geliştirme Takip Kılavuzu

---

## 📌 BÖLÜM 1: GEÇMİŞTEN BUGÜNE TAMAMLANAN FAZLAR (FAZ 1 — FAZ 14)
*Aşağıdaki tüm fazlar kodlanmış, 130 otomatik test ile doğrulanmış ve canlı sunucuda devreye alınmıştır.*

| Faz | Kapsam | Durum | Test Sayısı |
| :--- | :--- | :---: | :---: |
| **Faz 1** | Kritik Hata, Veri Bütünlüğü ve Türkçe Locale Düzeltmeleri | ✅ TAMAMLANDI | 67 Test |
| **Faz 2** | Mobil Responsive, Çevrimdışı Dayanıklılık ve 404 Temizliği | ✅ TAMAMLANDI | 69 Test |
| **Faz 3** | Günlük Operasyon ve CRUD Eksikleri | ✅ TAMAMLANDI | 72 Test |
| **Faz 4** | Ham Django Admin Bağımlılığını Kaldıran Özel Yönetim Sayfaları | ✅ TAMAMLANDI | 76 Test |
| **Faz 5** | Raporlama, Toplu İşlemler ve Kalite Güvencesi | ✅ TAMAMLANDI | 79 Test |
| **Faz 6** | Büyük Veri Performansı & Sayfalama (Pagination) Standardizasyonu | ✅ TAMAMLANDI | 81 Test |
| **Faz 7** | Eksik CRUD & Yönetici Aksiyonları (Güvenli Silme, Fiş İptali) | ✅ TAMAMLANDI | 84 Test |
| **Faz 8** | Eksik Raporlar, CSV Dışa Aktarımlar & Dashboard Entegrasyonu | ✅ TAMAMLANDI | 88 Test |
| **Faz 9** | Terminal Cihaz Eşleştirme & Münhasır Kasa Güvenliği (1 Kasa = 1 Terminal) | ✅ TAMAMLANDI | 92 Test |
| **Faz 10** | Kullanılabilirlik, Satış Hızı, Numpad Ergonomisi & Inline Hızlı Fiyat | ✅ TAMAMLANDI | 96 Test |
| **Faz 11** | Çoklu Mağaza (Multi-Tenancy), Şirket İzolasyonu & Sıfır Kurulum Sihirbazı | ✅ TAMAMLANDI | 103 Test |
| **Faz 12** | Kasa Başına Lisanslama, Abonelik, Süper Admin Konsolu & 403 Güvenliği | ✅ TAMAMLANDI | 115 Test |
| **Faz 13** | Sektörel Modülerlik (Market/Giyim/Perakende), Beden-Renk Varyant Motoru, Dokunmatik Kodpos Modalı & Hediye Değişim Fişi | ✅ TAMAMLANDI | 123 Test |
| **Faz 14** | Çevrimdışı (Offline) Kasa Motoru (PWA, IndexedDB, Arka Plan Senkronizasyon & Idempotency) | ✅ TAMAMLANDI | 130 Test |

> [!NOTE]
> **Halihazırda Çalışan Kritik MVP Özellikleri:**
> - **Çevrimdışı (Offline) Kasa Dayanıklılığı:** İnternet kopsa dahi sıfır kesintiyle barkod tarama (terazi ve varyantlar dahil), yerel fiş basımı, IndexedDB kuyruğu ve bağlantı gelince otomatik çift-kayıt korumalı (idempotent) senkronizasyon.
> - **Sektörel Uyumluluk & Varyantlar:** Giyim/Butik sektöründe Beden x Renk matrisi, her varyanta otomatik tekil EAN-13 barkodu, kasada dokunmatik seçim modalı, sepette varyant rozetleri ve fiyatsız 30 günlük Hediye Değişim Fişi.
> - **Terazi Ayrıştırma Motoru:** GS1 Türkiye standartlarında 27 (Ağırlık/Gramaj) ve 28 (Fiyat/Tutar) gömülü barkodları otomatik tanır (Market modunda aktif).
> - **Hızlı Tuşlar & Kısayollar:** F2 Nakit, F3 Kart, F4 Beklet, F7 Kısmi, F8 İade, F9 Veresiye, Dokunmatik hızlı ürün butonları.
> - **Veresiye Defteri:** Müşteri borçlandırma, limit kontrolleri, bakiye ekstresi ve tahsilat makbuzu.

---

## 🚀 BÖLÜM 2: YENİ STRATEJİK YOL HARİTASI (FAZ 15 — FAZ 18)
*Bu fazlar işletmenin kesintisiz çalışmasını, ürün havuzuna anında erişmesini ve kurumsal seviyeye ulaşmasını sağlar.*

```mermaid
flowchart LR
    F13["✅ Faz 13: Sektörel Modülerlik"] --> F14["✅ Faz 14: Çevrimdışı Kasa (Offline)"]
    F14 --> F15["✅ Faz 15: Ortak Barkod Havuzu"]
    F15 --> F16["✅ Faz 16: SKT Takibi & Mobil Patron Kokpiti"]
    F16 --> F17["Faz 17: Müşteri Sadakati (Puan & Kart)"]
    F17 --> F18["Faz 18: Çoklu Şube, Depo & E-Belge / ÖKC"]
```

---

### 🟢 FAZ 14: ÇEVRİMDIŞI (OFFLINE) KASA MOTORU (PWA + INDEXEDDB) — TAMAMLANDI ✅
*Hedef: İnternet kesilse bile kasada satışın sıfır kesintiyle devam etmesi; bağlantı gelince otomatik senkronize olması.*

#### A. Tamamlanan ve Doğrulanan Kabiliyetler
1. **PWA Altyapısı (Service Worker & Manifest):**
   - `static/manifest.json`: Standalone POS terminal görünümü, kurumsal simgeler ve tema rengi.
   - `static/sw.js` (`/sw.js`): Statik CSS, JS, font ve Bootstrap varlıklarını önbelleğe alarak internetsiz tam sayfa açılışı sağlar.
2. **Yerel Veri Depolama (IndexedDB Motoru - `offline_engine.js`):**
   - Kasa açıldığında şirketin ürün kataloğu (`/api/pos/catalog-cache/`) tek tıkla IndexedDB `products` ve `variants` tablolarına indirilir.
   - Çevrimdışıyken yerel barkod arama, terazi (27/28) gramaj/tutar çözümleme ve giyim varyantları anında bulunur.
3. **Çevrimdışı Satış ve Fiş Üretimi:**
   - İnternet koptuğunda veya sunucuya erişilemediğinde satış duraksamaz; fiş `offline_sales` deposuna yazılır, yerel stok düşülür.
   - Kasada 80mm/58mm çevrimdışı bilgilendirme fişi basılır.
4. **Idempotent Arka Plan Senkronizasyonu (`/api/sale/offline-sync/`):**
   - İnternet bağlantısı geldiğinde `window.online` olayı ile bekleyen tüm fişler sunucuya aktarılır.
   - `Sale.offline_sync_id` sayesinde ağ kopmalarında mükerrer istek gelse dahi çift kayıt veya çift stok düşümü engellenir.
5. **Kasa Kuyruk Yönetimi Modalı (`#offlineQueueModal`):**
   - Kasa üst menüsünde bekleyen çevrimdışı fiş sayısı anlık gösterilir; tıklandığında liste incelenebilir ve manuel senkronizasyon tetiklenebilir.
6. **Birim Testleri:**
   - 7 yeni birim testi yazıldı (`OfflineKasaAndPwaTestSuite`), toplam **130 test eksiksiz geçti (%100 Yeşil)**.

---

### 🟢 FAZ 15: ORTAK HAZIR ÜRÜN HAVUZU & GLOBAL BARKOD KATALOĞU — TAMAMLANDI ✅
*Hedef: Yeni müşteri kayıt olduğunda 10.000 ürünü tek tek elle girmekten kurtarmak; Türkiye'deki yaygın FMCG (hızlı tüketim) market ürünlerini hazır getirmek ve Excel/CSV ile toplu aktarımı sağlamak.*

#### A. Tamamlanan ve Doğrulanan Kabiliyetler
1. **Global Master Barcode Modeli & Tohum Verisi (`GlobalProductPool`):**
   - Türkiye genelinde sık tüketilen bisküvi, meşrubat, süt ürünleri, temizlik ve temel gıdalar (Ülker, Eti, Sütaş, Pınar, Çaykur, Yudum, Fairy, Domestos vb. 50+ ürün) EAN-13 barkodları, yasal KDV oranları ve tavsiye edilen raf fiyatlarıyla `seed_global_pool` komutu ile hazırlandı.
2. **Kasada Akıllı Eşleşme & Anında Stoğa Katma Modalı (`#poolProductModal`):**
   - Kasada dükkanda henüz olmayan bir barkod okutulduğunda `search_barcode` API havuzdan ürünü yakalar (`pool_found: true`).
   - Ekranda dokunmatik `#poolProductModal` açılır; kasiyer dükkan satış fiyatını onaylar/değiştirir ve tek tıkla (`/api/product/create-from-pool/`) ürünü mağaza kataloğuna kaydedip müşterinin sepetine atar. Kasa akışı hiç duraksamaz.
3. **Excel & CSV Toplu İçe Aktarma Sihirbazı (`/management/products/import/`):**
   - Sürükle-bırak destekli dosya yükleme, UTF-8 BOM, noktalı virgül (;) ve virgül (,) otomatik tespiti.
   - "Mevcut Ürünleri Güncelle" seçeneği ile fiyat ve stokları toplu revize etme.
   - `urun_aktarim_sablonu.csv` örnek şablon indirme butonu (`/management/products/import/sample-csv/`).
   - İşlem sonrasında detaylı rapor kartı (yeni eklenen, güncellenen, atlanan, hatalı satırlar).
4. **Yönetim Paneli Ortak Barkod Havuzu Arama Ekranı (`/management/products/pool/`):**
   - Kategori ve arama filtreli, sayfalama destekli arayüz.
   - Mağazada zaten mevcut olan ürünleri yeşil `[✓ Mağazanızda Var]` rozetiyle, olmayanları ise tek tıkla fiyata göre ekleme modalı ile listeler.
5. **Birim Testleri & Kalite Güvencesi:**
   - 6 yeni birim testi yazıldı (`GlobalProductPoolTestSuite`), toplam **136 test eksiksiz geçti (%100 Yeşil)**.

---

### 🟢 FAZ 16: SKT & PARTİ (LOT) TAKİBİ VE MOBİL PATRON KOKPİTİ — TAMAMLANDI ✅
*Hedef: Gıda ürünlerinde son kullanma tarihi yaklaşanları uyararak zayiatı önlemek; işletme sahibinin telefonundan canlı ciroyu, brüt/net kâr marjını ve iptal edilen şüpheli fişleri izlemesini sağlamak.*

#### A. Tamamlanan ve Doğrulanan Kabiliyetler
1. **Ürün Kartında ve Mal Kabulünde SKT & Parti (Lot) Modeli:**
   - `Product` ve `StockEntryItem` modellerine `track_expiration`, `expiration_date` ve `lot_number` alanları eklendi (Migrasyon: `0017`).
   - `Product.days_to_expiration` ve `Product.expiration_status` dinamik property'leri ile durum sınıflandırması: `EXPIRED` (Tarihi Geçmiş), `CRITICAL` (0-3 gün), `WARNING` (4-7 gün), `UPCOMING` (8-30 gün) ve `OK`.
2. **Kasa Ekranında Sesli ve Görsel SKT / Gıda Güvenliği Alarmı:**
   - Kasiyer barkod okuttuğunda ürünün SKT'si geçmişse kalın ikaz sesi (`playBeep(260, 0.25)`) ve kırmızı uyarı tost bildirimi (`⚠️ DİKKAT: SKT'si Geçmiştir! Raftan kaldırınız`) tetiklenir. Tarihi yaklaşmış ürünlerde sarı bilgilendirme verilir.
3. **SKT Takip & Zayiat Önleme Paneli (`/management/expiration/`):**
   - Günü geçen ürünler (Kırmızı Alarm), 0-3 gün kalanlar (Acil Tüketim/İndirim), 4-7 gün ve 8-30 gün kalanlar için renkli KPI kartları ve potansiyel zayiat maliyet hesaplaması.
   - Excel uyumlu CSV dışa aktarımı (`/management/expiration/export/`).
4. **Mobil Patron Kokpiti (Mobile Boss Cockpit PWA — `/boss/` veya `/management/boss/`):**
   - Telefon ekranına tam oturan (Mobile-First) şık, koyu degrade başlıklı patron paneli.
   - **Canlı Ciro:** Bugünkü Net Satış Cirosu, Nakit / Kredi Kartı / Veresiye dağılım çubuğu.
   - **Gerçek Kâr Analizi:** Satılan kalemlerin anlık maliyeti üzerinden Tahmini Brüt Kâr (₺) ve Net Kâr Marjı Yüzdesi (%..).
   - **Kasa Güvenliği & Şüpheli İşlem Alarmı:** Gün içinde kasiyerlerin iptal ettiği fişler, iptal tutarı, saati ve iptal gerekçeleri.
   - **Açık Kasa & Vardiya Durumu:** Hangi kasa açık, hangi kasiyer nöbette, çekmecedeki güncel nakit tutarı.
   - **Canlı Yenileme:** Sayfayı yenilemeden 30 saniyede bir otomatik ve manuel tek dokunuşla `/api/boss/live-metrics/` üzerinden animasyonlu canlı veri güncelleme.
   - Dönem filtreleri: [Bugün], [Dün], [Bu Hafta], [Bu Ay].
5. **Birim Testleri & Kalite Güvencesi:**
   - 4 yeni kapsamlı birim testi yazıldı (`ExpirationAndBossCockpitTestSuite`), toplam **140 test eksiksiz geçti (%100 Yeşil)**.

---

### 🔵 FAZ 17: MÜŞTERİ SADAKATİ (LOYALTY & PUAN SİSTEMİ)
*Hedef: Müşterileri markete veya butiğe bağlamak, ciro artıran kampanya araçları sunmak.*

#### A. İşlevsel Kapsam
1. **Puan Kazanma & Harcama:**
   - Alışveriş tutarının belirlenen yüzdesi kadar puan yükleme (Örn: Her 100 TL'ye 2 TL puan).
   - Kasada puanla ödeme seçeneği.
2. **Müşteri İndirim Kartları & SMS:**
   - Telefon numarası veya müşteri kartı ile anında tanıma.

---

### 🔵 FAZ 18: ÇOKLU ŞUBE, DEPO TRANSFERLERİ & E-BELGE / ÖKC
*Hedef: Zincirleşen işletmeler ve kurumsal yasal mevzuat uyumluluğu.*

#### A. İşlevsel Kapsam
1. **Şubeler Arası Stok Transferi:** Merkez depo -> Şube 1, Şube 2 arası irsaliyeli mal sevkiyatı.
2. **Merkezi Fiyat Yönetimi:** Merkezden tek tıkla tüm şubelerin satış fiyatını güncelleme.
3. **Yeni Nesil ÖKC & E-Arşiv:** GMP-3 haberleşmesi ve entegratör üzerinden e-Arşiv / e-Fatura üretimi.

---

## 🛡️ BÖLÜM 3: ÇALIŞMA PROTOKOLÜ VE DİSİPLİN KURALLARI
1. **Adım Adım İlerleme (No Skipping):** Bir faz %100 bitip test edilmeden ve canlıda doğrulanmadan kesinlikle diğer faza geçilmeyecektir.
2. **Sıfır Regresyon (Backend Bütünlüğü):** Mevcut 115 test asla bozulmayacak, her yeni faz kendi otomatik testleriyle gelecektir.
3. **Cihaz & Ekran Ergonomisi (KodPOS & Responsive):** Yapılan her ekran 1024×768 dokunmatik kasalar, 1366×768 laptoplar, tabletler ve telefonlarda test edilerek ferah ve parmak dostu olarak teslim edilecektir.
4. **Dokümantasyon Senkronizasyonu:** Her faz tamamlandığında bu belge güncellenecek, tamamlanan ve bekleyen maddeler şeffaf tutulacaktır.
