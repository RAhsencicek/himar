/**
 * HiMarket POS - Çevrimdışı (Offline) Kasa Motoru (Faz 14)
 * PWA + IndexedDB Altyapısı
 * 
 * Sorumluluklar:
 * 1. Ürün Kataloğu ve Beden/Renk Varyantlarının IndexedDB'ye önbelleklenmesi
 * 2. İnternet kesildiğinde yerel barkod arama ve terazi barkodu (27/28) çözümleme
 * 3. Çevrimdışı satışın yerel hafızaya kaydedilmesi ve yerel fiş üretimi
 * 4. Bağlantı geldiğinde satışların sunucuya otomatik senkronize edilmesi
 */

const HiMarketOfflineEngine = (function () {
    const DB_NAME = 'HiMarketDB_v1';
    const DB_VERSION = 1;

    let dbInstance = null;
    let isSyncing = false;

    // IndexedDB Başlatma
    function openDB() {
        if (dbInstance) return Promise.resolve(dbInstance);

        return new Promise((resolve, reject) => {
            const request = indexedDB.open(DB_NAME, DB_VERSION);

            request.onupgradeneeded = function (event) {
                const db = event.target.result;

                // 1. Ürünler Deposu
                if (!db.objectStoreNames.contains('products')) {
                    const productStore = db.createObjectStore('products', { keyPath: 'id' });
                    productStore.createIndex('barcode', 'barcode', { unique: false });
                    productStore.createIndex('name', 'name', { unique: false });
                    productStore.createIndex('has_variants', 'has_variants', { unique: false });
                }

                // 2. Varyantlar Deposu
                if (!db.objectStoreNames.contains('variants')) {
                    const variantStore = db.createObjectStore('variants', { keyPath: 'id' });
                    variantStore.createIndex('barcode', 'barcode', { unique: false });
                    variantStore.createIndex('product_id', 'product_id', { unique: false });
                }

                // 3. Çevrimdışı Satış Fişleri Deposu
                if (!db.objectStoreNames.contains('offline_sales')) {
                    const saleStore = db.createObjectStore('offline_sales', { keyPath: 'offline_receipt_id' });
                    saleStore.createIndex('status', 'status', { unique: false });
                    saleStore.createIndex('created_at', 'created_at', { unique: false });
                }

                // 4. Meta Veriler Deposu (Son senkronizasyon tarihi vb.)
                if (!db.objectStoreNames.contains('metadata')) {
                    db.createObjectStore('metadata', { keyPath: 'key' });
                }
            };

            request.onsuccess = function (event) {
                dbInstance = event.target.result;
                resolve(dbInstance);
            };

            request.onerror = function (event) {
                console.error('[OfflineEngine] IndexedDB açılamadı:', event.target.error);
                reject(event.target.error);
            };
        });
    }

    // Ürün Kataloğunu Sunucudan İndirip IndexedDB'ye Yazma
    async function syncCatalogFromServer() {
        if (!navigator.onLine) {
            return { success: false, reason: 'OFFLINE', message: 'Çevrimdışı moddasınız. Mevcut yerel katalog kullanılacak.' };
        }

        try {
            const resp = await fetch('/api/pos/catalog-cache/');
            if (!resp.ok) throw new Error('Katalog sunucudan alınamadı.');
            const data = await resp.json();

            if (!data.success || !Array.isArray(data.products)) {
                throw new Error(data.message || 'Geçersiz katalog verisi.');
            }

            const db = await openDB();
            const tx = db.transaction(['products', 'variants', 'metadata'], 'readwrite');
            const productStore = tx.objectStore('products');
            const variantStore = tx.objectStore('variants');
            const metaStore = tx.objectStore('metadata');

            // Eski ürünleri temizle ve güncellerini bas
            productStore.clear();
            variantStore.clear();

            for (const prod of data.products) {
                productStore.put(prod);
                if (prod.has_variants && Array.isArray(prod.variants)) {
                    for (const v of prod.variants) {
                        variantStore.put({
                            ...v,
                            product_id: prod.id,
                            product_name: prod.name,
                            vat_rate: prod.vat_rate,
                            unit: prod.unit
                        });
                    }
                }
            }

            metaStore.put({
                key: 'catalog_info',
                last_synced_at: new Date().toISOString(),
                total_products: data.total_count,
                company_name: data.company_name,
                business_type: data.business_type
            });

            return new Promise((resolve) => {
                tx.oncomplete = () => {
                    resolve({
                        success: true,
                        total_count: data.total_count,
                        company_name: data.company_name,
                        last_synced_at: new Date().toISOString()
                    });
                };
                tx.onerror = (e) => resolve({ success: false, reason: e.target.error });
            });
        } catch (err) {
            console.warn('[OfflineEngine] Katalog senkronizasyonu hatası:', err);
            return { success: false, reason: err.message };
        }
    }

    // Çevrimdışı Barkod Arama ve Terazi Barkodu Çözümleme
    async function findProductByBarcode(rawCode) {
        const query = String(rawCode || '').trim();
        if (!query) return null;

        const db = await openDB();

        // 1. Terazi Barkodu Kontrolü (27 veya 28 ile başlayan 13 haneli barkodlar)
        if (query.length === 13 && (query.startsWith('27') || query.startsWith('28'))) {
            const itemCode = query.substring(2, 7);
            const valuePart = query.substring(7, 12);
            const isWeight = query.startsWith('27');

            // Ürünü itemCode veya 27/28 prefixiyle eşleşen barkod olarak ara
            const matchedProduct = await searchProductByPrefixOrCode(itemCode);
            if (matchedProduct) {
                if (isWeight) {
                    const weightKg = parseFloat(valuePart) / 1000.0;
                    return {
                        found: true,
                        product: matchedProduct,
                        is_weighted_barcode: true,
                        quantity: weightKg > 0 ? weightKg : 1.0,
                        effective_price: matchedProduct.sell_price
                    };
                } else {
                    const totalPrice = parseFloat(valuePart) / 100.0;
                    const calculatedQty = (matchedProduct.sell_price > 0) ? (totalPrice / matchedProduct.sell_price) : 1.0;
                    return {
                        found: true,
                        product: matchedProduct,
                        is_weighted_barcode: true,
                        quantity: parseFloat(calculatedQty.toFixed(3)),
                        effective_price: matchedProduct.sell_price
                    };
                }
            }
        }

        // 2. Önce Varyantlar Tablosunda Barkod Arama (Beden / Renk Barkodu)
        const variantMatch = await findInStoreByIndex(db, 'variants', 'barcode', query);
        if (variantMatch) {
            const parentProduct = await findInStoreByKey(db, 'products', variantMatch.product_id);
            const effectivePrice = variantMatch.price_override !== null ? variantMatch.price_override : (parentProduct ? parentProduct.sell_price : 0);
            return {
                found: true,
                product: parentProduct || { id: variantMatch.product_id, name: variantMatch.product_name, vat_rate: variantMatch.vat_rate, unit: variantMatch.unit },
                variant: variantMatch,
                has_variants: false, // Doğrudan spesifik varyant okutuldu
                effective_price: effectivePrice,
                variant_id: variantMatch.id,
                variant_info: `${variantMatch.size} · ${variantMatch.color}`
            };
        }

        // 3. Ana Ürünler Tablosunda Barkod Arama
        const productMatch = await findInStoreByIndex(db, 'products', 'barcode', query);
        if (productMatch) {
            // Eğer ürün varyantlı bir ürünse, varyantlarını da getir
            if (productMatch.has_variants) {
                const variants = await getVariantsForProduct(db, productMatch.id);
                return {
                    found: true,
                    product: productMatch,
                    has_variants: true,
                    variants: variants,
                    effective_price: productMatch.sell_price
                };
            }
            return {
                found: true,
                product: productMatch,
                has_variants: false,
                effective_price: productMatch.sell_price
            };
        }

        // 4. Doğrudan Eşleşme Bulunamadıysa Tüm Ürünleri Tara (Sayısal / String Uyuşmazlığı Önlemi)
        const fallbackMatch = await scanProductsForBarcode(db, query);
        if (fallbackMatch) {
            if (fallbackMatch.has_variants) {
                const variants = await getVariantsForProduct(db, fallbackMatch.id);
                return {
                    found: true,
                    product: fallbackMatch,
                    has_variants: true,
                    variants: variants,
                    effective_price: fallbackMatch.sell_price
                };
            }
            return {
                found: true,
                product: fallbackMatch,
                has_variants: false,
                effective_price: fallbackMatch.sell_price
            };
        }

        return null;
    }

    // Yardımcı: Index üzerinden tekil kayıt bulma
    function findInStoreByIndex(db, storeName, indexName, value) {
        return new Promise((resolve) => {
            const tx = db.transaction(storeName, 'readonly');
            const store = tx.objectStore(storeName);
            const index = store.index(indexName);
            const req = index.get(value);
            req.onsuccess = () => resolve(req.result || null);
            req.onerror = () => resolve(null);
        });
    }

    // Yardımcı: Key üzerinden kayıt bulma
    function findInStoreByKey(db, storeName, key) {
        return new Promise((resolve) => {
            const tx = db.transaction(storeName, 'readonly');
            const store = tx.objectStore(storeName);
            const req = store.get(key);
            req.onsuccess = () => resolve(req.result || null);
            req.onerror = () => resolve(null);
        });
    }

    // Yardımcı: Ürüne ait varyantları listeleme
    function getVariantsForProduct(db, productId) {
        return new Promise((resolve) => {
            const tx = db.transaction('variants', 'readonly');
            const store = tx.objectStore('variants');
            const index = store.index('product_id');
            const req = index.getAll(productId);
            req.onsuccess = () => resolve(req.result || []);
            req.onerror = () => resolve([]);
        });
    }

    // Yardımcı: Terazi ürün kodu veya prefix ile eşleşme
    function searchProductByPrefixOrCode(itemCode) {
        return new Promise(async (resolve) => {
            const db = await openDB();
            const tx = db.transaction('products', 'readonly');
            const store = tx.objectStore('products');
            const req = store.openCursor();

            req.onsuccess = function (e) {
                const cursor = e.target.result;
                if (cursor) {
                    const prod = cursor.value;
                    const bc = String(prod.barcode || '');
                    if (bc.includes(itemCode) || bc === itemCode) {
                        resolve(prod);
                        return;
                    }
                    cursor.continue();
                } else {
                    resolve(null);
                }
            };
            req.onerror = () => resolve(null);
        });
    }

    // Yardımcı: Tam liste tarama fallback
    function scanProductsForBarcode(db, targetBarcode) {
        return new Promise((resolve) => {
            const tx = db.transaction('products', 'readonly');
            const store = tx.objectStore('products');
            const req = store.openCursor();

            req.onsuccess = function (e) {
                const cursor = e.target.result;
                if (cursor) {
                    const prod = cursor.value;
                    if (String(prod.barcode || '').trim() === targetBarcode) {
                        resolve(prod);
                        return;
                    }
                    cursor.continue();
                } else {
                    resolve(null);
                }
            };
            req.onerror = () => resolve(null);
        });
    }

    // Çevrimdışı Satışı IndexedDB'ye Kaydetme
    async function saveOfflineSale(salePayload) {
        const db = await openDB();

        const now = new Date();
        const dateStr = now.toISOString().replace(/\D/g, '').slice(0, 14);
        const randStr = Math.random().toString(36).substring(2, 6).toUpperCase();
        const offlineId = `OFF-${dateStr}-${randStr}`;

        const saleRecord = {
            offline_receipt_id: offlineId,
            created_at: now.toISOString(),
            status: 'PENDING', // PENDING, SYNCED, FAILED
            items: salePayload.items || [],
            payment_type: salePayload.payment_type || 'NAKIT',
            cash_paid: Number(salePayload.cash_paid) || 0,
            card_paid: Number(salePayload.card_paid) || 0,
            customer_id: salePayload.customer_id || null,
            discount_amount: Number(salePayload.discount_amount) || 0,
            is_return: Boolean(salePayload.is_return),
            total_amount: Number(salePayload.total_amount) || 0,
            items_summary: (salePayload.items || []).map(i => `${i.name || 'Ürün'} x${i.quantity}`).join(', '),
            server_receipt_number: null,
            synced_at: null
        };

        const tx = db.transaction(['offline_sales', 'products'], 'readwrite');
        const saleStore = tx.objectStore('offline_sales');
        const productStore = tx.objectStore('products');

        saleStore.put(saleRecord);

        // Yerel ürün stoğunu düş
        for (const item of (salePayload.items || [])) {
            if (item.product_id) {
                const getReq = productStore.get(item.product_id);
                getReq.onsuccess = function () {
                    const prod = getReq.result;
                    if (prod) {
                        const delta = saleRecord.is_return ? item.quantity : -item.quantity;
                        prod.stock_quantity = (prod.stock_quantity || 0) + delta;
                        productStore.put(prod);
                    }
                };
            }
        }

        return new Promise((resolve, reject) => {
            tx.oncomplete = () => {
                updateOfflineUI();
                resolve(saleRecord);
            };
            tx.onerror = (e) => reject(e.target.error);
        });
    }

    // Bekleyen Çevrimdışı Satışları Alma
    async function getPendingSales() {
        const db = await openDB();
        return new Promise((resolve) => {
            const tx = db.transaction('offline_sales', 'readonly');
            const store = tx.objectStore('offline_sales');
            const req = store.getAll();

            req.onsuccess = function () {
                const all = req.result || [];
                const pending = all.filter(s => s.status === 'PENDING').sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
                resolve(pending);
            };
            req.onerror = () => resolve([]);
        });
    }

    // Bekleyen Satışları Sunucuya Senkronize Etme
    async function syncPendingSales(csrfToken) {
        if (isSyncing) return { status: 'ALREADY_SYNCING' };
        if (!navigator.onLine) return { status: 'OFFLINE' };

        const pending = await getPendingSales();
        if (pending.length === 0) {
            updateOfflineUI(0);
            return { status: 'EMPTY', count: 0 };
        }

        isSyncing = true;
        try {
            const token = csrfToken || getCsrfToken();
            const resp = await fetch('/api/sale/offline-sync/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': token
                },
                body: JSON.stringify({ sales: pending })
            });

            if (!resp.ok) {
                throw new Error(`Senkronizasyon sunucu hatası (HTTP ${resp.status})`);
            }

            const data = await resp.json();
            if (data.success && Array.isArray(data.results)) {
                const db = await openDB();
                const tx = db.transaction('offline_sales', 'readwrite');
                const store = tx.objectStore('offline_sales');

                for (const res of data.results) {
                    if (res.status === 'SYNCED' || res.status === 'ALREADY_SYNCED') {
                        const getReq = store.get(res.offline_receipt_id);
                        getReq.onsuccess = function () {
                            const record = getReq.result;
                            if (record) {
                                record.status = 'SYNCED';
                                record.server_receipt_number = res.server_receipt_number;
                                record.synced_at = new Date().toISOString();
                                store.put(record);
                            }
                        };
                    }
                }

                await new Promise((resolve) => {
                    tx.oncomplete = resolve;
                });

                // Kataloğu da tazele ki güncel merkezi stoklar gelsin
                syncCatalogFromServer().catch(() => {});

                const remaining = await getPendingSales();
                updateOfflineUI(remaining.length);

                // Özel event tetikle
                window.dispatchEvent(new CustomEvent('himarket:offline-synced', {
                    detail: { synced_count: data.synced_count, results: data.results }
                }));

                return {
                    status: 'SUCCESS',
                    synced_count: data.synced_count,
                    failed_count: data.failed_count,
                    results: data.results
                };
            } else {
                throw new Error(data.message || 'Senkronizasyon cevabı geçersiz.');
            }
        } catch (err) {
            console.error('[OfflineEngine] Senkronizasyon hatası:', err);
            return { status: 'ERROR', message: err.message };
        } finally {
            isSyncing = false;
        }
    }

    // Çevrimdışı Fiş Termal HTML Çıktısı Üretici
    function generateOfflineReceiptHtml(saleRecord, companyProfile) {
        const cName = companyProfile ? (companyProfile.title || companyProfile.name || 'HİMARKET') : 'HİMARKET';
        const lines = (saleRecord.items || []).map(it => `
            <tr>
                <td style="text-align: left; padding: 2px 0;">
                    ${escapeHtml(it.name || 'Ürün')}
                    ${it.variant_info ? `<br><small style="color: #555;">[${escapeHtml(it.variant_info)}]</small>` : ''}
                </td>
                <td style="text-align: right; white-space: nowrap; padding: 2px 0;">
                    ${it.quantity} x ${Number(it.unit_price).toFixed(2)} TL<br>
                    <strong>${(it.quantity * it.unit_price).toFixed(2)} TL</strong>
                </td>
            </tr>
        `).join('');

        return `
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <title>Çevrimdışı Satış Fişi #${saleRecord.offline_receipt_id}</title>
                <style>
                    body { font-family: monospace; font-size: 12px; margin: 0; padding: 10px; width: 80mm; color: #000; }
                    .center { text-align: center; }
                    .hr { border-bottom: 1px dashed #000; margin: 8px 0; }
                    .badge-offline { background: #000; color: #fff; padding: 2px 6px; font-weight: bold; font-size: 10px; display: inline-block; margin: 4px 0; }
                    table { width: 100%; border-collapse: collapse; font-size: 11px; }
                    .total-row { font-size: 14px; font-weight: bold; margin-top: 6px; }
                </style>
            </head>
            <body>
                <div class="center">
                    <h2 style="margin: 0;">${escapeHtml(cName)}</h2>
                    <div class="badge-offline">*** ÇEVRİMDIŞI SATIŞ FİŞİ ***</div>
                    <div>(İnternet Gelince Otomatik Mali Fişe Dönüşecektir)</div>
                    <div>Tarih: ${new Date(saleRecord.created_at).toLocaleString('tr-TR')}</div>
                    <div>Fiş No: ${escapeHtml(saleRecord.offline_receipt_id)}</div>
                </div>
                <div class="hr"></div>
                <table>
                    <thead>
                        <tr style="border-bottom: 1px solid #000;">
                            <th style="text-align: left;">ÜRÜN</th>
                            <th style="text-align: right;">TUTAR</th>
                        </tr>
                    </thead>
                    <tbody>${lines}</tbody>
                </table>
                <div class="hr"></div>
                <div style="display: flex; justify-content: space-between;" class="total-row">
                    <span>GENEL TOPLAM:</span>
                    <span>${Number(saleRecord.total_amount).toFixed(2)} TL</span>
                </div>
                <div style="display: flex; justify-content: space-between; font-size: 11px; margin-top: 4px;">
                    <span>Ödeme Türü:</span>
                    <span>${saleRecord.payment_type}</span>
                </div>
                <div class="hr"></div>
                <div class="center" style="font-size: 10px;">
                    Mali Değeri Yoktur - Bilgilendirme Fişidir.<br>
                    İyi Günler Dileriz!
                </div>
            </body>
            </html>
        `;
    }

    // UI Rozetini Güncelleme
    async function updateOfflineUI(count) {
        let pendingCount = count;
        if (typeof pendingCount !== 'number') {
            const list = await getPendingSales();
            pendingCount = list.length;
        }

        const badgeBtn = document.getElementById('offlineQueueBtn');
        const badgeText = document.getElementById('offlineQueueBadgeText');
        const posQueueBtn = document.getElementById('posOfflineQueueBadge');
        const posQueueText = document.getElementById('posOfflineQueueBadgeText');

        if (badgeBtn) {
            if (pendingCount > 0) {
                badgeBtn.classList.remove('d-none');
                badgeBtn.classList.add('d-inline-flex');
                if (badgeText) badgeText.textContent = `${pendingCount} Çevrimdışı Fiş`;
            } else {
                badgeBtn.classList.add('d-none');
                badgeBtn.classList.remove('d-inline-flex');
                if (badgeText) badgeText.textContent = `0 Çevrimdışı Satış`;
            }
        }

        if (posQueueBtn) {
            if (pendingCount > 0) {
                posQueueBtn.classList.remove('d-none');
                posQueueBtn.classList.add('d-inline-flex');
                if (posQueueText) posQueueText.textContent = `${pendingCount} Çevrimdışı Fiş`;
            } else {
                posQueueBtn.classList.add('d-none');
                posQueueBtn.classList.remove('d-inline-flex');
            }
        }
    }

    function updateNetworkBadge(isOnline) {
        const netBadge = document.getElementById('networkStatusBadge');
        const posBadge = document.getElementById('posNetworkBadge');

        if (isOnline) {
            if (netBadge) {
                netBadge.className = 'badge bg-success-subtle text-success border border-success-subtle fw-bold d-inline-flex align-items-center gap-1';
                netBadge.innerHTML = '<i class="bi bi-wifi"></i> Çevrimiçi';
            }
            if (posBadge) {
                posBadge.style.background = 'rgba(16, 185, 129, 0.15)';
                posBadge.style.color = '#059669';
                posBadge.style.borderColor = '#10b981';
                posBadge.innerHTML = '<i class="bi bi-wifi"></i> Çevrimiçi';
            }
        } else {
            if (netBadge) {
                netBadge.className = 'badge bg-warning text-dark border border-warning fw-bold d-inline-flex align-items-center gap-1';
                netBadge.innerHTML = '<i class="bi bi-wifi-off"></i> Çevrimdışı (Offline)';
            }
            if (posBadge) {
                posBadge.style.background = '#fef08a';
                posBadge.style.color = '#854d0e';
                posBadge.style.borderColor = '#eab308';
                posBadge.innerHTML = '<i class="bi bi-wifi-off"></i> ÇEVRİMDİŞI (OFFLINE)';
            }
        }
    }

    function getCsrfToken() {
        const match = document.cookie.match(/csrftoken=([^;]+)/);
        return match ? match[1] : '';
    }

    function escapeHtml(str) {
        if (!str) return '';
        return String(str).replace(/[&<>"']/g, function (m) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m];
        });
    }

    return {
        openDB,
        syncCatalogFromServer,
        findProductByBarcode,
        saveOfflineSale,
        getPendingSales,
        syncPendingSales,
        generateOfflineReceiptHtml,
        updateOfflineUI,
        updateNetworkBadge
    };
})();

