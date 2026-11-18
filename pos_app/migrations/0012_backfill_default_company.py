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

    models_to_update = [
        Category, Product, CashRegister, Shift, Customer, Sale,
        CustomerTransaction, Supplier, SupplierTransaction, StockEntry,
        StockTake, SystemLog
    ]

    for model in models_to_update:
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
