import uuid
import datetime
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from decimal import Decimal


class Company(models.Model):
    """
    Faz 11: Çoklu Mağaza (Multi-Tenancy) Kök Modeli
    Her market bağımsız bir Company kaydıdır. Tüm ürünler, satışlar, kasalar ve
    müşteriler bu mağazaya bağlıdır.
    """
    name = models.CharField(max_length=200, verbose_name="Market / Şirket Adı")
    code = models.SlugField(max_length=60, unique=True, verbose_name="Mağaza Kodu")
    phone = models.CharField(max_length=30, blank=True, default='', verbose_name="Telefon")
    email = models.EmailField(blank=True, default='', verbose_name="E-posta")
    address = models.TextField(blank=True, default='', verbose_name="Adres")
    tax_number = models.CharField(max_length=50, blank=True, default='', verbose_name="Vergi No / TCKN")
    tax_office = models.CharField(max_length=100, blank=True, default='', verbose_name="Vergi Dairesi")
    receipt_header = models.CharField(max_length=255, blank=True, default="Hİ MARKET", verbose_name="Fiş Üstü Başlığı")
    receipt_footer = models.CharField(max_length=255, blank=True, default="Bizi tercih ettiğiniz için teşekkür ederiz.", verbose_name="Fiş Altı Notu")
    
    BUSINESS_TYPE_CHOICES = [
        ('MARKET', 'Süpermarket / Bakkal / Şarküteri / Manav'),
        ('APPAREL', 'Giyim / Butik / Ayakkabı / Tekstil'),
        ('RETAIL', 'Genel Perakende / Kırtasiye / Kozmetik / Züccaciye'),
    ]
    business_type = models.CharField(
        max_length=20,
        choices=BUSINESS_TYPE_CHOICES,
        default='MARKET',
        verbose_name="İşletme Sektör Tipi"
    )

    # Faz 18: Çoklu Şube & Depo Hiyerarşisi
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='branches',
        verbose_name="Bağlı Olduğu Merkez / Ana Şirket"
    )
    is_warehouse = models.BooleanField(
        default=False,
        verbose_name="Depo mu? (Merkez / Lojistik Deposu)"
    )

    is_active = models.BooleanField(default=True, verbose_name="Aktif Mağaza")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Oluşturulma Tarihi")

    class Meta:
        verbose_name = "Mağaza / Şirket"
        verbose_name_plural = "Mağazalar / Şirketler"
        ordering = ['name']

    def __str__(self):
        return f"{self.name} [{self.code}]"

    @classmethod
    def get_default_company(cls):
        comp = cls.objects.filter(is_active=True).first()
        if not comp:
            comp = cls.objects.create(name="Merkez Mağaza", code="default", receipt_header="Hİ MARKET")
        return comp


