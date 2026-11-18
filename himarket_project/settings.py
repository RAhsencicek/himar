import os
import sqlite3
from pathlib import Path
from django.db.backends.signals import connection_created

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file(env_path: Path):
    """
    Harici kütüphane bağımlılığı olmadan .env dosyasını güvenli şekilde okur ve
    os.environ içerisine yükler.
    """
    if not env_path.exists():
        return
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except Exception:
        pass


# .env dosyasını yükle
load_env_file(BASE_DIR / '.env')

# ==========================================================
# GÜVENLİK VE PRODÜKSİYON AYARLARI (.env Tabanlı)
# ==========================================================
SECRET_KEY = os.environ.get(
    'SECRET_KEY',
    'himarket-prod-2026-secure-pos-9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c'
)

DEBUG = os.environ.get('DEBUG', 'False').lower() in ('true', '1', 'yes')

_raw_hosts = os.environ.get('ALLOWED_HOSTS', '*')
if _raw_hosts == '*':
    ALLOWED_HOSTS = ['*']
else:
    ALLOWED_HOSTS = [h.strip() for h in _raw_hosts.split(',') if h.strip()]
    if 'testserver' not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append('testserver')

# Kasa API Güvenlik Anahtarı ve Donanım Ayarları
POS_API_TOKEN = os.environ.get('POS_API_TOKEN', 'himarket-pos-terminal-token-2026')
ESC_POS_ENABLED = os.environ.get('ESC_POS_ENABLED', 'True').lower() in ('true', '1', 'yes')
PRINTER_MODE = os.environ.get('PRINTER_MODE', 'SPOOL_AND_SOCKET')
PRINTER_HOST = os.environ.get('PRINTER_HOST', '127.0.0.1')
PRINTER_PORT = int(os.environ.get('PRINTER_PORT', '9100'))
PRINTER_NAME = os.environ.get('PRINTER_NAME', 'POS-80')
CASH_DRAWER_KICK_ON_CASH = os.environ.get('CASH_DRAWER_KICK_ON_CASH', 'True').lower() in ('true', '1', 'yes')

# Yedekleme Klasörü (backups/)
BACKUP_DIR = BASE_DIR / 'backups'
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Hİ MARKET Kurumsal POS Uygulaması
    'pos_app.apps.PosAppConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'himarket_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'himarket_project.wsgi.application'

# ==========================================================
# VERİTABANI YAPILANDIRMASI (SQLite WAL Modu & PostgreSQL Desteği)
# ==========================================================
DB_ENGINE_SETTING = os.environ.get('DB_ENGINE', 'sqlite3').lower()

if DB_ENGINE_SETTING in ('postgresql', 'postgres', 'pgsql'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('DB_NAME', 'himarket_db'),
            'USER': os.environ.get('DB_USER', 'postgres'),
            'PASSWORD': os.environ.get('DB_PASSWORD', 'postgres'),
            'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
            'PORT': os.environ.get('DB_PORT', '5432'),
            'CONN_MAX_AGE': 60,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / os.environ.get('DB_NAME', 'db.sqlite3'),
            'OPTIONS': {
                'timeout': 30,  # Eşzamanlı kasa işlemlerinde kilitlemeyi önler (30 sn)
            },
        }
    }


def activate_sqlite_wal_mode(sender, connection, **kwargs):
    """
    SQLite bağlantısı kurulduğu anda WAL (Write-Ahead Logging) modunu ve
    eşzamanlılık optimizasyonlarını aktif eder.
    Böylece Kasa 1, Kasa 2 ve Yönetim Paneli aynı anda okuma/yazma yaparken
    'database is locked' hatası oluşmaz.
    """
    if connection.vendor == 'sqlite':
        cursor = connection.cursor()
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA synchronous=NORMAL;')
        cursor.execute('PRAGMA busy_timeout=30000;')
        cursor.execute('PRAGMA temp_store=MEMORY;')
        cursor.execute('PRAGMA foreign_keys=ON;')


connection_created.connect(activate_sqlite_wal_mode)

# Başlangıçta ana db.sqlite3 dosyasında WAL modunu kalıcı olarak etkinleştir
try:
    _db_file = BASE_DIR / os.environ.get('DB_NAME', 'db.sqlite3')
    if _db_file.exists():
        with sqlite3.connect(str(_db_file), timeout=30) as _conn:
            _conn.execute('PRAGMA journal_mode=WAL;')
            _conn.execute('PRAGMA synchronous=NORMAL;')
except Exception:
    pass


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'tr-tr'
TIME_ZONE = 'Europe/Istanbul'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files (Ürün Görselleri vb.)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Authentication URLs & Security Cookie Flags
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/management/dashboard/'
LOGOUT_REDIRECT_URL = '/login/'
CSRF_COOKIE_HTTPONLY = False  # Frontend JS fetch() isteklerinin CSRF token okuyabilmesi için
SESSION_COOKIE_HTTPONLY = True

from django.contrib.messages import constants as message_constants
MESSAGE_TAGS = {
    message_constants.ERROR: 'danger',
}

# Otomatik birim testlerinde (manage.py test) şifre özetleme hızlandırması
import sys
if 'test' in sys.argv:
    PASSWORD_HASHERS = [
        'django.contrib.auth.hashers.MD5PasswordHasher',
    ]