// Tarayıcı Online / Offline Olaylarını Dinleme
window.addEventListener('online', function () {
    console.log('[OfflineEngine] İnternet bağlantısı sağlandı. Çevrimdışı kuyruk taranıyor...');
    HiMarketOfflineEngine.updateNetworkBadge(true);

    // Otomatik Arka Plan Senkronizasyonunu Tetikle
    HiMarketOfflineEngine.syncPendingSales().then((res) => {
        if (res && res.status === 'SUCCESS' && res.synced_count > 0) {
            if (typeof showPosToast === 'function') {
                showPosToast(`İnternet bağlantısı geldi! ${res.synced_count} çevrimdışı fiş başarıyla sunucuya aktarıldı.`, 'success');
            }
        }
    });
});

window.addEventListener('offline', function () {
    console.warn('[OfflineEngine] İnternet bağlantısı koptu! Çevrimdışı moda geçildi.');
    HiMarketOfflineEngine.updateNetworkBadge(false);
    if (typeof showPosToast === 'function') {
        showPosToast('İnternet kesildi! Satışlar çevrimdışı devam edecek, yerel hafızaya kaydediliyor.', 'warning');
    }
});

// Sayfa yüklendiğinde DB'yi aç, bağlantı durumunu ve çevrimdışı kuyruk sayısını güncelle
document.addEventListener('DOMContentLoaded', function () {
    HiMarketOfflineEngine.updateNetworkBadge(navigator.onLine);
    HiMarketOfflineEngine.openDB().then(() => {
        HiMarketOfflineEngine.updateOfflineUI();
        // Online ise kataloğu arka planda güncelle
        if (navigator.onLine) {
            HiMarketOfflineEngine.syncCatalogFromServer();
            // Eğer bekleyen varsa senkronize et
            HiMarketOfflineEngine.syncPendingSales();
        }
    }).catch(err => console.error('[OfflineEngine] Başlatma hatası:', err));
});
