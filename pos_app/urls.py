from django.urls import path
from . import views

urlpatterns = [
    # Kullanıcı Oturum Açma & Kapatma (Dokunmatik PIN Pad & Standart Giriş)
    path('login/', views.login_view, name='login_view'),
    path('logout/', views.logout_view, name='logout_view'),

    # Kasa Satış Ana Ekranı (Hem ana dizin / hem de /pos/ üzerinden doğrudan erişim)
    path('', views.pos_screen, name='pos_screen'),
    path('pos/', views.pos_screen, name='pos_screen_alias'),

    # Barkod & Terazi API Uç Noktası
    path('api/barcode/', views.search_barcode, name='search_barcode'),
    path('api/products/search/', views.product_search_api, name='product_search_api'),
    path('api/product/create-from-pool/', views.create_product_from_pool_api, name='create_product_from_pool_api'),

    # Satış İşleme & ESC/POS Sessiz Baskı / Çekmece API Uç Noktaları
    path('api/sale/process/', views.process_sale, name='process_sale'),
    path('api/sale/<str:receipt_number>/silent-print/', views.silent_print_api_view, name='silent_print_api_view'),
    path('api/pos/open-drawer/', views.open_cash_drawer_api, name='open_cash_drawer_api'),
    path('api/pos/verify-supervisor/', views.verify_supervisor_pin_api, name='verify_supervisor_pin_api'),
    path('pos/terminal/pair/', views.pair_terminal_action, name='pair_terminal_action'),
    path('pos/terminal/unpair/', views.unpair_terminal_action, name='unpair_terminal_action'),
    path('api/terminal/request-code/', views.terminal_device_request_code_api, name='terminal_device_request_code_api'),
    path('api/terminal/pairing-status/', views.terminal_device_pairing_status_api, name='terminal_device_pairing_status_api'),
    path('api/terminal/approve/', views.terminal_device_approve_api, name='terminal_device_approve_api'),
    path('api/terminal/unbind/', views.terminal_device_unbind_api, name='terminal_device_unbind_api'),
    path('api/pos/supervisor-force-close/', views.supervisor_force_close_shift_api, name='supervisor_force_close_shift_api'),

    # Termal Fiş Görüntüleme & Yazdırma
    path('receipt/<str:receipt_number>/', views.receipt_view, name='receipt_view'),
    path('receipt/<str:receipt_number>/gift/', views.gift_receipt_view, name='gift_receipt_view'),

    # Z Raporu Ekranı & Vardiya İşlemleri
    path('z-report/', views.z_report_view, name='z_report_view'),
    path('api/shift/close/', views.close_shift_action, name='close_shift_action'),
    path('api/shift/open/', views.open_shift_action, name='open_shift_action'),

    # Yönetim Kontrol Paneli & Dashboard
    path('management/dashboard/', views.admin_dashboard_view, name='admin_dashboard_view'),
    path('management/dashboard/home/', views.admin_dashboard_view, name='admin_dashboard'),
    path('yonetim/', views.admin_dashboard_view, name='yonetim_alias'),

    # Faz 16: Mobil Patron Kokpiti (Mobile Boss Dashboard PWA)
    path('management/boss/', views.mobile_boss_dashboard_view, name='mobile_boss_dashboard_view'),
    path('boss/', views.mobile_boss_dashboard_view, name='boss_dashboard_alias'),
    path('api/boss/live-metrics/', views.boss_live_metrics_api, name='boss_live_metrics_api'),

    # Veritabanı Yedek İndirme (Admin Tek Tıkla Yedek İndir)
    path('management/backup/download/', views.download_backup_view, name='download_backup_view'),

    # Toptancı Carileri
    path('management/suppliers/', views.supplier_list_view, name='supplier_list_view'),
    path('management/suppliers/export/', views.export_suppliers_csv, name='export_suppliers_csv'),
    path('management/suppliers/create/', views.supplier_create_view, name='supplier_create_view'),
    path('management/suppliers/<int:supplier_id>/edit/', views.supplier_edit_view, name='supplier_edit_view'),
    path('management/suppliers/<int:supplier_id>/payment/', views.supplier_payment_view, name='supplier_payment_view'),
    path('management/suppliers/<int:supplier_id>/export/', views.export_supplier_ledger_csv, name='export_supplier_ledger_csv'),
    path('management/suppliers/<int:supplier_id>/toggle-status/', views.supplier_toggle_status_action, name='supplier_toggle_status_action'),

    # Müşteri & Veresiye (Cari) Yönetimi ve Tahsilat
    path('management/customers/', views.customer_list_view, name='customer_list_view'),
    path('management/customers/export/', views.export_customers_csv, name='export_customers_csv'),
    path('management/customers/create/', views.customer_create_view, name='customer_create_view'),
    path('management/customers/<int:customer_id>/', views.customer_detail_view, name='customer_detail_view'),
    path('management/customers/<int:customer_id>/export/', views.export_customer_ledger_csv, name='export_customer_ledger_csv'),
    path('management/customers/<int:customer_id>/edit/', views.customer_edit_view, name='customer_edit_view'),
    path('management/customers/<int:customer_id>/toggle-status/', views.customer_toggle_status_action, name='customer_toggle_status_action'),
    path('management/customers/<int:customer_id>/collect/', views.customer_collect_debt_api, name='customer_collect_debt_api'),
    path('api/customers/search/', views.customer_search_api, name='customer_search_api'),
    path('receipt/collection/<int:transaction_id>/', views.collection_receipt_view, name='collection_receipt_view'),
    path('api/receipt/collection/<int:transaction_id>/silent-print/', views.silent_print_collection_receipt_api, name='silent_print_collection_receipt_api'),

    # Stok Giriş & İrsaliye
    path('management/stock-entry/', views.stock_entry_list_view, name='stock_entry_list'),
    path('management/stock-entry/export/', views.export_stock_entries_csv, name='export_stock_entries_csv'),
    path('management/stock-entry/create/', views.stock_entry_create_view, name='stock_entry_create'),

    # Ürün Kataloğu & Ürün Yönetimi
    path('management/products/', views.product_catalog_view, name='product_catalog_view'),
    path('management/products/export/', views.export_products_csv, name='export_products_csv'),
    path('management/products/import/', views.product_import_view, name='product_import_view'),
    path('management/products/import/sample-csv/', views.download_product_import_sample_csv, name='download_product_import_sample_csv'),
    path('management/products/pool/', views.global_product_pool_view, name='global_product_pool_view'),
    path('management/products/pool/add-to-store/', views.add_pool_product_to_store_action, name='add_pool_product_to_store_action'),
    path('management/products/bulk-update/', views.product_bulk_update_action, name='product_bulk_update_action'),
    path('api/product/<int:product_id>/quick-price-update/', views.product_quick_price_update_api, name='product_quick_price_update_api'),
    path('management/products/create/', views.product_create_edit_view, name='product_create_view'),
    path('management/products/<int:product_id>/edit/', views.product_create_edit_view, name='product_edit_view'),
    path('management/products/<int:product_id>/toggle-active/', views.product_toggle_active_action, name='product_toggle_active_action'),
    path('management/products/<int:product_id>/delete/', views.product_delete_action, name='product_delete_action'),

    # Faz 16: Son Kullanma Tarihi (SKT) & Zayiat Önleme
    path('management/expiration/', views.expiration_tracking_view, name='expiration_tracking_view'),
    path('management/expiration/export/', views.export_expirations_csv, name='export_expirations_csv'),

    # Kategori Yönetimi, Satış Fişleri & Kasa Vardiyaları (Özel Admin Ekranları - Faz 4)
    path('management/categories/', views.category_list_view, name='category_list_view'),
    path('management/categories/save/', views.category_save_action, name='category_save_action'),
    path('management/categories/<int:category_id>/toggle-status/', views.category_toggle_status_action, name='category_toggle_status_action'),
    path('management/sales/', views.sale_list_view, name='sale_list_view'),
    path('management/sales/export/', views.export_sales_csv, name='export_sales_csv'),
    path('management/sales/<int:sale_id>/cancel/', views.sale_cancel_action, name='sale_cancel_action'),
    path('api/management/sales/<int:sale_id>/', views.sale_detail_api, name='sale_detail_api'),
    path('management/shifts/', views.shift_list_view, name='shift_list_view'),
    path('management/shifts/export/', views.export_shifts_csv, name='export_shifts_csv'),
    path('management/registers/', views.cash_register_list_view, name='cash_register_list_view'),
    path('management/registers/create/', views.cash_register_create_action, name='cash_register_create_action'),
    path('management/registers/<int:register_id>/toggle-status/', views.cash_register_toggle_status_action, name='cash_register_toggle_status_action'),

    # Sistem İşlem Logları
    path('management/logs/', views.system_logs_view, name='system_logs_view'),

    # Kullanıcı ve Personel Yetkilendirme (RBAC)
    path('management/users/', views.user_management_view, name='user_management_view'),
    path('management/users/create/', views.user_create_action, name='user_create_action'),
    path('management/users/<int:user_id>/edit/', views.user_edit_action, name='user_edit_action'),
    path('management/users/<int:user_id>/toggle-status/', views.user_toggle_status_action, name='user_toggle_status_action'),
    path('management/users/<int:user_id>/change-role/', views.user_change_role_action, name='user_change_role_action'),
    path('management/users/<int:user_id>/reset-password/', views.user_reset_password_action, name='user_reset_password_action'),

    # Hızlı Ürün Tanımlama & Mal Kabul Barkod API
    path('api/product/quick-create/', views.quick_create_product_api, name='quick_create_product_api'),
    path('api/stock/barcode-search/', views.stock_barcode_search_api, name='stock_barcode_search_api'),

    # İşletme, Mali e-Arşiv ve Donanım Yapılandırması (Dinamik Müşteri Ayarları)
    path('management/settings/', views.company_settings_view, name='company_settings_view'),
    path('api/settings/test-print/', views.test_print_settings_api, name='test_print_settings_api'),

    # Elektronik Terazi (Scale) API Uç Noktaları
    path('api/scale/read/', views.scale_read_api, name='scale_read_api'),
    path('api/scale/ports/', views.scale_ports_api, name='scale_ports_api'),

    # Müşteri Göstergesi (Dual-Screen / İkinci Ekran)
    path('pos/customer-display/', views.customer_display_view, name='customer_display_view'),
    path('api/pos/customer-display-state/', views.customer_display_state_api, name='customer_display_state_api'),

    # Raf Etiketi & Barkod Basım
    path('management/products/labels/', views.product_labels_view, name='product_labels_view'),
    path('api/product/generate-barcode/', views.generate_barcode_api, name='generate_barcode_api'),
    path('api/product/<int:product_id>/label-raw/', views.product_label_raw_api, name='product_label_raw_api'),

    # Ürün Varyant Yönetimi API (Faz 13: Giyim/Butik Beden-Renk)
    path('api/products/<int:product_id>/variants/', views.product_variants_api, name='product_variants_api'),
    path('api/variants/<int:variant_id>/update/', views.product_variant_update_api, name='product_variant_update_api'),
    path('api/variants/<int:variant_id>/delete/', views.product_variant_delete_api, name='product_variant_delete_api'),

    # Stok Sayım Fişleri & El Terminali (Faz 5.1)
    path('management/stock-take/', views.stock_take_list_view, name='stock_take_list'),
    path('management/stock-take/create/', views.stock_take_create_action, name='stock_take_create_action'),
    path('management/stock-take/<int:take_id>/', views.stock_take_detail_view, name='stock_take_detail'),
    path('management/stock-take/<int:take_id>/export/', views.export_stock_take_csv, name='export_stock_take_csv'),
    path('management/stock-take/<int:take_id>/cancel/', views.stock_take_cancel_action, name='stock_take_cancel_action'),
    path('api/stock-take/<int:take_id>/scan/', views.stock_take_scan_api, name='stock_take_scan_api'),
    path('api/stock-take/item/<int:item_id>/update/', views.stock_take_item_update_api, name='stock_take_item_update_api'),
    path('api/stock-take/<int:take_id>/apply/', views.stock_take_apply_api, name='stock_take_apply_api'),

    # Kritik Stok & Toptancı Otomatik Sipariş Listesi (Faz 5.2)
    path('management/stock/critical-orders/', views.critical_stock_orders_view, name='critical_stock_orders'),
    path('management/stock/critical-orders/export/', views.export_critical_orders_csv, name='export_critical_orders_csv'),

    # Sistem Sağlık ve Teşhis Uç Noktası (Faz 6)
    path('api/health/', views.health_check_api, name='health_check_api'),

    # Platform Süper Admin Konsolu (Faz 12)
    path('super-admin/', views.super_admin_dashboard_view, name='super_admin_dashboard'),
    path('superadmin/', views.super_admin_dashboard_view, name='superadmin_dashboard_alias'),
    path('super-admin/companies/create/', views.super_admin_create_company_action, name='super_admin_create_company'),
    path('super-admin/companies/<int:company_id>/update-subscription/', views.super_admin_update_subscription_action, name='super_admin_update_subscription'),
    path('super-admin/companies/<int:company_id>/toggle-status/', views.super_admin_toggle_company_status_action, name='super_admin_toggle_company_status'),
    path('super-admin/registers/<int:register_id>/reset-hardware/', views.super_admin_reset_register_hardware_action, name='super_admin_reset_register_hardware'),

    # PWA & Çevrimdışı (Offline) Kasa Motoru (Faz 14)
    path('sw.js', views.service_worker_view, name='service_worker'),
    path('manifest.json', views.manifest_view, name='pwa_manifest'),
    path('api/pos/catalog-cache/', views.pos_catalog_cache_api, name='pos_catalog_cache_api'),
    path('api/sale/offline-sync/', views.offline_sale_sync_api, name='offline_sale_sync_api'),

    # Faz 18: Çoklu Şube, Depo Stok Transferi & Merkezi Fiyat Dağıtımı
    path('management/transfers/', views.branch_transfers_view, name='branch_transfers_view'),
    path('management/transfers/create/', views.branch_transfer_create_view, name='branch_transfer_create_view'),
    path('management/transfers/export/', views.export_transfers_csv, name='export_transfers_csv'),
    path('management/transfers/<int:transfer_id>/', views.branch_transfer_detail_view, name='branch_transfer_detail_view'),
    path('management/transfers/<int:transfer_id>/ship/', views.branch_transfer_ship_action, name='branch_transfer_ship_action'),
    path('management/transfers/<int:transfer_id>/receive/', views.branch_transfer_receive_action, name='branch_transfer_receive_action'),
    path('management/transfers/<int:transfer_id>/cancel/', views.branch_transfer_cancel_action, name='branch_transfer_cancel_action'),
    path('api/product/<int:product_id>/propagate-price/', views.product_propagate_price_api, name='product_propagate_price_api'),

    # Faz 18: Yeni Nesil ÖKC & E-Belge Simülasyon API (GMP-3 / JSON)
    path('api/okc/sale-trigger/', views.okc_sale_trigger_api, name='okc_sale_trigger_api'),
]