class UserProfile(models.Model):
    """
    Kullanıcı ile Mağaza eşleştirmesi
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile', verbose_name="Kullanıcı")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name='users', verbose_name="Bağlı Olduğu Mağaza")
    pin_code = models.CharField(max_length=10, blank=True, default='', verbose_name="Hızlı PIN Kodu")
    plain_password = models.CharField(max_length=128, blank=True, default='', verbose_name="Görüntülenebilir Şifre / PIN")

    class Meta:
        verbose_name = "Kullanıcı Profili"
        verbose_name_plural = "Kullanıcı Profilleri"

    def __str__(self):
        comp_name = self.company.name if self.company else "Genel Yönetici"
        return f"{self.user.username} ({comp_name})"


class Subscription(models.Model):
    """
    Faz 12: Kasa Başına Lisanslama ve Mağaza Abonelik Modeli
    """
    PLAN_CHOICES = [
        ('BASIC', 'Tek Kasa Başlangıç Paketi (1 Kasa)'),
        ('PRO', 'Çift Kasa Standart Paket (2 Kasa)'),
        ('GROWTH', 'Çoklu Kasa Büyüme Paketi (5 Kasa)'),
        ('ENTERPRISE', 'Kurumsal Zincir Paket (Sınırsız)'),
    ]

    STATUS_CHOICES = [
        ('TRIAL', 'Deneme Süresi (14 Gün)'),
        ('ACTIVE', 'Aktif Lisans'),
        ('SUSPENDED', 'Askıya Alındı (Ödeme Bekleniyor)'),
        ('EXPIRED', 'Süresi Doldu'),
    ]

    company = models.OneToOneField(
        Company,
        on_delete=models.CASCADE,
        related_name='subscription',
        verbose_name="Mağaza"
    )
    plan_type = models.CharField(
        max_length=20,
        choices=PLAN_CHOICES,
        default='PRO',
        verbose_name="Abonelik Paketi"
    )
    max_cash_registers = models.PositiveIntegerField(
        default=2,
        verbose_name="Azami Aktif Kasa Sayısı"
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='ACTIVE',
        verbose_name="Abonelik Durumu"
    )
    starts_at = models.DateTimeField(
        default=timezone.now,
        verbose_name="Başlangıç Tarihi"
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Lisans Bitiş Tarihi"
    )
    notes = models.TextField(
        blank=True,
        default='',
        verbose_name="Lisans / Sözleşme Notları"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Mağaza Aboneliği / Lisansı"
        verbose_name_plural = "Mağaza Abonelikleri / Lisansları"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.company.name} - {self.get_plan_type_display()} ({self.get_status_display()})"

    @property
    def is_valid(self):
        """Aboneliğin geçerli olup olmadığını kontrol eder."""
        if not self.company.is_active:
            return False
        if self.status not in ('TRIAL', 'ACTIVE'):
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        return True

    @property
    def days_remaining(self):
        """Kalan gün sayısı"""
        if not self.expires_at:
            return 9999
        diff = (self.expires_at - timezone.now()).days
        return max(0, diff)

    @classmethod
    def get_or_create_for_company(cls, company):
        """Mağazaya ait abonelik kaydı yoksa varsayılan aktif 2 kasalık lisans oluşturur."""
        if not company:
            return None
        sub = cls.objects.filter(company=company).first()
        if not sub:
            sub = cls.objects.create(
                company=company,
                plan_type='PRO',
                max_cash_registers=2,
                status='ACTIVE',
                starts_at=timezone.now(),
                expires_at=timezone.now() + datetime.timedelta(days=365),
                notes='Otomatik oluşturulan başlangıç lisansı'
            )
        return sub


def get_user_company(user):
    """
    Kullanıcının bağlı olduğu şirketi döner.
    """
    if not user or not user.is_authenticated:
        return Company.get_default_company()
    profile = getattr(user, 'profile', None)
    if profile and profile.company:
        return profile.company
    default_comp = Company.get_default_company()
    if profile:
        profile.company = default_comp
        profile.save(update_fields=['company'])
    else:
        UserProfile.objects.create(user=user, company=default_comp)
    return default_comp


class Category(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="categories", verbose_name="Mağaza")
    name = models.CharField(max_length=100, verbose_name="Kategori Adı")
    description = models.TextField(blank=True, null=True, verbose_name="Açıklama")
    display_order = models.PositiveIntegerField(default=0, verbose_name="Sıralama")
    is_active = models.BooleanField(default=True, verbose_name="Aktif mi?")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Kategori"
        verbose_name_plural = "Kategoriler"
        ordering = ['display_order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['company', 'name'], name='unique_company_category_name')
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.company_id:
            self.company = Company.get_default_company()
        super().save(*args, **kwargs)



class GlobalProductPool(models.Model):
    """
    Faz 15: Ortak Hazır Ürün Havuzu (Global Master Barcode Catalog)
    Tüm market ve kiracıların ortak faydalandığı Türkiye genelindeki 
    standart FMCG (hızlı tüketim ürünleri) barkod, ad, marka, kategori ve KDV havuzu.
    """
    barcode = models.CharField(max_length=50, unique=True, db_index=True, verbose_name="Barkod (EAN-13 / GS1)")
    name = models.CharField(max_length=255, db_index=True, verbose_name="Ürün Adı")
    brand = models.CharField(max_length=100, blank=True, default='', db_index=True, verbose_name="Marka (Ülker, Eti, Sütaş vb.)")
    category_name = models.CharField(max_length=100, blank=True, default='Genel Gıda', verbose_name="Önerilen Kategori")
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('1.00'), verbose_name="Yasal KDV Oranı (%)")
    recommended_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Tavsiye Edilen Satış Fiyatı (TL)")
    unit = models.CharField(max_length=20, default='Adet', verbose_name="Birim")
    is_weighted = models.BooleanField(default=False, verbose_name="Tartılı Ürün mü?")
    image_url = models.URLField(max_length=500, blank=True, default='', verbose_name="Ürün Görsel URL")
    created_at = models.DateTimeField(auto_now_add=True)
    is_verified = models.BooleanField(default=True, verbose_name="Onaylı Global Veri")

    class Meta:
        verbose_name = "Global Ürün Havuzu"
        verbose_name_plural = "Global Ürün Havuzu"
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.barcode})"


class Product(models.Model):
    UNIT_CHOICES = [
        ('Adet', 'Adet'),
        ('KG', 'Kilogram (KG)'),
        ('Gram', 'Gram'),
        ('Litre', 'Litre'),
        ('Paket', 'Paket'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="products", verbose_name="Mağaza")
    barcode = models.CharField(max_length=50, db_index=True, verbose_name="Barkod (EAN-13 vb.)")
    secondary_barcode = models.CharField(max_length=50, blank=True, null=True, verbose_name="İkincil / Koli Barkodu")
    name = models.CharField(max_length=200, verbose_name="Ürün Adı")
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, related_name="products", verbose_name="Kategori")
    
    buy_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Alış Fiyatı (TL)")
    sell_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Satış Fiyatı (TL)")
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('20.00'), verbose_name="KDV Oranı (%)")
    
    is_weighted = models.BooleanField(default=False, verbose_name="Tartılı / Terazi Ürünü mü?")
    unit = models.CharField(max_length=20, choices=UNIT_CHOICES, default='Adet', verbose_name="Satış Birimi")
    
    stock_quantity = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'), verbose_name="Mevcut Stok Miktarı")
    min_stock_level = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('5.000'), verbose_name="Kritik Stok Uyarısı Seviyesi")
    
    is_quick_button = models.BooleanField(default=False, verbose_name="Hızlı Buton mu? (Barkodsuz Satış)")
    quick_button_order = models.PositiveIntegerField(default=0, verbose_name="Hızlı Buton Sırası")
    image = models.ImageField(upload_to='products/', blank=True, null=True, verbose_name="Ürün Görseli")
    
    supplier = models.ForeignKey('Supplier', on_delete=models.SET_NULL, null=True, blank=True, related_name="products", verbose_name="Varsayılan Toptancı")
    has_variants = models.BooleanField(default=False, verbose_name="Varyantlı Ürün mü? (Beden/Renk)")
    
    # Faz 16: Son Kullanma Tarihi (SKT) & Parti (Lot) Takibi
    track_expiration = models.BooleanField(default=False, verbose_name="SKT Takibi Yapılsın mı?")
    expiration_date = models.DateField(null=True, blank=True, db_index=True, verbose_name="Son Kullanma Tarihi (SKT)")
    lot_number = models.CharField(max_length=50, blank=True, default='', verbose_name="Parti / Lot Numarası")

    is_active = models.BooleanField(default=True, verbose_name="Satışta mı?")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ürün"
        verbose_name_plural = "Ürünler"
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['company', 'barcode'], name='unique_company_product_barcode')
        ]

    def __str__(self):
        return f"{self.name} ({self.barcode})"

    def save(self, *args, **kwargs):
        if not self.company_id:
            self.company = Company.get_default_company()
        super().save(*args, **kwargs)

    @property
    def is_low_stock(self):
        return self.stock_quantity <= self.min_stock_level

    @property
    def days_to_expiration(self):
        """Son kullanma tarihine kaç gün kaldığını hesaplar. Geçmişse negatif döner."""
        if not self.expiration_date:
            return None
        today = timezone.localdate()
        return (self.expiration_date - today).days

    @property
    def expiration_status(self):
        """
        'NONE': SKT tanımlanmamış
        'EXPIRED': Tarihi geçmiş (Acil raftan çekilmeli!)
        'CRITICAL': 0-3 gün kalmış (Acil tükenmeli / Kampanya yapılmalı)
        'WARNING': 4-7 gün kalmış
        'UPCOMING': 8-30 gün kalmış
        'OK': 30 günden fazla var
        """
        days = self.days_to_expiration
        if days is None:
            return 'NONE'
        if days < 0:
            return 'EXPIRED'
        if days <= 3:
            return 'CRITICAL'
        if days <= 7:
            return 'WARNING'
        if days <= 30:
            return 'UPCOMING'
        return 'OK'

    @property
    def profit_amount(self):
        """Birim Net Kâr Tutarı (TL) = Satış Fiyatı - Alış Fiyatı"""
        return self.sell_price - self.buy_price

    @property
    def profit_percent(self):
        """Kâr Marjı Yüzdesi (%) = ((Satış - Alış) / Alış) * 100"""
        if self.buy_price and self.buy_price > Decimal('0.00'):
            return ((self.sell_price - self.buy_price) / self.buy_price) * Decimal('100.00')
        return Decimal('0.00')

    @property
    def vat_exclusive_price(self):
        """KDV Dahil Satış Fiyatından İç KDV Hariç Matrah"""
        if self.vat_rate and self.vat_rate > Decimal('0.00'):
            return self.sell_price / (Decimal('1.00') + (self.vat_rate / Decimal('100.00')))
        return self.sell_price

    @property
    def vat_amount_in_sell(self):
        """Satış Fiyatı İçerisindeki KDV Tutarı"""
        return self.sell_price - self.vat_exclusive_price


class ProductVariant(models.Model):
    """
    Faz 13: Giyim / Butik Sektörü için Beden ve Renk Varyant Modeli
    Örn: Bir gömlek ana ürünü altında (Beden: M, Renk: Mavi) alt barkodu ve stoku
    """
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name='product_variants', verbose_name="Mağaza")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants', verbose_name="Ana Ürün")
    size = models.CharField(max_length=40, blank=True, default='', verbose_name="Beden / Numara")
    color = models.CharField(max_length=40, blank=True, default='', verbose_name="Renk")
    barcode = models.CharField(max_length=50, db_index=True, verbose_name="Varyant Barkodu")
    stock_quantity = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'), verbose_name="Varyant Stok Miktarı")
    price_override = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name="Özel Satış Fiyatı (Boşsa Ana Ürün Fiyatı)")
    is_active = models.BooleanField(default=True, verbose_name="Aktif mi?")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ürün Varyantı"
        verbose_name_plural = "Ürün Varyantları"
        constraints = [
            models.UniqueConstraint(fields=['company', 'barcode'], name='unique_company_variant_barcode')
        ]

    def __str__(self):
        desc = []
        if self.size: desc.append(f"Beden: {self.size}")
        if self.color: desc.append(f"Renk: {self.color}")
        desc_str = " · ".join(desc) if desc else "Standart"
        return f"{self.product.name} ({desc_str}) [{self.barcode}]"

    def save(self, *args, **kwargs):
        if not self.company_id and self.product and self.product.company_id:
            self.company = self.product.company
        super().save(*args, **kwargs)

    @property
    def final_price(self):
        return self.price_override if (self.price_override is not None and self.price_override > Decimal('0.00')) else self.product.sell_price


class CashRegister(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="cash_registers", verbose_name="Mağaza")
    name = models.CharField(max_length=100, default="Kasa 1", verbose_name="Kasa Adı")
    register_code = models.CharField(max_length=20, default="KASA-01", verbose_name="Kasa Kodu")
    is_active = models.BooleanField(default=True, verbose_name="Aktif mi?")
    
    # Cihaz Mühürleme (Device Binding / Pairing) Alanları
    device_token = models.CharField(max_length=120, null=True, blank=True, unique=True, db_index=True, verbose_name="Cihaz Mührü (Token)")
    device_name = models.CharField(max_length=255, null=True, blank=True, verbose_name="Bağlı Donanım / Tarayıcı")
    device_ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="Son Bağlanan IP")
    device_paired_at = models.DateTimeField(null=True, blank=True, verbose_name="Mühürlenme Tarihi")
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name="Son Çevrimiçi Zamanı")
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Kasa Terminali"
        verbose_name_plural = "Kasa Terminalleri"
        constraints = [
            models.UniqueConstraint(fields=['company', 'register_code'], name='unique_company_register_code')
        ]

    def __str__(self):
        paired_str = f" [Mühürlü: {self.device_name}]" if self.device_token else " [Cihaz Bağlı Değil]"
        return f"{self.name} ({self.register_code}){paired_str}"

    @property
    def is_paired(self):
        return bool(self.device_token)

    def unbind_device(self):
        """Mevcut cihaz mührünü kaldırır ve kasayı boşaltır"""
        self.device_token = None
        self.device_name = None
        self.device_ip = None
        self.device_paired_at = None
        self.save(update_fields=['device_token', 'device_name', 'device_ip', 'device_paired_at'])

    def save(self, *args, **kwargs):
        if not self.company_id:
            self.company = Company.get_default_company()
        super().save(*args, **kwargs)


class DevicePairingSession(models.Model):
    """
    Kasa Terminali Cihaz Mühürleme Oturumu (Smart TV / Cihaz Kodu Modeli)
    """
    STATUS_CHOICES = [
        ('PENDING', 'Onay Bekliyor'),
        ('APPROVED', 'Onaylandı'),
        ('REJECTED', 'Reddedildi'),
        ('EXPIRED', 'Süresi Doldu'),
    ]

    session_id = models.CharField(max_length=64, unique=True, db_index=True, verbose_name="Oturum Kimliği")
    pairing_code = models.CharField(max_length=10, unique=True, db_index=True, verbose_name="6 Haneli Eşleme Kodu")
    device_info = models.CharField(max_length=255, blank=True, default='', verbose_name="Cihaz Tarayıcı Bilgisi")
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name="Cihaz IP")
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING', verbose_name="Durum")
    
    # Onaylandığında doldurulan alanlar:
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="pairing_sessions", verbose_name="Mağaza")
    cash_register = models.ForeignKey(CashRegister, on_delete=models.CASCADE, null=True, blank=True, related_name="pairing_sessions", verbose_name="Atanan Kasa")
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_pairings", verbose_name="Onaylayan Yönetici")
    device_token = models.CharField(max_length=120, blank=True, default='', verbose_name="Üretilen Cihaz Mührü")

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(verbose_name="Son Geçerlilik Zamanı")

    class Meta:
        verbose_name = "Cihaz Eşleme Oturumu"
        verbose_name_plural = "Cihaz Eşleme Oturumları"
        ordering = ['-created_at']

    def is_expired(self):
        from django.utils import timezone
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"{self.pairing_code} ({self.get_status_display()})"


class Shift(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="shifts", verbose_name="Mağaza")
    cash_register = models.ForeignKey(CashRegister, on_delete=models.CASCADE, related_name="shifts", verbose_name="Kasa")
    cashier = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="shifts", verbose_name="Kasiyer")
    
    opened_at = models.DateTimeField(auto_now_add=True, verbose_name="Açılış Zamanı")
    closed_at = models.DateTimeField(blank=True, null=True, verbose_name="Kapanış Zamanı")
    
    opening_float = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Açılış Devir Parası (TL)")
    total_cash_sales = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Toplam Nakit Satış")
    total_card_sales = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Toplam Kart Satışı")
    total_veresiye_sales = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Toplam Veresiye Satışı")
    total_collections = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Toplam Cari Tahsilat")
    total_sales_count = models.PositiveIntegerField(default=0, verbose_name="Toplam Fiş Sayısı")
    
    expected_cash = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Beklenen Nakit")
    actual_cash = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Sayılan Nakit")
    difference = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Kasa Farkı (Açık/Fazla)")
    
    is_closed = models.BooleanField(default=False, verbose_name="Kasa Kapandı mı? (Z Raporu)")
    notes = models.TextField(blank=True, null=True, verbose_name="Vardiya Notları")
    denomination_counts = models.JSONField(default=dict, blank=True, verbose_name="Banknot ve Madeni Para Sayım Detayı (Kör Sayım)")

    class Meta:
        verbose_name = "Kasa Vardiyası / Z Raporu"
        verbose_name_plural = "Kasa Vardiyaları / Z Raporları"
        ordering = ['-opened_at']

    def __str__(self):
        status = "KAPALI" if self.is_closed else "AÇIK"
        return f"{self.cash_register.name} - {self.opened_at.strftime('%d.%m.%Y %H:%M')} [{status}]"

    def save(self, *args, **kwargs):
        if not self.company_id:
            if self.cash_register and self.cash_register.company_id:
                self.company = self.cash_register.company
            else:
                self.company = Company.get_default_company()
        super().save(*args, **kwargs)


class Customer(models.Model):
    """
    Müşteri / Cari Hesap ve Veresiye Defteri Modeli
    """
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="customers", verbose_name="Mağaza")
    first_name = models.CharField(max_length=100, verbose_name="Müşteri Adı")
    last_name = models.CharField(max_length=100, blank=True, default='', verbose_name="Soyadı")
    phone = models.CharField(max_length=30, db_index=True, verbose_name="Telefon Numarası")
    tax_number = models.CharField(max_length=11, blank=True, default='', verbose_name="TCKN / VKN")
    company_title = models.CharField(max_length=200, blank=True, default='', verbose_name="Firma / Ticari Ünvan")
    address = models.TextField(blank=True, default='', verbose_name="Adres")
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('5000.00'), verbose_name="Veresiye / Borç Limiti (TL)")
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name="Güncel Borç Bakiyesi (TL)")
    notes = models.TextField(blank=True, default='', verbose_name="Müşteri Notları")
    is_active = models.BooleanField(default=True, verbose_name="Aktif mi?")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Müşteri / Cari Kart"
        verbose_name_plural = "Müşteriler / Veresiye Defteri"
        ordering = ['first_name', 'last_name']
        constraints = [
            models.UniqueConstraint(fields=['company', 'phone'], name='unique_company_customer_phone')
        ]

    def __str__(self):
        full_name = f"{self.first_name} {self.last_name}".strip()
        return f"{full_name} ({self.phone}) - Bakiye: ₺{self.balance}"

    def save(self, *args, **kwargs):
        if not self.company_id:
            self.company = Company.get_default_company()
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def remaining_credit(self):
        """Kalan Kullanılabilir Veresiye Limiti"""
        return max(Decimal('0.00'), self.credit_limit - self.balance)


class Sale(models.Model):
    PAYMENT_CHOICES = [
        ('NAKIT', 'Nakit'),
        ('KART', 'Banka / Kredi Kartı'),
        ('PARCALI', 'Parçalı Ödeme'),
        ('VERESIYE', 'Veresiye / Cari'),
    ]

    DOCUMENT_CHOICES = [
        ('E_ARSIV', 'e-Arşiv Fatura (VUK 507)'),
        ('E_FATURA', 'e-Fatura'),
        ('BILGI_FISI', 'Mali Değersiz Bilgi Fişi'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="sales", verbose_name="Mağaza")
    receipt_number = models.CharField(max_length=50, db_index=True, verbose_name="Fiş Numarası")
    ettn = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True, verbose_name="ETTN (Evrensel Fatura No)")
    document_type = models.CharField(max_length=20, choices=DOCUMENT_CHOICES, default='E_ARSIV', verbose_name="Belge Türü")
    
    # Müşteri Yasal Bilgileri (e-Arşiv / e-Fatura İçin Zorunlu)
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="sales", verbose_name="Müşteri / Cari")
    customer_tax_number = models.CharField(max_length=11, default='11111111111', verbose_name="Müşteri TCKN / VKN")
    customer_title = models.CharField(max_length=200, default='Nihai Tüketici', verbose_name="Müşteri Ünvanı / Adı")
    customer_tax_office = models.CharField(max_length=100, blank=True, default='', verbose_name="Vergi Dairesi")
    
    # GİB Mali Doğrulama ve Karekod (VUK 507 Standardı)
    qr_code_data = models.TextField(blank=True, null=True, verbose_name="GİB Karekod Metni / URL")
    fiscal_approval_code = models.CharField(max_length=60, blank=True, null=True, verbose_name="Mali Mühür / Doğrulama Kodu")

    shift = models.ForeignKey(Shift, on_delete=models.SET_NULL, null=True, blank=True, related_name="sales", verbose_name="Vardiya")
    cashier = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="sales", verbose_name="Kasiyer")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Satış Tarihi")
    
    # Finansal ve KDV Matrah Ayrımı (%1, %10, %20)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Ara Toplam (TL)")
    total_vat = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Toplam KDV (TL)")
    
    vat_1_base = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="%1 KDV Matrahı (TL)")
    vat_1_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="%1 KDV Tutarı (TL)")
    vat_10_base = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="%10 KDV Matrahı (TL)")
    vat_10_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="%10 KDV Tutarı (TL)")
    vat_20_base = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="%20 KDV Matrahı (TL)")
    vat_20_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="%20 KDV Tutarı (TL)")

    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="İskonto (TL)")
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Genel Toplam (TL)")
    
    payment_type = models.CharField(max_length=20, choices=PAYMENT_CHOICES, default='NAKIT', verbose_name="Ödeme Tipi")
    cash_paid = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Alınan Nakit (TL)")
    card_paid = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Alınan Kart (TL)")
    change_given = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Para Üstü (TL)")
    
    is_cancelled = models.BooleanField(default=False, verbose_name="Fiş İptal Edildi mi?")
    cancellation_reason = models.TextField(blank=True, null=True, verbose_name="İptal Gerekçesi")
    is_return = models.BooleanField(default=False, verbose_name="İade Fişi mi?")
    return_reason = models.TextField(blank=True, null=True, verbose_name="İade Gerekçesi")

    # Çevrimdışı (Offline) Kasa & Senkronizasyon (Faz 14)
    is_offline_sync = models.BooleanField(default=False, db_index=True, verbose_name="Çevrimdışı Satış mı?")
    offline_sync_id = models.CharField(max_length=100, blank=True, null=True, db_index=True, verbose_name="Çevrimdışı Fiş ID")
    offline_created_at = models.DateTimeField(blank=True, null=True, verbose_name="Çevrimdışı Kesilme Zamanı")

    class Meta:
        verbose_name = "Satış Fişi"
        verbose_name_plural = "Satış Fişleri"
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['company', 'receipt_number'], name='unique_company_receipt_number')
        ]

    def __str__(self):
        return f"Fiş #{self.receipt_number} - {self.total_amount} TL"

    def save(self, *args, **kwargs):
        if not self.company_id:
            if self.shift and self.shift.company_id:
                self.company = self.shift.company
            else:
                self.company = Company.get_default_company()
        super().save(*args, **kwargs)


class CustomerTransaction(models.Model):
    """
    Veresiye Satış, Tahsilat ve Müşteri Borç-Alacak Hareketleri
    """
    TRANSACTION_TYPES = [
        ('VERESIYE_SATIS', 'Veresiye Satış (Borç Eklendi)'),
        ('TAHSILAT_NAKIT', 'Borç Tahsilatı (Nakit)'),
        ('TAHSILAT_KART', 'Borç Tahsilatı (Kart)'),
        ('IADE', 'Müşteri Satış İadesi (Borç Düşüldü)'),
        ('DUZELTME', 'Bakiye Düzeltme'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="customer_transactions", verbose_name="Mağaza")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="transactions", verbose_name="Müşteri")
    transaction_type = models.CharField(max_length=30, choices=TRANSACTION_TYPES, verbose_name="İşlem Türü")
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="İşlem Tutarı (TL)")
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name="İşlem Sonrası Bakiye (TL)")
    sale = models.ForeignKey(Sale, on_delete=models.SET_NULL, null=True, blank=True, related_name="customer_transactions", verbose_name="İlgili Fiş")
    shift = models.ForeignKey(Shift, on_delete=models.SET_NULL, null=True, blank=True, related_name="customer_transactions", verbose_name="Kasa Vardiyası")
    document_no = models.CharField(max_length=50, blank=True, default='', verbose_name="Makbuz / Fiş No")
    description = models.TextField(blank=True, default='', verbose_name="Açıklama / Not")
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="İşlemi Yapan")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="İşlem Tarihi")

    class Meta:
        verbose_name = "Müşteri Cari Hareketi"
        verbose_name_plural = "Müşteri Cari Hareketleri"
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.get_transaction_type_display()}] {self.customer.full_name}: ₺{self.amount}"

    def save(self, *args, **kwargs):
        if not self.company_id:
            if self.customer and self.customer.company_id:
                self.company = self.customer.company
            else:
                self.company = Company.get_default_company()
        super().save(*args, **kwargs)


class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items", verbose_name="Fiş")
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, related_name="sale_items", verbose_name="Ürün")
    variant = models.ForeignKey('ProductVariant', on_delete=models.SET_NULL, null=True, blank=True, related_name="sale_items", verbose_name="Ürün Varyantı")
    product_name = models.CharField(max_length=200, verbose_name="Ürün Adı")
    variant_info = models.CharField(max_length=120, blank=True, default='', verbose_name="Varyant Bilgisi (Beden/Renk)")
    barcode = models.CharField(max_length=50, verbose_name="Barkod")
    
    quantity = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('1.000'), verbose_name="Miktar / Gramaj")
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Birim Fiyat")
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('10.00'), verbose_name="KDV Oranı (%)")
    vat_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="KDV Tutarı")
    total_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Satır Toplamı")
    
    # Faz 16: Satış anındaki birim alış maliyeti (Patron Kokpiti Net Kâr Marjı Hesabı İçin)
    buy_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Satış Anındaki Birim Maliyet (TL)")

    class Meta:
        verbose_name = "Fiş Kalemi"
        verbose_name_plural = "Fiş Kalemleri"

    def __str__(self):
        return f"{self.product_name} x {self.quantity} = {self.total_price} TL"

    @property
    def total_cost(self):
        """Satır Toplam Maliyeti"""
        return (self.buy_price * self.quantity).quantize(Decimal('0.01'))

    @property
    def profit_amount(self):
        """Satır Brüt Kâr Tutarı (TL)"""
        return self.total_price - self.total_cost


class Supplier(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="suppliers", verbose_name="Mağaza")
    company_name = models.CharField(max_length=200, verbose_name="Toptancı / Firma Adı")
    contact_name = models.CharField(max_length=100, blank=True, null=True, verbose_name="Yetkili Kişi")
    phone = models.CharField(max_length=30, blank=True, null=True, verbose_name="Telefon")
    email = models.EmailField(blank=True, null=True, verbose_name="E-Posta")
    tax_office = models.CharField(max_length=100, blank=True, null=True, verbose_name="Vergi Dairesi")
    tax_number = models.CharField(max_length=50, blank=True, null=True, verbose_name="Vergi Numarası")
    address = models.TextField(blank=True, null=True, verbose_name="Adres")
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name="Cari Bakiye (TL) [+ Borç / - Alacak]")
    notes = models.TextField(blank=True, null=True, verbose_name="Notlar")
    is_active = models.BooleanField(default=True, verbose_name="Aktif mi?")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Toptancı Carisi"
        verbose_name_plural = "Toptancı Carileri"
        ordering = ['company_name']
        constraints = [
            models.UniqueConstraint(fields=['company', 'company_name'], name='unique_company_supplier_name')
        ]

    def __str__(self):
        return self.company_name

    def save(self, *args, **kwargs):
        if not self.company_id:
            self.company = Company.get_default_company()
        super().save(*args, **kwargs)


class SupplierTransaction(models.Model):
    TRANSACTION_TYPES = [
        ('ALIS', 'Mal Alımı (Borç Eklendi)'),
        ('ODEME', 'Toptancıya Ödeme Yapıldı'),
        ('IADE', 'Mal İadesi (Alacak Eklendi)'),
        ('DUZELTME', 'Cari Bakiye Düzeltme'),
    ]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="supplier_transactions", verbose_name="Mağaza")
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="transactions", verbose_name="Toptancı")
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES, verbose_name="İşlem Tipi")
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="Tutar (TL)")
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name="İşlem Sonrası Bakiye")
    document_no = models.CharField(max_length=50, blank=True, null=True, verbose_name="Fatura / İrsaliye No")
    description = models.TextField(blank=True, null=True, verbose_name="Açıklama")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="İşlem Tarihi")
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="İşlemi Yapan")

    class Meta:
        verbose_name = "Cari Hesap Hareketi"
        verbose_name_plural = "Cari Hesap Hareketleri"
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.company_id:
            if self.supplier and self.supplier.company_id:
                self.company = self.supplier.company
            else:
                self.company = Company.get_default_company()
        super().save(*args, **kwargs)


class StockEntry(models.Model):
    PAYMENT_STATUS_CHOICES = [
        ('NAKIT', 'Nakit Ödendi'),
        ('HAVALE', 'Banka / Havale Ödendi'),
        ('KART', 'Kredi Kartı Ödendi'),
        ('VERESIYE', 'Cari Hesaba İşlendi (Veresiye)'),
    ]

    ENTRY_TYPE_CHOICES = [
        ('GIRIS', 'Mal Alımı (Stok Girişi)'),
        ('IADE', 'Toptancıya İade (Stok Çıkışı)'),
        ('FIRE_ZAYI', 'Fire / Hasar / SKT Çıkışı'),
        ('SARF_TUKETIM', 'İşletme İçi Sarf / Tüketim'),
        ('SAYIM_DUZELTME', 'Stok Sayım Düzeltmesi'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="stock_entries", verbose_name="Mağaza")
    entry_number = models.CharField(max_length=50, verbose_name="Stok Fiş No")
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPE_CHOICES, default='GIRIS', verbose_name="İşlem Türü")
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, related_name="stock_entries", verbose_name="Toptancı")
    invoice_number = models.CharField(max_length=100, blank=True, null=True, verbose_name="Fatura / İrsaliye No")
    entry_date = models.DateTimeField(default=timezone.now, verbose_name="Fatura / Giriş Tarihi")
    payment_type = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='VERESIYE', verbose_name="Ödeme Yöntemi")
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name="Toplam Fatura Tutarı (TL)")
    notes = models.TextField(blank=True, null=True, verbose_name="Notlar / Açıklama")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Kaydeden")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Stok Giriş Fişi"
        verbose_name_plural = "Stok Giriş Fişleri"
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['company', 'entry_number'], name='unique_company_entry_number')
        ]

    def __str__(self):
        supplier_name = self.supplier.company_name if self.supplier else "Genel Stok Girişi"
        return f"Giriş #{self.entry_number} - {supplier_name} ({self.total_amount} TL)"

    def save(self, *args, **kwargs):
        if not self.company_id:
            if self.supplier and self.supplier.company_id:
                self.company = self.supplier.company
            else:
                self.company = Company.get_default_company()
        super().save(*args, **kwargs)

    def __str__(self):
        supplier_name = self.supplier.company_name if self.supplier else "Genel Stok Girişi"
        return f"Giriş #{self.entry_number} - {supplier_name} ({self.total_amount} TL)"


class StockEntryItem(models.Model):
    stock_entry = models.ForeignKey(StockEntry, on_delete=models.CASCADE, related_name="items", verbose_name="Stok Giriş Fişi")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="stock_entry_items", verbose_name="Ürün")
    quantity = models.DecimalField(max_digits=10, decimal_places=3, verbose_name="Miktar")
    unit = models.CharField(max_length=20, choices=Product.UNIT_CHOICES, default='Adet', verbose_name="Birim")
    list_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Birim Fiyat / Liste Fiyatı (TL)")
    discount_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'), verbose_name="İskonto Oranı (%)")
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('20.00'), verbose_name="KDV Oranı (%)")
    buy_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Hesaplanan Alış Maliyeti (TL)")
    total_price = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="Satır Toplamı (TL)")
    
    # Faz 16: Mal Kabulünde SKT ve Parti No
    expiration_date = models.DateField(null=True, blank=True, verbose_name="Son Kullanma Tarihi (SKT)")
    lot_number = models.CharField(max_length=50, blank=True, default='', verbose_name="Parti / Lot Numarası")

    class Meta:
        verbose_name = "Stok Giriş Kalemi"
        verbose_name_plural = "Stok Giriş Kalemleri"


class StockTake(models.Model):
    """
    El Terminali & Barkodlu Stok Sayım Fişi (Faz 5.1)
    Reyon bazlı veya genel mağaza sayımı.
    """
    STATUS_CHOICES = [
        ('DRAFT', 'Sayım Devam Ediyor'),
        ('COMPLETED', 'Sayım Tamamlandı & Stoğa İşlendi'),
        ('CANCELLED', 'İptal Edildi'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="stock_takes", verbose_name="Mağaza")
    take_number = models.CharField(max_length=50, verbose_name="Sayım Fiş No")
    title = models.CharField(max_length=150, verbose_name="Sayım Başlığı / Reyon")
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Kategori Filtresi")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT', verbose_name="Durum")
    notes = models.TextField(blank=True, default='', verbose_name="Sayım Notları")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Sayan Personel")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Başlama Tarihi")
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name="Tamamlanma Tarihi")

    class Meta:
        verbose_name = "Stok Sayım Fişi"
        verbose_name_plural = "Stok Sayım Fişleri"
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['company', 'take_number'], name='unique_company_take_number')
        ]

    def __str__(self):
        return f"{self.take_number} - {self.title} [{self.get_status_display()}]"

    @property
    def total_counted_items(self):
        return self.items.count()

    @property
    def total_discrepancy_cost(self):
        return sum((item.discrepancy_cost for item in self.items.all()), Decimal('0.00'))

    def save(self, *args, **kwargs):
        if not self.company_id:
            self.company = Company.get_default_company()
        super().save(*args, **kwargs)


class StockTakeItem(models.Model):
    """
    Stok Sayım Detay Kalemi: Sistemdeki Stok vs Sayılan Fiziki Stok
    """
    stock_take = models.ForeignKey(StockTake, on_delete=models.CASCADE, related_name="items", verbose_name="Sayım Fişi")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="stock_take_items", verbose_name="Ürün")
    system_quantity = models.DecimalField(max_digits=10, decimal_places=3, verbose_name="Sistemdeki Stok")
    counted_quantity = models.DecimalField(max_digits=10, decimal_places=3, verbose_name="Sayılan Fiziki Stok")
    difference = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'), verbose_name="Fark (Sayılan - Sistem)")
    buy_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="Birim Alış Maliyeti")
    discrepancy_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name="Fark Tutarı / Maliyet")
    scanned_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Stok Sayım Kalemi"
        verbose_name_plural = "Stok Sayım Kalemleri"
        unique_together = ('stock_take', 'product')
        ordering = ['-scanned_at']

    def save(self, *args, **kwargs):
        self.difference = self.counted_quantity - self.system_quantity
        self.discrepancy_cost = round(self.difference * self.buy_price, 2)
        super().save(*args, **kwargs)


class BranchTransfer(models.Model):
    """
    Faz 18: Şubeler ve Depolar Arası Stok Transferi (Sevk İrsaliyesi)
    Merkez Depo -> Şube veya Şube -> Şube arası irsaliyeli mal sevkiyatı ve stok transferi.
    """
    STATUS_CHOICES = [
        ('DRAFT', 'Taslak (Hazırlanıyor)'),
        ('IN_TRANSIT', 'Sevk Edildi (Yolda)'),
        ('COMPLETED', 'Teslim Alındı (Tamamlandı)'),
        ('CANCELLED', 'İptal Edildi'),
    ]

    transfer_number = models.CharField(max_length=50, unique=True, verbose_name="Transfer / İrsaliye No")
    from_company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='transfers_sent', verbose_name="Çıkış Şubesi / Depo")
    to_company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='transfers_received', verbose_name="Varış Şubesi / Depo")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT', verbose_name="Transfer Durumu")
    
    notes = models.TextField(blank=True, default='', verbose_name="Transfer Notları / Sevk Açıklaması")
    
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='transfers_created', verbose_name="Oluşturan Personel")
    received_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='transfers_received_by', verbose_name="Teslim Alan Personel")
    
    shipped_at = models.DateTimeField(null=True, blank=True, verbose_name="Sevk / Yola Çıkış Tarihi")
    received_at = models.DateTimeField(null=True, blank=True, verbose_name="Teslim Alma / Varış Tarihi")
    
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Kayıt Tarihi")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Son Güncelleme")

    class Meta:
        verbose_name = "Şubeler Arası Stok Transferi"
        verbose_name_plural = "Şubeler Arası Stok Transferleri"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.transfer_number} ({self.from_company.name} -> {self.to_company.name}) [{self.get_status_display()}]"

    @property
    def total_quantity(self):
        return sum((item.quantity for item in self.items.all()), Decimal('0.000'))

    @property
    def total_items_count(self):
        return self.items.count()

    def ship(self, user=None):
        """
        Transferi yola çıkarır:
        1. Çıkış şubesindeki ürünlerin stoklarından transfer miktarlarını düşer.
        2. Durumu IN_TRANSIT yapar.
        """
        if self.status != 'DRAFT':
            raise ValueError(f"Sadece taslak transferler sevk edilebilir. Mevcut durum: {self.get_status_display()}")
        if self.items.count() == 0:
            raise ValueError("Transfer belgesinde hiç ürün bulunmamaktadır.")
        
        for item in self.items.select_related('product'):
            item.product.stock_quantity -= item.quantity
            item.product.save(update_fields=['stock_quantity', 'updated_at'])
            
        self.status = 'IN_TRANSIT'
        self.shipped_at = timezone.now()
        if user and not self.created_by:
            self.created_by = user
        self.save(update_fields=['status', 'shipped_at', 'created_by', 'updated_at'])

    def receive(self, user=None):
        """
        Transferi teslim alır:
        1. Varış şubesinde aynı barkodlu ürünü bulur veya oluşturur.
        2. Varış şubesindeki ürün stoğunu artırır.
        3. Durumu COMPLETED yapar.
        """
        if self.status != 'IN_TRANSIT':
            raise ValueError(f"Sadece yoldaki transferler teslim alınabilir. Mevcut durum: {self.get_status_display()}")
        
        for item in self.items.select_related('product', 'product__category'):
            src_prod = item.product
            dest_prod = Product.objects.filter(company=self.to_company, barcode=src_prod.barcode).first()
            if dest_prod:
                dest_prod.stock_quantity += item.quantity
                dest_prod.save(update_fields=['stock_quantity', 'updated_at'])
            else:
                dest_cat = None
                if src_prod.category:
                    dest_cat = Category.objects.filter(company=self.to_company, name=src_prod.category.name).first()
                    if not dest_cat:
                        dest_cat = Category.objects.create(
                            company=self.to_company,
                            name=src_prod.category.name,
                            description=src_prod.category.description or ''
                        )
                Product.objects.create(
                    company=self.to_company,
                    barcode=src_prod.barcode,
                    secondary_barcode=src_prod.secondary_barcode or '',
                    name=src_prod.name,
                    category=dest_cat,
                    buy_price=src_prod.buy_price,
                    sell_price=src_prod.sell_price,
                    vat_rate=src_prod.vat_rate,
                    unit=item.unit or src_prod.unit,
                    is_weighted=src_prod.is_weighted,
                    stock_quantity=item.quantity,
                    min_stock_level=src_prod.min_stock_level,
                    track_expiration=src_prod.track_expiration,
                    expiration_date=src_prod.expiration_date,
                    lot_number=src_prod.lot_number,
                    has_variants=src_prod.has_variants,
                    is_active=True
                )
        self.status = 'COMPLETED'
        self.received_at = timezone.now()
        if user:
            self.received_by = user
        self.save(update_fields=['status', 'received_at', 'received_by', 'updated_at'])

    def cancel(self, user=None):
        """
        Transferi iptal eder:
        Eğer IN_TRANSIT ise kaynak şubeden düşülen stoklar geri iade edilir.
        """
        if self.status == 'COMPLETED':
            raise ValueError("Teslim alınmış transferler iptal edilemez.")
        if self.status == 'CANCELLED':
            return
        
        if self.status == 'IN_TRANSIT':
            for item in self.items.select_related('product'):
                item.product.stock_quantity += item.quantity
                item.product.save(update_fields=['stock_quantity', 'updated_at'])
                
        self.status = 'CANCELLED'
        self.save(update_fields=['status', 'updated_at'])


class BranchTransferItem(models.Model):
    """
    Stok Transfer Detay Kalemi
    """
    transfer = models.ForeignKey(BranchTransfer, on_delete=models.CASCADE, related_name='items', verbose_name="Transfer Fişi")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='transfer_items', verbose_name="Sevk Edilen Ürün")
    quantity = models.DecimalField(max_digits=10, decimal_places=3, verbose_name="Miktar")
    unit = models.CharField(max_length=20, default='Adet', verbose_name="Birim")
    notes = models.CharField(max_length=255, blank=True, default='', verbose_name="Kalem Notu")

    class Meta:
        verbose_name = "Transfer Kalemi"
        verbose_name_plural = "Transfer Kalemleri"

    def __str__(self):
        return f"{self.product.name} ({self.quantity} {self.unit})"


class SystemLog(models.Model):
    LOG_LEVELS = [
        ('INFO', 'Bilgi'),
        ('WARNING', 'Uyarı'),
        ('ERROR', 'Hata'),
        ('SECURITY', 'Güvenlik / Denetim'),
    ]

    MODULES = [
        ('POS', 'Kasa Satış'),
        ('STOCK', 'Stok Yönetimi'),
        ('TRANSFER', 'Şube Stok Transferi'),
        ('SUPPLIER', 'Toptancı Carileri'),
        ('PRODUCT', 'Ürün Yönetimi'),
        ('AUTH', 'Kullanıcı / Oturum'),
        ('SYSTEM', 'Sistem'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="system_logs", verbose_name="Mağaza")
    level = models.CharField(max_length=10, choices=LOG_LEVELS, default='INFO', verbose_name="Seviye")
    module = models.CharField(max_length=20, choices=MODULES, default='SYSTEM', verbose_name="Modül")
    action = models.CharField(max_length=100, verbose_name="Eylem / İşlem Adı")
    message = models.TextField(verbose_name="Log Detayı / Mesaj")
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Kullanıcı")
    ip_address = models.GenericIPAddressField(blank=True, null=True, verbose_name="IP Adresi")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Tarih / Zaman")

    class Meta:
        verbose_name = "Sistem İşlem Logu"
        verbose_name_plural = "Sistem İşlem Logları"
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.created_at.strftime('%Y-%m-%d %H:%M')}] [{self.module}] {self.action}: {self.message[:50]}"


def log_action(module, action, message, level='INFO', user=None, ip_address=None, company=None):
    """
    Sistem çapında işlem loglama yardımcısı
    """
    try:
        if company is None:
            if user and user.is_authenticated:
                company = get_user_company(user)
            else:
                company = Company.get_default_company()
        SystemLog.objects.create(
            company=company,
            module=module,
            action=action,
            message=message,
            level=level,
            user=user if (user and user.is_authenticated) else None,
            ip_address=ip_address
        )
    except Exception as e:
        print(f"Log kaydetme hatası: {e}")


class CompanySetting(models.Model):
    """
    İşletme Kimlik Bilgileri, e-Arşiv Fatura Entegratörü ve Donanım Yapılandırması (Singleton Model).
    Farklı marketlere kurulum yapıldığında kod değiştirmeden yönetim panelinden yapılandırılır.
    """
    FISCAL_MODE_CHOICES = [
        ('VUK507_EARSIV', 'VUK 507 e-Arşiv Fatura (Entegratör API & GİB Karekod)'),
        ('OKC_HARDWARE', 'Yeni Nesil ÖKC (Harici Yazar Kasa Donanımı)'),
        ('LOCAL_MOCK', 'Yerel Simülasyon / Test Modu (GİB Karekodlu Bilgi Fişi)'),
    ]

    INTEGRATOR_CHOICES = [
        ('LOCAL_MOCK', 'Yerel Simülasyon (Lisans / Test Modu)'),
        ('NILVERA', 'Nilvera Bilişim (API v2)'),
        ('EDM', 'EDM Bilişim e-Dönüşüm'),
        ('PARASUT', 'Paraşüt e-Arşiv / e-Fatura API'),
        ('SOVOS', 'Sovos / Digital Planet'),
        ('UYUMSOFT', 'Uyumsoft Bilgi Sistemleri'),
        ('GIB_PORTAL', 'GİB Doğrudan e-Arşiv Portal'),
    ]

    ENVIRONMENT_CHOICES = [
        ('TEST', 'Test / Sandbox Ortamı'),
        ('PRODUCTION', 'Canlı / Resmi Üretim Ortamı'),
    ]

    PRINTER_TYPE_CHOICES = [
        ('SPOOL_AND_SOCKET', 'Ağ Yazıcısı (Raw Socket 9100) + Yerel Spooler'),
        ('SOCKET_ONLY', 'Yalnızca Ağ Yazıcısı (TCP Port 9100)'),
        ('LOCAL_SPOOLER', 'Yalnızca Yerel Dosya Spooler (Dosya Arşivi)'),
    ]

    SCALE_PROTOCOL_CHOICES = [
        ('CAS_PD2', 'CAS PD-II / Toledo Protokolü (W + CR)'),
        ('TEM', 'TEM / Dibal Terazi Protokolü'),
        ('METTLER', 'Mettler Toledo Standart Protokol'),
        ('CONTINUOUS', 'Sürekli Akış (Continuous Stream)'),
        ('LOCAL_MOCK', 'Simülasyon / Test Modu (Fiziki Donanımsız)'),
    ]

    CUSTOMER_DISPLAY_MODE_CHOICES = [
        ('DUAL_SCREEN', 'Çift Monitör (2. HDMI Ekranı / Canlı Tarayıcı Yayını)'),
        ('VFD_SERIAL', '2x20 Seri Port VFD Kutup Ekranı (RS232 / ESC Q A)'),
        ('DISABLED', 'Kapalı'),
    ]

    LABEL_PRINTER_CHOICES = [
        ('BROWSER_HTML', 'Tarayıcı Termal Baskı (HTML / CSS @page 60x40mm)'),
        ('A4_SHEET', 'Standart A4 Lazer/Mürekkep Etiket Kağıdı (24\'lü Izgara)'),
        ('TSPL_RAW', 'TSPL / TSC Masaüstü Barkod Yazıcı (Raw Byte)'),
        ('ZPL_RAW', 'Zebra ZPL Masaüstü Barkod Yazıcı (Raw Byte)'),
    ]

    company = models.OneToOneField(Company, on_delete=models.CASCADE, null=True, blank=True, related_name='settings', verbose_name="Mağaza")

    # 1. İŞLETME RESMİ VE İLETİŞİM BİLGİLERİ
    title = models.CharField(max_length=255, default='Hİ MARKET GIDA VE İHTİYAÇ MAD. TİC. LTD. ŞTİ.', verbose_name="Resmi Şirket / Mükellef Ünvanı")
    brand_name = models.CharField(max_length=100, default='Hİ MARKET', verbose_name="Tabela / Mağaza Adı")
    tax_number = models.CharField(max_length=11, default='1234567890', verbose_name="Vergi Kimlik No (VKN / TCKN)")
    tax_office = models.CharField(max_length=100, default='KADIKÖY VERGİ DAİRESİ', verbose_name="Vergi Dairesi")
    address = models.TextField(default='Bağdat Caddesi No:142/A Kadıköy / İSTANBUL', verbose_name="Resmi İşletme Adresi")
    phone = models.CharField(max_length=30, blank=True, default='0216 555 00 00', verbose_name="Telefon Numarası")
    email = models.EmailField(blank=True, default='info@himarket.com', verbose_name="E-Posta Adresi")
    mersis_no = models.CharField(max_length=50, blank=True, default='0123456789000001', verbose_name="Mersis Numarası")
    ticaret_sicil_no = models.CharField(max_length=50, blank=True, default='345678-5', verbose_name="Ticaret Sicil No")
    web_site = models.CharField(max_length=100, blank=True, default='www.himarket.com', verbose_name="Web Sitesi")

    # 2. MALİ VE E-ARŞİV FATURA YAPILANDIRMASI
    fiscal_mode = models.CharField(max_length=30, choices=FISCAL_MODE_CHOICES, default='VUK507_EARSIV', verbose_name="Mali Çalışma Modu")
    fiscal_provider = models.CharField(max_length=30, choices=INTEGRATOR_CHOICES, default='LOCAL_MOCK', verbose_name="e-Arşiv Entegratörü")
    fiscal_environment = models.CharField(max_length=20, choices=ENVIRONMENT_CHOICES, default='TEST', verbose_name="Çalışma Ortamı")
    
    api_username = models.CharField(max_length=150, blank=True, default='', verbose_name="Entegratör API Kullanıcı Adı")
    api_password = models.CharField(max_length=150, blank=True, default='', verbose_name="Entegratör API Şifresi")
    api_key = models.CharField(max_length=255, blank=True, default='', verbose_name="API Key / Entegratör Anahtarı")
    api_secret = models.CharField(max_length=255, blank=True, default='', verbose_name="API Secret / Token")

    # 3. DONANIM, YAZICI VE ÇEKMECE AYARLARI
    printer_type = models.CharField(max_length=30, choices=PRINTER_TYPE_CHOICES, default='SPOOL_AND_SOCKET', verbose_name="Yazıcı İletişim Tipi")
    printer_ip = models.CharField(max_length=50, default='127.0.0.1', verbose_name="Termal Yazıcı IP Adresi")
    printer_port = models.PositiveIntegerField(default=9100, verbose_name="Yazıcı Portu (Standart: 9100)")
    paper_width = models.PositiveIntegerField(default=80, verbose_name="Kağıt Genişliği (mm - 80 veya 58)")
    drawer_kick_on_cash = models.BooleanField(default=True, verbose_name="Nakit Satışta Para Çekmecesini Otomatik Aç (RJ11)")
    
    receipt_header_note = models.CharField(max_length=150, blank=True, default='HI MARKET PERAKENDE OTOMASYONU', verbose_name="Fiş Üst Başlık Notu")
    receipt_footer_note = models.TextField(default='Bizi tercih ettiğiniz için teşekkür ederiz.\nİyi günler dileriz!', verbose_name="Fiş Altı Özel Teşekkür Notu")

    # 4. ELEKTRONİK KASA TERAZİSİ (RS232 / USB SERİ PORT)
    scale_enabled = models.BooleanField(default=True, verbose_name="Elektronik Kasa Terazisi Aktif mi?")
    scale_port = models.CharField(max_length=50, default='AUTO', verbose_name="Terazi COM / Seri Portu (Örn: COM1, /dev/ttyUSB0, AUTO, LOCAL_MOCK)")
    scale_baudrate = models.PositiveIntegerField(default=9600, verbose_name="Terazi Baud Hızı (Örn: 9600)")
    scale_protocol = models.CharField(max_length=30, choices=SCALE_PROTOCOL_CHOICES, default='CAS_PD2', verbose_name="Terazi Haberleşme Protokolü")
    scale_mock_weight = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal('1.250'), verbose_name="Simülasyon / Test Ağırlığı (kg)")

    # 5. MÜŞTERİ FİYAT GÖSTERGESİ (CUSTOMER DISPLAY / ÇİFT MONİTÖR)
    customer_display_enabled = models.BooleanField(default=True, verbose_name="Müşteri Fiyat Ekranı Aktif mi?")
    customer_display_mode = models.CharField(max_length=30, choices=CUSTOMER_DISPLAY_MODE_CHOICES, default='DUAL_SCREEN', verbose_name="Müşteri Ekranı Modu")
    customer_display_port = models.CharField(max_length=50, default='COM2', blank=True, verbose_name="VFD Seri Portu (VFD Modu için)")
    customer_display_welcome_msg = models.CharField(max_length=150, default='Hİ MARKET - HOŞ GELDİNİZ', verbose_name="Müşteri Ekranı Karşılama Mesajı")

    # 6. RAF VE BARKOD ETİKETİ BASIM YAPILANDIRMASI
    label_printer_type = models.CharField(max_length=30, choices=LABEL_PRINTER_CHOICES, default='BROWSER_HTML', verbose_name="Etiket Basım Formatı")
    label_width_mm = models.PositiveIntegerField(default=60, verbose_name="Etiket Genişliği (mm)")
    label_height_mm = models.PositiveIntegerField(default=40, verbose_name="Etiket Yüksekliği (mm)")

    # 7. KASA GÜVENLİĞİ VE KASİYER YETKİLERİ (FAZ 4)
    blind_close_enabled = models.BooleanField(
        default=True,
        verbose_name="Kör Kasa Kapanışı Aktif mi? (Kasiyerden beklenen tutarı gizler)"
    )
    supervisor_cancel_required = models.BooleanField(
        default=False,
        verbose_name="Ürün Silme ve Fiş İptali için Yetkili Şifresi Zorunlu mu?"
    )
    supervisor_discount_limit_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('10.00'),
        verbose_name="Kasiyer Maksimum İskonto Oranı (%)"
    )

    updated_at = models.DateTimeField(auto_now=True, verbose_name="Son Güncelleme")

    class Meta:
        verbose_name = "İşletme ve Mali Ayarlar"
        verbose_name_plural = "İşletme ve Mali Ayarlar"

    def __str__(self):
        return f"{self.brand_name} - {self.title} [VKN: {self.tax_number}]"

    @classmethod
    def get_settings(cls, company=None):
        """
        Singleton / Multi-Tenant ayar güvencesi:
        Eğer company verilmezse varsayılan şirketi getirir.
        """
        if company is None:
            company = Company.get_default_company()
        setting = cls.objects.filter(company=company).first()
        if not setting:
            setting = cls.objects.filter(id=1).first()
            if setting and (setting.company is None or setting.company == company):
                setting.company = company
                setting.save(update_fields=['company'])
            else:
                setting, _ = cls.objects.get_or_create(company=company)
        return setting


