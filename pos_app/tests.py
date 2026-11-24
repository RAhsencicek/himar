from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from django.db.models import F
from django.utils import timezone
import datetime
from datetime import timedelta
import io
from decimal import Decimal
from pathlib import Path
import json
from .models import (
    Product, ProductVariant, Category, CashRegister, Shift, Sale, SaleItem, Supplier, SupplierTransaction,
    CompanySetting, Customer, CustomerTransaction, StockTake, StockTakeItem, StockEntry, StockEntryItem,
    SystemLog, Company, UserProfile, Subscription, GlobalProductPool, BranchTransfer, BranchTransferItem
)


class BasePosTestCase(TestCase):
    """
    Testler için ortak veri hazırlığı (Fixture & Setup)
    """
    def setUp(self):
        self.client = Client()

        # 1. Kasiyer ve Yönetici Kullanıcıları
        self.cashier_user = User.objects.create_user(
            username='kasiyer1',
            password='Password123!',
            first_name='Ali',
            last_name='Kasiyer',
            is_staff=False
        )

        self.manager_user = User.objects.create_user(
            username='yonetici1',
            password='Password123!',
            first_name='Ahmet',
            last_name='Müdür',
            is_staff=True,
            is_superuser=True
        )

        # 2. Örnek Kategori ve Kasa
        self.category = Category.objects.create(name='Gıda', is_active=True)
        self.register = CashRegister.objects.create(
            register_code='KASA-01',
            name='Kasa 1 (Ana Kasa)',
            is_active=True
        )

        # 3. Örnek Ürünler (Stoklu ve Terazili)
        self.product_bread = Product.objects.create(
            name='Somun Ekmek 200g',
            barcode='8690001001',
            category=self.category,
            buy_price=Decimal('7.00'),
            sell_price=Decimal('10.00'),
            vat_rate=Decimal('1.00'),
            unit='Adet',
            stock_quantity=Decimal('50.000'),
            min_stock_level=Decimal('5.000'),
            is_active=True
        )

        self.product_low_stock = Product.objects.create(
            name='Kritik Stoklu Çikolata',
            barcode='8690001002',
            category=self.category,
            buy_price=Decimal('15.00'),
            sell_price=Decimal('25.00'),
            vat_rate=Decimal('10.00'),
            unit='Adet',
            stock_quantity=Decimal('2.000'), # Sadece 2 adet var
            min_stock_level=Decimal('5.000'),
            is_active=True
        )

        self.product_apple = Product.objects.create(
            name='Amasya Elması',
            barcode='2700010',
            category=self.category,
            buy_price=Decimal('20.00'),
            sell_price=Decimal('35.00'),
            vat_rate=Decimal('1.00'),
            unit='Kg',
            stock_quantity=Decimal('100.000'),
            is_weighted=True,
            is_active=True
        )

        self.product_inactive = Product.objects.create(
            name='Satıştan Kaldırılmış Ürün',
            barcode='8690001003',
            category=self.category,
            buy_price=Decimal('10.00'),
            sell_price=Decimal('20.00'),
            vat_rate=Decimal('20.00'),
            unit='Adet',
            stock_quantity=Decimal('10.000'),
            is_active=False
        )

        # 4. Aktif Vardiya
        self.shift = Shift.objects.create(
            cash_register=self.register,
            cashier=self.cashier_user,
            opening_float=Decimal('500.00'),
            expected_cash=Decimal('500.00'),
            is_closed=False
        )


class AuthenticationAndPermissionsTest(BasePosTestCase):
    """
    ADIM 2 TESTLERİ:
    - Oturumsuz kullanıcıların engellenmesi
    - Kasiyer / Yönetici yetki sınırlarının doğrulanması
    """
    def test_unauthenticated_user_redirected_to_login(self):
        # Oturum açmamış kullanıcı ana sayfayı veya yönetimi açamaz
        res_pos = self.client.get(reverse('pos_screen'))
        self.assertEqual(res_pos.status_code, 302)
        self.assertIn('/login/', res_pos.url)

        res_dash = self.client.get(reverse('admin_dashboard_view'))
        self.assertEqual(res_dash.status_code, 302)
        self.assertIn('/login/', res_dash.url)

        res_products = self.client.get(reverse('product_catalog_view'))
        self.assertEqual(res_products.status_code, 302)
        self.assertIn('/login/', res_products.url)

    def test_unauthenticated_api_rejected(self):
        # Oturumsuz yapılan API istekleri 401 Unauthorized dönmelidir
        res = self.client.post(
            reverse('process_sale'),
            data=json.dumps({'items': [{'product_id': self.product_bread.id, 'quantity': 1}]}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 401)
        data = res.json()
        self.assertFalse(data['success'])

    def test_cashier_cannot_access_manager_pages(self):
        # Kasiyer giriş yapar
        self.client.force_login(self.cashier_user)

        # Kasiyer POS ekranına girebilir
        res_pos = self.client.get(reverse('pos_screen'))
        self.assertEqual(res_pos.status_code, 200)

        # Kasiyer Dashboard veya Ürün Yönetimi sayfalarına ERİŞEMEZ (Kasa ekranına yönlendirilir)
        res_dash = self.client.get(reverse('admin_dashboard_view'))
        self.assertEqual(res_dash.status_code, 302)
        self.assertEqual(res_dash.url, reverse('pos_screen'))

        res_catalog = self.client.get(reverse('product_catalog_view'))
        self.assertEqual(res_catalog.status_code, 302)
        self.assertEqual(res_catalog.url, reverse('pos_screen'))

    def test_manager_has_full_access(self):
        # Yönetici giriş yapar
        self.client.force_login(self.manager_user)

        res_dash = self.client.get(reverse('admin_dashboard_view'))
        self.assertEqual(res_dash.status_code, 200)

        res_catalog = self.client.get(reverse('product_catalog_view'))
        self.assertEqual(res_catalog.status_code, 200)


class StockControlAndTransactionTest(BasePosTestCase):
    """
    ADIM 4 TESTLERİ:
    - Yetersiz stokta satışın engellenmesi (Stok eksiye düşemez)
    - transaction.atomic() ile yarım kalan satışların engellenmesi
    """
    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier_user)

    def test_insufficient_stock_prevents_sale(self):
        # Kritik çikolatada stok 2 adet var, 5 adet satılmak isteniyor
        payload = {
            'items': [{'product_id': self.product_low_stock.id, 'quantity': 5}],
            'payment_type': 'NAKIT',
            'cash_paid': 125.00,
            'is_return': False
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertFalse(data['success'])
        self.assertIn('Yetersiz Stok', data['message'])

        # Stok kesinlikle değişmemiş ve eksiye düşmemiş olmalı (Hala 2 olmalı)
        self.product_low_stock.refresh_from_db()
        self.assertEqual(self.product_low_stock.stock_quantity, Decimal('2.000'))

        # Veritabanında hiçbir fiş oluşmamış olmalı
        self.assertEqual(Sale.objects.count(), 0)

    def test_successful_sale_decrements_stock(self):
        # Stok 50 adet ekmek, 3 adet satıyoruz
        payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': 3}],
            'payment_type': 'NAKIT',
            'cash_paid': 30.00,
            'is_return': False
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])

        # Stok 50 - 3 = 47 olmalı
        self.product_bread.refresh_from_db()
        self.assertEqual(self.product_bread.stock_quantity, Decimal('47.000'))

        # Fiş ve kalem kaydı oluşmuş olmalı
        sale = Sale.objects.get(receipt_number=data['receipt_number'])
        self.assertEqual(sale.total_amount, Decimal('30.00'))
        self.assertEqual(sale.cashier, self.cashier_user)
        self.assertEqual(sale.items.count(), 1)


class ReturnsAndPaymentSeparationTest(BasePosTestCase):
    """
    ADIM 5 TESTLERİ:
    - İade yapıldığında stoğun artması
    - Nakit iadede kasa nakdinin doğru düşülmesi
    - Z Raporu mali formüllerinin doğruluğu (Brüt - İade = Net Ciro)
    """
    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier_user)

    def test_customer_return_increments_stock(self):
        # Ekmek stoğu: 50. Müşteri 2 adet ekmek iade ediyor.
        payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': 2}],
            'payment_type': 'NAKIT',
            'cash_paid': 0.00,
            'is_return': True
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['is_return'])
        self.assertTrue(data['receipt_number'].startswith('IADE-'))

        # Stok 50 + 2 = 52 olmalı (azalmamalı, artmalı!)
        self.product_bread.refresh_from_db()
        self.assertEqual(self.product_bread.stock_quantity, Decimal('52.000'))

    def test_cash_return_decreases_cash_register(self):
        # 1. Önce 100 TL nakit satış yapalım (10 ekmek x 10 TL = 100 TL)
        sale_payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': 10}],
            'payment_type': 'NAKIT',
            'cash_paid': 100.00,
            'is_return': False
        }
        self.client.post(reverse('process_sale'), data=json.dumps(sale_payload), content_type='application/json')

        self.shift.refresh_from_db()
        # Açılış 500 + 100 = 600 TL
        self.assertEqual(self.shift.total_cash_sales, Decimal('100.00'))
        self.assertEqual(self.shift.expected_cash, Decimal('600.00'))

        # 2. Şimdi 20 TL nakit iade alalım (2 ekmek x 10 TL = 20 TL iade)
        return_payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': 2}],
            'payment_type': 'NAKIT',
            'cash_paid': 0.00,
            'is_return': True
        }
        self.client.post(reverse('process_sale'), data=json.dumps(return_payload), content_type='application/json')

        self.shift.refresh_from_db()
        # Net nakit satış: 100 - 20 = 80 TL
        # Kasadaki beklenen nakit: 500 + 80 = 580 TL olmalı!
        self.assertEqual(self.shift.total_cash_sales, Decimal('80.00'))
        self.assertEqual(self.shift.expected_cash, Decimal('580.00'))

    def test_z_report_revenue_calculation(self):
        # 100 TL Nakit Satış + 50 TL Kart Satışı + 20 TL Nakit İade
        self.client.post(reverse('process_sale'), data=json.dumps({
            'items': [{'product_id': self.product_bread.id, 'quantity': 10}],
            'payment_type': 'NAKIT',
            'cash_paid': 100.00,
            'is_return': False
        }), content_type='application/json')

        self.client.post(reverse('process_sale'), data=json.dumps({
            'items': [{'product_id': self.product_bread.id, 'quantity': 5}],
            'payment_type': 'KART',
            'cash_paid': 50.00,
            'is_return': False
        }), content_type='application/json')

        self.client.post(reverse('process_sale'), data=json.dumps({
            'items': [{'product_id': self.product_bread.id, 'quantity': 2}],
            'payment_type': 'NAKIT',
            'cash_paid': 0.00,
            'is_return': True
        }), content_type='application/json')

        # Z Raporunu çek
        res = self.client.get(reverse('z_report_view'))
        self.assertEqual(res.status_code, 200)

        # Context verilerini doğrula:
        # Brüt Nakit: 100 TL, Brüt Kart: 50 TL
        # İade: 20 TL
        # Net Ciro: 100 + 50 - 20 = 130 TL olmalı!
        self.assertEqual(res.context['gross_cash_sales'], Decimal('100.00'))
        self.assertEqual(res.context['gross_card_sales'], Decimal('50.00'))
        self.assertEqual(res.context['total_returns'], Decimal('20.00'))
        self.assertEqual(res.context['total_revenue'], Decimal('130.00'))


class SecurityAndInputValidationTest(BasePosTestCase):
    """
    GİRDİ DOĞRULAMA VE GÜVENLİK TESTLERİ:
    - Negatif miktar engellemesi
    - Pasif ürün satış engellemesi
    - Eksik nakit ödeme kontrolü
    """
    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier_user)

    def test_negative_quantity_is_rejected(self):
        payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': -5}],
            'payment_type': 'NAKIT',
            'cash_paid': 100.00,
            'is_return': False
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 400)
        self.assertFalse(res.json()['success'])

    def test_inactive_product_is_rejected(self):
        payload = {
            'items': [{'product_id': self.product_inactive.id, 'quantity': 1}],
            'payment_type': 'NAKIT',
            'cash_paid': 20.00,
            'is_return': False
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('satışa kapalıdır', res.json()['message'])

    def test_insufficient_cash_payment_is_rejected(self):
        # 30 TL tutar için müşteri 20 TL verirse işlem reddedilmeli
        payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': 3}],
            'payment_type': 'NAKIT',
            'cash_paid': 20.00,
            'is_return': False
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('az olamaz', res.json()['message'])


class ScaleBarcodeAndDynamicRegisterTest(BasePosTestCase):
    """
    TERAZİ BARKODU VE DİNAMİK KASA TESTLERİ:
    - 27 ile başlayan EAN-13 ağırlık çözümlemesi
    - KASA-01 harici dinamik kasa desteği
    """
    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier_user)

    def test_scale_barcode_parses_weight_correctly(self):
        # 27 + 00010 (ürün kodu) + 01450 (1.450 kg) + 5 (kontrol hanesi)
        barcode = '2700010014505'
        res = self.client.get(f"{reverse('search_barcode')}?barcode={barcode}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['is_weighted_barcode'])
        self.assertEqual(data['product']['name'], 'Amasya Elması')
        self.assertAlmostEqual(data['quantity'], 1.450, places=3)

    def test_scale_barcode_parses_price_correctly(self):
        # 28 + 00010 (ürün kodu) + 03500 (35.00 TL etiket tutarı) + 0 (kontrol hanesi)
        # Amasya Elması satış fiyatı 35.00 TL ise 35.00 TL / 35.00 TL = 1.000 kg olmalı
        barcode = '2800010035000'
        res = self.client.get(f"{reverse('search_barcode')}?barcode={barcode}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['is_weighted_barcode'])
        self.assertEqual(data['product']['name'], 'Amasya Elması')
        self.assertAlmostEqual(data['quantity'], 1.000, places=3)

    def test_dynamic_register_creation_and_switching(self):
        # KASA-02 parametresi ile istek atıldığında KASA-02 kaydı otomatik oluşmalı
        res = self.client.get(f"{reverse('pos_screen')}?register=KASA-02")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(CashRegister.objects.filter(register_code='KASA-02').exists())


class RegisterIndependenceAndUserManagementTest(BasePosTestCase):
    """
    KASA 1 & KASA 2 TAM BAĞIMSIZLIK VE YENİ KULLANICI YÖNETİMİ TESTLERİ
    """
    def setUp(self):
        super().setUp()
        self.reg2 = CashRegister.objects.create(register_code='KASA-02', name='Kasa 2 (Ek Kasa)', is_active=True)
        self.shift2 = Shift.objects.create(
            cash_register=self.reg2,
            cashier=self.cashier_user,
            opening_float=Decimal('500.00'),
            expected_cash=Decimal('500.00'),
            is_closed=False
        )

    def test_closing_kasa1_does_not_close_kasa2(self):
        """Kasa 1'in vardiyası kapatıldığında Kasa 2 açık kalmalıdır."""
        self.client.force_login(self.manager_user)
        res = self.client.post(
            reverse('close_shift_action'),
            data=json.dumps({
                'shift_id': self.shift.id,
                'register_code': 'KASA-01',
                'actual_cash': 500.00,
                'password': 'Password123!'
            }),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['success'])

        # Kasa 1 kapandı mı?
        self.shift.refresh_from_db()
        self.assertTrue(self.shift.is_closed)

        # Kasa 2 hala açık mı?
        self.shift2.refresh_from_db()
        self.assertFalse(self.shift2.is_closed, "Kasa 1 kapanınca Kasa 2 de kapandı, bu olmamalı!")

    def test_cashier_cannot_access_user_management(self):
        """Kasiyer rolündeki kullanıcı personel yönetimine erişemez."""
        self.client.force_login(self.cashier_user)
        res = self.client.get(reverse('user_management_view'))
        self.assertEqual(res.status_code, 302) # POS ekranına yönlendirilmeli

    def test_manager_can_create_new_staff(self):
        """Yönetici yeni kasiyer personeli başarıyla oluşturabilir."""
        self.client.force_login(self.manager_user)
        res = self.client.post(
            reverse('user_create_action'),
            data={
                'full_name': 'Mehmet Demir',
                'username': 'mehmet_kasiyer',
                'password': '9876',
                'role': 'KASIYER'
            }
        )
        self.assertEqual(res.status_code, 302)
        new_user = User.objects.filter(username='mehmet_kasiyer').first()
        self.assertIsNotNone(new_user)
        self.assertEqual(new_user.first_name, 'Mehmet')
        self.assertFalse(new_user.is_staff) # Kasiyer olduğu için is_staff=False olmalı


class EnterpriseBackupWalPinAndPrinterTest(BasePosTestCase):
    """
    YENİ KURUMSAL GEREKSİNİM TESTLERİ:
    - SQLite WAL Modu kontrolü
    - Z Raporu kapanışında otomatik backup_database() çalışması ve backups/ kaydı
    - Admin panelinde 'Yedek İndir' butonunun çalışması ve yetki kontrolü
    - CSRF korumasının (@csrf_exempt kaldırılması) doğrulanması
    - Dokunmatik PIN Pad (1234) ile hızlı kasiyer girişi
    - ESC/POS sessiz termal fiş baskısı ve RJ11 para çekmecesi tetikleme
    """
    def test_sqlite_wal_mode_on_primary_db_file(self):
        import sqlite3
        from django.conf import settings
        db_file = settings.BASE_DIR / 'db.sqlite3'
        if db_file.exists():
            with sqlite3.connect(str(db_file)) as conn:
                mode = conn.execute('PRAGMA journal_mode;').fetchone()[0].lower()
                self.assertEqual(mode, 'wal')

    def test_z_report_close_automatically_creates_timestamped_backup(self):
        from pathlib import Path
        from django.conf import settings
        self.client.force_login(self.manager_user)

        res = self.client.post(
            reverse('close_shift_action'),
            data=json.dumps({
                'shift_id': self.shift.id,
                'register_code': 'KASA-01',
                'actual_cash': 500.00,
                'password': 'Password123!'
            }),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertIn('backup', data)
        self.assertTrue(data['backup']['success'])

        backup_file = Path(settings.BACKUP_DIR) / data['backup']['filename']
        self.assertTrue(backup_file.exists(), "Z Raporu kapandığında backups/ klasöründe yedek dosyası oluşmalı!")
        self.assertGreater(backup_file.stat().st_size, 0)

    def test_admin_can_download_backup_and_cashier_cannot(self):
        # 1. Kasiyer yedek indiremez
        self.client.force_login(self.cashier_user)
        res_cashier = self.client.get(reverse('download_backup_view'))
        self.assertEqual(res_cashier.status_code, 302)

        # 2. Yönetici tek tıkla yedek indirebilir
        self.client.force_login(self.manager_user)
        res_admin = self.client.get(reverse('download_backup_view'))
        self.assertEqual(res_admin.status_code, 200)
        self.assertIn('attachment; filename="himarket_backup_', res_admin['Content-Disposition'])
        res_admin.close()

    def test_csrf_protection_enforced_on_process_sale(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.cashier_user)
        res = csrf_client.post(
            reverse('process_sale'),
            data=json.dumps({'items': [{'product_id': self.product_bread.id, 'quantity': 1}]}),
            content_type='application/json'
        )
        # CSRF token olmadan yapılan POST isteği 403 Forbidden ile reddedilmeli
        self.assertEqual(res.status_code, 403)

    def test_pin_pad_quick_login_with_1234(self):
        res = self.client.post(
            reverse('login_view'),
            data={
                'login_mode': 'pin',
                'pin_code': '1234',
                'register_code': 'KASA-02'
            }
        )
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.url, reverse('pos_screen'))
        self.assertEqual(self.client.session.get('active_register_code'), 'KASA-02')

    def test_escpos_silent_print_and_drawer_kick_on_sale(self):
        self.client.force_login(self.cashier_user)
        payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': 2}],
            'payment_type': 'NAKIT',
            'cash_paid': 20.00,
            'is_return': False
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertIsNotNone(data.get('silent_print'))
        self.assertTrue(data['silent_print']['success'])
        self.assertTrue(data['silent_print']['drawer_kicked'])
        self.assertGreater(data['silent_print']['bytes_written'], 50)

    def test_atomic_stock_lock_and_f_expression_multiple_lines(self):
        """Aynı ürün sepette birden fazla satırda olduğunda toplam miktarın stok kontrolü ve F() düşümü doğrulanır."""
        self.client.force_login(self.cashier_user)
        # Kritik stoklu çikolata: stokta 2 adet var. Sepette 1 + 2 = 3 adet gönderilirse reddedilmeli.
        payload_fail = {
            'items': [
                {'product_id': self.product_low_stock.id, 'quantity': 1},
                {'product_id': self.product_low_stock.id, 'quantity': 2},
            ],
            'payment_type': 'NAKIT',
            'cash_paid': 100.00,
            'is_return': False
        }
        res_fail = self.client.post(reverse('process_sale'), data=json.dumps(payload_fail), content_type='application/json')
        self.assertEqual(res_fail.status_code, 400)
        self.product_low_stock.refresh_from_db()
        self.assertEqual(self.product_low_stock.stock_quantity, Decimal('2.000'))

        # Sepette 1 + 1 = 2 adet gönderilirse F('stock_quantity') ile 0'a düşmeli
        payload_ok = {
            'items': [
                {'product_id': self.product_low_stock.id, 'quantity': 1},
                {'product_id': self.product_low_stock.id, 'quantity': 1},
            ],
            'payment_type': 'NAKIT',
            'cash_paid': 50.00,
            'is_return': False
        }
        res_ok = self.client.post(reverse('process_sale'), data=json.dumps(payload_ok), content_type='application/json')
        self.assertEqual(res_ok.status_code, 200)
        self.product_low_stock.refresh_from_db()
        self.assertEqual(self.product_low_stock.stock_quantity, Decimal('0.000'))

    def test_admin_dashboard_renders_backup_download_button_and_kasa_cards(self):
        """Yönetici panelinde Yedek İndir butonu, WAL durumu ve Kasa 1 / Kasa 2 kartları görünmelidir."""
        self.client.force_login(self.manager_user)
        res = self.client.get(reverse('admin_dashboard_view'))
        self.assertEqual(res.status_code, 200)
        html = res.content.decode('utf-8')
        self.assertIn('Yedek İndir', html)
        self.assertIn('KASA-01', html)
        self.assertIn('KASA-02', html)

    def test_stock_entry_and_supplier_return_atomic_calculation(self):
        """Mal kabul ve toptancı iadesinde stok ve cari bakiye F() ile doğru güncellenmelidir."""
        self.client.force_login(self.manager_user)
        supplier = Supplier.objects.create(company_name='Örnek Gıda A.Ş.', balance=Decimal('0.00'))

        # 10 adet ekmek mal kabul (Liste: 10 TL, İskonto: %0, KDV: %10 -> Maliyet: 11 TL, Toplam: 110 TL)
        res = self.client.post(reverse('stock_entry_create'), data={
            'supplier_id': supplier.id,
            'invoice_number': 'FAT-001',
            'payment_type': 'VERESIYE',
            'entry_type': 'GIRIS',
            'product_id[]': [self.product_bread.id],
            'quantity[]': ['10'],
            'unit[]': ['Adet'],
            'list_price[]': ['10.00'],
            'discount_rate[]': ['0.00'],
            'vat_rate[]': ['10.00'],
            'buy_price[]': ['11.00'],
        })
        self.assertEqual(res.status_code, 302)
        self.product_bread.refresh_from_db()
        supplier.refresh_from_db()
        self.assertEqual(self.product_bread.stock_quantity, Decimal('60.000'))
        self.assertEqual(supplier.balance, Decimal('110.00'))

    def test_system_logs_register_filtering(self):
        """Sistem loglarında KASA-01 ve KASA-02 filtrelemesi doğru çalışmalıdır."""
        self.client.force_login(self.manager_user)
        res = self.client.get(f"{reverse('system_logs_view')}?register=KASA-01")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context['selected_register'], 'KASA-01')

    def test_vuk507_vat_breakdown_calculation(self):
        """KDV baremleri (%1, %10, %20) yasal formülle matrah ve vergi olarak doğru ayrıştırılmalıdır."""
        from .fiscal_service import calculate_vat_breakdown
        # Ekmek (%1 KDV, 10 TL) x 2 = 20 TL brüt
        # Çikolata (%10 KDV, 25 TL) x 1 = 25 TL brüt
        items = [
            (self.product_bread, Decimal('2.000')),
            (self.product_low_stock, Decimal('1.000')),
        ]
        breakdown = calculate_vat_breakdown(items, discount_amount=Decimal('0.00'))
        
        # %1 KDV: 20 TL / 1.01 = 19.80 TL matrah, 0.20 TL KDV
        self.assertEqual(breakdown['vat_1_base'], Decimal('19.80'))
        self.assertEqual(breakdown['vat_1_amount'], Decimal('0.20'))
        # %10 KDV: 25 TL / 1.10 = 22.73 TL matrah, 2.27 TL KDV
        self.assertEqual(breakdown['vat_10_base'], Decimal('22.73'))
        self.assertEqual(breakdown['vat_10_amount'], Decimal('2.27'))
        self.assertEqual(breakdown['raw_gross_total'], Decimal('45.00'))

    def test_vuk507_gib_qr_code_and_ettn_generation(self):
        """GİB VUK 507 Karekod dizisi, ETTN ve onay kodu standart formatta üretilmelidir."""
        from .fiscal_service import generate_gib_qr_code_data, build_escpos_qr_command
        self.client.force_login(self.cashier_user)

        # 1 adet ekmek satışı
        res = self.client.post(
            reverse('process_sale'),
            data=json.dumps({
                'items': [{'product_id': self.product_bread.id, 'quantity': 1}],
                'payment_type': 'NAKIT',
                'cash_paid': '10.00'
            }),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        sale_data = res.json()
        sale = Sale.objects.get(receipt_number=sale_data['receipt_number'])

        qr_text, approval_code, verify_url = generate_gib_qr_code_data(sale)
        self.assertIn('VKN:', qr_text)
        self.assertIn('ETTN:', qr_text)
        self.assertIn(str(sale.ettn), qr_text)
        self.assertTrue(approval_code.startswith('HM-'))
        self.assertIn('ebelge.gib.gov.tr', verify_url)

        # ESC/POS QR kod bayt akışı testi
        qr_bytes = build_escpos_qr_command(qr_text)
        self.assertTrue(len(qr_bytes) > 20)
        self.assertEqual(qr_bytes[:3], b'\x1d\x28\x6b')

    def test_process_sale_with_corporate_earsiv_customer(self):
        """Kurumsal fatura talebinde VKN/TCKN ve firma ünvanı satışa ve fişe işlenmelidir."""
        self.client.force_login(self.cashier_user)

        payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': 2}],
            'payment_type': 'KART',
            'cash_paid': '0.00',
            'customer': {
                'tax_number': '9876543210',
                'title': 'Anadolu Market Ltd. Şti.',
                'tax_office': 'Üsküdar V.D.',
                'document_type': 'E_ARSIV'
            }
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        sale = Sale.objects.get(receipt_number=res.json()['receipt_number'])

        self.assertEqual(sale.customer_tax_number, '9876543210')
        self.assertEqual(sale.customer_title, 'Anadolu Market Ltd. Şti.')
        self.assertEqual(sale.customer_tax_office, 'Üsküdar V.D.')
        self.assertEqual(sale.document_type, 'E_ARSIV')
        self.assertIsNotNone(sale.qr_code_data)
        self.assertIn('9876543210', sale.qr_code_data)

    def test_z_report_includes_vuk507_vat_distribution(self):
        """Z Raporu ekranında yasal KDV baremleri toplamı (%1, %10, %20) doğru hesaplanmalıdır."""
        self.client.force_login(self.manager_user)
        # Önce bir satış yapalım
        self.client.post(
            reverse('process_sale'),
            data=json.dumps({
                'items': [{'product_id': self.product_bread.id, 'quantity': 1}],
                'payment_type': 'NAKIT',
                'cash_paid': '10.00'
            }),
            content_type='application/json'
        )

        res = self.client.get(reverse('z_report_view'))
        self.assertEqual(res.status_code, 200)
        self.assertIn('vat_1_base_sum', res.context)
        self.assertIn('vat_10_base_sum', res.context)
        self.assertIn('vat_20_base_sum', res.context)
        self.assertIn('total_vat_sum', res.context)
        html = res.content.decode('utf-8')
        self.assertIn('VUK 507 Yasal Mali KDV Dağılımı', html)

    def test_company_setting_singleton_and_fiscal_profile_sync(self):
        """Dinamik CompanySetting güncellendiğinde fiscal profile ve fiş başlığı anında değişmelidir."""
        from .models import CompanySetting
        from .fiscal_service import get_company_fiscal_profile

        setting = CompanySetting.get_settings()
        setting.title = "GORAL MEGA MARKET A.Ş."
        setting.tax_number = "9988776655"
        setting.fiscal_provider = "NILVERA"
        setting.save()

        profile = get_company_fiscal_profile()
        self.assertEqual(profile['company_title'], "GORAL MEGA MARKET A.Ş.")
        self.assertEqual(profile['tax_number'], "9988776655")
        self.assertEqual(profile['fiscal_provider'], "NILVERA")

    def test_company_settings_view_security_and_update(self):
        """Kasiyer ayarlar sayfasına girememeli, yönetici bilgileri güncelleyebilmelidir."""
        from .models import CompanySetting
        # 1. Kasiyer engeli (302 -> pos_screen)
        self.client.force_login(self.cashier_user)
        res_cashier = self.client.get(reverse('company_settings_view'))
        self.assertEqual(res_cashier.status_code, 302)

        # 2. Yönetici erişimi ve formu kaydetme
        self.client.force_login(self.manager_user)
        res_manager = self.client.get(reverse('company_settings_view'))
        self.assertEqual(res_manager.status_code, 200)
        self.assertIn('setting', res_manager.context)

        post_res = self.client.post(reverse('company_settings_view'), data={
            'title': 'YENİ MÜŞTERİ TİC. LTD. ŞTİ.',
            'brand_name': 'YENİ MARKET',
            'tax_number': '5555444433',
            'tax_office': 'BEŞİKTAŞ V.D.',
            'address': 'Çarşı Cad. No:1 Beşiktaş / İSTANBUL',
            'fiscal_mode': 'VUK507_EARSIV',
            'fiscal_provider': 'EDM',
            'fiscal_environment': 'PRODUCTION',
            'printer_type': 'SPOOL_AND_SOCKET',
            'printer_ip': '192.168.1.150',
            'printer_port': '9100',
            'paper_width': '80',
            'drawer_kick_on_cash': '1',
            'receipt_footer_note': 'Yeni fiş altı mesajı!',
        })
        self.assertEqual(post_res.status_code, 302)

        updated_setting = CompanySetting.get_settings()
        self.assertEqual(updated_setting.title, 'YENİ MÜŞTERİ TİC. LTD. ŞTİ.')
        self.assertEqual(updated_setting.tax_number, '5555444433')
        self.assertEqual(updated_setting.fiscal_provider, 'EDM')
        self.assertEqual(updated_setting.printer_ip, '192.168.1.150')
        self.assertTrue(updated_setting.drawer_kick_on_cash)

    def test_test_print_settings_api(self):
        """Yönetici panelinden deneme fişi basma API'si başarıyla çalışmalıdır."""
        self.client.force_login(self.manager_user)
        res = self.client.post(reverse('test_print_settings_api'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertIn('Deneme fişi', data['message'])


class RetailCashierOperationsPhase2Test(BasePosTestCase):
    """
    FAZ 2: PERAKENDE KASA, PARÇALI ÖDEME, VERESİYE DEFTERİ VE BORÇ TAHSİLATI TESTLERİ
    """
    def setUp(self):
        super().setUp()
        self.client.force_login(self.cashier_user)

        # Test Müşterisi Tanımlama
        self.customer = Customer.objects.create(
            first_name="Mehmet",
            last_name="Demir",
            phone="05321112233",
            tax_number="12345678901",
            company_title="Demir Ticaret",
            credit_limit=Decimal('500.00'),
            balance=Decimal('0.00'),
            is_active=True
        )

    def test_split_payment_process_sale_success(self):
        """Parçalı ödemede (Nakit + Kart) toplam tutar doğru bölünmeli ve vardiyaya işlenmelidir."""
        # 10 ekmek x 10 TL = 100 TL. 40 TL Nakit + 60 TL Kredi Kartı
        payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': 10}],
            'payment_type': 'PARCALI',
            'cash_paid': 40.00,
            'card_paid': 60.00,
            'is_return': False
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])

        sale = Sale.objects.get(receipt_number=data['receipt_number'])
        self.assertEqual(sale.payment_type, 'PARCALI')
        self.assertEqual(sale.cash_paid, Decimal('40.00'))
        self.assertEqual(sale.card_paid, Decimal('60.00'))
        self.assertEqual(sale.total_amount, Decimal('100.00'))

        # Vardiya kontrolleri: devir 500 + 40 nakit = 540 beklenen nakit, kart satışı = 60
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.total_cash_sales, Decimal('40.00'))
        self.assertEqual(self.shift.total_card_sales, Decimal('60.00'))
        self.assertEqual(self.shift.expected_cash, Decimal('540.00'))

    def test_split_payment_insufficient_payment_rejected(self):
        """Parçalı ödemede nakit + kart toplamı fiş tutarından az ise reddedilmelidir."""
        # 10 ekmek = 100 TL. Ancak 30 TL Nakit + 50 TL Kart = 80 TL verilmiş.
        payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': 10}],
            'payment_type': 'PARCALI',
            'cash_paid': 30.00,
            'card_paid': 50.00,
            'is_return': False
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertFalse(data['success'])
        self.assertIn('az olamaz', data['message'])

    def test_customer_search_api(self):
        """Müşteri arama API'si isim veya telefonla <10ms içinde sonuç döndürmelidir."""
        # 1. İsme göre ara
        res = self.client.get(reverse('customer_search_api') + '?q=Mehmet')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['customers']), 1)
        self.assertEqual(data['customers'][0]['id'], self.customer.id)

        # 2. Telefona göre ara
        res2 = self.client.get(reverse('customer_search_api') + '?q=0532111')
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertEqual(len(data2['customers']), 1)

    def test_veresiye_sale_and_credit_limit_enforcement(self):
        """Veresiye satış borcu artırmalı ve müşteri kredi limitini aşarsa satışı bloke etmelidir."""
        # Stok miktarını artıralım ki stok engeline takılmadan kredi limiti test edilebilsin
        self.product_bread.stock_quantity = Decimal('100.000')
        self.product_bread.save()

        # 1. 200 TL Veresiye Satış Yap (20 ekmek x 10 TL)
        payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': 20}],
            'payment_type': 'VERESIYE',
            'customer_id': self.customer.id,
            'is_return': False
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, Decimal('200.00'))
        self.assertEqual(self.customer.remaining_credit, Decimal('300.00'))  # 500 - 200 = 300 TL

        # Cari Hareket Kaydını Kontrol Et
        tx = CustomerTransaction.objects.filter(customer=self.customer, transaction_type='VERESIYE_SATIS').first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.amount, Decimal('200.00'))
        self.assertEqual(tx.balance_after, Decimal('200.00'))

        # Vardiya veresiye sayacını kontrol et
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.total_veresiye_sales, Decimal('200.00'))

        # 2. Limiti Aşan Satış Dene (Mevcut borç 200 + Yeni talep 400 = 600 > 500 Limit)
        overflow_payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': 40}],
            'payment_type': 'VERESIYE',
            'customer_id': self.customer.id,
            'is_return': False
        }
        res_overflow = self.client.post(reverse('process_sale'), data=json.dumps(overflow_payload), content_type='application/json')
        self.assertEqual(res_overflow.status_code, 400)
        data_over = res_overflow.json()
        self.assertFalse(data_over['success'])
        self.assertIn('Veresiye Limiti Aşıldı', data_over['message'])

    def test_customer_debt_collection_cash_and_card(self):
        """Veresiye borç tahsilatı bakiyeyi düşürmeli, makbuz üretmeli ve nakit ise kasaya eklenmelidir."""
        # Müşterinin 300 TL borcu olsun
        self.customer.balance = Decimal('300.00')
        self.customer.save()

        # 1. 100 TL NAKİT Tahsilat
        collect_cash_payload = {
            'amount': '100.00',
            'payment_method': 'NAKIT',
            'notes': 'Elden nakit ödeme'
        }
        res_cash = self.client.post(
            reverse('customer_collect_debt_api', kwargs={'customer_id': self.customer.id}),
            data=json.dumps(collect_cash_payload),
            content_type='application/json'
        )
        self.assertEqual(res_cash.status_code, 200)
        data_cash = res_cash.json()
        self.assertTrue(data_cash['success'])
        self.assertEqual(data_cash['new_balance'], 200.00)

        # Müşteri bakiyesi 200 TL olmalı
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, Decimal('200.00'))

        # Vardiya nakit kasası: 500 (devir) + 100 (tahsilat) = 600 TL olmalı!
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.total_collections, Decimal('100.00'))
        self.assertEqual(self.shift.expected_cash, Decimal('600.00'))

        # 2. 200 TL KREDİ KARTI Tahsilatı
        collect_card_payload = {
            'amount': '200.00',
            'payment_method': 'KART',
            'notes': 'POS ile çekildi'
        }
        res_card = self.client.post(
            reverse('customer_collect_debt_api', kwargs={'customer_id': self.customer.id}),
            data=json.dumps(collect_card_payload),
            content_type='application/json'
        )
        self.assertEqual(res_card.status_code, 200)
        data_card = res_card.json()
        self.assertEqual(data_card['new_balance'], 0.00)

        # Müşteri borcu tamamen kapanmış olmalı (0 TL)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, Decimal('0.00'))

        # Toplam tahsilat 100 + 200 = 300 TL, ancak kart kasadaki fiziki nakdi artırmaz (hala 600 TL)
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.total_collections, Decimal('300.00'))
        self.assertEqual(self.shift.expected_cash, Decimal('600.00'))

    def test_customer_management_views_and_detail(self):
        """Yönetici müşteri listesi, detay ekstresi ve termal tahsilat makbuzunu görüntüleyebilmelidir."""
        self.client.force_login(self.manager_user)

        # 1. Liste Görünümü
        res_list = self.client.get(reverse('customer_list_view'))
        self.assertEqual(res_list.status_code, 200)
        self.assertContains(res_list, self.customer.full_name)

        # 2. Detay & Ekstre Görünümü
        res_detail = self.client.get(reverse('customer_detail_view', kwargs={'customer_id': self.customer.id}))
        self.assertEqual(res_detail.status_code, 200)
        self.assertContains(res_detail, self.customer.phone)

        # 3. Yeni Müşteri Oluşturma
        create_res = self.client.post(reverse('customer_create_view'), data={
            'first_name': 'Ali',
            'last_name': 'Kaya',
            'phone': '05443332211',
            'credit_limit': '3000.00',
            'is_active': 'on'
        })
        self.assertEqual(create_res.status_code, 302)
        self.assertTrue(Customer.objects.filter(phone='05443332211').exists())

        # 4. Tahsilat Makbuzu Görünümü
        tx = CustomerTransaction.objects.create(
            customer=self.customer,
            transaction_type='TAHSILAT_NAKIT',
            amount=Decimal('50.00'),
            balance_after=Decimal('0.00'),
            shift=self.shift,
            document_no='TAH-20261104-0001',
            user=self.manager_user
        )
        res_receipt = self.client.get(reverse('collection_receipt_view', kwargs={'transaction_id': tx.id}))
        self.assertEqual(res_receipt.status_code, 200)
        self.assertContains(res_receipt, 'CARİ BORÇ TAHSİLAT MAKBUZU')
        self.assertContains(res_receipt, 'TAH-20261104-0001')
        self.assertContains(res_receipt, '50')


