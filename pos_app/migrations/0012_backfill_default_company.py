from django.db import migrations


def backfill_company_data(apps, schema_editor):
    Company = apps.get_model('pos_app', 'Company')
    UserProfile = apps.get_model('pos_app', 'UserProfile')
    Category = apps.get_model('pos_app', 'Category')
    Product = apps.get_model('pos_app', 'Product')
    CashRegister = apps.get_model('pos_app', 'CashRegister')
    Shift = apps.get_model('pos_app', 'Shift')
    Customer = apps.get_model('pos_app', 'Customer')
    Sale = apps.get_model('pos_app', 'Sale')
    CustomerTransaction = apps.get_model('pos_app', 'CustomerTransaction')
    Supplier = apps.get_model('pos_app', 'Supplier')
    SupplierTransaction = apps.get_model('pos_app', 'SupplierTransaction')
    StockEntry = apps.get_model('pos_app', 'StockEntry')
    StockTake = apps.get_model('pos_app', 'StockTake')
    SystemLog = apps.get_model('pos_app', 'SystemLog')
    CompanySetting = apps.get_model('pos_app', 'CompanySetting')
    User = apps.get_model('auth', 'User')

    default_company, _ = Company.objects.get_or_create(
        code="default",
        defaults={
            "name": "Merkez Mağaza",
            "receipt_header": "Hİ MARKET",
            "is_active": True
        }
    )

    # 1. Supplier: Çakışmaları önleyerek tek tek güncelle
    for sup in Supplier.objects.filter(company__isnull=True):
        existing = Supplier.objects.filter(company=default_company, company_name=sup.company_name).first()
        if existing and existing.id != sup.id:
            sup.company_name = f"{sup.company_name} ({sup.id})"
        sup.company = default_company
        sup.save(update_fields=['company', 'company_name'])

    # 2. CashRegister: Kasa kodu çakışmalarını önle
    for reg in CashRegister.objects.filter(company__isnull=True):
        existing = CashRegister.objects.filter(company=default_company, register_code=reg.register_code).first()
        if existing and existing.id != reg.id:
            reg.register_code = f"{reg.register_code}-{reg.id}"
        reg.company = default_company
        reg.save(update_fields=['company', 'register_code'])

    # 3. Diğer modeller: Toplu güncelle
    other_models = [
        Category, Product, Shift, Customer, Sale,
        CustomerTransaction, SupplierTransaction, StockEntry,
        StockTake, SystemLog
    ]

    for model in other_models:
        model.objects.filter(company__isnull=True).update(company=default_company)

    setting = CompanySetting.objects.filter(id=1).first()
    if setting and not setting.company_id:
        setting.company = default_company
        setting.save()

    for user in User.objects.all():
        profile, _ = UserProfile.objects.get_or_create(user=user)
        if not profile.company_id:
            profile.company = default_company
            profile.save()


class Migration(migrations.Migration):

    dependencies = [
        ('pos_app', '0011_company_userprofile_alter_cashregister_register_code_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_company_data, migrations.RunPython.noop),
    ]
