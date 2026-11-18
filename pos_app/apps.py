from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _enable_sqlite_wal(sender, connection, **kwargs):
    if connection.vendor == 'sqlite':
        cursor = connection.cursor()
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA synchronous=NORMAL;')
        cursor.execute('PRAGMA busy_timeout=30000;')
        cursor.execute('PRAGMA foreign_keys=ON;')


class PosAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pos_app'
    verbose_name = 'Hİ MARKET Kasa & Stok Yönetimi'

    def ready(self):
        connection_created.connect(_enable_sqlite_wal, dispatch_uid='pos_app_sqlite_wal')