class HardwareAndPeripheralsPhase3Tests(BasePosTestCase):
    """
    Faz 3: Canlı Donanım, Terazi, 2. Müşteri Ekranı ve Raf Etiketi Entegrasyon Testleri
    """
    def setUp(self):
        super().setUp()
        self.company_setting = CompanySetting.get_settings()
        self.company_setting.scale_enabled = True
        self.company_setting.scale_port = 'LOCAL_MOCK'
        self.company_setting.scale_mock_weight = Decimal('1.750')
        self.company_setting.customer_display_enabled = True
        self.company_setting.customer_display_welcome_msg = 'TEST MARKET HOŞ GELDİNİZ'
        self.company_setting.save()

    def test_scale_read_api(self):
        """Elektronik kasa terazisinden ağırlık okuma API'si doğru veri ve birim dönmelidir."""
        # Kasiyer girişli istek
        self.client.force_login(self.cashier_user)
        res = self.client.get(reverse('scale_read_api'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['weight'], 1.750)
        self.assertEqual(data['unit'], 'kg')
        self.assertTrue(data['is_stable'])

        # Mock ağırlık değiştiğinde güncel ağırlık dönmeli
        self.company_setting.scale_mock_weight = Decimal('3.200')
        self.company_setting.save()
        res2 = self.client.get(reverse('scale_read_api'))
        self.assertEqual(res2.json()['weight'], 3.200)

    def test_scale_ports_api(self):
        """Sistemdeki seri / COM portları listeleyen API başarıyla yanıt vermelidir."""
        self.client.force_login(self.manager_user)
        res = self.client.get(reverse('scale_ports_api'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertIn('ports', data)
        self.assertIsInstance(data['ports'], list)

    def test_customer_display_view(self):
        """2. Ekran / Müşteri Fiyat Göstergesi HTML sayfası açılmalı ve firma ile kasa bilgilerini içermelidir."""
        res = self.client.get(reverse('customer_display_view'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, self.company_setting.brand_name)
        self.assertContains(res, 'CANLI BAĞLANTI AKTİF')
        self.assertContains(res, 'himarket_pos_channel')

    def test_barcode_service_ean13_calculation_and_svg(self):
        """EAN-13 Modulo-10 sağlama basamağı ve saf Python SVG barkod üretimi doğru çalışmalıdır."""
        from .barcode_service import calculate_ean13_checksum, validate_ean13, generate_ean13_svg

        # 1. 12 haneli gövdeden 13. kontrol basamağı hesabı
        body = "869012345678"
        check_digit = calculate_ean13_checksum(body)
        full_barcode = body + str(check_digit)
        self.assertEqual(len(full_barcode), 13)
        self.assertTrue(validate_ean13(full_barcode))

        # Hatalı kontrol basamağı reddedilmeli
        wrong_barcode = body + str((check_digit + 1) % 10)
        self.assertFalse(validate_ean13(wrong_barcode))

        # 2. Saf Python SVG üretimi
        svg = generate_ean13_svg(full_barcode)
        self.assertTrue(svg.startswith('<svg'))
        self.assertIn('</svg>', svg)
        self.assertIn('<rect', svg)

    def test_generate_barcode_api(self):
        """Otomatik EAN-13 barkod oluşturma API'si geçerli ve benzersiz 13 haneli barkod üretmelidir."""
        self.client.force_login(self.manager_user)
        res = self.client.get(reverse('generate_barcode_api'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        barcode = data['barcode']
        self.assertEqual(len(barcode), 13)
        self.assertTrue(barcode.isdigit())

        from .barcode_service import validate_ean13
        self.assertTrue(validate_ean13(barcode))

    def test_product_labels_view(self):
        """Yönetici raf ve reyon etiketi basım sayfasını görüntüleyebilmelidir."""
        self.client.force_login(self.manager_user)
        res = self.client.get(reverse('product_labels_view'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'RAF ETİKETİ VE BARKOD BASIMI')
        self.assertContains(res, self.product_bread.name)
        self.assertContains(res, 'TÜRKİYE')  # Bakanlık zorunlu yerli üretim menşei
        self.assertContains(res, 'KDV DAHİL')

    def test_product_label_raw_api(self):
        """Endüstriyel etiket yazıcıları için doğrudan TSPL ve ZPL kod üreten API doğru format dönmelidir."""
        self.client.force_login(self.manager_user)

        # 1. TSPL Etiket Komutu
        res_tspl = self.client.get(
            reverse('product_label_raw_api', kwargs={'product_id': self.product_bread.id}) + '?lang=tspl'
        )
        self.assertEqual(res_tspl.status_code, 200)
        data_tspl = res_tspl.json()
        self.assertTrue(data_tspl['success'])
        self.assertEqual(data_tspl['lang'], 'tspl')
        self.assertIn('SIZE 60 mm, 40 mm', data_tspl['raw_commands'])
        self.assertIn('PRINT 1', data_tspl['raw_commands'])

        # 2. ZPL Etiket Komutu
        res_zpl = self.client.get(
            reverse('product_label_raw_api', kwargs={'product_id': self.product_bread.id}) + '?lang=zpl'
        )
        self.assertEqual(res_zpl.status_code, 200)
        data_zpl = res_zpl.json()
        self.assertTrue(data_zpl['success'])
        self.assertEqual(data_zpl['lang'], 'zpl')
        self.assertIn('^XA', data_zpl['raw_commands'])
        self.assertIn('^XZ', data_zpl['raw_commands'])

    def test_label_service_ministry_compliance(self):
        """Bakanlık Fiyat Etiketi Yönetmeliği gereği birim fiyat ve menşei doğru hazırlanmalıdır."""
        from .label_service import prepare_product_label_context

        label_data = prepare_product_label_context(self.product_bread)
        self.assertEqual(label_data['origin'], 'TÜRKİYE')
        self.assertIn('1 Adet = 10.00 TL', label_data['unit_price_display'])
        self.assertTrue(label_data['barcode_svg'].startswith('<svg'))


class CashierSecurityAndPhase4Tests(BasePosTestCase):
    """
    Faz 4: Kasa Güvenliği, Kör Sayım (Blind Shift Close) ve Yönetici PIN İptal Onayı Testleri
    """
    def setUp(self):
        super().setUp()
        self.company_setting = CompanySetting.get_settings()
        self.company_setting.blind_close_enabled = True
        self.company_setting.supervisor_cancel_required = True
        self.company_setting.supervisor_discount_limit_percent = Decimal('10.00')
        self.company_setting.save()

    def test_blind_shift_closing_hides_expected_cash_from_cashier(self):
        """Kör sayım aktifken kasiyer beklenen nakdi görememeli, fark sadece yöneticiye yansımalıdır."""
        # 1. Kasiyer Z raporu sayfasını ziyaret ettiğinde beklenen nakit gizlenmiş olmalıdır
        self.client.force_login(self.cashier_user)
        res_view = self.client.get(reverse('z_report_view'))
        self.assertEqual(res_view.status_code, 200)
        self.assertContains(res_view, 'KÖR KASA SAYIMI AKTİF')
        self.assertContains(res_view, 'GİZLİ')

        # 2. Kasiyer çekmecedeki parayı banknot ve madeni detaylarıyla sayıp girer
        denom_counts = {
            '200': 3,   # 600 TL
            '100': 4,   # 400 TL
            'coins': 50 # 50 TL -> Toplam 1050 TL sayıldı
        }
        close_payload = {
            'shift_id': self.shift.id,
            'register_code': self.register.register_code,
            'actual_cash': 1050.00,
            'denominations': denom_counts,
            'notes': 'Kör sayım ile kapatıldı',
            'password': 'Password123!'
        }
        res_close = self.client.post(
            reverse('close_shift_action'),
            data=json.dumps(close_payload),
            content_type='application/json'
        )
        self.assertEqual(res_close.status_code, 200)
        data = res_close.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['is_blind_close'])
        # Kasiyere fark veya beklenen nakit sızdırılmamalıdır
        self.assertNotIn('difference', data)
        self.assertNotIn('expected_cash', data)

        # 3. Veritabanında sayım dökümü kaydedilmiş olmalıdır
        self.shift.refresh_from_db()
        self.assertTrue(self.shift.is_closed)
        self.assertEqual(self.shift.actual_cash, Decimal('1050.00'))
        self.assertEqual(self.shift.denomination_counts, denom_counts)

        # 4. Yönetici Z raporunu açtığında gerçek farkı ve beklenen nakdi görebilmelidir
        self.client.force_login(self.manager_user)
        res_mgr_view = self.client.get(reverse('z_report_view') + f'?shift_id={self.shift.id}')
        self.assertEqual(res_mgr_view.status_code, 200)
        # Yönetici fark rozetini görebilir
        self.assertContains(res_mgr_view, 'KASA FARKI')

    def test_manager_close_shift_shows_difference(self):
        """Yönetici Z raporunu kapattığında kasa farkı ve beklenen nakit doğrudan gösterilmelidir."""
        self.client.force_login(self.manager_user)
        close_payload = {
            'shift_id': self.shift.id,
            'register_code': self.register.register_code,
            'actual_cash': 500.00,
            'notes': 'Müdür sayımı',
            'password': 'Password123!'
        }
        res = self.client.post(
            reverse('close_shift_action'),
            data=json.dumps(close_payload),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertFalse(data['is_blind_close'])
        self.assertIn('difference', data)
        self.assertIn('expected_cash', data)

    def test_verify_supervisor_pin_api(self):
        """Yetkili PIN doğrulama servisi yönetici şifresini onaylamalı, hatalı şifreyi reddetmelidir."""
        from .models import SystemLog

        self.client.force_login(self.cashier_user)

        # 1. Başarılı Yetkili Onayı (Yöneticinin şifresi girildiğinde)
        valid_payload = {
            'pin': 'Password123!',
            'action': 'SATIR_SILME',
            'details': 'Somun Ekmek sepetten silindi',
            'register_code': 'KASA-01'
        }
        res_ok = self.client.post(
            reverse('verify_supervisor_pin_api'),
            data=json.dumps(valid_payload),
            content_type='application/json'
        )
        self.assertEqual(res_ok.status_code, 200)
        data_ok = res_ok.json()
        self.assertTrue(data_ok['success'])
        self.assertEqual(data_ok['supervisor_name'], self.manager_user.get_full_name() or self.manager_user.username)

        # Denetim izinde SECURITY logu oluşmuş olmalı
        self.assertTrue(
            SystemLog.objects.filter(level='SECURITY', action__icontains='Yetkili Onayı Verildi').exists()
        )

        # 2. Hatalı Şifre Denemesi (Reddedilmeli)
        invalid_payload = {
            'pin': 'YanlisSifre999',
            'action': 'FIS_IPTAL',
            'details': 'Sepeti tamamen iptal etme',
            'register_code': 'KASA-01'
        }
        res_fail = self.client.post(
            reverse('verify_supervisor_pin_api'),
            data=json.dumps(invalid_payload),
            content_type='application/json'
        )
        self.assertEqual(res_fail.status_code, 403)
        data_fail = res_fail.json()
        self.assertFalse(data_fail['success'])

        # Denetim izinde WARNING logu oluşmuş olmalı
        self.assertTrue(
            SystemLog.objects.filter(level='WARNING', action__icontains='Yetkisiz İşlem Girişimi').exists()
        )

    def test_manual_drawer_kick_security_audit(self):
        """Satış olmadan çekmece manuel tetiklendiğinde güvenlik denetim logu yazılmalıdır."""
        from .models import SystemLog

        self.client.force_login(self.cashier_user)
        res = self.client.post(reverse('open_cash_drawer_api'))
        self.assertEqual(res.status_code, 200)

        # SECURITY seviyesinde denetim kaydı olmalıdır
        self.assertTrue(
            SystemLog.objects.filter(
                level='SECURITY',
                action__icontains='Satışsız Manuel Çekmece Açıldı'
            ).exists()
        )

    def test_system_logs_view_shows_audit_kpis(self):
        """Yönetici sistem denetim loglarını ve güvenlik KPI özet kartlarını görebilmelidir."""
        from .models import log_action

        # Örnek güvenlik logları oluştur
        log_action('POS', '[KASA-01] Satışsız Manuel Çekmece Açıldı', 'Test çekmece', level='SECURITY', user=self.cashier_user)
        log_action('POS', '[KASA-01] Yetkili Onayı Verildi (SATIR_SILME)', 'Müdür onayı', level='SECURITY', user=self.manager_user)

        self.client.force_login(self.manager_user)
        res = self.client.get(reverse('system_logs_view'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Satışsız Manuel Çekmece')
        self.assertContains(res, 'Yetkili Onaylı İptaller')
        self.assertContains(res, 'Kasa Farkı Olan Vardiya')


class InventoryAndStockTakePhase5Tests(BasePosTestCase):
    """
    Faz 5: Envanter & Stok Sayım (El Terminali), Fire/Zayi & Kritik Stok Testleri
    """
    def setUp(self):
        super().setUp()

        # Örnek Toptancı
        self.supplier = Supplier.objects.create(
            company_name='Sütaş Dağıtım A.Ş.',
            contact_name='Kemal Bey',
            phone='05321112233',
            balance=Decimal('5000.00'),
            is_active=True
        )

        # Ürün 1: Kritik stok seviyesinde (Mevcut: 10 <= Asgari: 15)
        self.prod_critical = Product.objects.create(
            barcode='8690000000010',
            name='Sütaş Süt 1L',
            category=self.category,
            supplier=self.supplier,
            buy_price=Decimal('25.00'),
            sell_price=Decimal('35.00'),
            vat_rate=Decimal('1.00'),
            stock_quantity=Decimal('10.000'),
            min_stock_level=Decimal('15.000'),
            unit='Adet',
            is_active=True
        )

        # Ürün 2: Yeterli stok (Mevcut: 50 > Asgari: 10)
        self.prod_healthy = Product.objects.create(
            barcode='8690000000027',
            name='Çaykur Rize Çay 1KG',
            category=self.category,
            supplier=self.supplier,
            buy_price=Decimal('120.00'),
            sell_price=Decimal('160.00'),
            vat_rate=Decimal('1.00'),
            stock_quantity=Decimal('50.000'),
            min_stock_level=Decimal('10.000'),
            unit='Adet',
            is_active=True
        )

        # Ürün 3: Toptancısız ve Kritik Stok (Mevcut: 2 <= Asgari: 20)
        self.prod_unassigned = Product.objects.create(
            barcode='8690000000034',
            name='Eti Burçak Bisküvi',
            category=self.category,
            supplier=None,
            buy_price=Decimal('15.00'),
            sell_price=Decimal('22.50'),
            vat_rate=Decimal('1.00'),
            stock_quantity=Decimal('2.000'),
            min_stock_level=Decimal('20.000'),
            unit='Adet',
            is_active=True
        )

    def test_stock_take_create_and_scan_flow(self):
        """Stok sayım fişi başlatılmalı, el terminali ile okutulan ürünler fark ve maliyetle işlenmelidir."""
        self.client.force_login(self.manager_user)

        # 1. Yeni sayım fişi oluştur
        create_res = self.client.post(
            reverse('stock_take_create_action'),
            data={'title': 'Süt Ürünleri Sayımı', 'category_id': self.category.id, 'notes': 'Test sayımı'}
        )
        self.assertEqual(create_res.status_code, 302)
        take = StockTake.objects.filter(title='Süt Ürünleri Sayımı').first()
        self.assertIsNotNone(take)
        self.assertEqual(take.status, 'DRAFT')

        # 2. El terminali ile ilk okutma: 5 Adet sayıldı (Sistemde 10 vardı -> Fark: -5)
        scan_res1 = self.client.post(
            reverse('stock_take_scan_api', kwargs={'take_id': take.id}),
            data={'barcode': self.prod_critical.barcode, 'quantity': '5.000', 'mode': 'add'}
        )
        self.assertEqual(scan_res1.status_code, 200)
        data1 = scan_res1.json()
        self.assertTrue(data1['success'])
        self.assertTrue(data1['is_new'])
        self.assertEqual(data1['item']['counted_quantity'], 5.0)
        self.assertEqual(data1['item']['difference'], -5.0)
        self.assertEqual(data1['item']['discrepancy_cost'], -125.0)

        # 3. İkinci okutma: Aynı barkod 3 adet daha okutuldu (Mod: add -> 5 + 3 = 8)
        scan_res2 = self.client.post(
            reverse('stock_take_scan_api', kwargs={'take_id': take.id}),
            data={'barcode': self.prod_critical.barcode, 'quantity': '3.000', 'mode': 'add'}
        )
        self.assertEqual(scan_res2.status_code, 200)
        data2 = scan_res2.json()
        self.assertTrue(data2['success'])
        self.assertFalse(data2['is_new'])
        self.assertEqual(data2['item']['counted_quantity'], 8.0)
        self.assertEqual(data2['item']['difference'], -2.0)
        self.assertEqual(data2['item']['discrepancy_cost'], -50.0)

        # 4. Kasiyer yetkisiz erişim testi (Yönetici yetkisi şarttır)
        self.client.force_login(self.cashier_user)
        unauth_res = self.client.post(
            reverse('stock_take_scan_api', kwargs={'take_id': take.id}),
            data={'barcode': self.prod_critical.barcode, 'quantity': '1.000'}
        )
        self.assertEqual(unauth_res.status_code, 403)

    def test_stock_take_item_update_and_delete(self):
        """Sayım satırındaki fiziki miktar güncellenebilmeli ve satır silinebilmelidir."""
        self.client.force_login(self.manager_user)
        take = StockTake.objects.create(take_number='SYM-TEST-001', title='Test Reyon Sayımı')
        item = StockTakeItem.objects.create(
            stock_take=take,
            product=self.prod_critical,
            system_quantity=Decimal('10.000'),
            counted_quantity=Decimal('8.000'),
            buy_price=Decimal('25.00')
        )

        # 1. Miktar doğrudan 15 olarak güncellendi (Fark: +5 fazla)
        upd_res = self.client.post(
            reverse('stock_take_item_update_api', kwargs={'item_id': item.id}),
            data={'counted_quantity': '15.000'}
        )
        self.assertEqual(upd_res.status_code, 200)
        data = upd_res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['item']['counted_quantity'], 15.0)
        self.assertEqual(data['item']['difference'], 5.0)
        self.assertEqual(data['item']['discrepancy_cost'], 125.0)

        # 2. Satırı sil
        del_res = self.client.post(
            reverse('stock_take_item_update_api', kwargs={'item_id': item.id}),
            data={'action': 'delete'}
        )
        self.assertEqual(del_res.status_code, 200)
        self.assertTrue(del_res.json()['deleted'])
        self.assertFalse(StockTakeItem.objects.filter(id=item.id).exists())

    def test_stock_take_apply_api_updates_stocks_and_creates_sayim_duzeltme_entry(self):
        """Sayım onaylandığında sistem stokları fiziki sayıma eşitlenmeli ve SAYIM_DUZELTME fişi oluşmalıdır."""
        self.client.force_login(self.manager_user)
        take = StockTake.objects.create(take_number='SYM-TEST-999', title='Yıl Sonu Sayımı')

        # prod_critical: sistem 10, fiziki 7 (3 eksik)
        StockTakeItem.objects.create(
            stock_take=take,
            product=self.prod_critical,
            system_quantity=Decimal('10.000'),
            counted_quantity=Decimal('7.000'),
            buy_price=self.prod_critical.buy_price
        )
        # prod_healthy: sistem 50, fiziki 54 (4 fazla)
        StockTakeItem.objects.create(
            stock_take=take,
            product=self.prod_healthy,
            system_quantity=Decimal('50.000'),
            counted_quantity=Decimal('54.000'),
            buy_price=self.prod_healthy.buy_price
        )

        # Sayımı onayla
        apply_res = self.client.post(reverse('stock_take_apply_api', kwargs={'take_id': take.id}))
        self.assertEqual(apply_res.status_code, 302)

        # Sayım durumu COMPLETED olmalı
        take.refresh_from_db()
        self.assertEqual(take.status, 'COMPLETED')
        self.assertIsNotNone(take.completed_at)

        # Ürünlerin gerçek stokları güncellenmiş olmalı
        self.prod_critical.refresh_from_db()
        self.assertEqual(self.prod_critical.stock_quantity, Decimal('7.000'))

        self.prod_healthy.refresh_from_db()
        self.assertEqual(self.prod_healthy.stock_quantity, Decimal('54.000'))

        # Denetim için SAYIM_DUZELTME fişi oluşmuş olmalı
        entry = StockEntry.objects.filter(entry_type='SAYIM_DUZELTME', invoice_number=take.take_number).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.items.count(), 2)

    def test_fire_zayi_entry_decreases_stock_without_touching_supplier_balance(self):
        """Fire/Zayiat çıkışı stok miktarını düşürmeli fakat toptancı cari bakiyesini etkilememelidir."""
        self.client.force_login(self.manager_user)
        initial_balance = self.supplier.balance  # 5000.00 TL
        initial_stock = self.prod_critical.stock_quantity  # 10.000 Adet

        fire_payload = {
            'entry_type': 'FIRE_ZAYI',
            'notes': 'Kırılan ve patlayan sütler',
            'product_id[]': [str(self.prod_critical.id)],
            'quantity[]': ['3.000'],
            'unit[]': ['Adet'],
            'list_price[]': ['0.00'],
            'discount_rate[]': ['0.00'],
            'vat_rate[]': ['1.00'],
            'buy_price[]': ['25.00'],
        }

        res = self.client.post(reverse('stock_entry_create'), data=fire_payload)
        self.assertEqual(res.status_code, 302)

        # Ürün stoğu 3 adet düşmüş olmalı (10 - 3 = 7)
        self.prod_critical.refresh_from_db()
        self.assertEqual(self.prod_critical.stock_quantity, Decimal('7.000'))

        # Toptancı bakiyesi kesinlikle değişmemelidir (Fire işletmenin zayiatıdır, toptancıya borç/alacak değildir)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.balance, initial_balance)

        # StockEntry kaydı FIRE_ZAYI türünde oluşturulmuş olmalıdır
        entry = StockEntry.objects.filter(entry_type='FIRE_ZAYI').order_by('-created_at').first()
        self.assertIsNotNone(entry)
        self.assertTrue(entry.entry_number.startswith('FIRE-'))

    def test_sarf_tuketim_entry_decreases_stock_without_touching_supplier_balance(self):
        """İşletme içi sarf fişi stok miktarını düşürmeli, cari bakiyeye dokunmamalıdır."""
        self.client.force_login(self.manager_user)
        initial_balance = self.supplier.balance
        initial_stock = self.prod_critical.stock_quantity

        sarf_payload = {
            'entry_type': 'SARF_TUKETIM',
            'notes': 'Personel çay-kahve ikramı ve tüketim',
            'product_id[]': [str(self.prod_critical.id)],
            'quantity[]': ['2.000'],
            'unit[]': ['Adet'],
            'list_price[]': ['0.00'],
            'discount_rate[]': ['0.00'],
            'vat_rate[]': ['1.00'],
            'buy_price[]': ['25.00'],
        }

        res = self.client.post(reverse('stock_entry_create'), data=sarf_payload)
        self.assertEqual(res.status_code, 302)

        self.prod_critical.refresh_from_db()
        self.assertEqual(self.prod_critical.stock_quantity, initial_stock - Decimal('2.000'))

        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.balance, initial_balance)

        entry = StockEntry.objects.filter(entry_type='SARF_TUKETIM').order_by('-created_at').first()
        self.assertIsNotNone(entry)
        self.assertTrue(entry.entry_number.startswith('SARF-'))

    def test_critical_stock_orders_view_groups_suppliers_and_suggests_quantities(self):
        """Kritik stok listesi azalan ürünleri toptancıya göre gruplamalı ve sipariş miktarı önermelidir."""
        self.client.force_login(self.manager_user)

        res = self.client.get(reverse('critical_stock_orders'))
        self.assertEqual(res.status_code, 200)

        # prod_critical (10 <= 15) ve prod_unassigned (2 <= 20) kritik olmalı
        self.assertContains(res, 'Sütaş Süt 1L')
        self.assertContains(res, 'Eti Burçak Bisküvi')

        # prod_healthy (50 > 10) kritik tabloda yer almamalıdır
        self.assertNotContains(res, 'Çaykur Rize Çay 1KG')

        # Toptancı telefonuna WhatsApp sipariş bağlantısı üretilmiş olmalıdır
        self.assertContains(res, 'https://wa.me/905321112233')
        self.assertContains(res, 'WhatsApp Siparişi Gönder')


class ProductionHardeningAndDiagnosticsPhase6Tests(BasePosTestCase):
    """
    Faz 6: Üretim Ortamı Sertleştirme, Sağlık İzleme (Health Check) & Otomatik Yedekleme Testleri
    """
    def test_health_check_api_endpoint(self):
        """Sağlık teşhis API'si veritabanı, gecikme süresi, disk ve yedekleme metriklerini dönmelidir."""
        res = self.client.get(reverse('health_check_api'))
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertEqual(data['status'], 'healthy')
        self.assertIn('database', data)
        self.assertTrue(data['database']['connected'])
        self.assertGreaterEqual(data['database']['latency_ms'], 0.0)

        self.assertIn('disk', data)
        self.assertGreater(data['disk']['total_gb'], 0.0)
        self.assertGreater(data['disk']['free_gb'], 0.0)

        self.assertIn('backup', data)
        self.assertGreaterEqual(data['backup']['total_count'], 0)

        self.assertEqual(data['app_version'], '2.6.0-enterprise')

    def test_backup_database_management_command(self):
        """backup_database yönetim komutu sıkıştırılmış dosya ve SHA-256 sağlama özeti üretmelidir."""
        from .models import SystemLog
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            call_command(
                'backup_database',
                dest=tmp_dir,
                compress=True,
                keep=10,
                reason='FAZ6_TEST'
            )

            target_dir = Path(tmp_dir)
            gz_files = list(target_dir.glob('*.gz'))
            self.assertEqual(len(gz_files), 1, "Hedef dizinde 1 adet sıkıştırılmış .gz yedeği oluşmalıdır.")

            gz_file = gz_files[0]
            self.assertGreater(gz_file.stat().st_size, 0)

            # Checksum dosyası oluşmuş olmalıdır
            sha_file = gz_file.with_name(gz_file.name + '.sha256')
            self.assertTrue(sha_file.exists(), "SHA-256 doğrulama özeti dosyası (.sha256) mevcut olmalıdır.")
            with open(sha_file, 'r', encoding='utf-8') as f:
                content = f.read()
                self.assertIn(gz_file.name, content)
                self.assertEqual(len(content.split()[0]), 64, "SHA-256 özeti 64 karakter hexadecimal olmalıdır.")

            # Sistem denetim loguna işlenmiş olmalıdır
            self.assertTrue(
                SystemLog.objects.filter(
                    module='SYSTEM',
                    action='Otomatik Veritabanı Yedeği Alındı',
                    message__icontains='FAZ6_TEST'
                ).exists()
            )


class QuickProductCreationAndBarcodeTest(BasePosTestCase):
    """
    Kasa ve Mobil Barkod Okuma Esnasında Hızlı Ürün Tanımlama ve Çözümleme Testleri
    """

    def test_quick_create_product_and_immediate_barcode_search(self):
        # 1. Kasiyer girişi
        self.client.login(username='kasiyer1', password='Password123!')

        # 2. Henüz kayıtlı olmayan bir barkod sorgulanır
        unregistered_barcode = '8689999999999'
        res_404 = self.client.get(reverse('search_barcode'), {'barcode': unregistered_barcode})
        self.assertEqual(res_404.status_code, 404)
        data_404 = res_404.json()
        self.assertFalse(data_404['success'])
        self.assertEqual(data_404['barcode'], unregistered_barcode)

        # 3. Kasiyer kasadan hızlı ürün oluşturur
        payload = {
            'barcode': unregistered_barcode,
            'name': 'Test Masadaki Su 0.5L',
            'sell_price': '12.50',
            'buy_price': '8.00',
            'vat_rate': '10.00',
            'stock_quantity': '100.0',
            'unit': 'Adet',
            'is_weighted': False
        }
        res_create = self.client.post(
            reverse('quick_create_product_api'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(res_create.status_code, 200)
        data_create = res_create.json()
        self.assertTrue(data_create['success'])
        self.assertEqual(data_create['product']['barcode'], unregistered_barcode)
        self.assertEqual(data_create['product']['name'], 'Test Masadaki Su 0.5L')

        # 4. Hemen ardından aynı barkod tekrar okutulur -> Artık anında 200 döner!
        res_200 = self.client.get(reverse('search_barcode'), {'barcode': unregistered_barcode})
        self.assertEqual(res_200.status_code, 200)
        data_200 = res_200.json()
        self.assertTrue(data_200['success'])
        self.assertEqual(data_200['product']['barcode'], unregistered_barcode)
        self.assertEqual(data_200['product']['sell_price'], 12.50)

    def test_product_search_api_for_pos_stock_lookup(self):
        """Stok Bul API yetkisiz erişimi engeller, aktif ürünleri döndürür ve alış maliyetini sızdırmaz."""
        res_unauth = self.client.get(reverse('product_search_api'), {'q': 'Ekmek'})
        self.assertEqual(res_unauth.status_code, 401)

        self.client.force_login(self.cashier_user)
        inactive = Product.objects.create(
            barcode='8699990001112',
            name='Pasif Ürün Test',
            category=self.category,
            sell_price=Decimal('99.00'),
            stock_quantity=Decimal('10.000'),
            is_active=False,
        )
        res = self.client.get(reverse('product_search_api'), {'q': 'Ekmek', 'category': str(self.category.id)})
        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertTrue(payload['success'])
        names = [p['name'] for p in payload['products']]
        self.assertIn(self.product_bread.name, names)
        self.assertNotIn(inactive.name, names)
        first = next(p for p in payload['products'] if p['id'] == self.product_bread.id)
        self.assertNotIn('buy_price', first)
        self.assertEqual(first['category_name'], self.category.name)

    def test_pos_screen_and_sameorigin_receipt_frame(self):
        """Yenilenen POS satış ekranı ve aynı-kökenli fiş önizleme başlıkları doğrulanır."""
        self.client.force_login(self.cashier_user)
        res_pos = self.client.get(reverse('pos_screen'))
        self.assertEqual(res_pos.status_code, 200)
        html = res_pos.content.decode('utf-8')
        self.assertIn('id="payableTotalBox"', html)
        self.assertIn('id="btnCreditSale"', html)
        self.assertIn('id="btnSplitModal"', html)
        self.assertIn('pos-pad-key--qty', html)
        self.assertIn('pos-pad-key--disc', html)
        self.assertIn('pos-pad-key--price', html)
        self.assertIn('pos-pad-key--quickcash', html)
        self.assertNotIn('id="subTotalDisplay"', html)

        sale = Sale.objects.create(
            receipt_number='FIS-TEST-FRAME-001',
            shift=Shift.objects.create(cash_register=self.register, cashier=self.cashier_user),
            cashier=self.cashier_user,
            payment_type='NAKIT',
            total_amount=Decimal('10.00'),
            cash_paid=Decimal('10.00'),
        )
        res_rcpt = self.client.get(reverse('receipt_view', args=[sale.receipt_number]))
        self.assertEqual(res_rcpt.status_code, 200)
        self.assertEqual(res_rcpt.headers.get('X-Frame-Options'), 'SAMEORIGIN')


class AdminPhase1ValidationAndLocaleTests(BasePosTestCase):
    """Faz 1: Ürün doğrulama, unlocalize inputlar ve toptancı DUZELTME bakiye testleri."""

    def test_duplicate_barcode_and_invalid_decimal_return_clean_form_error(self):
        self.client.force_login(self.manager_user)

        res_dup = self.client.post(reverse('product_create_view'), {
            'barcode': self.product_bread.barcode,
            'name': 'Çakışan Ekmek',
            'buy_price': '5.00',
            'sell_price': '10.00',
            'vat_rate': '1.00',
            'stock_quantity': '5',
        })
        self.assertEqual(res_dup.status_code, 200)
        html = res_dup.content.decode('utf-8')
        self.assertIn('alert-danger', html)
        self.assertIn('Çakışan Ekmek', html)

        res_bad_num = self.client.post(reverse('product_create_view'), {
            'barcode': '8691112223334',
            'name': 'Hatalı Fiyat Ürün',
            'buy_price': 'abc',
            'sell_price': '10.00',
        })
        self.assertEqual(res_bad_num.status_code, 200)
        self.assertIn('alert-danger', res_bad_num.content.decode('utf-8'))

        res_ok = self.client.post(reverse('product_create_view'), {
            'barcode': '8691112223334',
            'secondary_barcode': '',
            'name': 'Temiz Yeni Ürün',
            'buy_price': '12,50',
            'sell_price': '19,90',
            'vat_rate': '20.00',
            'stock_quantity': '3',
        })
        self.assertEqual(res_ok.status_code, 302)
        created = Product.objects.get(barcode='8691112223334')
        self.assertIsNone(created.secondary_barcode)
        self.assertEqual(created.sell_price, Decimal('19.90'))

    def test_number_inputs_rendered_with_dots_under_tr_locale(self):
        self.client.force_login(self.manager_user)
        cust = Customer.objects.create(
            first_name='Ali', last_name='Test', phone='05559990011', credit_limit=Decimal('2500.50')
        )
        res_cust = self.client.get(reverse('customer_edit_view', args=[cust.id]))
        self.assertEqual(res_cust.status_code, 200)
        self.assertContains(res_cust, 'value="2500.50"')

        setting = CompanySetting.get_settings()
        setting.scale_mock_weight = Decimal('1.250')
        setting.supervisor_discount_limit_percent = Decimal('10.50')
        setting.save()
        res_cfg = self.client.get(reverse('company_settings_view'))
        self.assertEqual(res_cfg.status_code, 200)
        cfg_html = res_cfg.content.decode('utf-8')
        self.assertIn('value="1.250"', cfg_html)
        self.assertIn('value="10.50"', cfg_html)

    def test_supplier_duzeltme_updates_balance_for_add_sub_set(self):
        self.client.force_login(self.manager_user)
        sup = Supplier.objects.create(company_name='Düzeltme Test A.Ş.', balance=Decimal('500.00'))

        self.client.post(reverse('supplier_payment_view', args=[sup.id]), {
            'transaction_type': 'DUZELTME',
            'adjustment_direction': 'ADD',
            'amount': '150.00',
        })
        sup.refresh_from_db()
        self.assertEqual(sup.balance, Decimal('650.00'))

        self.client.post(reverse('supplier_payment_view', args=[sup.id]), {
            'transaction_type': 'DUZELTME',
            'adjustment_direction': 'SUB',
            'amount': '200.00',
        })
        sup.refresh_from_db()
        self.assertEqual(sup.balance, Decimal('450.00'))

        self.client.post(reverse('supplier_payment_view', args=[sup.id]), {
            'transaction_type': 'DUZELTME',
            'adjustment_direction': 'SET',
            'amount': '0.00',
        })
        sup.refresh_from_db()
        self.assertEqual(sup.balance, Decimal('0.00'))
        self.assertEqual(SupplierTransaction.objects.filter(supplier=sup, transaction_type='DUZELTME').count(), 3)


class AdminPhase2ResponsiveAndOfflineTests(BasePosTestCase):
    """Faz 2: Çevrimdışı JS/ikon bütünlüğü ve mobil responsive düzen korumaları."""

    def test_admin_and_customer_display_include_inline_favicon(self):
        self.client.force_login(self.manager_user)
        for url_name in ('admin_dashboard_view', 'customer_display_view'):
            res = self.client.get(reverse(url_name))
            self.assertEqual(res.status_code, 200)
            self.assertIn('rel="icon"', res.content.decode('utf-8'))

    def test_stock_take_detail_uses_local_html5_qrcode(self):
        self.client.force_login(self.manager_user)
        take = StockTake.objects.create(title='Faz 2 Sayım', created_by=self.manager_user)
        res = self.client.get(reverse('stock_take_detail', args=[take.id]))
        self.assertEqual(res.status_code, 200)
        html = res.content.decode('utf-8')
        self.assertIn('/static/js/html5-qrcode.min.js', html)
        self.assertNotIn('unpkg.com/html5-qrcode', html)


class AdminPhase3CrudAndUxTests(BasePosTestCase):
    """Faz 3: Otomatik barkod butonu, toptancı düzenleme ve hızlı ürün aktif/pasif geçişi."""

    def test_product_form_has_auto_barcode_button_and_api(self):
        self.client.force_login(self.manager_user)
        res = self.client.get(reverse('product_create_view'))
        self.assertEqual(res.status_code, 200)
        html = res.content.decode('utf-8')
        self.assertIn('id="generateBarcodeBtn"', html)
        self.assertIn(reverse('generate_barcode_api'), html)

        api_res = self.client.get(reverse('generate_barcode_api'))
        self.assertEqual(api_res.status_code, 200)
        data = api_res.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['is_valid_ean13'])
        self.assertEqual(len(data['barcode']), 13)

        self.client.force_login(self.cashier_user)
        self.assertEqual(self.client.get(reverse('generate_barcode_api')).status_code, 403)

    def test_supplier_edit_updates_details_without_resetting_balance(self):
        sup = Supplier.objects.create(
            company_name='Eski Toptancı Ltd.',
            contact_name='Ali',
            phone='05550000001',
            balance=Decimal('1250.50'),
            is_active=True,
        )

        self.client.force_login(self.cashier_user)
        self.assertEqual(self.client.get(reverse('supplier_edit_view', args=[sup.id])).status_code, 302)

        self.client.force_login(self.manager_user)
        res_bad = self.client.post(reverse('supplier_create_view'), {
            'company_name': '',
            'contact_name': 'Mehmet Korunmalı',
            'phone': '05551112233',
        })
        self.assertEqual(res_bad.status_code, 200)
        self.assertIn('Mehmet Korunmalı', res_bad.content.decode('utf-8'))

        res_get = self.client.get(reverse('supplier_edit_view', args=[sup.id]))
        self.assertEqual(res_get.status_code, 200)
        self.assertIn('Eski Toptancı Ltd.', res_get.content.decode('utf-8'))

        res_post = self.client.post(reverse('supplier_edit_view', args=[sup.id]), {
            'company_name': 'Yeni Toptancı A.Ş.',
            'contact_name': 'Veli',
            'phone': '05559998877',
            'email': 'info@yeni.test',
            'tax_office': 'Kadıköy',
            'tax_number': '1234567890',
            'address': 'İstanbul',
            'notes': 'Salı sevkiyatı',
            # is_active omitted -> deactivated
        })
        self.assertEqual(res_post.status_code, 302)
        sup.refresh_from_db()
        self.assertEqual(sup.company_name, 'Yeni Toptancı A.Ş.')
        self.assertEqual(sup.contact_name, 'Veli')
        self.assertEqual(sup.tax_number, '1234567890')
        self.assertFalse(sup.is_active)
        self.assertEqual(sup.balance, Decimal('1250.50'))
        self.assertTrue(SystemLog.objects.filter(module='SUPPLIER', action='Toptancı Güncellendi').exists())

        list_html = self.client.get(reverse('supplier_list_view')).content.decode('utf-8')
        self.assertIn(reverse('supplier_edit_view', args=[sup.id]), list_html)
        self.assertIn('PASİF', list_html)

    def test_product_toggle_active_action_and_catalog_filter(self):
        self.client.force_login(self.cashier_user)
        res_cashier = self.client.post(reverse('product_toggle_active_action', args=[self.product_bread.id]))
        self.assertEqual(res_cashier.status_code, 302)
        self.product_bread.refresh_from_db()
        self.assertTrue(self.product_bread.is_active)

        self.client.force_login(self.manager_user)
        self.assertEqual(
            self.client.get(reverse('product_toggle_active_action', args=[self.product_bread.id])).status_code,
            405
        )

        toggle_url = reverse('product_toggle_active_action', args=[self.product_bread.id])
        res_off = self.client.post(toggle_url, {'next': '/management/products/?stock=passive'})
        self.assertEqual(res_off.status_code, 302)
        self.assertEqual(res_off['Location'], '/management/products/?stock=passive')
        self.product_bread.refresh_from_db()
        self.assertFalse(self.product_bread.is_active)

        passive_res = self.client.get(reverse('product_catalog_view'), {'stock': 'passive'})
        self.assertEqual(passive_res.status_code, 200)
        passive_html = passive_res.content.decode('utf-8')
        self.assertIn(self.product_bread.name, passive_html)
        self.assertIn('Aktifleştir', passive_html)

        res_on = self.client.post(toggle_url, {'next': 'https://evil.example/phish'})
        self.assertEqual(res_on.status_code, 302)
        self.assertEqual(res_on['Location'], reverse('product_catalog_view'))
        self.product_bread.refresh_from_db()
        self.assertTrue(self.product_bread.is_active)
        self.assertTrue(SystemLog.objects.filter(module='PRODUCT', action='Ürün Durumu Güncellendi').exists())


class AdminPhase4CustomAdminViewsTests(BasePosTestCase):
    """Faz 4: Özel kategori yönetimi, satış fişleri/iadeler ve kasa vardiyaları ekranları."""

    def test_sidebar_links_point_to_custom_phase4_views_and_rbac(self):
        self.client.force_login(self.cashier_user)
        for url_name in ('category_list_view', 'sale_list_view', 'shift_list_view'):
            self.assertEqual(self.client.get(reverse(url_name)).status_code, 302)

        self.client.force_login(self.manager_user)
        dash_html = self.client.get(reverse('admin_dashboard_view')).content.decode('utf-8')
        self.assertIn(reverse('category_list_view'), dash_html)
        self.assertIn(reverse('sale_list_view'), dash_html)
        self.assertIn(reverse('shift_list_view'), dash_html)
        self.assertNotIn('/pos_app/category/', dash_html)
        self.assertNotIn('/pos_app/sale/', dash_html)
        self.assertNotIn('/pos_app/shift/', dash_html)

    def test_category_crud_and_toggle_status(self):
        self.client.force_login(self.manager_user)
        res_create = self.client.post(reverse('category_save_action'), {
            'name': 'İçecekler',
            'display_order': '3',
            'description': 'Soğuk ve sıcak içecekler',
            'is_active': 'on',
        })
        self.assertEqual(res_create.status_code, 302)
        cat = Category.objects.get(name='İçecekler')
        self.assertEqual(cat.display_order, 3)
        self.assertTrue(cat.is_active)

        res_edit = self.client.post(reverse('category_save_action'), {
            'category_id': str(cat.id),
            'name': 'Soğuk İçecekler',
            'display_order': '1',
            'description': 'Reyon 2',
            'is_active': 'on',
        })
        self.assertEqual(res_edit.status_code, 302)
        cat.refresh_from_db()
        self.assertEqual(cat.name, 'Soğuk İçecekler')
        self.assertEqual(cat.display_order, 1)

        res_toggle = self.client.post(reverse('category_toggle_status_action', args=[cat.id]))
        self.assertEqual(res_toggle.status_code, 302)
        cat.refresh_from_db()
        self.assertFalse(cat.is_active)

        list_html = self.client.get(reverse('category_list_view'), {'status': 'passive'}).content.decode('utf-8')
        self.assertIn('Soğuk İçecekler', list_html)
        self.assertIn('PASİF', list_html)

    def test_sale_list_filters_kpis_and_detail_api(self):
        shift = Shift.objects.create(cash_register=self.register, cashier=self.cashier_user)
        sale_ok = Sale.objects.create(
            receipt_number='FIS-F4-001',
            shift=shift,
            cashier=self.cashier_user,
            subtotal=Decimal('100.00'),
            total_vat=Decimal('10.00'),
            total_amount=Decimal('110.00'),
            payment_type='NAKIT',
            cash_paid=Decimal('110.00'),
        )
        SaleItem.objects.create(
            sale=sale_ok,
            product=self.product_bread,
            product_name=self.product_bread.name,
            barcode=self.product_bread.barcode,
            quantity=Decimal('2.000'),
            unit_price=Decimal('55.00'),
            vat_rate=Decimal('1.00'),
            vat_amount=Decimal('1.09'),
            total_price=Decimal('110.00'),
        )
        Sale.objects.create(
            receipt_number='IADE-F4-002',
            shift=shift,
            cashier=None,
            total_amount=Decimal('20.00'),
            payment_type='NAKIT',
            is_return=True,
            return_reason='Son kullanma tarihi',
        )

        self.client.force_login(self.cashier_user)
        self.assertEqual(self.client.get(reverse('sale_detail_api', args=[sale_ok.id])).status_code, 403)

        self.client.force_login(self.manager_user)
        res = self.client.get(reverse('sale_list_view'), {'cashier': 'invalid_id'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context['gross_sales'], Decimal('110.00'))
        self.assertEqual(res.context['total_returns'], Decimal('20.00'))
        self.assertEqual(res.context['net_revenue'], Decimal('90.00'))

        res_ret = self.client.get(reverse('sale_list_view'), {'doc_status': 'returns'})
        ret_html = res_ret.content.decode('utf-8')
        self.assertIn('IADE-F4-002', ret_html)
        self.assertNotIn('FIS-F4-001', ret_html)

        detail_res = self.client.get(reverse('sale_detail_api', args=[sale_ok.id]))
        self.assertEqual(detail_res.status_code, 200)
        detail_json = detail_res.json()
        self.assertTrue(detail_json['success'])
        self.assertEqual(detail_json['sale']['receipt_number'], 'FIS-F4-001')
        self.assertEqual(len(detail_json['items']), 1)

    def test_shift_list_filters_and_difference_kpis(self):
        Shift.objects.create(cash_register=self.register, cashier=None, is_closed=False)
        closed_diff = Shift.objects.create(
            cash_register=self.register,
            cashier=self.manager_user,
            is_closed=True,
            expected_cash=Decimal('500.00'),
            actual_cash=Decimal('485.00'),
            difference=Decimal('-15.00'),
            total_cash_sales=Decimal('400.00'),
        )

        self.client.force_login(self.manager_user)
        res_all = self.client.get(reverse('shift_list_view'))
        self.assertEqual(res_all.status_code, 200)
        res = self.client.get(reverse('shift_list_view'), {'status': 'diff'})
        self.assertEqual(res.status_code, 200)
        html = res.content.decode('utf-8')
        self.assertIn(f"#{closed_diff.id}", html)
        self.assertIn('EKSİK', html)
        self.assertEqual(res.context['net_difference'], Decimal('-15.00'))
        self.assertIn(f"?shift_id={closed_diff.id}", html)


class AdminPhase5ExportAndBulkUpdateTests(BasePosTestCase):
    """Faz 5: CSV dışa aktarım, toplu fiyat/KDV güncelleme ve kalıcı regresyon paketi."""

    def test_csv_exports_utf8_bom_content_and_rbac(self):
        sup = Supplier.objects.create(company_name='=EXCEL_INJECT Toptancı', balance=Decimal('300.00'))
        cust = Customer.objects.create(first_name='Ayşe', last_name='Yılmaz', phone='05554443322', balance=Decimal('120.00'))
        CustomerTransaction.objects.create(
            customer=cust,
            transaction_type='VERESIYE_SATIS',
            amount=Decimal('120.00'),
            balance_after=Decimal('120.00'),
            description='Test fiş',
        )
        SupplierTransaction.objects.create(
            supplier=sup,
            transaction_type='ALIS',
            amount=Decimal('300.00'),
            balance_after=Decimal('300.00'),
            document_no='IRS-01',
        )

        self.client.force_login(self.cashier_user)
        for url_name in ('export_products_csv', 'export_critical_orders_csv', 'export_customers_csv', 'export_suppliers_csv', 'export_stock_entries_csv', 'export_sales_csv'):
            self.assertEqual(self.client.get(reverse(url_name)).status_code, 302)

        self.client.force_login(self.manager_user)

        res_prod = self.client.get(reverse('export_products_csv'), {'stock': 'low'})
        self.assertEqual(res_prod.status_code, 200)
        self.assertIn('text/csv', res_prod['Content-Type'])
        text_prod = res_prod.content.decode('utf-8')
        self.assertTrue(text_prod.startswith('\ufeff'))
        self.assertIn(self.product_low_stock.name, text_prod)
        self.assertNotIn(self.product_bread.name, text_prod)

        res_crit = self.client.get(reverse('export_critical_orders_csv'))
        self.assertEqual(res_crit.status_code, 200)
        self.assertIn(self.product_low_stock.barcode, res_crit.content.decode('utf-8'))

        res_cust = self.client.get(reverse('export_customers_csv'))
        self.assertEqual(res_cust.status_code, 200)
        self.assertIn('Ayşe Yılmaz', res_cust.content.decode('utf-8'))

        res_cust_ledger = self.client.get(reverse('export_customer_ledger_csv', args=[cust.id]))
        self.assertEqual(res_cust_ledger.status_code, 200)
        self.assertIn('120.00', res_cust_ledger.content.decode('utf-8'))

        res_sup = self.client.get(reverse('export_suppliers_csv'))
        self.assertEqual(res_sup.status_code, 200)
        sup_csv = res_sup.content.decode('utf-8')
        # Formula injection neutralization check
        self.assertIn("'=EXCEL_INJECT Toptancı", sup_csv)

        res_sup_ledger = self.client.get(reverse('export_supplier_ledger_csv', args=[sup.id]))
        self.assertEqual(res_sup_ledger.status_code, 200)
        self.assertIn('IRS-01', res_sup_ledger.content.decode('utf-8'))

        # Stok Fişleri CSV Dışa Aktarımı
        res_stock_entry = self.client.get(reverse('export_stock_entries_csv'))
        self.assertEqual(res_stock_entry.status_code, 200)
        self.assertIn('text/csv', res_stock_entry['Content-Type'])
        self.assertTrue(res_stock_entry.content.decode('utf-8').startswith('\ufeff'))

        # Satış Fişleri CSV Dışa Aktarımı
        res_sales_csv = self.client.get(reverse('export_sales_csv'))
        self.assertEqual(res_sales_csv.status_code, 200)
        self.assertIn('text/csv', res_sales_csv['Content-Type'])
        self.assertTrue(res_sales_csv.content.decode('utf-8').startswith('\ufeff'))

    def test_bulk_price_and_vat_updates(self):
        self.client.force_login(self.cashier_user)
        res_cashier = self.client.post(reverse('product_bulk_update_action'), {
            'apply_scope': 'selected',
            'product_ids': [str(self.product_bread.id)],
            'action_type': 'price_percent_up',
            'action_value': '10',
        })
        self.assertEqual(res_cashier.status_code, 302)
        self.product_bread.refresh_from_db()
        self.assertEqual(self.product_bread.sell_price, Decimal('10.00'))

        self.client.force_login(self.manager_user)
        # 1) %20 Zam on selected bread (10.00 -> 12.00)
        self.client.post(reverse('product_bulk_update_action'), {
            'apply_scope': 'selected',
            'product_ids': [str(self.product_bread.id)],
            'action_type': 'price_percent_up',
            'action_value': '20',
        })
        self.product_bread.refresh_from_db()
        self.assertEqual(self.product_bread.sell_price, Decimal('12.00'))

        # 2) %25 İndirim on filtered low stock (25.00 -> 18.75)
        self.client.post(reverse('product_bulk_update_action'), {
            'apply_scope': 'filtered',
            'stock': 'low',
            'action_type': 'price_percent_down',
            'action_value': '25',
        })
        self.product_low_stock.refresh_from_db()
        self.assertEqual(self.product_low_stock.sell_price, Decimal('18.75'))

        # 3) KDV oranı güncelleme -> %10
        self.client.post(reverse('product_bulk_update_action'), {
            'apply_scope': 'selected',
            'product_ids': [str(self.product_bread.id)],
            'action_type': 'vat_set',
            'action_value': '10',
        })
        self.product_bread.refresh_from_db()
        self.assertEqual(self.product_bread.vat_rate, Decimal('10.00'))

        # 4) Geçersiz KDV (>100) reddedilmeli
        self.client.post(reverse('product_bulk_update_action'), {
            'apply_scope': 'selected',
            'product_ids': [str(self.product_bread.id)],
            'action_type': 'vat_set',
            'action_value': '150',
        })
        self.product_bread.refresh_from_db()
        self.assertEqual(self.product_bread.vat_rate, Decimal('10.00'))
        self.assertTrue(SystemLog.objects.filter(module='PRODUCT', action='Toplu Fiyat/KDV Güncelleme').exists())

    def test_management_product_stock_sales_enhancements(self):
        """Ürün, Stok ve Satış yönetim ekranlarındaki yeni filtreleme, CSV ve köprü bağlantılarını test eder."""
        self.client.force_login(self.manager_user)

        # 1. Stok Giriş Fişi - FIRE_ZAYI türü ile oluşturma ve filtreleme
        sup = Supplier.objects.create(company_name="Zayiat Toptancı", phone="05551112233")
        entry_fire = StockEntry.objects.create(
            entry_number="FIRE-TEST-001",
            entry_type="FIRE_ZAYI",
            supplier=sup,
            total_amount=Decimal('45.00')
        )
        StockEntryItem.objects.create(
            stock_entry=entry_fire,
            product=self.product_bread,
            quantity=Decimal('3.000'),
            buy_price=Decimal('15.00'),
            total_price=Decimal('45.00')
        )

        res_fire = self.client.get(reverse('stock_entry_list'), {'type': 'FIRE_ZAYI'})
        self.assertEqual(res_fire.status_code, 200)
        self.assertContains(res_fire, "FIRE-TEST-001")
        self.assertContains(res_fire, "Fire / Zayiat")

        # 2. Stok Mal Kabul ekranına toptancı ID'si ile ön-seçimli giriş (?supplier_id=...)
        res_create_preselect = self.client.get(reverse('stock_entry_create'), {'supplier_id': str(sup.id)})
        self.assertEqual(res_create_preselect.status_code, 200)
        self.assertEqual(res_create_preselect.context['preselected_supplier_id'], sup.id)

        # 3. Ürün Kataloğu - Raf Etiketi bağlantıları ve butonları
        res_catalog = self.client.get(reverse('product_catalog_view'))
        self.assertEqual(res_catalog.status_code, 200)
        self.assertContains(res_catalog, reverse('product_labels_view'))
        self.assertContains(res_catalog, f"{reverse('product_labels_view')}?q={self.product_bread.barcode}")

        # 4. Raf Etiketi ekranı - doğrudan barkod arama sorgusu ile getirme (?q=...)
        res_label_query = self.client.get(reverse('product_labels_view'), {'q': self.product_bread.barcode})
        self.assertEqual(res_label_query.status_code, 200)
        self.assertContains(res_label_query, self.product_bread.name)

        # 5. Satış Fişleri CSV dışa aktarımı ve filtreleme
        res_sales_filtered_csv = self.client.get(reverse('export_sales_csv'), {'doc_status': 'sales'})
        self.assertEqual(res_sales_filtered_csv.status_code, 200)
        self.assertIn('text/csv', res_sales_filtered_csv['Content-Type'])


class AdminPhase6PaginationStandardizationTests(BasePosTestCase):
    """
    Faz 6: Büyük Veri Performansı & Sayfalama (Pagination) Standardizasyonu Testleri
    """
    def test_product_catalog_pagination_and_query_preservation(self):
        """Ürün kataloğu 50'lik sayfalara bölünmeli ve filtre parametrelerini korumalıdır."""
        self.client.force_login(self.manager_user)
        cat = Category.objects.create(name="Gıda Test")

        # 55 adet test ürünü oluştur
        products_to_create = [
            Product(
                barcode=f"86900000{i:05d}",
                name=f"Toplu Test Ürünü {i}",
                buy_price=Decimal('10.00'),
                sell_price=Decimal('15.00'),
                stock_quantity=Decimal('20.000'),
                category=cat,
            )
            for i in range(1, 56)
        ]
        Product.objects.bulk_create(products_to_create)

        # 1. Sayfa sorgusu
        res_p1 = self.client.get(reverse('product_catalog_view'), {'q': 'Toplu Test Ürünü'})
        self.assertEqual(res_p1.status_code, 200)
        self.assertEqual(len(res_p1.context['products']), 50)
        self.assertTrue(res_p1.context['page_obj'].has_next())
        self.assertContains(res_p1, 'page=2')
        self.assertContains(res_p1, 'q=Toplu+Test+%C3%9Cr%C3%BCn%C3%BC')

        # 2. Sayfa sorgusu
        res_p2 = self.client.get(reverse('product_catalog_view'), {'q': 'Toplu Test Ürünü', 'page': 2})
        self.assertEqual(res_p2.status_code, 200)
        self.assertEqual(len(res_p2.context['products']), 5)
        self.assertFalse(res_p2.context['page_obj'].has_next())

    def test_stock_entry_and_take_pagination(self):
        """Stok girişleri (25'lik) ve stok sayım fişleri (20'lik) sayfalanmalıdır."""
        self.client.force_login(self.manager_user)

        # 28 adet stok fişi oluştur
        StockEntry.objects.bulk_create([
            StockEntry(
                entry_number=f"STK-PAGI-{i:03d}",
                entry_type='GIRIS',
                total_amount=Decimal('100.00'),
            )
            for i in range(1, 29)
        ])

        res_stock = self.client.get(reverse('stock_entry_list'))
        self.assertEqual(res_stock.status_code, 200)
        self.assertEqual(len(res_stock.context['entries']), 25)
        self.assertTrue(res_stock.context['page_obj'].has_next())

        # 22 adet sayım fişi oluştur
        StockTake.objects.bulk_create([
            StockTake(
                take_number=f"SYM-PAGI-{i:03d}",
                title=f"Sayım Fişi {i}",
                status='DRAFT',
            )
            for i in range(1, 23)
        ])

        res_take = self.client.get(reverse('stock_take_list'))
        self.assertEqual(res_take.status_code, 200)
        self.assertEqual(len(res_take.context['takes']), 20)
        self.assertTrue(res_take.context['page_obj'].has_next())


class AdminPhase7CrudAndManagerActionsTests(BasePosTestCase):
    """
    Faz 7: Eksik CRUD & Yönetici Aksiyonları Testleri (Silme, İptal, Hızlı Durum)
    """
    def test_product_safe_delete_protection_and_unreferenced_delete(self):
        """Satışı veya mal kabulü olan ürün silinmemeli, hareketi olmayan ürün silinebilmelidir."""
        self.client.force_login(self.manager_user)

        # 1) Satışı olan ürün (self.product_bread) silinmeye çalışıldığında reddedilmeli
        sale_prev = Sale.objects.create(
            receipt_number="HIS-PREV-01",
            cashier=self.cashier_user,
            shift=self.shift,
            total_amount=Decimal('10.00')
        )
        SaleItem.objects.create(
            sale=sale_prev,
            product=self.product_bread,
            product_name=self.product_bread.name,
            barcode=self.product_bread.barcode,
            quantity=Decimal('1.000'),
            unit_price=Decimal('10.00'),
            vat_rate=Decimal('10.00'),
            total_price=Decimal('10.00'),
        )

        res_del_protected = self.client.post(reverse('product_delete_action', args=[self.product_bread.id]))
        self.assertEqual(res_del_protected.status_code, 302)
        self.assertTrue(Product.objects.filter(id=self.product_bread.id).exists())

        # 2) Hiçbir hareketi olmayan yeni ürün güvenle silinmeli
        temp_prod = Product.objects.create(
            barcode="8699999999999",
            name="Geçici Test Ürünü",
            buy_price=Decimal('5.00'),
            sell_price=Decimal('8.00'),
            stock_quantity=Decimal('0.000'),
        )
        res_del_ok = self.client.post(reverse('product_delete_action', args=[temp_prod.id]))
        self.assertEqual(res_del_ok.status_code, 302)
        self.assertFalse(Product.objects.filter(id=temp_prod.id).exists())

    def test_customer_and_supplier_toggle_status_actions(self):
        """Müşteri ve toptancı tek tıkla aktif/pasif yapılabilmelidir."""
        self.client.force_login(self.manager_user)

        # Müşteri durumu tersine çevirme
        cust = Customer.objects.create(first_name="Fatma", last_name="Demir", phone="05332221100", is_active=True)
        res_cust = self.client.post(reverse('customer_toggle_status_action', args=[cust.id]))
        self.assertEqual(res_cust.status_code, 302)
        cust.refresh_from_db()
        self.assertFalse(cust.is_active)

        # Toptancı durumu tersine çevirme
        sup = Supplier.objects.create(company_name="Özlem Gıda Ltd.", is_active=True)
        res_sup = self.client.post(reverse('supplier_toggle_status_action', args=[sup.id]))
        self.assertEqual(res_sup.status_code, 302)
        sup.refresh_from_db()
        self.assertFalse(sup.is_active)

    def test_sale_cancel_action_restores_stock_and_reverses_credit(self):
        """Yönetimden fiş iptal edildiğinde stoklar iade edilmeli ve veresiye borcu düşürülmelidir."""
        self.client.force_login(self.manager_user)

        cust = Customer.objects.create(first_name="Mehmet", last_name="Öz", phone="05443332211", balance=Decimal('100.00'), credit_limit=Decimal('1000.00'))
        initial_stock = self.product_bread.stock_quantity

        # 2 adet ekmek veresiye satışı
        sale = Sale.objects.create(
            receipt_number="HIS-CANCEL-TEST-001",
            cashier=self.cashier_user,
            shift=self.shift,
            customer=cust,
            customer_title=cust.full_name,
            payment_type='VERESIYE',
            total_amount=Decimal('30.00'),
        )
        SaleItem.objects.create(
            sale=sale,
            product=self.product_bread,
            product_name=self.product_bread.name,
            barcode=self.product_bread.barcode,
            quantity=Decimal('2.000'),
            unit_price=Decimal('15.00'),
            vat_rate=Decimal('10.00'),
            total_price=Decimal('30.00'),
        )
        self.product_bread.stock_quantity = F('stock_quantity') - Decimal('2.000')
        self.product_bread.save(update_fields=['stock_quantity'])
        cust.balance = F('balance') + Decimal('30.00')
        cust.save(update_fields=['balance'])
        cust.refresh_from_db()
        self.assertEqual(cust.balance, Decimal('130.00'))

        # Yönetici iptal aksiyonu
        res_cancel = self.client.post(reverse('sale_cancel_action', args=[sale.id]), {
            'cancellation_reason': 'Müşteri siparişi iptal etti'
        })
        self.assertEqual(res_cancel.status_code, 302)

        sale.refresh_from_db()
        self.assertTrue(sale.is_cancelled)
        self.assertEqual(sale.cancellation_reason, 'Müşteri siparişi iptal etti')

        # Stok geri gelmiş olmalı
        self.product_bread.refresh_from_db()
        self.assertEqual(self.product_bread.stock_quantity, initial_stock)

        # Müşteri borcu 100 TL'ye düşmüş olmalı
        cust.refresh_from_db()
        self.assertEqual(cust.balance, Decimal('100.00'))
        self.assertTrue(CustomerTransaction.objects.filter(customer=cust, transaction_type='IADE', document_no=sale.receipt_number).exists())


class AdminPhase8ReportingAndDashboardTests(BasePosTestCase):
    """
    Faz 8: Vardiya CSV, Sayım Fark CSV, Müşteri Cari Ekstresi Tarih Filtresi & Dashboard
    """
    def setUp(self):
        super().setUp()
        self.client.force_login(self.manager_user)

    def test_export_shifts_csv(self):
        """Kasa vardiyaları Excel uyumlu CSV olarak dışa aktarılabilmelidir."""
        # Açık vardiyayı kapatalım
        self.shift.is_closed = True
        self.shift.actual_cash = Decimal('150.00')
        self.shift.difference = Decimal('10.00')
        self.shift.total_cash_sales = Decimal('140.00')
        self.shift.total_sales_count = 5
        self.shift.save()

        res = self.client.get(reverse('export_shifts_csv'))
        self.assertEqual(res.status_code, 200)
        self.assertIn('text/csv', res['Content-Type'])
        self.assertIn('kasa_vardiyalari_', res['Content-Disposition'])

        csv_content = res.content.decode('utf-8-sig')
        self.assertIn('Vardiya No;Kasa;Kasiyer', csv_content)
        self.assertIn('140.00', csv_content)
        self.assertIn('+10.00', csv_content)

    def test_export_stock_take_csv(self):
        """Stok sayım fişi kalemleri ve maliyet farkı CSV olarak dışa aktarılabilmelidir."""
        take = StockTake.objects.create(
            title="Süt Ürünleri Genel Sayımı",
            created_by=self.manager_user
        )
        StockTakeItem.objects.create(
            stock_take=take,
            product=self.product_bread,
            system_quantity=Decimal('20.000'),
            counted_quantity=Decimal('25.000'),
            buy_price=Decimal('10.00')
        )

        res = self.client.get(reverse('export_stock_take_csv', args=[take.id]))
        self.assertEqual(res.status_code, 200)
        self.assertIn('text/csv', res['Content-Type'])
        self.assertIn(f"stok_sayim_{take.take_number}_", res['Content-Disposition'])

        csv_content = res.content.decode('utf-8-sig')
        self.assertIn('Barkod;Ürün Adı;Kategori / Reyon', csv_content)
        self.assertIn(self.product_bread.barcode, csv_content)
        self.assertIn(self.product_bread.name, csv_content)
        self.assertIn('+5.000', csv_content)
        self.assertIn('+50.00', csv_content)

    def test_customer_detail_date_filter_and_csv_export(self):
        """Müşteri cari ekstresi tarih filtresi hem ekranda hem CSV çıktısında doğru çalışmalıdır."""
        cust = Customer.objects.create(first_name="Hakan", last_name="Yılmaz", phone="05553332211")
        
        # 1. Eski işlem (40 gün önce)
        past_date = timezone.now() - datetime.timedelta(days=40)
        tx_old = CustomerTransaction.objects.create(
            customer=cust,
            transaction_type='VERESIYE_SATIS',
            amount=Decimal('50.00'),
            balance_after=Decimal('50.00'),
            description="Geçen ay veresiye"
        )
        CustomerTransaction.objects.filter(id=tx_old.id).update(created_at=past_date)

        # 2. Yeni işlem (Bugün)
        CustomerTransaction.objects.create(
            customer=cust,
            transaction_type='TAHSILAT_NAKIT',
            amount=Decimal('20.00'),
            balance_after=Decimal('30.00'),
            description="Bugünkü tahsilat"
        )

        # Tarih filtresiz: 2 işlem görünmeli
        res_all = self.client.get(reverse('customer_detail_view', args=[cust.id]))
        self.assertEqual(len(res_all.context['transactions']), 2)

        # Sadece son 5 günü kapsayan filtre: 1 işlem görünmeli
        recent_date_from = (timezone.localdate() - datetime.timedelta(days=5)).isoformat()
        recent_date_to = (timezone.localdate() + datetime.timedelta(days=1)).isoformat()
        res_filtered = self.client.get(reverse('customer_detail_view', args=[cust.id]), {
            'date_from': recent_date_from,
            'date_to': recent_date_to
        })
        self.assertEqual(len(res_filtered.context['transactions']), 1)
        self.assertEqual(res_filtered.context['transactions'][0].description, "Bugünkü tahsilat")

        # CSV çıktısında da sadece filtrelenen işlem yer almalı
        res_csv = self.client.get(reverse('export_customer_ledger_csv', args=[cust.id]), {
            'date_from': recent_date_from,
            'date_to': recent_date_to
        })
        csv_text = res_csv.content.decode('utf-8-sig')
        self.assertIn("Bugünkü tahsilat", csv_text)
        self.assertNotIn("Geçen ay veresiye", csv_text)

    def test_dashboard_quick_actions_links(self):
        """Dashboard'da satış fişleri, kasa vardiyaları ve etiket hızlı erişim butonları yer almalıdır."""
        res = self.client.get(reverse('admin_dashboard_view'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, reverse('sale_list_view'))
        self.assertContains(res, reverse('shift_list_view'))
        self.assertContains(res, reverse('product_labels_view'))


class AdminPhase9TerminalLockAndSecurityTests(BasePosTestCase):
    """
    Faz 9: Donanım/Cihaz Eşleştirme, 1 Kasa = 1 Kasiyer Münhasır Kasa Kilidi ve Kasa CRUD
    """
    def setUp(self):
        super().setUp()
        # İkinci kasiyer kullanıcısı
        self.cashier2 = User.objects.create_user(
            username='kasiyer2',
            password='Password123!',
            first_name='Zeynep',
            last_name='Kasiyer',
            is_staff=False
        )
        self.reg2, _ = CashRegister.objects.get_or_create(
            register_code='KASA-02',
            defaults={'name': 'Kasa 2 (Hızlı Kasa)', 'is_active': True}
        )
        # Faz 12: Kasa ekleme testleri için varsayılan mağaza lisans kotasını yükselt
        sub = Subscription.get_or_create_for_company(Company.get_default_company())
        sub.max_cash_registers = 5
        sub.save(update_fields=['max_cash_registers'])

    def test_terminal_pairing_cookie_and_register_binding(self):
        """Tarayıcı bir kasaya bağlandığında (cookie) o kasaya mühürlenmeli ve serbest geçiş engellenmelidir."""
        self.client.force_login(self.cashier_user)

        # 1. Cihazı KASA-02'ye mühürle
        res_pair = self.client.post(reverse('pair_terminal_action'), {
            'register_code': 'KASA-02'
        })
        self.assertEqual(res_pair.status_code, 302)
        self.assertIn('himarket_terminal_code', res_pair.cookies)
        self.assertEqual(res_pair.cookies['himarket_terminal_code'].value, 'KASA-02')

        # 2. Cihaz mühürlüyken POS ekranı açıldığında KASA-02 gelmelidir
        self.client.cookies['himarket_terminal_code'] = 'KASA-02'
        res_pos = self.client.get(reverse('pos_screen'))
        self.assertEqual(res_pos.status_code, 200)
        self.assertEqual(res_pos.context['register'].register_code, 'KASA-02')

    def test_exclusive_shift_lock_blocks_other_cashiers(self):
        """1 Kasa = 1 Kasiyer Kuralı: Kasa 1 Kasiyer Ali'ye aitken Kasiyer Zeynep girememeli ve satış yapamamalıdır."""
        # 1. Kasiyer Ali Kasa 1'de açık bir vardiyaya sahip
        self.shift.cashier = self.cashier_user
        self.shift.is_closed = False
        self.shift.save()

        # 2. Kasiyer Zeynep bu bilgisayardan Kasa 1'e girmeye çalışır
        self.client.force_login(self.cashier2)
        self.client.cookies['himarket_terminal_code'] = 'KASA-01'

        res_locked = self.client.get(reverse('pos_screen'))
        # Kilit ekranı (HTTP 423 Locked) dönmeli
        self.assertEqual(res_locked.status_code, 423)
        self.assertTemplateUsed(res_locked, 'pos_register_locked.html')
        self.assertContains(res_locked, 'KASA TERMİNALİ KİLİTLİ', status_code=423)
        self.assertContains(res_locked, self.cashier_user.first_name, status_code=423)

        # 3. Kasiyer Zeynep Kasa 1 adına API'den satış yapmaya çalışırsa 403 ile engellenmelidir
        payload = {
            'items': [{'product_id': self.product_bread.id, 'quantity': 1}],
            'payment_type': 'NAKIT',
            'cash_paid': 15.00
        }
        res_sale = self.client.post(
            reverse('process_sale'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(res_sale.status_code, 403)
        sale_data = res_sale.json()
        self.assertFalse(sale_data['success'])
        self.assertIn('kilitlidir', sale_data['message'])

        # 4. Kasiyer Ali ise kendi kasasından sorunsuzca satış yapabilmelidir
        self.client.force_login(self.cashier_user)
        res_sale_ali = self.client.post(
            reverse('process_sale'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(res_sale_ali.status_code, 200)
        self.assertTrue(res_sale_ali.json()['success'])

    def test_supervisor_force_close_shift_and_handover(self):
        """Kasiyer Ali kasayı kapatmadan ayrılırsa süpervizör PIN ile vardiyayı kapatıp Kasiyer Zeynep'e açabilmelidir."""
        self.shift.cashier = self.cashier_user
        self.shift.is_closed = False
        self.shift.save()

        # Süpervizör PIN ile zorla vardiya kapatma
        self.client.force_login(self.cashier2)
        res_force = self.client.post(
            reverse('supervisor_force_close_shift_api'),
            data=json.dumps({
                'shift_id': self.shift.id,
                'pin': 'Password123!', # manager_user şifresi
                'actual_cash': '650.00',
                'notes': 'Kasiyer acil çıktı, devir yapıldı'
            }),
            content_type='application/json'
        )
        self.assertEqual(res_force.status_code, 200)
        self.assertTrue(res_force.json()['success'])

        self.shift.refresh_from_db()
        self.assertTrue(self.shift.is_closed)
        self.assertEqual(self.shift.actual_cash, Decimal('650.00'))

        # Kasa serbest kaldı! Kasiyer Zeynep artık Kasa 1'e girebilmelidir
        self.client.cookies['himarket_terminal_code'] = 'KASA-01'
        res_pos = self.client.get(reverse('pos_screen'))
        self.assertEqual(res_pos.status_code, 200)
        self.assertEqual(res_pos.context['shift'].cashier, self.cashier2)

    def test_cash_register_management_and_safety(self):
        """Yönetici yeni kasa ekleyebilmeli ve açık vardiyası olan kasayı pasife alamamalıdır."""
        self.client.force_login(self.manager_user)

        # 1. Kasa listesi
        res_list = self.client.get(reverse('cash_register_list_view'))
        self.assertEqual(res_list.status_code, 200)
        self.assertContains(res_list, 'Kasa Terminalleri')

        # 2. Yeni kasa ekleme
        res_create = self.client.post(reverse('cash_register_create_action'), {
            'register_code': 'KASA-03',
            'name': '3. Kat İçecek Kasası'
        })
        self.assertEqual(res_create.status_code, 302)
        new_reg = CashRegister.objects.filter(register_code='KASA-03').first()
        self.assertIsNotNone(new_reg)
        self.assertEqual(new_reg.name, '3. Kat İçecek Kasası')

        # 3. Kasa 1'de açık vardiya varken pasife alma engellenmeli
        self.shift.is_closed = False
        self.shift.save()
        res_toggle_active = self.client.post(reverse('cash_register_toggle_status_action', args=[self.register.id]))
        self.assertEqual(res_toggle_active.status_code, 302)
        self.register.refresh_from_db()
        self.assertTrue(self.register.is_active) # Pasife alınamadı!

        # 4. KASA-03 boşta olduğu için pasife alınabilmeli
        res_toggle_empty = self.client.post(reverse('cash_register_toggle_status_action', args=[new_reg.id]))
        self.assertEqual(res_toggle_empty.status_code, 302)
        new_reg.refresh_from_db()
        self.assertFalse(new_reg.is_active)


class ProductQuickPriceUpdateApiTest(BasePosTestCase):
    """
    FAZ 10.2 TESTİ: Ürün Kataloğunda Satır İçi Hızlı Fiyat Güncelleme API
    """
    def setUp(self):
        super().setUp()
        self.client.force_login(self.manager_user)

    def test_quick_price_update_success(self):
        url = reverse('product_quick_price_update_api', args=[self.product_bread.id])
        res = self.client.post(
            url,
            data=json.dumps({'sell_price': '18.75'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['new_sell_price'], 18.75)
        self.product_bread.refresh_from_db()
        self.assertEqual(self.product_bread.sell_price, Decimal('18.75'))

    def test_quick_price_update_rejects_negative_or_invalid(self):
        url = reverse('product_quick_price_update_api', args=[self.product_bread.id])
        res = self.client.post(
            url,
            data=json.dumps({'sell_price': '-5.00'}),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(res.json()['success'])

    def test_quick_price_update_requires_staff(self):
        self.client.force_login(self.cashier_user)
        url = reverse('product_quick_price_update_api', args=[self.product_bread.id])
        res = self.client.post(
            url,
            data=json.dumps({'sell_price': '25.00'}),
            content_type='application/json'
        )
        self.assertIn(res.status_code, [302, 403])


class MultiTenantIsolationTestSuite(TestCase):
    """
    Faz 11: Çoklu Mağaza (Multi-Tenancy) & Güvenli Veri İzolasyonu Testleri.
    Farklı mağazaların (Company A & Company B) birbirinin ürünlerine, müşterilerine,
    stoklarına, kasalarına ve fişlerine kesinlikle erişemeyeceğini,
    aynı barkod ve kasa kodlarının farklı mağazalarda çakışma olmadan var olabileceğini doğrular.
    """
    def setUp(self):
        self.client = Client()

        # Mağaza A ve Mağaza B oluştur
        self.company_a = Company.objects.create(name="Mağaza A (Kadıköy)", code="magaza-a")
        self.company_b = Company.objects.create(name="Mağaza B (Beşiktaş)", code="magaza-b")

        # Mağaza A Kullanıcıları (Kasiyer ve Yönetici)
        self.user_a = User.objects.create_user(username='kasiyer_a', password='pass')
        UserProfile.objects.create(user=self.user_a, company=self.company_a)

        self.manager_a = User.objects.create_user(username='yonetici_a', password='pass', is_staff=True)
        UserProfile.objects.create(user=self.manager_a, company=self.company_a)

        # Mağaza B Kullanıcıları (Kasiyer ve Yönetici)
        self.user_b = User.objects.create_user(username='kasiyer_b', password='pass')
        UserProfile.objects.create(user=self.user_b, company=self.company_b)

        self.manager_b = User.objects.create_user(username='yonetici_b', password='pass', is_staff=True)
        UserProfile.objects.create(user=self.manager_b, company=self.company_b)

        # Kasalar
        self.reg_a = CashRegister.objects.create(company=self.company_a, register_code='KASA-01', name='Kasa A-1')
        self.reg_b = CashRegister.objects.create(company=self.company_b, register_code='KASA-01', name='Kasa B-1')

        # Aynı Barkodlu (8690001001) Farklı Ürünler
        self.prod_a = Product.objects.create(
            company=self.company_a,
            barcode='8690001001',
            name='Mağaza A Çikolata',
            sell_price=Decimal('25.00'),
            buy_price=Decimal('15.00'),
            stock_quantity=Decimal('50.000'),
            is_active=True
        )
        self.prod_b = Product.objects.create(
            company=self.company_b,
            barcode='8690001001',
            name='Mağaza B Çikolata',
            sell_price=Decimal('35.00'),
            buy_price=Decimal('20.00'),
            stock_quantity=Decimal('80.000'),
            is_active=True
        )

        # Aynı Telefonlu Müşteriler
        self.cust_a = Customer.objects.create(
            company=self.company_a,
            first_name='Ahmet',
            last_name='Yılmaz',
            phone='05551112233',
            balance=Decimal('100.00')
        )
        self.cust_b = Customer.objects.create(
            company=self.company_b,
            first_name='Mehmet',
            last_name='Demir',
            phone='05551112233',
            balance=Decimal('250.00')
        )

    def test_composite_unique_constraints_allow_duplicate_barcodes_across_companies(self):
        """Farklı mağazalarda aynı barkod ve aynı kasa kodu hata almadan var olabilmelidir."""
        self.assertEqual(self.prod_a.barcode, self.prod_b.barcode)
        self.assertNotEqual(self.prod_a.company_id, self.prod_b.company_id)
        self.assertEqual(self.reg_a.register_code, self.reg_b.register_code)

    def test_barcode_search_isolation(self):
        """Kasiyer A barkod aradığında sadece Mağaza A'nın ürününü ve fiyatını görmelidir."""
        self.client.force_login(self.user_a)
        res = self.client.get(reverse('search_barcode'), {'barcode': '8690001001'})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['product']['id'], self.prod_a.id)
        self.assertEqual(data['product']['name'], 'Mağaza A Çikolata')
        self.assertEqual(data['product']['sell_price'], 25.00)

        # Kasiyer B aynı barkodu aradığında Mağaza B'nin ürününü görmelidir
        self.client.force_login(self.user_b)
        res_b = self.client.get(reverse('search_barcode'), {'barcode': '8690001001'})
        self.assertEqual(res_b.status_code, 200)
        data_b = res_b.json()
        self.assertEqual(data_b['product']['id'], self.prod_b.id)
        self.assertEqual(data_b['product']['name'], 'Mağaza B Çikolata')
        self.assertEqual(data_b['product']['sell_price'], 35.00)

    def test_pos_sale_inventory_isolation(self):
        """Mağaza A'da yapılan satış sadece Mağaza A'nın stoğunu düşürmelidir."""
        self.client.force_login(self.user_a)
        payload = {
            'items': [{'id': self.prod_a.id, 'quantity': 2, 'price': 25.00}],
            'payment_type': 'NAKIT',
            'cash_paid': 50.00,
            'card_paid': 0.00,
            'discount': 0.00
        }
        res = self.client.post(
            reverse('process_sale'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['success'])

        self.prod_a.refresh_from_db()
        self.prod_b.refresh_from_db()

        self.assertEqual(self.prod_a.stock_quantity, Decimal('48.000'))
        self.assertEqual(self.prod_b.stock_quantity, Decimal('80.000'))  # Mağaza B'nin stoğu değişmemeli

    def test_cross_tenant_sale_prevention(self):
        """Mağaza A kasiyeri, Mağaza B'ye ait bir ürün ID'si ile satış yapmaya kalkarsa engellenmelidir."""
        self.client.force_login(self.user_a)
        payload = {
            'items': [{'id': self.prod_b.id, 'quantity': 1, 'price': 35.00}],
            'payment_type': 'NAKIT',
            'cash_paid': 35.00,
            'card_paid': 0.00,
            'discount': 0.00
        }
        res = self.client.post(
            reverse('process_sale'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        data = res.json()
        self.assertFalse(data['success'])
        # Mağaza B ürünü stoktan düşmemeli
        self.prod_b.refresh_from_db()
        self.assertEqual(self.prod_b.stock_quantity, Decimal('80.000'))

    def test_customer_isolation_and_same_phone(self):
        """Müşteri aramalarında ve listelerinde sadece ilgili mağazanın müşterisi görünmelidir."""
        self.client.force_login(self.user_a)
        res_a = self.client.get(reverse('customer_search_api'), {'q': '05551112233'})
        self.assertEqual(res_a.status_code, 200)
        custs_a = res_a.json()['customers']
        self.assertEqual(len(custs_a), 1)
        self.assertEqual(custs_a[0]['id'], self.cust_a.id)
        self.assertEqual(custs_a[0]['first_name'], 'Ahmet')

        self.client.force_login(self.user_b)
        res_b = self.client.get(reverse('customer_search_api'), {'q': '05551112233'})
        self.assertEqual(res_b.status_code, 200)
        custs_b = res_b.json()['customers']
        self.assertEqual(len(custs_b), 1)
        self.assertEqual(custs_b[0]['id'], self.cust_b.id)
        self.assertEqual(custs_b[0]['first_name'], 'Mehmet')

    def test_admin_catalog_isolation(self):
        """Yönetici A ürün kataloğunda Mağaza B'nin ürünlerini göremez."""
        self.client.force_login(self.manager_a)
        res = self.client.get(reverse('product_catalog_view'))
        self.assertEqual(res.status_code, 200)
        products = res.context['products']
        product_ids = [p.id for p in products]
        self.assertIn(self.prod_a.id, product_ids)
        self.assertNotIn(self.prod_b.id, product_ids)

    def test_stock_take_isolation(self):
        """Mağaza A yöneticisi Mağaza B'nin sayım fişine erişemez."""
        take_b = StockTake.objects.create(
            company=self.company_b,
            take_number='SYM-B-001',
            title='B Sayımı'
        )
        self.client.force_login(self.manager_a)
        res = self.client.get(reverse('stock_take_detail', args=[take_b.id]))
        self.assertEqual(res.status_code, 404)


class SuperAdminConsoleTestSuite(TestCase):
    """
    Faz 12: Platform Süper Admin Konsolu (/super-admin/), Lisans ve Kasa Kotası Test Paketi
    """
    def setUp(self):
        self.client = Client()

        # 1. Kullanıcı Rolleri
        self.superuser = User.objects.create_superuser(
            username='platform_superadmin',
            email='superadmin@himarket.com',
            password='SuperPassword123!'
        )

        self.store_manager = User.objects.create_user(
            username='store_manager_user',
            email='manager@himarket.com',
            password='ManagerPassword123!',
            is_staff=True,
            is_superuser=False
        )

        self.cashier = User.objects.create_user(
            username='cashier_user',
            email='cashier@himarket.com',
            password='CashierPassword123!',
            is_staff=False,
            is_superuser=False
        )

        # 2. Şirketler (Tenants)
        self.default_company = Company.get_default_company()
        self.company_kadikoy = Company.objects.create(
            name='HiMarket Kadıköy Şubesi',
            code='kadikoy',
            phone='02163334455',
            email='kadikoy@himarket.com',
            tax_number='1234567890',
            is_active=True
        )
        self.company_besiktas = Company.objects.create(
            name='HiMarket Beşiktaş Şubesi',
            code='besiktas',
            phone='02122223344',
            email='besiktas@himarket.com',
            tax_number='9876543210',
            is_active=True
        )

        # 3. Şirket Abonelikleri
        self.sub_kadikoy = Subscription.get_or_create_for_company(self.company_kadikoy)
        self.sub_kadikoy.plan_type = 'BASIC'
        self.sub_kadikoy.max_cash_registers = 1
        self.sub_kadikoy.status = 'ACTIVE'
        self.sub_kadikoy.save()

        self.sub_besiktas = Subscription.get_or_create_for_company(self.company_besiktas)
        self.sub_besiktas.plan_type = 'PRO'
        self.sub_besiktas.max_cash_registers = 2
        self.sub_besiktas.status = 'ACTIVE'
        self.sub_besiktas.save()

        # 4. Kasa Terminalleri
        self.reg_k1 = CashRegister.objects.create(
            company=self.company_kadikoy,
            name='Kadıköy Kasa 1',
            register_code='KDK-01',
            is_active=True
        )
        self.reg_b1 = CashRegister.objects.create(
            company=self.company_besiktas,
            name='Beşiktaş Kasa 1',
            register_code='BSK-01',
            is_active=True
        )
        self.reg_b2 = CashRegister.objects.create(
            company=self.company_besiktas,
            name='Beşiktaş Kasa 2',
            register_code='BSK-02',
            is_active=True
        )

    def test_anonymous_user_redirected_to_login(self):
        """Oturum açmamış misafir kullanıcı /super-admin/ sayfasına erişmek istediğinde login sayfasına yönlendirilmelidir."""
        res = self.client.get(reverse('super_admin_dashboard'))
        self.assertEqual(res.status_code, 302)
        self.assertRedirects(res, '/login/?next=/super-admin/')

    def test_cashier_access_denied_403(self):
        """Kasiyer kullanıcısı (is_staff=False, is_superuser=False) /super-admin/ sayfasına erişememeli ve 403 Forbidden almalıdır."""
        self.client.force_login(self.cashier)
        res = self.client.get(reverse('super_admin_dashboard'))
        self.assertEqual(res.status_code, 403)

    def test_store_manager_access_denied_403(self):
        """Mağaza yöneticisi (is_staff=True, is_superuser=False) /super-admin/ sayfasına erişememeli ve 403 Forbidden almalıdır."""
        self.client.force_login(self.store_manager)
        res = self.client.get(reverse('super_admin_dashboard'))
        self.assertEqual(res.status_code, 403)

    def test_superuser_access_granted_200(self):
        """Platform süper yöneticisi (is_superuser=True) /super-admin/ sayfasına sorunsuz erişebilmelidir."""
        self.client.force_login(self.superuser)
        res = self.client.get(reverse('super_admin_dashboard'))
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, 'admin/super_admin_dashboard.html')
        self.assertIn('company_rows', res.context)

    def test_dashboard_displays_all_companies_and_quotas(self):
        """Süper admin paneli platformdaki tüm şirketleri, aktif kasaları ve kotaları eksiksiz listelemelidir."""
        self.client.force_login(self.superuser)
        res = self.client.get(reverse('super_admin_dashboard'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'HiMarket Kadıköy Şubesi')
        self.assertContains(res, 'HiMarket Beşiktaş Şubesi')
        self.assertContains(res, 'kadikoy')
        self.assertContains(res, 'besiktas')
        self.assertGreaterEqual(res.context['total_companies'], 3)
        self.assertGreaterEqual(res.context['total_active_registers'], 3)

    def test_update_company_subscription_action_success(self):
        """Süper admin bir şirketin abonelik paketini, kasa kotasını ve lisans bitiş tarihini güncelleyebilmelidir."""
        self.client.force_login(self.superuser)
        update_url = reverse('super_admin_update_subscription', args=[self.company_kadikoy.id])
        res = self.client.post(update_url, {
            'plan_type': 'GROWTH',
            'max_cash_registers': '5',
            'status': 'ACTIVE',
            'expires_at': '2028-12-31',
            'notes': 'Kurumsal büyüme paketi sözleşmesi'
        })
        self.assertEqual(res.status_code, 302)
        self.assertRedirects(res, reverse('super_admin_dashboard'))

        self.sub_kadikoy.refresh_from_db()
        self.assertEqual(self.sub_kadikoy.plan_type, 'GROWTH')
        self.assertEqual(self.sub_kadikoy.max_cash_registers, 5)
        self.assertEqual(self.sub_kadikoy.status, 'ACTIVE')
        self.assertEqual(self.sub_kadikoy.notes, 'Kurumsal büyüme paketi sözleşmesi')
        self.assertEqual(self.sub_kadikoy.expires_at.year, 2028)
        self.assertEqual(self.sub_kadikoy.expires_at.month, 12)
        self.assertEqual(self.sub_kadikoy.expires_at.day, 31)

    def test_update_subscription_denied_for_store_manager(self):
        """Mağaza yöneticisi süper admin lisans güncelleme POST uç noktasına istek attığında 403 Forbidden almalı ve veri değişmemelidir."""
        self.client.force_login(self.store_manager)
        update_url = reverse('super_admin_update_subscription', args=[self.company_kadikoy.id])
        res = self.client.post(update_url, {
            'plan_type': 'ENTERPRISE',
            'max_cash_registers': '99',
            'status': 'ACTIVE'
        })
        self.assertEqual(res.status_code, 403)
        self.sub_kadikoy.refresh_from_db()
        self.assertEqual(self.sub_kadikoy.plan_type, 'BASIC')
        self.assertEqual(self.sub_kadikoy.max_cash_registers, 1)

    def test_toggle_company_status_action(self):
        """Süper admin bir şirketi pasife alabilmeli ve tekrar aktife çekebilmelidir."""
        self.client.force_login(self.superuser)
        toggle_url = reverse('super_admin_toggle_company_status', args=[self.company_kadikoy.id])

        # 1. Pasife Al
        res1 = self.client.post(toggle_url)
        self.assertEqual(res1.status_code, 302)
        self.company_kadikoy.refresh_from_db()
        self.assertFalse(self.company_kadikoy.is_active)

        # 2. Tekrar Aktife Al
        res2 = self.client.post(toggle_url)
        self.assertEqual(res2.status_code, 302)
        self.company_kadikoy.refresh_from_db()
        self.assertTrue(self.company_kadikoy.is_active)

        # 3. Mağaza yöneticisi yetkisiz erişim denemesi -> 403
        self.client.force_login(self.store_manager)
        res3 = self.client.post(toggle_url)
        self.assertEqual(res3.status_code, 403)

    def test_subscription_is_valid_property_matrix(self):
        """Subscription.is_valid özelliği durum, tarih ve firma aktiflik matrisini doğru değerlendirmelidir."""
        now = timezone.now()
        future = now + datetime.timedelta(days=30)
        past = now - datetime.timedelta(days=1)

        # Case 1: Aktif firma, ACTIVE, gelecekte bitiş -> Geçerli
        self.sub_kadikoy.status = 'ACTIVE'
        self.sub_kadikoy.expires_at = future
        self.assertTrue(self.sub_kadikoy.is_valid)

        # Case 2: Aktif firma, TRIAL, gelecekte bitiş -> Geçerli
        self.sub_kadikoy.status = 'TRIAL'
        self.assertTrue(self.sub_kadikoy.is_valid)

        # Case 3: Aktif firma, SUSPENDED -> Geçersiz
        self.sub_kadikoy.status = 'SUSPENDED'
        self.assertFalse(self.sub_kadikoy.is_valid)

        # Case 4: Aktif firma, EXPIRED -> Geçersiz
        self.sub_kadikoy.status = 'EXPIRED'
        self.assertFalse(self.sub_kadikoy.is_valid)

        # Case 5: Aktif firma, ACTIVE, geçmişte bitiş -> Geçersiz
        self.sub_kadikoy.status = 'ACTIVE'
        self.sub_kadikoy.expires_at = past
        self.assertFalse(self.sub_kadikoy.is_valid)

        # Case 6: Pasif firma, ACTIVE, gelecekte bitiş -> Geçersiz
        self.company_kadikoy.is_active = False
        self.company_kadikoy.save()
        self.sub_kadikoy.expires_at = future
        self.assertFalse(self.sub_kadikoy.is_valid)

        # Case 7: Aktif firma, ACTIVE, süresiz (expires_at=None) -> Geçerli
        self.company_kadikoy.is_active = True
        self.company_kadikoy.save()
        self.sub_kadikoy.expires_at = None
        self.assertTrue(self.sub_kadikoy.is_valid)

    def test_end_to_end_quota_unlock_flow(self):
        """
        Uçtan Uca Kota Açma Akışı:
        1. Kadıköy 1 kasa kotasına sahipken mağaza yöneticisi 2. kasa ekleyemez (kota engeller).
        2. Platform süper yöneticisi /super-admin/ üzerinden kotayı 5 kasaya yükseltir.
        3. Mağaza yöneticisi 2. kasayı başarıyla ekler ve sistemde 2 aktif kasa oluşur.
        """
        # Kadıköy mağazası yöneticisini bağla
        UserProfile.objects.create(user=self.store_manager, company=self.company_kadikoy)

        # Adım 1: Mağaza yöneticisi olarak 2. kasayı eklemeyi dene (Kota: 1, Mevcut: 1)
        self.client.force_login(self.store_manager)
        res_create_blocked = self.client.post(reverse('cash_register_create_action'), {
            'register_code': 'KDK-02',
            'name': 'Kadıköy Kasa 2'
        })
        self.assertEqual(res_create_blocked.status_code, 302)
        # 2. kasa oluşturulamadı!
        self.assertFalse(CashRegister.objects.filter(company=self.company_kadikoy, register_code='KDK-02').exists())

        # Adım 2: Süper yönetici konsola girer ve Kadıköy kotasını 5 yapar
        self.client.force_login(self.superuser)
        update_url = reverse('super_admin_update_subscription', args=[self.company_kadikoy.id])
        res_upgrade = self.client.post(update_url, {
            'plan_type': 'GROWTH',
            'max_cash_registers': '5',
            'status': 'ACTIVE'
        })
        self.assertEqual(res_upgrade.status_code, 302)
        self.sub_kadikoy.refresh_from_db()
        self.assertEqual(self.sub_kadikoy.max_cash_registers, 5)

        # Adım 3: Mağaza yöneticisi tekrar 2. kasayı eklemeyi dener -> Başarılı!
        self.client.force_login(self.store_manager)
        res_create_success = self.client.post(reverse('cash_register_create_action'), {
            'register_code': 'KDK-02',
            'name': 'Kadıköy Kasa 2'
        })
        self.assertEqual(res_create_success.status_code, 302)

        # 2. kasa artık veritabanında aktif olarak var!
        kasa2 = CashRegister.objects.filter(company=self.company_kadikoy, register_code='KDK-02').first()
        self.assertIsNotNone(kasa2)
        self.assertEqual(kasa2.name, 'Kadıköy Kasa 2')
        self.assertTrue(kasa2.is_active)
        self.assertEqual(self.company_kadikoy.cash_registers.filter(is_active=True).count(), 2)

    def test_super_admin_create_company_action_success(self):
        """Süper admin onboarding sihirbazı ile yeni mağaza, lisans ve yönetici hesabı açabilmelidir."""
        self.client.force_login(self.superuser)
        create_url = reverse('super_admin_create_company')
        res = self.client.post(create_url, {
            'name': 'HiMarket İzmir Şubesi',
            'code': 'himarket-izmir',
            'tax_number': '9876543210',
            'tax_office': 'Konak VD',
            'phone': '0232 444 00 00',
            'email': 'izmir@himarket.com',
            'address': 'Alsancak İzmir',
            'plan_type': 'GROWTH',
            'max_cash_registers': '5',
            'duration_days': '365',
            'manager_username': 'izmir_mudur',
            'manager_password': 'mudurpass123'
        })
        self.assertEqual(res.status_code, 302)
        self.assertRedirects(res, reverse('super_admin_dashboard'))

        # Firma kontrolü
        izmir = Company.objects.filter(code='himarket-izmir').first()
        self.assertIsNotNone(izmir)
        self.assertEqual(izmir.name, 'HiMarket İzmir Şubesi')
        self.assertTrue(izmir.is_active)

        # Abonelik kontrolü
        sub = Subscription.objects.filter(company=izmir).first()
        self.assertIsNotNone(sub)
        self.assertEqual(sub.plan_type, 'GROWTH')
        self.assertEqual(sub.max_cash_registers, 5)
        self.assertEqual(sub.status, 'ACTIVE')
        self.assertIsNotNone(sub.expires_at)

        # Otomatik Kasa 1 kontrolü
        kasa1 = CashRegister.objects.filter(company=izmir, register_code='KASA-01').first()
        self.assertIsNotNone(kasa1)
        self.assertTrue(kasa1.is_active)

        # Yönetici kullanıcısı kontrolü
        mgr_user = User.objects.filter(username='izmir_mudur').first()
        self.assertIsNotNone(mgr_user)
        self.assertTrue(mgr_user.is_staff)
        self.assertEqual(mgr_user.profile.company, izmir)

    def test_super_admin_reset_register_hardware_action(self):
        """Süper admin bir kasanın terminal donanım mühürünü sıfırlayabilmelidir."""
        self.client.force_login(self.superuser)
        reg = self.reg_k1
        reset_url = reverse('super_admin_reset_register_hardware', args=[reg.id])
        res = self.client.post(reset_url)
        self.assertEqual(res.status_code, 302)
        self.assertRedirects(res, reverse('super_admin_dashboard'))


class VariantAndApparelModuleTestSuite(TestCase):
    """
    Faz 13: Sektörel Modülerlik, Giyim/Butik Beden-Renk Varyant Motoru ve Hediye Fişi Test Paketi
    """
    def setUp(self):
        self.client = Client()

        # Mağaza ve Kullanıcı Oluşturma
        self.boutique = Company.objects.create(
            name="Zarafet Butik",
            code="BOUTIQUE-01",
            business_type="APPAREL",
            is_active=True
        )
        self.user = User.objects.create_user(
            username='butik_mudur',
            password='Password123!',
            is_staff=True
        )
        UserProfile.objects.create(user=self.user, company=self.boutique)

        self.cashier = User.objects.create_user(
            username='butik_kasiyer',
            password='Password123!',
            is_staff=False
        )
        UserProfile.objects.create(user=self.cashier, company=self.boutique)

        self.register = CashRegister.objects.create(
            company=self.boutique,
            register_code='KASA-01',
            name='Kasa 1 (Ana Kasa)',
            is_active=True
        )

        self.category = Category.objects.create(
            company=self.boutique,
            name="Kadın Giyim",
            is_active=True
        )

        # Ana Ürün (Örn: İpek Elbise)
        self.dress = Product.objects.create(
            company=self.boutique,
            category=self.category,
            name="İpek Desenli Elbise",
            barcode="8699999000014",
            buy_price=Decimal("450.00"),
            sell_price=Decimal("950.00"),
            vat_rate=Decimal("20.00"),
            unit="Adet",
            stock_quantity=Decimal("0.000"),
            has_variants=False,
            is_active=True
        )

    def test_company_business_type_and_settings(self):
        """Şirket business_type alanı APPAREL olarak ayarlanabilmeli ve ayarlar sayfasından güncellenebilmelidir."""
        self.assertEqual(self.boutique.business_type, 'APPAREL')
        self.assertEqual(self.boutique.get_business_type_display(), 'Giyim / Butik / Ayakkabı / Tekstil')

        self.client.force_login(self.user)
        res = self.client.post(reverse('company_settings_view'), {
            'title': 'Zarafet Butik A.Ş.',
            'brand_name': 'Zarafet Butik',
            'tax_number': '9876543210',
            'tax_office': 'Beyoğlu',
            'address': 'İstiklal Cad. No:45',
            'business_type': 'RETAIL',
        })
        self.assertEqual(res.status_code, 302)
        self.boutique.refresh_from_db()
        self.assertEqual(self.boutique.business_type, 'RETAIL')

    def test_variant_matrix_creation_api(self):
        """Beden x Renk matrisi gönderildiğinde tüm kombinasyonlar için otomatik EAN barkodlu varyantlar üretilmelidir."""
        self.client.force_login(self.user)
        url = reverse('product_variants_api', args=[self.dress.id])

        payload = {
            'sizes': ['S', 'M', 'L'],
            'colors': ['Siyah', 'Kırmızı'],
            'default_stock': 5,
            'default_price': None
        }
        res = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['created_count'], 6)

        self.dress.refresh_from_db()
        self.assertTrue(self.dress.has_variants)
        self.assertEqual(self.dress.stock_quantity, Decimal('30.000')) # 6 varyant x 5 adet

        # Varyantların veritabanında doğru beden/renk ve tekil barkodla oluştuğunu doğrula
        variants = self.dress.variants.all()
        self.assertEqual(variants.count(), 6)
        barcodes = [v.barcode for v in variants]
        self.assertEqual(len(barcodes), len(set(barcodes))) # Hepsi benzersiz
        for b in barcodes:
            self.assertTrue(b.startswith('8690002'))

    def test_variant_barcode_search_api(self):
        """Kasada doğrudan varyant barkodu okutulduğunda varyant bilgisi ve fiyatı anında dönmelidir."""
        self.client.force_login(self.cashier)

        # Bir varyant oluşturalım
        variant = ProductVariant.objects.create(
            company=self.boutique,
            product=self.dress,
            size='M',
            color='Siyah',
            barcode='8690002100015',
            stock_quantity=Decimal('8.000'),
            price_override=Decimal('990.00'),
            is_active=True
        )
        self.dress.has_variants = True
        self.dress.stock_quantity = Decimal('8.000')
        self.dress.save()

        # Varyant barkodunu ara
        search_url = f"{reverse('search_barcode')}?barcode=8690002100015"
        res = self.client.get(search_url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertFalse(data['has_variants'])
        self.assertEqual(data['product']['id'], self.dress.id)
        self.assertEqual(data['product']['variant_id'], variant.id)
        self.assertEqual(data['product']['variant_info'], 'M · Siyah')
        self.assertEqual(data['product']['sell_price'], 990.00) # price_override devrede
        self.assertEqual(data['product']['stock_quantity'], 8.0)

    def test_parent_product_search_returns_variant_list(self):
        """Kasada ana ürün barkodu okutulduğunda dokunmatik seçim için varyant listesi (has_variants=True) dönmelidir."""
        self.client.force_login(self.cashier)

        ProductVariant.objects.create(
            company=self.boutique,
            product=self.dress,
            size='S',
            color='Mavi',
            barcode='8690002100022',
            stock_quantity=Decimal('4.000'),
            is_active=True
        )
        ProductVariant.objects.create(
            company=self.boutique,
            product=self.dress,
            size='L',
            color='Mavi',
            barcode='8690002100039',
            stock_quantity=Decimal('6.000'),
            is_active=True
        )
        self.dress.has_variants = True
        self.dress.save()

        search_url = f"{reverse('search_barcode')}?barcode={self.dress.barcode}"
        res = self.client.get(search_url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['has_variants'])
        self.assertEqual(len(data['variants']), 2)
        self.assertEqual(data['variants'][0]['size'], 'L')
        self.assertEqual(data['variants'][1]['size'], 'S')

    def test_process_sale_with_variant_atomic_stock(self):
        """Varyantlı satış yapıldığında hem ProductVariant hem Product stoğu atomik düşmeli ve SaleItem'a variant FK yazılmalıdır."""
        self.client.force_login(self.cashier)

        variant = ProductVariant.objects.create(
            company=self.boutique,
            product=self.dress,
            size='38',
            color='Lacivert',
            barcode='8690002100046',
            stock_quantity=Decimal('10.000'),
            is_active=True
        )
        self.dress.has_variants = True
        self.dress.stock_quantity = Decimal('10.000')
        self.dress.save()

        # 3 Adet satalım
        sale_payload = {
            'items': [
                {
                    'product_id': self.dress.id,
                    'variant_id': variant.id,
                    'quantity': 3
                }
            ],
            'payment_type': 'NAKIT',
            'cash_paid': 3000,
            'is_return': False
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(sale_payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])

        # Stok düşümü kontrolü
        variant.refresh_from_db()
        self.assertEqual(variant.stock_quantity, Decimal('7.000')) # 10 - 3 = 7
        self.dress.refresh_from_db()
        self.assertEqual(self.dress.stock_quantity, Decimal('7.000')) # 10 - 3 = 7

        # Fiş ve Kalem Kontrolü
        sale = Sale.objects.get(receipt_number=data['receipt_number'])
        self.assertEqual(sale.items.count(), 1)
        item = sale.items.first()
        self.assertEqual(item.variant, variant)
        self.assertEqual(item.variant_info, '38 / Lacivert')
        self.assertIn('38 / Lacivert', item.product_name)
        self.assertEqual(item.barcode, '8690002100046')
        self.assertEqual(item.quantity, Decimal('3.000'))

    def test_process_sale_insufficient_variant_stock(self):
        """Varyant stoğundan fazla miktar talep edildiğinde satış 400 hatası vermeli ve işlem geri alınmalıdır."""
        self.client.force_login(self.cashier)

        variant = ProductVariant.objects.create(
            company=self.boutique,
            product=self.dress,
            size='40',
            color='Beyaz',
            barcode='8690002100053',
            stock_quantity=Decimal('2.000'),
            is_active=True
        )
        self.dress.has_variants = True
        self.dress.stock_quantity = Decimal('2.000')
        self.dress.save()

        sale_payload = {
            'items': [
                {
                    'product_id': self.dress.id,
                    'variant_id': variant.id,
                    'quantity': 5 # Stok 2 iken 5 talep ediliyor
                }
            ],
            'payment_type': 'NAKIT',
            'cash_paid': 5000,
            'is_return': False
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(sale_payload), content_type='application/json')
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertFalse(data['success'])
        self.assertIn('Yetersiz Stok', data['message'])

        # Stok değişmemeli
        variant.refresh_from_db()
        self.assertEqual(variant.stock_quantity, Decimal('2.000'))

    def test_return_sale_with_variant(self):
        """Müşteri iadesinde varyant stoğu ve ana ürün stoğu geri artmalıdır."""
        self.client.force_login(self.cashier)

        variant = ProductVariant.objects.create(
            company=self.boutique,
            product=self.dress,
            size='36',
            color='Pembe',
            barcode='8690002100060',
            stock_quantity=Decimal('5.000'),
            is_active=True
        )
        self.dress.has_variants = True
        self.dress.stock_quantity = Decimal('5.000')
        self.dress.save()

        # İade işlemi
        return_payload = {
            'items': [
                {
                    'product_id': self.dress.id,
                    'variant_id': variant.id,
                    'quantity': 2
                }
            ],
            'payment_type': 'NAKIT',
            'is_return': True
        }
        res = self.client.post(reverse('process_sale'), data=json.dumps(return_payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)

        variant.refresh_from_db()
        self.assertEqual(variant.stock_quantity, Decimal('7.000')) # 5 + 2 = 7
        self.dress.refresh_from_db()
        self.assertEqual(self.dress.stock_quantity, Decimal('7.000'))

    def test_gift_receipt_view_renders_clean_without_prices(self):
        """Hediye değişim fişi 200 dönmeli, ürün adı ve varyantını göstermeli ama fiyat bilgisi içermemelidir."""
        self.client.force_login(self.cashier)

        variant = ProductVariant.objects.create(
            company=self.boutique,
            product=self.dress,
            size='M',
            color='Bej',
            barcode='8690002100077',
            stock_quantity=Decimal('10.000'),
            is_active=True
        )
        self.dress.has_variants = True
        self.dress.stock_quantity = Decimal('10.000')
        self.dress.save()

        sale_payload = {
            'items': [
                {
                    'product_id': self.dress.id,
                    'variant_id': variant.id,
                    'quantity': 1
                }
            ],
            'payment_type': 'NAKIT',
            'cash_paid': 1000,
            'is_return': False
        }
        res_sale = self.client.post(reverse('process_sale'), data=json.dumps(sale_payload), content_type='application/json')
        receipt_no = res_sale.json()['receipt_number']

        # Hediye Fişi Görüntüleme
        gift_url = reverse('gift_receipt_view', args=[receipt_no])
        res_gift = self.client.get(gift_url)
        self.assertEqual(res_gift.status_code, 200)
        content = res_gift.content.decode('utf-8')

        self.assertIn('HEDİYE DEĞİŞİM FİŞİ', content)
        self.assertIn('İpek Desenli Elbise', content)
        self.assertIn('M / Bej', content)
        self.assertIn('30 gün', content)
        self.assertIn(receipt_no, content)
        # Fiyat ve TL bilgisi fişte yer almamalıdır
        self.assertNotIn('950.00', content)
        self.assertNotIn('Toplam Tutar', content)


class OfflineKasaAndPwaTestSuite(TestCase):
    """
    FAZ 14: ÇEVRİMDIŞI (OFFLINE) KASA MOTORU (PWA + INDEXEDDB) BİRİM TESTLERİ
    - Ürün Kataloğu Önbellek API'si (/api/pos/catalog-cache/)
    - Çevrimdışı Satış Senkronizasyonu (/api/sale/offline-sync/)
    - Idempotency & Tekrar Önleme (Aynı offline ID ile çift kayıt engelleme)
    - Varyantlı Ürünlerin Çevrimdışı Senkronizasyonu ve Stok Düşümü
    - Çevrimdışı Veresiye ve İade Senkronizasyonu
    - PWA Manifest ve Service Worker Uç Noktaları
    """

    def setUp(self):
        self.company = Company.objects.create(
            name="Test Offline Süpermarket",
            code="test-offline-market",
            tax_number="9876543210",
            business_type="MARKET",
            is_active=True
        )
        self.user = User.objects.create_user(
            username="kasiyer_offline",
            password="password123",
            is_staff=True
        )
        UserProfile.objects.create(user=self.user, company=self.company)

        self.register = CashRegister.objects.create(
            company=self.company,
            name="Offline Kasa 1",
            register_code="KASA-OFFLINE"
        )
        self.shift = Shift.objects.create(
            company=self.company,
            cash_register=self.register,
            cashier=self.user,
            is_closed=False
        )

        self.category = Category.objects.create(
            company=self.company,
            name="Temel Gıda"
        )
        self.product = Product.objects.create(
            company=self.company,
            category=self.category,
            name="Organik Köy Yumurtası 10'lu",
            barcode="8690001112223",
            sell_price=Decimal("65.00"),
            buy_price=Decimal("45.00"),
            vat_rate=Decimal("1.00"),
            stock_quantity=Decimal("100.000"),
            is_active=True
        )

        # Varyantlı Ürün (Giyim/Tekstil)
        self.apparel_product = Product.objects.create(
            company=self.company,
            category=self.category,
            name="Oversize Pamuklu Sweatshirt",
            barcode="8690003334445",
            sell_price=Decimal("400.00"),
            buy_price=Decimal("250.00"),
            vat_rate=Decimal("10.00"),
            stock_quantity=Decimal("30.000"),
            has_variants=True,
            is_active=True
        )
        self.variant = ProductVariant.objects.create(
            company=self.company,
            product=self.apparel_product,
            size="L",
            color="Antrasit",
            barcode="8690009998887",
            stock_quantity=Decimal("15.000"),
            price_override=Decimal("420.00"),
            is_active=True
        )

        self.customer = Customer.objects.create(
            company=self.company,
            first_name="Ahmet",
            last_name="Çevrimdışı",
            phone="05551112233",
            credit_limit=Decimal("2000.00"),
            balance=Decimal("0.00"),
            is_active=True
        )

        self.client = Client()
        self.client.force_login(self.user)

    def test_pos_catalog_cache_api(self):
        """Katalog önbellek API'si aktif ürünleri ve varyantları doğru formatta döndürmelidir."""
        url = reverse('pos_catalog_cache_api')
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertTrue(data['success'])
        self.assertEqual(data['company_name'], "Test Offline Süpermarket")
        self.assertGreaterEqual(data['total_count'], 2)

        products = {p['id']: p for p in data['products']}
        self.assertIn(self.product.id, products)
        self.assertEqual(products[self.product.id]['name'], "Organik Köy Yumurtası 10'lu")
        self.assertEqual(products[self.product.id]['sell_price'], 65.0)

        # Varyant kontrolü
        apparel_data = products[self.apparel_product.id]
        self.assertTrue(apparel_data['has_variants'])
        self.assertEqual(len(apparel_data['variants']), 1)
        self.assertEqual(apparel_data['variants'][0]['size'], 'L')
        self.assertEqual(apparel_data['variants'][0]['color'], 'Antrasit')
        self.assertEqual(apparel_data['variants'][0]['effective_price'], 420.0)

    def test_offline_sale_sync_api_single_sale(self):
        """Tekil bir çevrimdışı satış fişi sunucuya aktarıldığında Sale kaydı ve stok düşümü yapılmalıdır."""
        url = reverse('offline_sale_sync_api')
        offline_id = "OFF-20261119-100000-TEST1"

        payload = {
            "sales": [{
                "offline_receipt_id": offline_id,
                "created_at": "2026-11-19T10:00:00Z",
                "items": [
                    {"product_id": self.product.id, "quantity": 2}
                ],
                "payment_type": "NAKIT",
                "cash_paid": 150.00,
                "card_paid": 0.00,
                "discount_amount": 0.00,
                "is_return": False
            }]
        }

        res = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertTrue(data['success'])
        self.assertEqual(data['synced_count'], 1)
        self.assertEqual(data['failed_count'], 0)

        # Veritabanı doğrulama
        sale = Sale.objects.get(offline_sync_id=offline_id)
        self.assertTrue(sale.is_offline_sync)
        self.assertEqual(sale.total_amount, Decimal("130.00")) # 2 x 65
        self.assertEqual(sale.change_given, Decimal("20.00"))  # 150 - 130

        # Stok düşümü kontrolü
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal("98.000"))

    def test_offline_sale_sync_api_idempotency(self):
        """Aynı çevrimdışı fiş tekrar gönderildiğinde mükerrer kayıt açılmamalı ve stok tekrar düşmemelidir."""
        url = reverse('offline_sale_sync_api')
        offline_id = "OFF-20261119-100500-IDEMPOTENT"

        payload = {
            "sales": [{
                "offline_receipt_id": offline_id,
                "created_at": "2026-11-19T10:05:00Z",
                "items": [
                    {"product_id": self.product.id, "quantity": 1}
                ],
                "payment_type": "NAKIT",
                "cash_paid": 65.00,
                "card_paid": 0.00,
                "discount_amount": 0.00,
                "is_return": False
            }]
        }

        # İlk senkronizasyon
        res1 = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.json()['synced_count'], 1)

        self.product.refresh_from_db()
        stock_after_first = self.product.stock_quantity

        # İkinci senkronizasyon (Aynı offline_receipt_id ile)
        res2 = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()

        self.assertEqual(data2['synced_count'], 0)
        self.assertEqual(data2['already_synced_count'], 1)
        self.assertEqual(data2['results'][0]['status'], 'ALREADY_SYNCED')

        # Stok değişmemelidir
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, stock_after_first)
        self.assertEqual(Sale.objects.filter(offline_sync_id=offline_id).count(), 1)

    def test_offline_sale_sync_api_with_apparel_variants(self):
        """Varyantlı giyim ürünleri çevrimdışı satıldığında hem varyant hem ana stok düşmelidir."""
        url = reverse('offline_sale_sync_api')
        offline_id = "OFF-20261119-101000-VARIANT"

        payload = {
            "sales": [{
                "offline_receipt_id": offline_id,
                "created_at": "2026-11-19T10:10:00Z",
                "items": [
                    {"product_id": self.apparel_product.id, "variant_id": self.variant.id, "quantity": 3}
                ],
                "payment_type": "KART",
                "cash_paid": 0.00,
                "card_paid": 1260.00, # 3 x 420.00
                "discount_amount": 0.00,
                "is_return": False
            }]
        }

        res = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['synced_count'], 1)

        # Hem varyant stoku (15 - 3 = 12) hem ana ürün stoku (30 - 3 = 27) düşmelidir
        self.variant.refresh_from_db()
        self.apparel_product.refresh_from_db()
        self.assertEqual(self.variant.stock_quantity, Decimal("12.000"))
        self.assertEqual(self.apparel_product.stock_quantity, Decimal("27.000"))

        sale = Sale.objects.get(offline_sync_id=offline_id)
        self.assertEqual(sale.total_amount, Decimal("1260.00"))
        item = sale.items.first()
        self.assertEqual(item.variant, self.variant)
        self.assertEqual(item.variant_info, "L · Antrasit")

    def test_offline_sale_sync_api_veresiye(self):
        """Çevrimdışı veresiye satış senkronize edildiğinde müşterinin borç bakiyesi artmalıdır."""
        url = reverse('offline_sale_sync_api')
        offline_id = "OFF-20261119-101500-VERESIYE"

        payload = {
            "sales": [{
                "offline_receipt_id": offline_id,
                "created_at": "2026-11-19T10:15:00Z",
                "items": [
                    {"product_id": self.product.id, "quantity": 1}
                ],
                "payment_type": "VERESIYE",
                "customer_id": self.customer.id,
                "cash_paid": 0.00,
                "card_paid": 0.00,
                "discount_amount": 0.00,
                "is_return": False
            }]
        }

        res = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['synced_count'], 1)

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, Decimal("65.00"))
        self.assertTrue(CustomerTransaction.objects.filter(sale__offline_sync_id=offline_id).exists())

    def test_offline_sale_sync_api_return(self):
        """Çevrimdışı iade fişi senkronize edildiğinde stok artmalı ve iade fişi oluşmalıdır."""
        url = reverse('offline_sale_sync_api')
        offline_id = "OFF-20261119-102000-RETURN"

        payload = {
            "sales": [{
                "offline_receipt_id": offline_id,
                "created_at": "2026-11-19T10:20:00Z",
                "items": [
                    {"product_id": self.product.id, "quantity": 1}
                ],
                "payment_type": "NAKIT",
                "cash_paid": 0.00,
                "card_paid": 0.00,
                "discount_amount": 0.00,
                "is_return": True
            }]
        }

        res = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['synced_count'], 1)

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal("101.000"))

        sale = Sale.objects.get(offline_sync_id=offline_id)
        self.assertTrue(sale.is_return)
        self.assertTrue(sale.receipt_number.startswith('IADE-'))

    def test_pwa_service_worker_and_manifest_endpoints(self):
        """PWA manifest ve service worker uç noktaları HTTP 200 ve doğru content type ile yanıt vermelidir."""
        res_sw = self.client.get(reverse('service_worker'))
        self.assertEqual(res_sw.status_code, 200)
        self.assertIn('application/javascript', res_sw['Content-Type'])
        self.assertEqual(res_sw['Service-Worker-Allowed'], '/')

        res_manifest = self.client.get(reverse('pwa_manifest'))
        self.assertEqual(res_manifest.status_code, 200)
        self.assertIn('application/manifest+json', res_manifest['Content-Type'])
        manifest_data = json.loads(res_manifest.content.decode('utf-8'))
        self.assertEqual(manifest_data['short_name'], 'HiMarket POS')
        self.assertEqual(manifest_data['display'], 'standalone')


class GlobalProductPoolTestSuite(TestCase):
    """
    Faz 15: Ortak Hazır Ürün Havuzu (GlobalProductPool) ve Toplu CSV İçe Aktarma Testleri
    """
    def setUp(self):
        self.company = Company.objects.create(
            name="Test Market Şubesi",
            code="test-pool-market",
            is_active=True
        )
        self.user = User.objects.create_user(
            username="pool_admin",
            password="poolpassword123",
            is_staff=True
        )
        UserProfile.objects.create(
            user=self.user,
            company=self.company
        )
        self.category = Category.objects.create(
            company=self.company,
            name="Temel Gıda",
            is_active=True
        )
        self.local_product = Product.objects.create(
            company=self.company,
            category=self.category,
            barcode="8690000000001",
            name="Yerel Dükkan Ürünü",
            sell_price=Decimal("20.00"),
            stock_quantity=Decimal("50.000"),
            is_active=True
        )
        # Global Havuz Ürünü
        self.pool_item = GlobalProductPool.objects.create(
            barcode="8690504012345",
            name="Ülker Çikolatalı Gofret 35g",
            brand="Ülker",
            category_name="Bisküvi & Atıştırmalık",
            vat_rate=Decimal("1.00"),
            recommended_price=Decimal("15.00"),
            unit="Adet",
            is_verified=True
        )
        self.client.login(username="pool_admin", password="poolpassword123")

    def test_seed_global_pool_command(self):
        """seed_global_pool komutu veritabanına hazır ürünleri hatasız yüklemeli ve idempotent olmalıdır."""
        out = io.StringIO()
        call_command('seed_global_pool', stdout=out)
        self.assertIn("Global Barkod Havuzu Başarıyla Tohumlandı!", out.getvalue())
        self.assertGreaterEqual(GlobalProductPool.objects.count(), 50)

    def test_search_barcode_with_global_pool_fallback(self):
        """Dükkanda olmayan ama havuzda olan ürün sorgulandığında pool_found=True ve pool_product dönmelidir."""
        # 1. Dükkanda olan ürün
        res_local = self.client.get(reverse('search_barcode'), {'barcode': '8690000000001'})
        self.assertEqual(res_local.status_code, 200)
        self.assertTrue(res_local.json()['success'])
        self.assertEqual(res_local.json()['product']['name'], "Yerel Dükkan Ürünü")

        # 2. Dükkanda yok ama global havuzda var
        res_pool = self.client.get(reverse('search_barcode'), {'barcode': '8690504012345'})
        self.assertEqual(res_pool.status_code, 200)
        data_pool = res_pool.json()
        self.assertFalse(data_pool['success'])
        self.assertTrue(data_pool['pool_found'])
        self.assertEqual(data_pool['pool_product']['name'], "Ülker Çikolatalı Gofret 35g")
        self.assertEqual(data_pool['pool_product']['brand'], "Ülker")

        # 3. Ne dükkanda ne havuzda olan ürün -> 404
        res_none = self.client.get(reverse('search_barcode'), {'barcode': '9999999999999'})
        self.assertEqual(res_none.status_code, 404)

    def test_create_product_from_pool_api(self):
        """Kasadan tek tıkla havuzdaki ürün mağaza stoğuna aktarılabilmelidir."""
        url = reverse('create_product_from_pool_api')
        payload = {
            'barcode': '8690504012345',
            'sell_price': '16.50',
            'stock_quantity': '24'
        }
        res = self.client.post(url, data=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['product']['sell_price'], 16.50)

        # Veritabanında dükkana özel ürün oluştuğunu doğrula
        created_prod = Product.objects.get(company=self.company, barcode='8690504012345')
        self.assertEqual(created_prod.name, "Ülker Çikolatalı Gofret 35g")
        self.assertEqual(created_prod.sell_price, Decimal("16.50"))
        self.assertEqual(created_prod.stock_quantity, Decimal("24.000"))
        self.assertEqual(created_prod.category.name, "Bisküvi & Atıştırmalık")

        # Mükerrer çağrıldığında hata vermek yerine already_exists=True ile mevcut ürünü dönmelidir
        res_dup = self.client.post(url, data=payload)
        self.assertEqual(res_dup.status_code, 200)
        self.assertTrue(res_dup.json().get('already_exists'))

    def test_product_import_sample_csv_download(self):
        """Örnek CSV şablonu indirme uç noktası HTTP 200 ve UTF-8 BOM CSV dönmelidir."""
        res = self.client.get(reverse('download_product_import_sample_csv'))
        self.assertEqual(res.status_code, 200)
        self.assertIn('text/csv', res['Content-Type'])
        self.assertIn('attachment; filename="urun_aktarim_sablonu.csv"', res['Content-Disposition'])
        content = res.content.decode('utf-8')
        self.assertIn('Barkod;Ürün Adı;Kategori;Satış Fiyatı', content)

    def test_product_import_view_csv_upload(self):
        """CSV dosyası yüklendiğinde yeni ürünler oluşturulmalı ve var olanlar seçeneğe göre güncellenmelidir."""
        csv_content = (
            "Barkod;Ürün Adı;Kategori;Satış Fiyatı;Alış Fiyatı;Stok Miktarı;KDV Oranı;Birim;Kritik Stok\n"
            "8690000000001;Yerel Dükkan Ürünü Güncel;Temel Gıda;25.00;18.00;60;1;Adet;5\n"
            "8690111222333;Yeni İçecek 1L;İçecekler;30.00;22.00;40;10;Adet;10\n"
        )
        csv_file = SimpleUploadedFile("urunler.csv", csv_content.encode('utf-8-sig'), content_type="text/csv")

        url = reverse('product_import_view')
        res = self.client.post(url, {
            'csv_file': csv_file,
            'update_existing': 'on'
        })
        self.assertEqual(res.status_code, 200)
        self.assertIn('result_summary', res.context)
        summary = res.context['result_summary']
        self.assertEqual(summary['created_count'], 1)
        self.assertEqual(summary['updated_count'], 1)

        # Güncellenen ürünü doğrula
        self.local_product.refresh_from_db()
        self.assertEqual(self.local_product.name, "Yerel Dükkan Ürünü Güncel")
        self.assertEqual(self.local_product.sell_price, Decimal("25.00"))
        self.assertEqual(self.local_product.stock_quantity, Decimal("60.000"))

        # Yeni eklenen ürünü doğrula
        new_prod = Product.objects.get(company=self.company, barcode="8690111222333")
        self.assertEqual(new_prod.name, "Yeni İçecek 1L")
        self.assertEqual(new_prod.sell_price, Decimal("30.00"))

    def test_global_product_pool_view_and_add_action(self):
        """Yönetim paneli havuz ekranı açılmalı ve 'Mağazaya Ekle' aksiyonu dükkan stoğuna katmalıdır."""
        url = reverse('global_product_pool_view')
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Ülker Çikolatalı Gofret 35g")

        # Havuzdan mağazaya ekle aksiyonu
        add_url = reverse('add_pool_product_to_store_action')
        res_add = self.client.post(add_url, {
            'barcode': '8690504012345',
            'sell_price': '15.50',
            'stock_quantity': '15'
        })
        self.assertEqual(res_add.status_code, 302)

        added = Product.objects.get(company=self.company, barcode='8690504012345')
        self.assertEqual(added.sell_price, Decimal("15.50"))
        self.assertEqual(added.stock_quantity, Decimal("15.000"))


class ExpirationAndBossCockpitTestSuite(TestCase):
    """
    Faz 16: SKT & Parti (Lot) Takibi ve Mobil Patron Kokpiti Birim Testleri
    """
    def setUp(self):
        self.company = Company.objects.create(
            name="Test Süpermarket Şubesi",
            code="test-boss-market",
            is_active=True
        )
        self.user = User.objects.create_user(
            username="boss_user",
            password="bosspassword123",
            is_staff=True
        )
        UserProfile.objects.create(
            user=self.user,
            company=self.company
        )
        self.category = Category.objects.create(
            company=self.company,
            name="Süt & Şarküteri",
            is_active=True
        )

        today = timezone.localdate()

        # 1. Tarihi Geçmiş Ürün (Zayiat)
        self.prod_expired = Product.objects.create(
            company=self.company,
            category=self.category,
            barcode="869052000001",
            name="Tarihi Geçmiş Yoğurt 500g",
            buy_price=Decimal("20.00"),
            sell_price=Decimal("30.00"),
            stock_quantity=Decimal("10.000"),
            track_expiration=True,
            expiration_date=today - timedelta(days=2),
            lot_number="LOT-EXP-01",
            is_active=True
        )

        # 2. 2 Gün Kalan Kritik Ürün
        self.prod_critical = Product.objects.create(
            company=self.company,
            category=self.category,
            barcode="869052000002",
            name="Sütaş Taze Süt 1L",
            buy_price=Decimal("25.00"),
            sell_price=Decimal("35.00"),
            stock_quantity=Decimal("15.000"),
            track_expiration=True,
            expiration_date=today + timedelta(days=2),
            lot_number="LOT-CRT-02",
            is_active=True
        )

        # 3. Güvenli Normal Ürün
        self.prod_safe = Product.objects.create(
            company=self.company,
            category=self.category,
            barcode="869052000003",
            name="Kaşar Peyniri 400g",
            buy_price=Decimal("100.00"),
            sell_price=Decimal("150.00"),
            stock_quantity=Decimal("20.000"),
            track_expiration=True,
            expiration_date=today + timedelta(days=45),
            lot_number="LOT-SAFE-03",
            is_active=True
        )

        self.register = CashRegister.objects.create(
            company=self.company,
            name="Ana Kasa 1",
            register_code="KASA-01",
            is_active=True
        )
        self.shift = Shift.objects.create(
            company=self.company,
            cash_register=self.register,
            cashier=self.user,
            total_cash_sales=Decimal("500.00"),
            is_closed=False
        )

        self.client.login(username="boss_user", password="bosspassword123")

    def test_product_expiration_properties(self):
        """Ürünlerin kalan gün ve SKT alarm durumları matematiksel olarak doğru hesaplanmalıdır."""
        # Geçmiş
        self.assertEqual(self.prod_expired.days_to_expiration, -2)
        self.assertEqual(self.prod_expired.expiration_status, 'EXPIRED')

        # 2 gün kalmış (Kritik)
        self.assertEqual(self.prod_critical.days_to_expiration, 2)
        self.assertEqual(self.prod_critical.expiration_status, 'CRITICAL')

        # 45 gün kalmış (Güvenli)
        self.assertEqual(self.prod_safe.days_to_expiration, 45)
        self.assertEqual(self.prod_safe.expiration_status, 'OK')

    def test_search_barcode_expiration_alerts(self):
        """Kasada okutulan ürünün SKT alarm bayrakları JSON çıktısına eksiksiz yansımalıdır."""
        # Geçmiş ürün sorgusu
        res_exp = self.client.get(reverse('search_barcode'), {'barcode': '869052000001'})
        self.assertEqual(res_exp.status_code, 200)
        data_exp = res_exp.json()
        self.assertTrue(data_exp['success'])
        self.assertTrue(data_exp['product']['is_expired'])
        self.assertEqual(data_exp['product']['expiration_status'], 'EXPIRED')
        self.assertEqual(data_exp['product']['lot_number'], 'LOT-EXP-01')

        # Kritik ürün sorgusu
        res_crt = self.client.get(reverse('search_barcode'), {'barcode': '869052000002'})
        self.assertEqual(res_crt.status_code, 200)
        data_crt = res_crt.json()
        self.assertTrue(data_crt['success'])
        self.assertFalse(data_crt['product']['is_expired'])
        self.assertTrue(data_crt['product']['is_expiring_soon'])
        self.assertEqual(data_crt['product']['expiration_status'], 'CRITICAL')

    def test_expiration_tracking_view_and_export(self):
        """SKT takip paneli ve CSV indirme uç noktası doğru listeleme ve filtreleme yapmalıdır."""
        url = reverse('expiration_tracking_view')
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context['expired_count'], 1)
        self.assertEqual(res.context['critical_count'], 1)
        self.assertEqual(res.context['total_tracked_count'], 3)

        # Filtre testi: Sadece geçmişler
        res_filtered = self.client.get(url, {'status': 'EXPIRED'})
        self.assertEqual(res_filtered.status_code, 200)
        self.assertEqual(len(res_filtered.context['products']), 1)
        self.assertEqual(res_filtered.context['products'][0].barcode, "869052000001")

        # CSV İndirme
        res_csv = self.client.get(reverse('export_expirations_csv'))
        self.assertEqual(res_csv.status_code, 200)
        self.assertIn('text/csv', res_csv['Content-Type'])
        content = res_csv.content.decode('utf-8')
        self.assertIn('Tarihi Geçmiş Yoğurt 500g', content)
        self.assertIn('GEÇMİŞ', content)

    def test_mobile_boss_dashboard_and_live_metrics_api(self):
        """Mobil Patron Kokpiti ve canlı API uç noktası ciro, kâr marjı ve iptalleri doğru hesaplamalıdır."""
        # 1. Başarılı Satış Fişi Oluştur (Ciro: 150 TL, Maliyet: 100 TL -> Net Kâr: 50 TL)
        sale = Sale.objects.create(
            company=self.company,
            shift=self.shift,
            cashier=self.user,
            receipt_number="TEST-BOSS-001",
            payment_type="NAKIT",
            cash_paid=Decimal("150.00"),
            total_amount=Decimal("150.00"),
            is_cancelled=False
        )
        SaleItem.objects.create(
            sale=sale,
            product=self.prod_safe,
            product_name=self.prod_safe.name,
            barcode=self.prod_safe.barcode,
            quantity=Decimal("1.000"),
            unit_price=Decimal("150.00"),
            buy_price=Decimal("100.00"),
            total_price=Decimal("150.00")
        )

        # 2. İptal Edilen Şüpheli Satış Fişi Oluştur
        Sale.objects.create(
            company=self.company,
            shift=self.shift,
            cashier=self.user,
            receipt_number="TEST-CANCEL-002",
            payment_type="NAKIT",
            total_amount=Decimal("80.00"),
            is_cancelled=True,
            cancellation_reason="Müşteri vazgeçti"
        )

        # Web Arayüzü Testi
        url_web = reverse('mobile_boss_dashboard_view')
        res_web = self.client.get(url_web)
        self.assertEqual(res_web.status_code, 200)
        self.assertContains(res_web, "Mobil Patron Kokpiti")
        self.assertEqual(res_web.context['m']['net_revenue'], Decimal("150.00"))
        self.assertEqual(res_web.context['m']['gross_profit'], Decimal("50.00"))
        self.assertEqual(res_web.context['m']['cancelled_count'], 1)

        # Canlı JSON API Testi
        url_api = reverse('boss_live_metrics_api')
        res_api = self.client.get(url_api)
        self.assertEqual(res_api.status_code, 200)
        data = res_api.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['metrics']['net_revenue'], 150.00)
        self.assertEqual(data['metrics']['gross_profit'], 50.00)
        self.assertEqual(data['metrics']['cancelled_count'], 1)
        self.assertEqual(data['metrics']['cancelled_total'], 80.00)
        self.assertEqual(len(data['open_shifts']), 1)


class BranchTransferAndFiscalTestSuite(TestCase):
    """
    Faz 18: Çoklu Şube, Depo Stok Transferi, Merkezi Fiyat Yayılımı ve Yeni Nesil ÖKC Testleri
    """
    def setUp(self):
        self.client = Client()

        # 1. Merkez Mağaza / Depo ve Bağlı Şube
        self.hq_company = Company.objects.create(
            name="HiMarket Merkez Depo",
            code="merkez-depo",
            is_warehouse=True,
            is_active=True
        )
        self.branch_company = Company.objects.create(
            name="HiMarket Kadıköy Şube",
            code="kadikoy-sube",
            parent=self.hq_company,
            is_warehouse=False,
            is_active=True
        )

        # 2. Kullanıcılar ve Profiller
        self.hq_user = User.objects.create_user(
            username="hq_admin",
            password="Password123!",
            is_staff=True
        )
        UserProfile.objects.create(user=self.hq_user, company=self.hq_company)

        self.branch_user = User.objects.create_user(
            username="branch_admin",
            password="Password123!",
            is_staff=True
        )
        UserProfile.objects.create(user=self.branch_user, company=self.branch_company)

        # 3. Kategori ve Ürünler
        self.cat_hq = Category.objects.create(company=self.hq_company, name="Temel Gıda")
        self.prod_hq = Product.objects.create(
            company=self.hq_company,
            barcode="869000001801",
            name="Organik Süt 1L",
            category=self.cat_hq,
            buy_price=Decimal("20.00"),
            sell_price=Decimal("30.00"),
            vat_rate=Decimal("1.00"),
            stock_quantity=Decimal("50.000"),
            unit="Adet",
            is_active=True
        )

        # Kasa ve Satış için setup
        self.register = CashRegister.objects.create(
            company=self.hq_company,
            name="HQ Kasa 1",
            register_code="HQ-01",
            is_active=True
        )
        self.shift = Shift.objects.create(
            cash_register=self.register,
            cashier=self.hq_user,
            opening_float=Decimal("500.00"),
            is_closed=False
        )

    def test_company_hierarchy_and_warehouse(self):
        """Şirket hiyerarşisi (parent/child) ve depo özelliği doğru çalışmalıdır."""
        self.assertTrue(self.hq_company.is_warehouse)
        self.assertFalse(self.branch_company.is_warehouse)
        self.assertEqual(self.branch_company.parent, self.hq_company)
        self.assertIn(self.branch_company, self.hq_company.branches.all())

    def test_branch_transfer_lifecycle_ship_and_receive(self):
        """Stok transferi sevk edildiğinde kaynak stok düşmeli, teslim alındığında hedef stoğa eklenmelidir."""
        # 1. Transfer Oluştur (Taslak)
        transfer = BranchTransfer.objects.create(
            transfer_number="TRF-TEST-001",
            from_company=self.hq_company,
            to_company=self.branch_company,
            status="DRAFT",
            created_by=self.hq_user
        )
        BranchTransferItem.objects.create(
            transfer=transfer,
            product=self.prod_hq,
            quantity=Decimal("15.000"),
            unit="Adet"
        )
        self.assertEqual(transfer.status, "DRAFT")
        self.assertEqual(transfer.total_quantity, Decimal("15.000"))

        # 2. Sevk Et (Yola Çıkar)
        transfer.ship(user=self.hq_user)
        self.assertEqual(transfer.status, "IN_TRANSIT")
        self.assertIsNotNone(transfer.shipped_at)

        self.prod_hq.refresh_from_db()
        self.assertEqual(self.prod_hq.stock_quantity, Decimal("35.000"))  # 50 - 15 = 35

        # 3. Varış Şubesinde Teslim Al
        transfer.receive(user=self.branch_user)
        self.assertEqual(transfer.status, "COMPLETED")
        self.assertIsNotNone(transfer.received_at)
        self.assertEqual(transfer.received_by, self.branch_user)

        # Hedef şubede ürün otomatik oluşmuş veya stoku artmış olmalı
        branch_prod = Product.objects.filter(company=self.branch_company, barcode=self.prod_hq.barcode).first()
        self.assertIsNotNone(branch_prod)
        self.assertEqual(branch_prod.stock_quantity, Decimal("15.000"))
        self.assertEqual(branch_prod.sell_price, Decimal("30.000"))

    def test_branch_transfer_cancellation_restores_stock(self):
        """Yoldaki bir transfer iptal edildiğinde düşülen stoklar kaynak depoya iade edilmelidir."""
        transfer = BranchTransfer.objects.create(
            transfer_number="TRF-TEST-002",
            from_company=self.hq_company,
            to_company=self.branch_company,
            status="DRAFT",
            created_by=self.hq_user
        )
        BranchTransferItem.objects.create(
            transfer=transfer,
            product=self.prod_hq,
            quantity=Decimal("10.000"),
            unit="Adet"
        )
        transfer.ship(user=self.hq_user)
        self.prod_hq.refresh_from_db()
        self.assertEqual(self.prod_hq.stock_quantity, Decimal("40.000"))  # 50 - 10 = 40

        # İptal et
        transfer.cancel(user=self.hq_user)
        self.assertEqual(transfer.status, "CANCELLED")
        self.prod_hq.refresh_from_db()
        self.assertEqual(self.prod_hq.stock_quantity, Decimal("50.000"))  # Stok geri iade edildi!

    def test_branch_transfers_views_and_actions(self):
        """Transfer yönetim ekranları, oluşturma formu ve aksiyon uç noktaları doğru çalışmalıdır."""
        self.client.force_login(self.hq_user)

        # Liste sayfası
        res_list = self.client.get(reverse('branch_transfers_view'))
        self.assertEqual(res_list.status_code, 200)

        # Yeni transfer oluşturma formu (GET)
        res_create_get = self.client.get(reverse('branch_transfer_create_view'))
        self.assertEqual(res_create_get.status_code, 200)

        # Yeni transfer oluşturma (POST - DRAFT)
        res_create_post = self.client.post(reverse('branch_transfer_create_view'), {
            'to_company_id': self.branch_company.id,
            'action_type': 'DRAFT',
            'notes': 'Test sevk',
            'product_id[]': [self.prod_hq.id],
            'quantity[]': ['5.000'],
            'item_notes[]': ['Özenli taşıyınız']
        })
        self.assertEqual(res_create_post.status_code, 302)

        created_transfer = BranchTransfer.objects.filter(from_company=self.hq_company, status='DRAFT').first()
        self.assertIsNotNone(created_transfer)
        self.assertEqual(created_transfer.items.count(), 1)

        # Detay sayfası (GET)
        res_detail = self.client.get(reverse('branch_transfer_detail_view', args=[created_transfer.id]))
        self.assertEqual(res_detail.status_code, 200)
        self.assertContains(res_detail, created_transfer.transfer_number)

        # Sevk aksiyonu (POST)
        res_ship = self.client.post(reverse('branch_transfer_ship_action', args=[created_transfer.id]))
        self.assertEqual(res_ship.status_code, 302)
        created_transfer.refresh_from_db()
        self.assertEqual(created_transfer.status, 'IN_TRANSIT')

        # CSV İndirme
        res_csv = self.client.get(reverse('export_transfers_csv'))
        self.assertEqual(res_csv.status_code, 200)
        self.assertIn('text/csv', res_csv['Content-Type'])

    def test_central_price_propagation(self):
        """Merkez mağazadan fiyat güncellendiğinde bağlı alt şubelere otomatik yayılmalıdır."""
        # Şubede de aynı ürün tanımlı olsun
        branch_prod = Product.objects.create(
            company=self.branch_company,
            barcode=self.prod_hq.barcode,
            name="Organik Süt 1L",
            buy_price=Decimal("20.00"),
            sell_price=Decimal("30.00"),
            vat_rate=Decimal("1.00"),
            stock_quantity=Decimal("5.000"),
            unit="Adet",
            is_active=True
        )

        self.client.force_login(self.hq_user)

        # 1. API ile Fiyat Yayılımı
        self.prod_hq.sell_price = Decimal("35.00")
        self.prod_hq.save()

        propagate_url = reverse('product_propagate_price_api', args=[self.prod_hq.id])
        res_prop = self.client.post(propagate_url)
        self.assertEqual(res_prop.status_code, 200)
        data = res_prop.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['updated_count'], 1)

        branch_prod.refresh_from_db()
        self.assertEqual(branch_prod.sell_price, Decimal("35.00"))

        # 2. Hızlı Satır İçi Fiyat Güncellemede Şubelere Yayılım
        quick_url = reverse('product_quick_price_update_api', args=[self.prod_hq.id])
        res_quick = self.client.post(
            quick_url,
            data=json.dumps({'sell_price': '38.50', 'propagate_to_branches': True}),
            content_type='application/json'
        )
        self.assertEqual(res_quick.status_code, 200)
        branch_prod.refresh_from_db()
        self.assertEqual(branch_prod.sell_price, Decimal("38.50"))

    def test_okc_sale_trigger_api(self):
        """Yeni Nesil ÖKC API'si satış için geçerli mali onay kodu, ETTN ve QR kod üretmelidir."""
        sale = Sale.objects.create(
            company=self.hq_company,
            shift=self.shift,
            cashier=self.hq_user,
            receipt_number="OKC-TEST-001",
            payment_type="KART",
            total_amount=Decimal("125.50"),
            is_cancelled=False
        )

        url = reverse('okc_sale_trigger_api')
        res = self.client.post(
            url,
            data=json.dumps({
                'sale_id': sale.id,
                'device_brand': 'BEKO_300TR',
                'slip_number': '123456'
            }),
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['fiscal_code'].startswith('FP-'))
        self.assertIsNotNone(data['ettn'])
        self.assertIn('VKN:', data['qr_code_data'])
        self.assertIn('TUTAR:125.50', data['qr_code_data'])

        sale.refresh_from_db()
        self.assertEqual(sale.fiscal_approval_code, data['fiscal_code'])
        self.assertEqual(str(sale.ettn), data['ettn'])
        self.assertEqual(sale.qr_code_data, data['qr_code_data'])


class MobilePosAndProductVisibilityTestSuite(TestCase):
    """
    Mobil Kasa Satış (POS) ve Patron Kokpiti Ürün Listeleme & Ergonomi Testleri.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Mobil Test Market Ltd", code="mobil-market")
        self.user = User.objects.create_user(
            username="mobil_kasiyer",
            password="pospassword123",
            is_staff=True
        )
        UserProfile.objects.create(user=self.user, company=self.company)

        self.category = Category.objects.create(
            company=self.company,
            name="Temel Gıda"
        )
        self.product = Product.objects.create(
            company=self.company,
            name="Günlük Süt 1L",
            barcode="869000000001",
            sell_price=Decimal("25.00"),
            buy_price=Decimal("18.00"),
            stock_quantity=Decimal("5.00"),
            min_stock_level=Decimal("10.00"),
            category=self.category,
            is_active=True,
            is_quick_button=True
        )

        self.register = CashRegister.objects.create(
            company=self.company,
            register_code="KASA-MOBIL",
            name="Mobil Satış Terminali",
            is_active=True
        )
        self.client.login(username="mobil_kasiyer", password="pospassword123")

    def test_pos_screen_renders_mobile_navigation_and_product_catalog(self):
        """POS ekranı mobil sekme çubuğunu, yüzen sepet çubuğunu ve hızlı ürünleri eksiksiz render etmelidir."""
        res = self.client.get(reverse('pos_screen'))
        self.assertEqual(res.status_code, 200)
        content = res.content.decode('utf-8')

        # Mobil Sekme Barı ve Düğmeleri
        self.assertIn('pos-mobile-nav', content)
        self.assertIn('id="btnTabCatalog"', content)
        self.assertIn('id="btnTabReceipt"', content)
        self.assertIn('id="btnTabOps"', content)

        # Mobil Temiz Arayüz: Açılır Kategori Seçici Barı (Arama ve gereksiz kalabalık gizli)
        self.assertIn('pos-mobile-cat-selector', content)
        self.assertIn('id="posMobileCatBtn"', content)

        # Sepet Altı Mobil Hızlı Ödeme Butonları
        self.assertIn('pos-mobile-receipt-actions', content)

        # Ürünün katalogda görünürlüğü
        self.assertIn('Günlük Süt 1L', content)
        self.assertIn('869000000001', content)

        # Tuş & Ödeme Sekmesi Temizliği: Gereksiz Butonlar Kaldırıldı
        self.assertNotIn('id="btnEditQty"', content)
        self.assertNotIn('id="btnRemoveSelected"', content)

        # Modallarda Otomatik Klavye Açılması Önleme ve Doğru Numpad Inputmode
        self.assertIn('data-autofocus="true"', content)
        self.assertIn('inputmode="decimal"', content)
        self.assertIn('inputmode="numeric"', content)

    def test_boss_dashboard_renders_top_selling_and_critical_products(self):
        """Mobil Patron Kokpiti kritik stoktaki ve en çok satan ürünleri liste halinde sunmalıdır."""
        res = self.client.get(reverse('mobile_boss_dashboard_view'))
        self.assertEqual(res.status_code, 200)
        content = res.content.decode('utf-8')

        # Kritik Stok Bölümü
        self.assertIn('Kritik Stok Alarmı Veren Ürünler', content)
        self.assertIn('Günlük Süt 1L', content)
        self.assertIn('En Çok Satan Ürünler', content)

    def test_scale_barcode_27_weight_embedded_with_flexible_plu(self):
        """27 ile başlayan EAN-13 terazi barkodları, ürün PLU'su sıfırsız ('101') dahi girilmiş olsa gramajı (1.425 KG) çözmelidir."""
        p_elma = Product.objects.create(
            company=self.company,
            barcode='101',
            name='Amasya Elması',
            sell_price=Decimal('40.00'),
            buy_price=Decimal('25.00'),
            unit='KG',
            is_weighted=True,
            stock_quantity=Decimal('50.000'),
            is_active=True
        )

        res = self.client.get(reverse('search_barcode') + '?barcode=2700101014256')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get('success'))
        self.assertTrue(data.get('is_weighted_barcode'))
        self.assertEqual(data.get('scale_prefix'), '27')
        self.assertEqual(data.get('scale_plu'), '101')
        self.assertEqual(data.get('quantity'), 1.425)
        self.assertEqual(data.get('product', {}).get('name'), 'Amasya Elması')

    def test_scale_barcode_28_price_embedded(self):
        """28 ile başlayan fiyat gömülü terazi barkodları etiketteki toplam tutara göre gramajı doğru hesaplamalıdır."""
        p_kasar = Product.objects.create(
            company=self.company,
            barcode='2700205000000',
            name='Taze Kaşar',
            sell_price=Decimal('200.00'),
            buy_price=Decimal('150.00'),
            unit='KG',
            is_weighted=True,
            stock_quantity=Decimal('20.000'),
            is_active=True
        )

        # 28 + 00205 + 05000 (50.00 TL) + 7
        res = self.client.get(reverse('search_barcode') + '?barcode=2800205050007')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get('success'))
        self.assertTrue(data.get('is_weighted_barcode'))
        self.assertEqual(data.get('scale_prefix'), '28')
        self.assertEqual(data.get('quantity'), 0.25)
        self.assertEqual(data.get('product', {}).get('name'), 'Taze Kaşar')

    def test_pos_screen_renders_mobile_camera_button_and_stock_badges(self):
        """Kasa ekranı mobil kamera butonunu ve eksi stok / tartılı rozet markup'larını içermelidir."""
        res = self.client.get(reverse('pos_screen'))
        self.assertEqual(res.status_code, 200)
        content = res.content.decode('utf-8')

        self.assertIn('id="btnMobileCameraScan"', content)
        self.assertIn('Eksi Stok:', content)
        self.assertIn('Tartılı', content)


class UserManagementRBACTests(TestCase):
    """
    Faz 19: Süper Admin & Şube Yöneticisi İzolasyonu ve RBAC Güvenlik Testleri
    """
    def setUp(self):
        self.client = Client()
        self.merkez_company = Company.objects.create(name='Merkez Market', code='merkez-test', is_active=True)
        self.diger_company = Company.objects.create(name='Güneş Market', code='gunes-test', is_active=True)

        # 1. Süper Admin
        self.super_admin = User.objects.create_superuser(username='super_boss', password='password123', email='boss@example.com')

        # 2. Şube Yöneticisi (Merkez Market)
        self.branch_manager = User.objects.create_user(username='sube_muduru', password='password123', is_staff=True, is_superuser=False)
        UserProfile.objects.create(user=self.branch_manager, company=self.merkez_company)

        # 3. Kendi Kasiyeri (Merkez Market)
        self.branch_cashier = User.objects.create_user(username='sube_kasiyeri', password='password123', is_staff=False, is_superuser=False)
        UserProfile.objects.create(user=self.branch_cashier, company=self.merkez_company)

        # 4. Başka Şirket Kasiyeri (Güneş Market)
        self.other_cashier = User.objects.create_user(username='diger_kasiyer', password='password123', is_staff=False, is_superuser=False)
        UserProfile.objects.create(user=self.other_cashier, company=self.diger_company)

    def test_branch_manager_cannot_see_super_admin_or_other_company_users(self):
        """Şube yöneticisi kullanıcı yönetiminde Süper Admin'i veya başka şirketin personelini göremez."""
        self.client.login(username='sube_muduru', password='password123')
        res = self.client.get(reverse('user_management_view'))
        self.assertEqual(res.status_code, 200)
        staff_users = res.context['staff_users']

        usernames = [u.username for u in staff_users]
        self.assertIn('sube_muduru', usernames)
        self.assertIn('sube_kasiyeri', usernames)
        # Süper Admin ve Başka Şirket personeli ASLA listede olmamalı:
        self.assertNotIn('super_boss', usernames)
        self.assertNotIn('diger_kasiyer', usernames)

    def test_super_admin_sees_all_users(self):
        """Süper Admin kullanıcı yönetiminde tüm kullanıcıları görebilir."""
        self.client.login(username='super_boss', password='password123')
        res = self.client.get(reverse('user_management_view'))
        self.assertEqual(res.status_code, 200)
        usernames = [u.username for u in res.context['staff_users']]
        self.assertIn('super_boss', usernames)
        self.assertIn('sube_muduru', usernames)
        self.assertIn('diger_kasiyer', usernames)

    def test_super_admin_hierarchical_company_filter(self):
        """Süper Admin bir firmanın içine girdiğinde (?company_id=X) sadece o firmanın personellerini görür."""
        self.client.login(username='super_boss', password='password123')
        
        # 1. Merkez Market personelleri içine gir
        res_merkez = self.client.get(reverse('user_management_view'), {'company_id': self.merkez_company.id})
        self.assertEqual(res_merkez.status_code, 200)
        self.assertEqual(res_merkez.context['view_mode'], 'company_detail')
        merkez_usernames = [u.username for u in res_merkez.context['staff_users']]
        self.assertIn('sube_muduru', merkez_usernames)
        self.assertIn('sube_kasiyeri', merkez_usernames)
        self.assertNotIn('diger_kasiyer', merkez_usernames) # Güneş Market kasiyeri listede olmamalı!

        # 2. Güneş Market personelleri içine gir
        res_diger = self.client.get(reverse('user_management_view'), {'company_id': self.diger_company.id})
        self.assertEqual(res_diger.status_code, 200)
        self.assertEqual(res_diger.context['view_mode'], 'company_detail')
        diger_usernames = [u.username for u in res_diger.context['staff_users']]
        self.assertIn('diger_kasiyer', diger_usernames)
        self.assertNotIn('sube_kasiyeri', diger_usernames) # Merkez Market kasiyeri listede olmamalı!

    def test_branch_manager_cannot_change_super_admin_role(self):
        """Şube yöneticisi Süper Admin'in rolünü değiştiremez (engellenir)."""
        self.client.login(username='sube_muduru', password='password123')
        res = self.client.post(reverse('user_change_role_action', args=[self.super_admin.id]), {'role': 'KASIYER'}, follow=True)
        # Süper admin rolü değişmemelidir:
        self.super_admin.refresh_from_db()
        self.assertTrue(self.super_admin.is_superuser)
        self.assertTrue(self.super_admin.is_staff)

    def test_branch_manager_cannot_toggle_super_admin_status(self):
        """Şube yöneticisi Süper Admin'i pasife alamaz / donduramaz."""
        self.client.login(username='sube_muduru', password='password123')
        res = self.client.get(reverse('user_toggle_status_action', args=[self.super_admin.id]), follow=True)
        self.super_admin.refresh_from_db()
        self.assertTrue(self.super_admin.is_active)

    def test_branch_manager_cannot_modify_other_company_user(self):
        """Şube yöneticisi başka şirketin personelini donduramaz veya rolünü değiştiremez."""
        self.client.login(username='sube_muduru', password='password123')
        res = self.client.post(reverse('user_change_role_action', args=[self.other_cashier.id]), {'role': 'YONETICI'}, follow=True)
        self.other_cashier.refresh_from_db()
        self.assertFalse(self.other_cashier.is_staff)

    def test_branch_manager_cannot_access_super_admin_dashboard(self):
        """Şube yöneticisi /super-admin/ sayfasına erişemez (403 Forbidden)."""
        self.client.login(username='sube_muduru', password='password123')
        res = self.client.get(reverse('super_admin_dashboard'))
        self.assertEqual(res.status_code, 403)


class SmartTvDevicePairingAndPinScopeTests(TestCase):
    """
    Faz 19: Akıllı TV / Cihaz Kodu Modeli (RFC 8628) Terminal Mühürleme & Mağaza İzolasyonlu PIN Testleri
    """
    def setUp(self):
        self.company_a = Company.objects.create(name="Güneş Market", code="gunes-market", is_active=True)
        self.company_b = Company.objects.create(name="Yıldız Bakkal", code="yildiz-bakkal", is_active=True)

        self.manager_a = User.objects.create_user(
            username='patron_gunes', password='password123', is_staff=True, is_active=True
        )
        UserProfile.objects.create(user=self.manager_a, company=self.company_a)

        # İki farklı şirkette aynı PIN (5555) kullanan iki kasiyer
        self.cashier_a = User.objects.create_user(
            username='kasiyer_gunes', password='password123', first_name='Ahmet', is_staff=False, is_active=True
        )
        UserProfile.objects.create(user=self.cashier_a, company=self.company_a, pin_code='5555')

        self.cashier_b = User.objects.create_user(
            username='kasiyer_yildiz', password='password123', first_name='Mehmet', is_staff=False, is_active=True
        )
        UserProfile.objects.create(user=self.cashier_b, company=self.company_b, pin_code='5555')

        self.register_a1 = CashRegister.objects.create(
            company=self.company_a, name="Kasa 1", register_code="KASA-01", is_active=True
        )
        self.register_a2 = CashRegister.objects.create(
            company=self.company_a, name="Kasa 2", register_code="KASA-02", is_active=True
        )

    def test_device_pairing_request_code_and_poll(self):
        """Kasadaki cihaz 6 haneli eşleme kodu alabilmeli ve polling yapabilmelidir."""
        res = self.client.get(reverse('terminal_device_request_code_api'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['ok'])
        self.assertEqual(len(data['pairing_code']), 6)
        session_id = data['session_id']

        # Polling: İlk başta PENDING olmalı
        poll_res = self.client.get(reverse('terminal_device_pairing_status_api'), {'session_id': session_id})
        self.assertEqual(poll_res.status_code, 200)
        self.assertEqual(poll_res.json()['status'], 'PENDING')

    def test_manager_approves_pairing_and_seals_device(self):
        """Patron 6 haneli kodu girerek kasayı mühürleyebilmeli ve cihaz anında token almalıdır."""
        # 1. Kasa cihazı kod ister
        res = self.client.get(reverse('terminal_device_request_code_api'))
        data = res.json()
        code = data['pairing_code']
        session_id = data['session_id']

        # 2. Patron telefonundan giriş yapar ve onaylar
        self.client.login(username='patron_gunes', password='password123')
        approve_res = self.client.post(
            reverse('terminal_device_approve_api'),
            data=json.dumps({
                'pairing_code': code,
                'register_id': self.register_a1.id,
                'device_name': 'Dell All-in-One Kasa PC'
            }),
            content_type='application/json'
        )
        self.assertEqual(approve_res.status_code, 200)
        self.assertTrue(approve_res.json()['ok'])

        # 3. Kasa güncellendi mi?
        self.register_a1.refresh_from_db()
        self.assertTrue(self.register_a1.is_paired)
        self.assertEqual(self.register_a1.device_name, 'Dell All-in-One Kasa PC')
        self.assertIsNotNone(self.register_a1.device_token)

        # 4. Kasa ekranı polling yaptığında APPROVED durumunu ve tokenı almalı
        self.client.logout()
        poll_res = self.client.get(reverse('terminal_device_pairing_status_api'), {'session_id': session_id})
        poll_data = poll_res.json()
        self.assertEqual(poll_data['status'], 'APPROVED')
        self.assertEqual(poll_data['device_token'], self.register_a1.device_token)
        self.assertEqual(poll_data['register_code'], 'KASA-01')

    def test_hardware_replacement_scenario(self):
        """Donanım Değişimi: Yeni ekran geldiğinde patron Kasa 1'e yeni ekranı atayabilmeli, eski mührü geçersiz kılmalıdır."""
        # 1. Kasa 1 ilk önce Eski Cihaz'a mühürlenir
        self.register_a1.device_token = "trm_old_device_token"
        self.register_a1.device_name = "Eski Kırık Tablet"
        self.register_a1.save()

        # 2. Yeni cihaz açılır ve yeni kod alır
        res = self.client.get(reverse('terminal_device_request_code_api'))
        new_code = res.json()['pairing_code']

        # 3. Patron yeni kodu Kasa 1 için onaylar
        self.client.login(username='patron_gunes', password='password123')
        approve_res = self.client.post(
            reverse('terminal_device_approve_api'),
            data=json.dumps({
                'pairing_code': new_code,
                'register_id': self.register_a1.id,
                'device_name': 'Yeni Dokunmatik Ekran'
            }),
            content_type='application/json'
        )
        self.assertEqual(approve_res.status_code, 200)

        # 4. Kasa 1 artık yeni cihaza mühürlüdür
        self.register_a1.refresh_from_db()
        self.assertEqual(self.register_a1.device_name, 'Yeni Dokunmatik Ekran')
        self.assertNotEqual(self.register_a1.device_token, "trm_old_device_token")

    def test_pin_login_isolated_to_paired_company(self):
        """PIN İzolasyonu: Güneş Market kasasında 5555 girildiğinde ASLA Yıldız Bakkal kasiyeri giriş yapamaz!"""
        # Kasa Güneş Market'e mühürlüdür
        self.register_a1.device_token = "trm_gunes_kasa_token"
        self.register_a1.save()

        self.client.cookies['himarket_device_token'] = 'trm_gunes_kasa_token'

        # Kasiyer sadece PIN (5555) ile giriş yapar
        res = self.client.post(reverse('login_view'), {
            'login_mode': 'pin',
            'pin_code': '5555'
        })
        self.assertEqual(res.status_code, 302)

        # Giriş yapan kişi Güneş Market'in Ahmet'i olmalıdır, Yıldız Bakkal'ın Mehmet'i DEĞİL!
        res_pos = self.client.get(reverse('pos_screen'))
        self.assertEqual(res_pos.context['shift'].cashier, self.cashier_a)
        self.assertEqual(res_pos.context['company'], self.company_a)

    def test_duplicate_pin_rejected_in_same_company(self):
        """Aynı şirkette iki personele aynı PIN verilmesi engellenir."""
        self.client.login(username='patron_gunes', password='password123')

        # Yeni kasiyere Ahmet'in PIN'i (5555) verilmeye çalışıldığında hata verir
        res = self.client.post(reverse('user_create_action'), {
            'username': 'yeni_kasiyer',
            'full_name': 'Yeni Personel',
            'password': '5555',
            'role': 'KASIYER'
        }, follow=True)
        self.assertFalse(User.objects.filter(username='yeni_kasiyer').exists())
        self.assertContains(res, 'zaten')

    def test_login_page_renders_clean_numpad_without_cashier_list(self):
        """Giriş sayfasında gereksiz kasiyer butonları veya terminal seçim radyo butonları yer almaz."""
        res = self.client.get(reverse('login_view'))
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, 'KASİYER SEÇİMİ:')
        self.assertNotContains(res, 'reg_selector')
        self.assertContains(res, 'pinCodeInput')

    def test_cashier_pin_must_be_numeric_and_between_4_and_6_digits(self):
        """Kasiyer PIN kodu sadece rakamlardan (4-6 hane) oluşmalıdır, harf girilemez."""
        self.client.login(username='patron_gunes', password='password123')

        # Harf içeren PIN ile kasiyer ekleme denemesi
        res1 = self.client.post(reverse('user_create_action'), {
            'username': 'harfli_kasiyer',
            'full_name': 'Harfli Personel',
            'password': 'abcd',
            'role': 'KASIYER'
        }, follow=True)
        self.assertFalse(User.objects.filter(username='harfli_kasiyer').exists())
        self.assertContains(res1, 'sadece rakam')

        # 3 haneli eksik PIN ile kasiyer ekleme denemesi
        res2 = self.client.post(reverse('user_create_action'), {
            'username': 'kisa_kasiyer',
            'full_name': 'Kısa Personel',
            'password': '123',
            'role': 'KASIYER'
        }, follow=True)
        self.assertFalse(User.objects.filter(username='kisa_kasiyer').exists())
        self.assertContains(res2, '4 ile 6 hane')

        # Kasiyerin şifresini harfle değiştirme denemesi
        res3 = self.client.post(reverse('user_reset_password_action', args=[self.cashier_a.id]), {
            'new_password': 'harf_pin'
        }, follow=True)
        self.assertContains(res3, 'sadece rakam')

    def test_manager_cannot_login_via_pin_pad(self):
        """Yöneticilerin PIN pad üzerinden kasadan giriş yapması güvenlik gereği engellenir."""
        # patron_gunes yöneticisine 9999 şifresi verelim
        self.manager_a.set_password('9999')
        self.manager_a.save()

        # patron_gunes PIN Pad üzerinden 9999 girmeyi dener
        res = self.client.post(reverse('login_view'), {
            'login_mode': 'pin',
            'pin_code': '9999'
        })
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Yöneticiler PIN ile giriş yapamaz")
        # Oturum açılmamış olmalı
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_manager_and_super_admin_redirect_after_standard_login(self):
        """Yönetici sekmesinden giriş yapan şube müdürü admin paneline, süper admin ise süper admin paneline yönlendirilir."""
        # 1. Şube Müdürü Girişi
        res_mgr = self.client.post(reverse('login_view'), {
            'login_mode': 'standard',
            'username': 'patron_gunes',
            'password': 'password123'
        })
        self.assertRedirects(res_mgr, reverse('admin_dashboard'))

        self.client.logout()

        # 2. Süper Admin Girişi
        super_admin = User.objects.create_superuser('super_patron', 'super@hi.com', 'SuperPass123!')
        res_super = self.client.post(reverse('login_view'), {
            'login_mode': 'standard',
            'username': 'super_patron',
            'password': 'SuperPass123!'
        })
        self.assertRedirects(res_super, reverse('super_admin_dashboard'))

    def test_user_edit_action_updates_username_fullname_and_credentials(self):
        """Yönetici bir personelin kullanıcı adını, adını soyadını ve şifresini/PIN'ini güncelleyebilir."""
        self.client.force_login(self.manager_a)

        # Yeni kasiyer oluştur
        staff = User.objects.create_user(username='eski_kullanici', password='1111', first_name='Eski', last_name='Ad')
        UserProfile.objects.create(user=staff, company=self.company_a, pin_code='1111', plain_password='1111')

        # 1. Kullanıcı adını ve PIN'ini güncelle
        res = self.client.post(reverse('user_edit_action', kwargs={'user_id': staff.id}), data={
            'username': 'yeni_kullanici',
            'full_name': 'Yeni İsim',
            'role': 'KASIYER',
            'new_password': '2222'
        })
        self.assertRedirects(res, reverse('user_management_view'))

        staff.refresh_from_db()
        self.assertEqual(staff.username, 'yeni_kullanici')
        self.assertEqual(staff.first_name, 'Yeni')
        self.assertEqual(staff.last_name, 'İsim')
        self.assertEqual(staff.profile.pin_code, '2222')
        self.assertEqual(staff.profile.plain_password, '2222')

        # 2. Kasiyeri Yöneticiye dönüştür (PIN temizlenmeli, yönetici şifresi atanmalı)
        res2 = self.client.post(reverse('user_edit_action', kwargs={'user_id': staff.id}), data={
            'username': 'yeni_kullanici',
            'full_name': 'Yeni Yönetici',
            'role': 'YONETICI',
            'new_password': 'GucluYoneticiSifresi2026!'
        })
        self.assertRedirects(res2, reverse('user_management_view'))

        staff.refresh_from_db()
        self.assertTrue(staff.is_staff)
        self.assertEqual(staff.profile.pin_code, '')  # Yöneticinin PIN'i kesinlikle silinmeli!
        self.assertEqual(staff.profile.plain_password, 'GucluYoneticiSifresi2026!')
        self.assertTrue(staff.check_password('GucluYoneticiSifresi2026!'))


























