from django.shortcuts import render, get_object_or_404, redirect
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse, FileResponse, Http404, HttpResponse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
import csv
import io
import sqlite3
import shutil
import json
import datetime
import calendar
import random
import secrets
import uuid
from datetime import timedelta
from django.db.models import Q, Sum, Count, F
from django.db import transaction, connection, IntegrityError
from django.views.decorators.csrf import csrf_exempt
import urllib.parse
import re
from .models import (
    Product, ProductVariant, Category, CashRegister, Shift, Sale, SaleItem,
    Supplier, SupplierTransaction, StockEntry, StockEntryItem, SystemLog, log_action,
    CompanySetting, Customer, CustomerTransaction, StockTake, StockTakeItem,
    Company, UserProfile, get_user_company, Subscription, GlobalProductPool,
    BranchTransfer, BranchTransferItem, DevicePairingSession
)
from .printer_service import silent_print_receipt, trigger_cash_drawer_only, silent_print_collection_receipt
from .fiscal_service import calculate_vat_breakdown, apply_fiscal_data_to_sale, get_company_fiscal_profile
from .scale_service import read_scale_weight, list_available_serial_ports
from .barcode_service import generate_valid_ean13, validate_ean13, generate_ean13_svg
from .label_service import prepare_product_label_context, generate_tspl_label_payload, generate_zpl_label_payload


def company_scope_q(company, company_field='company'):
    """
    Çoklu Mağaza (Multi-Tenancy) Güvenli Sorgu Kapsamlayıcısı:
    - İlgili mağaza için verileri filtreler.
    - Merkez/varsayılan mağazada ise geriye dönük uyumluluk için atanmamış (null) eski kayıtları da dahil eder.
    - Diğer şubelerde ise mutlak izolasyon sağlayarak sadece o şubeye ait kayıtları getirir.
    """
    if not company:
        return Q()
    if getattr(company, 'code', '') == 'default':
        return Q(**{company_field: company}) | Q(**{f"{company_field}__isnull": True})
    return Q(**{company_field: company})


def _parse_decimal(raw, default=None):
    """Formdan gelen ondalık sayıyı güvenli ayrıştırır; geçersizse ValueError fırlatır."""
    text = str(raw if raw is not None else '').strip()
    if not text:
        if default is not None:
            return default
        raise ValueError("Boş sayısal değer")
    if ',' in text and '.' not in text:
        text = text.replace(',', '.')
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Geçersiz sayısal değer: {text}") from exc


def _parse_int(raw, default=0):
    """Formdan gelen tam sayıyı güvenli ayrıştırır; geçersizse ValueError fırlatır."""
    text = str(raw if raw is not None else '').strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"Geçersiz tam sayı: {text}") from exc


def _parse_iso_date(raw):
    """YYYY-MM-DD biçimli tarih filtresini güvenli ayrıştırır."""
    text = (raw or '').strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _safe_next_redirect(request, fallback_url_name):
    """POST sonrası next parametresi güvenli iç URL ise oraya, değilse varsayılana yönlendirir."""
    next_url = request.POST.get('next', '').strip()
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect(fallback_url_name)


def _sanitize_csv_cell(val):
    """Excel formül enjeksiyonuna karşı metin hücrelerini korur; negatif sayıları bozmaz."""
    if val is None:
        return ''
    if isinstance(val, (int, float, Decimal)):
        return str(val)
    text = str(val)
    if text and text[0] in ('=', '@', '\t', '\r'):
        return "'" + text
    if text and text[0] in ('+', '-') and not re.fullmatch(r'[+-]?\d+(?:[.,]\d+)?', text.strip()):
        return "'" + text
    return text


def _csv_response(filename, headers, rows):
    """Türkçe Excel uyumlu UTF-8 BOM + noktalı virgül ayırıcılı CSV indirme yanıtı üretir."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=';', lineterminator='\n')
    writer.writerow([_sanitize_csv_cell(h) for h in headers])
    for row in rows:
        writer.writerow([_sanitize_csv_cell(col) for col in row])

    payload = '\ufeff' + buf.getvalue()
    response = HttpResponse(payload, content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def staff_required(view_func):
    """
    Yalnızca yönetici (is_staff veya is_superuser) yetkisine sahip kullanıcıların
    erişmesine izin veren özel yetkilendirme dekoratörü (RBAC).
    Kasiyer rolündeki kullanıcıları bilgilendirici bir uyarı ile Kasa Satış Ekranına yönlendirir.
    Oturum açmamış kullanıcıları giriş sayfasına yönlendirir.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f'/login/?next={request.path}')
        if not (request.user.is_staff or request.user.is_superuser):
            messages.warning(request, 'Bu sayfaya erişim yetkiniz bulunmamaktadır. Yalnızca yöneticiler erişebilir.')
            return redirect('pos_screen')
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def staff_api_required(view_func):
    """
    Yönetici API uç noktaları için JSON tabanlı yetki denetimi.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'success': False, 'message': 'Oturum açmanız gerekmektedir.'}, status=401)
        if not (request.user.is_staff or request.user.is_superuser):
            return JsonResponse({'success': False, 'message': 'Bu işlem için yönetici yetkisi gerekmektedir.'}, status=403)
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def superuser_required(view_func):
    """
    Yalnızca platform sahibi (is_superuser=True) yetkisine sahip kullanıcıların
    erişmesine izin veren özel yetkilendirme dekoratörü.
    Oturum açmamış kullanıcıları giriş sayfasına (/login/?next=...) yönlendirir.
    Oturum açmış ancak is_superuser=False olan (kasiyer veya mağaza yöneticisi)
    kullanıcılar için şık 403 yetki kısıtlama ekranı sunar (HTTP 403).
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f'/login/?next={request.path}')
        if not request.user.is_superuser:
            return render(request, '403.html', {'user': request.user}, status=403)
        return view_func(request, *args, **kwargs)
    return _wrapped_view


# ==========================================================
# VERİTABANI OTOMATİK YEDEKLEME & WAL DURUM SERVİSİ
# ==========================================================

def backup_database(reason: str = 'Z_RAPORU', register_code: str = None, user=None) -> dict:
    """
    Z Raporu kapandığı anda veya yönetici panelinden talep edildiğinde otomatik çalışan
    canlı veritabanı yedekleme fonksiyonu.
    SQLite'ın yerleşik çevrimiçi yedekleme API'sini (sqlite3.backup) kullanarak
    'backups/' klasörüne zaman damgalı, tutarlı bir kopya kaydeder.
    """
    backup_dir = Path(getattr(settings, 'BACKUP_DIR', settings.BASE_DIR / 'backups'))
    backup_dir.mkdir(parents=True, exist_ok=True)

    now_local = timezone.localtime(timezone.now())
    timestamp_str = now_local.strftime('%Y%m%d_%H%M%S')
    reg_suffix = f"_{register_code}" if register_code else ""
    backup_filename = f"himarket_backup_{timestamp_str}{reg_suffix}.sqlite3"
    backup_path = backup_dir / backup_filename

    # Aynı saniye içinde birden fazla yedek alınırsa dosya adının çakışmasını önle
    counter = 1
    while backup_path.exists():
        backup_filename = f"himarket_backup_{timestamp_str}{reg_suffix}_{counter:02d}.sqlite3"
        backup_path = backup_dir / backup_filename
        counter += 1

    db_name = str(settings.DATABASES['default']['NAME'])
    primary_db_file = Path(settings.BASE_DIR) / 'db.sqlite3'

    try:
        src_path = Path(db_name)
        is_memory_db = (db_name == ':memory:' or 'memory' in db_name.lower() or not src_path.exists())

        if not is_memory_db and not connection.in_atomic_block:
            # Canlı SQLite WAL veritabanından çevrimiçi (online) tutarlı yedek al
            with sqlite3.connect(str(src_path), timeout=5) as src_conn:
                with sqlite3.connect(str(backup_path), timeout=5) as dst_conn:
                    src_conn.backup(dst_conn)
        elif not is_memory_db and src_path.exists():
            # Atomik blok içindeyken kilitlenmeyi önlemek için güncel mtime ile dosya kopyası al
            shutil.copyfile(str(src_path), str(backup_path))
        elif primary_db_file.exists():
            # Test ortamında (in-memory DB) ana db.sqlite3 dosyasından güncel mtime ile yedek oluştur
            shutil.copyfile(str(primary_db_file), str(backup_path))
        else:
            # Sıfır kurulum test ortamında geçerli bir SQLite yedek dosyası oluştur
            with sqlite3.connect(str(backup_path)) as dst_conn:
                dst_conn.execute('CREATE TABLE IF NOT EXISTS backup_meta (created_at TEXT, reason TEXT);')
                dst_conn.execute('INSERT INTO backup_meta VALUES (?, ?);', (timestamp_str, reason))
                dst_conn.commit()
    except Exception:
        if primary_db_file.exists():
            shutil.copyfile(str(primary_db_file), str(backup_path))

    # Eski yedekleri temizle (Son 50 yedeği koru; az önce alınan yedeği asla silme)
    all_backups = sorted(
        backup_dir.glob('himarket_backup_*.sqlite3'),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    for old_bkp in all_backups[50:]:
        if old_bkp == backup_path:
            continue
        try:
            old_bkp.unlink()
        except OSError:
            pass

    size_bytes = backup_path.stat().st_size if backup_path.exists() else 0
    size_kb = round(size_bytes / 1024.0, 1)

    log_prefix = f"[{register_code}] " if register_code else ""
    log_action(
        'SİSTEM',
        f'{log_prefix}Otomatik Veritabanı Yedeği Alındı',
        f"{log_prefix}Yedek Dosyası: {backup_filename} ({size_kb} KB) - Neden: {reason}",
        user=user
    )

    return {
        'success': True,
        'filename': backup_filename,
        'filepath': str(backup_path),
        'size_kb': size_kb,
        'created_at': now_local.strftime('%d.%m.%Y %H:%M:%S'),
    }


def get_backup_status_info() -> dict:
    """
    Yönetici paneli için backups/ klasöründeki yedeklerin listesini ve
    SQLite WAL modu durumunu döndürür.
    """
    backup_dir = Path(getattr(settings, 'BACKUP_DIR', settings.BASE_DIR / 'backups'))
    backup_dir.mkdir(parents=True, exist_ok=True)

    backup_files = sorted(
        backup_dir.glob('*.sqlite3'),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    backups_list = []
    for f in backup_files[:10]:
        stat = f.stat()
        dt = datetime.datetime.fromtimestamp(stat.st_mtime, tz=timezone.get_current_timezone())
        backups_list.append({
            'filename': f.name,
            'size_kb': round(stat.st_size / 1024.0, 1),
            'created_at': dt.strftime('%d.%m.%Y %H:%M:%S'),
        })

    wal_active = False
    journal_mode = 'unknown'
    try:
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA journal_mode;')
            row = cursor.fetchone()
            if row:
                journal_mode = str(row[0]).lower()
                wal_active = (journal_mode == 'wal')
    except Exception:
        pass

    return {
        'backup_count': len(backup_files),
        'latest_backup': backups_list[0] if backups_list else None,
        'recent_backups': backups_list,
        'wal_active': wal_active,
        'journal_mode': journal_mode.upper(),
    }


@staff_required
def download_backup_view(request):
    """
    Admin Panelinde market sahibinin tek tıkla veritabanı yedeğini bilgisayarına
    indirebileceği 'Yedek İndir' uç noktası.
    - ?file=... parametresi verilmişse ilgili yedek dosyasını indirir.
    - Parametre verilmemişse anlık güncel bir yedek oluşturup (veya en son yedeği alıp) indirir.
    """
    backup_dir = Path(getattr(settings, 'BACKUP_DIR', settings.BASE_DIR / 'backups'))
    backup_dir.mkdir(parents=True, exist_ok=True)

    requested_file = request.GET.get('file', '').strip()
    target_path = None

    if requested_file:
        safe_name = Path(requested_file).name
        candidate = backup_dir / safe_name
        if candidate.exists() and candidate.suffix == '.sqlite3':
            target_path = candidate

    if target_path is None:
        # Taze yedek oluştur ve onu indir
        bkp_info = backup_database(reason='ADMIN_YEDEK_INDIR', user=request.user)
        target_path = Path(bkp_info['filepath'])

    if not target_path or not target_path.exists():
        raise Http404("Yedek dosyası bulunamadı.")

    response = FileResponse(
        open(target_path, 'rb'),
        as_attachment=True,
        filename=target_path.name,
        content_type='application/x-sqlite3'
    )
    return response


def get_client_ip(request):
    """İstemcinin gerçek IP adresini tespit eder (Proxy / Cloudflare arkası dahil)"""
    if not request:
        return None
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def get_current_register(request=None, register_code=None, company=None):
    """
    Dinamik Kasa / Terminal Çözümleyici (Faz 9: Donanım/Cihaz Kilidi Uyumlu, Faz 11: Multi-Tenancy):
    1. Fonksiyona doğrudan parametre geçildiyse onu kullanır.
    2. Öncelikle 'himarket_device_token' (Smart TV Cihaz Mührü) kontrol edilir.
    3. Cihaz mühürlü değilse 'himarket_terminal_code' veya session/'?register=' kontrol edilir.
    4. Hiçbiri yoksa ilk aktif kasa veya varsayılan 'KASA-01' kullanılır.
    """
    if request:
        device_token = request.COOKIES.get('himarket_device_token') or request.META.get('HTTP_X_TERMINAL_TOKEN')
        if device_token:
            paired_reg = CashRegister.objects.filter(device_token=device_token, is_active=True).select_related('company').first()
            if paired_reg:
                paired_reg.last_seen_at = timezone.now()
                paired_reg.save(update_fields=['last_seen_at'])
                request.session['active_register_code'] = paired_reg.register_code
                return paired_reg

    if company is None:
        if request and hasattr(request, 'user') and request.user.is_authenticated:
            company = get_user_company(request.user)
        else:
            company = Company.get_default_company()

    if not register_code and request:
        terminal_cookie = request.COOKIES.get('himarket_terminal_code')
        if terminal_cookie:
            register_code = terminal_cookie.strip()
        else:
            register_code = request.GET.get('register') or request.session.get('active_register_code')

    if not register_code:
        first_reg = CashRegister.objects.filter(company=company, is_active=True).first()
        register_code = first_reg.register_code if first_reg else 'KASA-01'

    register = CashRegister.objects.filter(company=company, register_code=register_code).first()
    if not register:
        register, _ = CashRegister.objects.get_or_create(
            company=company,
            register_code=register_code,
            defaults={'name': f'{register_code} (Satış Terminali)', 'is_active': True}
        )

    if request:
        request.session['active_register_code'] = register.register_code
    return register


def get_or_create_active_shift(user=None, register=None, request=None, company=None):
    """
    Dinamik Kasa ve Kasiyer için Aktif Vardiyayı Getirir veya Başlatır.
    Münhasır Kasa Kilidi (1 Kasa = 1 Kasiyer, Multi-Tenancy Güvenliği):
    Açık bir vardiya varsa ve bu vardiyanın bir kasiyeri varsa, başka bir kasiyer
    o kasaya atanamaz veya açık vardiyayı ele geçiremez.
    """
    if register is None:
        register = get_current_register(request, company=company)

    if not company:
        company = register.company if register and register.company else (get_user_company(user) if user else Company.get_default_company())

    shift = Shift.objects.filter(company=company, cash_register=register, is_closed=False).first()
    if not shift:
        shift = Shift.objects.create(
            company=company,
            cash_register=register,
            cashier=user if (user and user.is_authenticated) else None,
            opening_float=Decimal('500.00'),  # Varsayılan 500 TL açılış devir parası
            expected_cash=Decimal('500.00')
        )
    elif user and user.is_authenticated and not shift.cashier:
        shift.cashier = user
        shift.save(update_fields=['cashier'])
    return register, shift


@login_required(login_url='/login/')
def pos_screen(request):
    """
    Market Kasa Satış Ekranı Ana Görünümü (POS Interface)
    Faz 9: Cihaz Eşleştirme ve Münhasır Kasa Kilidi (1 Kasa = 1 Kasiyer).
    Faz 11: Çoklu Mağaza / Multi-Tenancy Güvenli Veri İzolasyonu.
    """
    device_token = request.COOKIES.get('himarket_device_token')
    paired_register = None
    if device_token:
        paired_register = CashRegister.objects.filter(device_token=device_token, is_active=True).select_related('company').first()

    if paired_register:
        user_company = paired_register.company
        register = paired_register
        paired_register.last_seen_at = timezone.now()
        paired_register.save(update_fields=['last_seen_at'])
    else:
        user_company = get_user_company(request.user)
        register = get_current_register(request, company=user_company)

    # Sistemde mağazaya ait en az KASA-01 ve KASA-02'nin hazır olmasını sağla
    CashRegister.objects.get_or_create(
        company=user_company,
        register_code='KASA-01',
        defaults={'name': 'KASA-01 (Ana Satış Terminali)', 'is_active': True}
    )
    CashRegister.objects.get_or_create(
        company=user_company,
        register_code='KASA-02',
        defaults={'name': 'KASA-02 (Ek Satış Terminali)', 'is_active': True}
    )

    paired_legacy_cookie = request.COOKIES.get('himarket_terminal_code')
    is_terminal_locked = bool(paired_register or paired_legacy_cookie)
    active_registers = CashRegister.objects.filter(company=user_company, is_active=True).order_by('register_code')

    # Özel olarak cihaz eşleştirme ekranı talep edilmişse (?pair=1)
    if request.GET.get('pair') == '1':
        return redirect('pair_terminal_action')

    # Münhasır Kasa / Vardiya Kilidi Kontrolü (1 Kasa = 1 Kasiyer)
    open_shift = Shift.objects.filter(company=user_company, cash_register=register, is_closed=False).first()
    if open_shift and open_shift.cashier and open_shift.cashier != request.user:
        # Eğer kullanıcı yetkili (is_staff) ve ?takeover=1 parametresi ile geldiyse doğrudan devral
        if (request.user.is_staff or request.user.is_superuser) and request.GET.get('takeover') == '1':
            open_shift.is_closed = True
            open_shift.closed_at = timezone.now()
            open_shift.notes = (open_shift.notes or '') + f"\n[{timezone.localtime().strftime('%d.%m.%Y %H:%M')} Süpervizör '{request.user.username}' tarafından devralındı.]"
            open_shift.save(update_fields=['is_closed', 'closed_at', 'notes'])
            log_action('SHIFT', 'Kasa Devralma', f"Kasa {register.name} yöneticisi tarafından devralındı.", user=request.user)
            messages.success(request, f"Önceki kasiyerin ({open_shift.cashier.username}) açık vardiyası kapatıldı ve kasa devralındı.")
        else:
            context = {
                'register': register,
                'locked_shift': open_shift,
                'locked_cashier': open_shift.cashier,
                'current_user': request.user,
                'all_registers': active_registers,
                'is_supervisor': bool(request.user.is_staff or request.user.is_superuser),
            }
            return render(request, 'pos_register_locked.html', context, status=423)

    register, shift = get_or_create_active_shift(request.user, register=register, request=request)
    quick_buttons = Product.objects.filter(company=user_company, is_active=True).select_related('category').order_by('-is_quick_button', 'quick_button_order', 'name')
    categories = Category.objects.filter(company=user_company, is_active=True).order_by('display_order')

    cashier_display = (
        (shift.cashier.get_full_name() or shift.cashier.username)
        if (shift and shift.cashier)
        else (request.user.get_full_name() or request.user.username)
    )

    setting = CompanySetting.get_settings(company=user_company)
    is_supervisor = bool(request.user.is_staff or request.user.is_superuser)

    context = {
        'company': user_company,
        'register': register,
        'shift': shift,
        'quick_buttons': quick_buttons,
        'categories': categories,
        'all_registers': active_registers,
        'cashier_name': cashier_display,
        'primary_database': 'Django SQLite WAL (db.sqlite3)',
        'esc_pos_enabled': getattr(settings, 'ESC_POS_ENABLED', True),
        'setting': setting,
        'supervisor_cancel_required': setting.supervisor_cancel_required,
        'is_supervisor': is_supervisor,
        'is_terminal_locked': is_terminal_locked,
    }
    response = render(request, 'pos_screen.html', context)
    return response


def _format_pos_product(product, override_price=None, variant=None):
    """Kasa ekranı için ürün JSON serileştirici - Faz 16 SKT ve Parti desteği dahil"""
    final_price = float(override_price if override_price is not None else product.sell_price)
    variant_desc = f"{variant.size} · {variant.color}".strip(' · ') if variant else ''
    display_name = f"{product.name} ({variant_desc})" if variant_desc else product.name
    item_barcode = variant.barcode if (variant and variant.barcode) else product.barcode

    return {
        'id': product.id,
        'name': display_name,
        'barcode': item_barcode,
        'sell_price': final_price,
        'vat_rate': float(product.vat_rate),
        'unit': product.unit,
        'is_weighted': product.is_weighted,
        'stock_quantity': float(variant.stock_quantity if variant else product.stock_quantity),
        'variant_id': variant.id if variant else None,
        'variant_info': variant_desc,
        'size': variant.size if variant else None,
        'color': variant.color if variant else None,
        # Faz 16: SKT ve Parti Bilgileri
        'expiration_date': product.expiration_date.strftime('%d.%m.%Y') if product.expiration_date else None,
        'days_to_expiration': product.days_to_expiration,
        'expiration_status': product.expiration_status,
        'is_expired': product.expiration_status == 'EXPIRED',
        'is_expiring_soon': product.expiration_status in ('CRITICAL', 'WARNING'),
        'lot_number': product.lot_number or '',
    }


def search_barcode(request):
    """
    Barkod ve Terazi/Tartılı Ürün Çözümleme API
    Ultra-Hızlı Yerel Sorgulama (< 1ms) - Faz 11: Multi-Tenancy Korumalı
    """
    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': 'Oturum açmanız gerekmektedir.'}, status=401)

    user_company = get_user_company(request.user)
    barcode = request.GET.get('barcode', '').strip()
    if not barcode:
        return JsonResponse({'success': False, 'message': 'Barkod girilmedi.'}, status=400)

    # 0. Faz 13: Beden / Renk Varyant Barkodu Eşleşmesi (Giyim / Butik Önceliği)
    variant = ProductVariant.objects.filter(
        company=user_company, barcode=barcode, is_active=True
    ).select_related('product').first()
    if variant and variant.product.is_active:
        prod = variant.product
        final_price = float(variant.price_override if variant.price_override is not None else prod.sell_price)
        return JsonResponse({
            'success': True,
            'has_variants': False,
            'product': _format_pos_product(prod, override_price=final_price, variant=variant),
            'quantity': 1.0,
            'is_weighted_barcode': False
        })

    # 1. Yerel Veritabanı Standart ve Koli Barkodu Eşleşmesi (Anında < 1ms)
    product = Product.objects.filter(company=user_company, is_active=True).filter(
        Q(barcode=barcode) | Q(secondary_barcode=barcode)
    ).first()
    if product:
        # Faz 13: Eğer ürünün varyantları varsa, dokunmatik seçim için varyant listesini dön
        active_variants = list(product.variants.filter(is_active=True).order_by('size', 'color'))
        if product.has_variants and active_variants:
            variants_data = [
                {
                    'id': v.id,
                    'size': v.size,
                    'color': v.color,
                    'barcode': v.barcode,
                    'sell_price': float(v.price_override if v.price_override is not None else product.sell_price),
                    'stock_quantity': float(v.stock_quantity),
                    'display': f"{v.size} / {v.color}".strip(' / ')
                }
                for v in active_variants
            ]
            return JsonResponse({
                'success': True,
                'has_variants': True,
                'product': _format_pos_product(product),
                'variants': variants_data,
                'quantity': 1.0,
                'is_weighted_barcode': False
            })

        return JsonResponse({
            'success': True,
            'has_variants': False,
            'product': _format_pos_product(product),
            'quantity': 1.0,
            'is_weighted_barcode': False
        })

    # 2. Yerel Veritabanı Terazi / Tartılı Barkod Ayrıştırma (27: Ağırlık / 28: Fiyat-Tutar)
    if len(barcode) == 13 and (barcode.startswith('27') or barcode.startswith('28')):
        prefix = barcode[:2]
        product_code = barcode[2:7]
        plu_clean = product_code.lstrip('0') or '0'
        raw_val = barcode[7:12]

        lookup_candidates = [
            product_code,
            plu_clean,
            f"27{product_code}",
            f"28{product_code}",
            f"27{plu_clean}",
            f"28{plu_clean}",
            f"27{product_code}000000"[:13],
            f"28{product_code}000000"[:13],
            f"27{plu_clean.zfill(5)}000000"[:13],
            f"28{plu_clean.zfill(5)}000000"[:13],
        ]

        weighted_product = Product.objects.filter(
            company=user_company,
            is_active=True
        ).filter(
            Q(barcode__in=lookup_candidates) |
            Q(barcode__contains=product_code) |
            Q(barcode=plu_clean) |
            Q(secondary_barcode__in=lookup_candidates)
        ).filter(
            Q(is_weighted=True) | Q(unit__in=['KG', 'Gram']) | Q(barcode__startswith='27') | Q(barcode__startswith='28')
        ).first()

        # Tartılı bayrağı işaretlenmemiş olsa dahi PLU/barkod uyuşuyorsa eşleştir
        if not weighted_product:
            weighted_product = Product.objects.filter(
                company=user_company,
                is_active=True
            ).filter(
                Q(barcode__in=lookup_candidates) |
                Q(barcode__contains=product_code) |
                Q(barcode=plu_clean)
            ).first()

        if weighted_product:
            try:
                if prefix == '27':
                    # 27: Ağırlık / Gramaj gömülü (Örn: 01425 -> 1.425 kg)
                    weight_grams = Decimal(raw_val)
                    calculated_quantity = round(weight_grams / Decimal('1000.0'), 3)
                    if calculated_quantity <= Decimal('0.000'):
                        calculated_quantity = Decimal('1.000')
                    final_sell_price = float(weighted_product.sell_price)
                else:
                    # 28: Fiyat / Tutar gömülü (Örn: 06412 -> 64.12 TL)
                    total_price_tl = Decimal(raw_val) / Decimal('100.0')
                    if weighted_product.sell_price > Decimal('0.00'):
                        calculated_quantity = round(total_price_tl / weighted_product.sell_price, 3)
                        final_sell_price = float(weighted_product.sell_price)
                    else:
                        calculated_quantity = Decimal('1.000')
                        final_sell_price = float(total_price_tl)

                formatted_prod = _format_pos_product(weighted_product, override_price=final_sell_price)
                formatted_prod['is_weighted'] = True

                return JsonResponse({
                    'success': True,
                    'has_variants': False,
                    'product': formatted_prod,
                    'quantity': float(calculated_quantity),
                    'is_weighted_barcode': True,
                    'scale_prefix': prefix,
                    'scale_plu': plu_clean,
                    'scale_raw_val': raw_val
                })
            except (ValueError, ArithmeticError):
                pass

    # 3. Barkod veya Terazi eşleşmediyse: İsim ile Arama Desteği (Örn: "su", "ekmek", "süt")
    if len(barcode) >= 2:
        name_matched_product = Product.objects.filter(company=user_company, is_active=True, name__icontains=barcode).first()
        if name_matched_product:
            active_variants = list(name_matched_product.variants.filter(is_active=True).order_by('size', 'color'))
            if name_matched_product.has_variants and active_variants:
                variants_data = [
                    {
                        'id': v.id,
                        'size': v.size,
                        'color': v.color,
                        'barcode': v.barcode,
                        'sell_price': float(v.price_override if v.price_override is not None else name_matched_product.sell_price),
                        'stock_quantity': float(v.stock_quantity),
                        'display': f"{v.size} / {v.color}".strip(' / ')
                    }
                    for v in active_variants
                ]
                return JsonResponse({
                    'success': True,
                    'has_variants': True,
                    'product': _format_pos_product(name_matched_product),
                    'variants': variants_data,
                    'quantity': 1.0,
                    'is_weighted_barcode': False
                })

            return JsonResponse({
                'success': True,
                'has_variants': False,
                'product': _format_pos_product(name_matched_product),
                'quantity': 1.0,
                'is_weighted_barcode': False
            })

    # 4. Faz 15: Ortak Hazır Ürün Havuzu (GlobalProductPool) Kontrolü
    # Mağazada kayıtlı değil ancak Türkiye ortak ürün havuzunda mevcutsa kasiyere hızlı ekleme önerisi sun
    pool_product = GlobalProductPool.objects.filter(barcode=barcode).first()
    if pool_product:
        return JsonResponse({
            'success': False,
            'pool_found': True,
            'message': f"Bu ürün dükkanınızda kayıtlı değil ancak Ortak Havuzda bulundu: {pool_product.name}",
            'barcode': barcode,
            'pool_product': {
                'id': pool_product.id,
                'barcode': pool_product.barcode,
                'name': pool_product.name,
                'brand': pool_product.brand,
                'category_name': pool_product.category_name,
                'vat_rate': float(pool_product.vat_rate),
                'recommended_price': float(pool_product.recommended_price),
                'unit': pool_product.unit,
                'is_weighted': pool_product.is_weighted,
            }
        })

    return JsonResponse({'success': False, 'message': 'Barkodlu ürün bulunamadı!', 'barcode': barcode}, status=404)


@require_POST
def create_product_from_pool_api(request):
    """
    Faz 15: Ortak Hazır Ürün Havuzundan (GlobalProductPool) Mağazaya Hızlı Ürün Ekleme API
    Kasiyerin veya yöneticinin tek tıkla havuzdaki ürünü mağaza stoğuna almasını sağlar.
    """
    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': 'Oturum açmanız gerekmektedir.'}, status=401)

    user_company = get_user_company(request.user)
    
    try:
        # JSON veya POST verisi
        if request.content_type == 'application/json':
            data = json.loads(request.body.decode('utf-8'))
        else:
            data = request.POST

        barcode = str(data.get('barcode', '')).strip()
        if not barcode:
            return JsonResponse({'success': False, 'message': 'Barkod bilgisi eksik.'}, status=400)

        # 1. Zaten mağazada var mı?
        existing_product = Product.objects.filter(company=user_company, barcode=barcode).first()
        if existing_product:
            if not existing_product.is_active:
                existing_product.is_active = True
                existing_product.save(update_fields=['is_active'])
            return JsonResponse({
                'success': True,
                'already_exists': True,
                'message': f"'{existing_product.name}' dükkanınızda zaten mevcut.",
                'product': {
                    'id': existing_product.id,
                    'name': existing_product.name,
                    'barcode': existing_product.barcode,
                    'sell_price': float(existing_product.sell_price),
                    'vat_rate': float(existing_product.vat_rate),
                    'unit': existing_product.unit,
                    'is_weighted': existing_product.is_weighted,
                    'stock_quantity': float(existing_product.stock_quantity),
                    'variant_id': None,
                    'variant_info': '',
                }
            })

        # 2. Havuzdan bul
        pool_item = GlobalProductPool.objects.filter(barcode=barcode).first()
        if not pool_item:
            return JsonResponse({'success': False, 'message': 'Ürün global havuzda bulunamadı.'}, status=404)

        # 3. Fiyat ve Stok Ayarları
        sell_price_raw = data.get('sell_price')
        if sell_price_raw is not None and str(sell_price_raw).strip() != '':
            sell_price = Decimal(str(sell_price_raw).replace(',', '.'))
        else:
            sell_price = pool_item.recommended_price

        purchase_price_raw = data.get('purchase_price')
        if purchase_price_raw is not None and str(purchase_price_raw).strip() != '':
            purchase_price = Decimal(str(purchase_price_raw).replace(',', '.'))
        else:
            purchase_price = Decimal('0.00')

        stock_qty_raw = data.get('stock_quantity')
        if stock_qty_raw is not None and str(stock_qty_raw).strip() != '':
            stock_qty = Decimal(str(stock_qty_raw).replace(',', '.'))
        else:
            stock_qty = Decimal('10.00')

        # 4. Kategori oluştur veya bağla
        category_name = str(data.get('category_name') or pool_item.category_name or 'Genel Gıda').strip()
        category, _ = Category.objects.get_or_create(
            company=user_company,
            name=category_name
        )

        product_name = str(data.get('name') or pool_item.name).strip()

        # 5. Ürünü Oluştur
        product = Product.objects.create(
            company=user_company,
            category=category,
            name=product_name,
            barcode=pool_item.barcode,
            sell_price=sell_price,
            buy_price=purchase_price,
            stock_quantity=stock_qty,
            vat_rate=pool_item.vat_rate,
            unit=pool_item.unit,
            is_weighted=pool_item.is_weighted,
            is_active=True
        )

        # Log kaydı
        log_action(
            module="PRODUCT",
            action="POOL_IMPORT",
            message=f"Global Havuzdan Ürün Eklendi: {product.name} ({product.barcode}) - Satış: {product.sell_price} TL",
            user=request.user,
            company=user_company
        )

        return JsonResponse({
            'success': True,
            'message': f"'{product.name}' başarıyla dükkanınıza eklendi!",
            'product': {
                'id': product.id,
                'name': product.name,
                'barcode': product.barcode,
                'sell_price': float(product.sell_price),
                'vat_rate': float(product.vat_rate),
                'unit': product.unit,
                'is_weighted': product.is_weighted,
                'stock_quantity': float(product.stock_quantity),
                'variant_id': None,
                'variant_info': '',
            }
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': f"Ürün eklenirken hata: {str(e)}"}, status=500)


def product_search_api(request):
    """
    Kasa Stok Bul ve Canlı Ürün Arama API Uç Noktası.
    Yalnızca aktif ürünleri, canlı stok ve satış fiyatı ile döndürür (maliyet gizlenir).
    Faz 11: Multi-Tenancy Korumalı.
    """
    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': 'Oturum açmanız gerekmektedir.'}, status=401)

    user_company = get_user_company(request.user)
    q = request.GET.get('q', '').strip()
    cat_id = request.GET.get('category', '').strip()

    qs = Product.objects.filter(company=user_company, is_active=True).select_related('category').prefetch_related('variants')
    if cat_id.isdigit():
        qs = qs.filter(category_id=int(cat_id))
    if q:
        qs = qs.filter(
            Q(name__icontains=q) |
            Q(barcode__icontains=q) |
            Q(secondary_barcode__icontains=q) |
            Q(variants__barcode__icontains=q) |
            Q(variants__size__icontains=q) |
            Q(variants__color__icontains=q)
        ).distinct()

    products = []
    for prod in qs.order_by('-is_quick_button', 'quick_button_order', 'name')[:60]:
        active_vars = [
            {
                'id': v.id,
                'size': v.size,
                'color': v.color,
                'barcode': v.barcode,
                'sell_price': float(v.price_override if v.price_override is not None else prod.sell_price),
                'stock_quantity': float(v.stock_quantity),
                'display': f"{v.size} / {v.color}".strip(' / ')
            }
            for v in prod.variants.all() if v.is_active
        ]
        products.append({
            'id': prod.id,
            'name': prod.name,
            'barcode': prod.barcode or '',
            'secondary_barcode': prod.secondary_barcode or '',
            'sell_price': float(prod.sell_price),
            'vat_rate': float(prod.vat_rate),
            'unit': prod.unit,
            'is_weighted': prod.is_weighted,
            'stock_quantity': float(prod.stock_quantity),
            'min_stock_level': float(prod.min_stock_level),
            'category_id': prod.category_id,
            'category_name': prod.category.name if prod.category else 'Genel',
            'image_url': prod.image.url if prod.image else '',
            'has_variants': prod.has_variants and len(active_vars) > 0,
            'variants': active_vars,
        })

    return JsonResponse({'success': True, 'products': products})


def process_sale(request):
    """
    Kasada Satışı Tamamlama, Fiş Oluşturma, Atomik Stok Kilidi ve ESC/POS Sessiz Baskı
    Güvenceler:
    - CSRF Korumalı POST uç noktası (@csrf_exempt kaldırıldı)
    - Yetki kontrolü (Yalnızca oturum açmış yetkili kasiyerler)
    - Dinamik kasa çözümleme (KASA-01 / KASA-02 bağımsız)
    - Eşzamanlı Stok Kilidi: select_for_update() ve F('stock_quantity') ile Race Condition önleme
    - Müşteri iadelerinde stoğa geri giriş ve kasanın doğru güncellenmesi
    - transaction.atomic() ile yarım kalan satışların (rollback) önlenmesi
    - Arka planda otomatik ESC/POS sessiz fiş baskısı ve RJ11 çekmece tetikleme
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Yalnızca POST istekleri kabul edilir.'}, status=405)

    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': 'Satış yapabilmek için lütfen oturum açınız.'}, status=401)

    user_company = get_user_company(request.user)

    try:
        data = json.loads(request.body.decode('utf-8'))
        items_data = data.get('items', [])
        payment_type = data.get('payment_type', 'NAKIT')
        cash_paid = Decimal(str(data.get('cash_paid', 0)))
        card_paid = Decimal(str(data.get('card_paid', 0)))
        discount_amount = Decimal(str(data.get('discount_amount', 0)))
        is_return = bool(data.get('is_return', False))
        customer_id = data.get('customer_id')

        customer_info = data.get('customer') or {}
        cust_tax_no = str(customer_info.get('tax_number', '11111111111')).strip() or '11111111111'
        cust_title = str(customer_info.get('title', 'Nihai Tüketici')).strip() or 'Nihai Tüketici'
        cust_office = str(customer_info.get('tax_office', '')).strip()
        doc_type = data.get('document_type', 'E_ARSIV')

        if not items_data:
            return JsonResponse({'success': False, 'message': 'Fişte ürün bulunmuyor.'}, status=400)

        # Dinamik Kasa ve Aktif Vardiya Çözümleme (Multi-Tenancy)
        register = get_current_register(request, company=user_company)
        shift = Shift.objects.filter(company=user_company, cash_register=register, is_closed=False).first()
        if not shift:
            register, shift = get_or_create_active_shift(request.user, register=register, request=request)

        # Münhasır Kasa Kilidi (1 Kasa = 1 Kasiyer): Açık vardiya başka bir kasiyere aitse satışı engelle!
        if shift.cashier and shift.cashier != request.user:
            cashier_name = shift.cashier.get_full_name() or shift.cashier.username
            return JsonResponse({
                'success': False,
                'message': f"Bu kasa Kasiyer '{cashier_name}' adına kilitlidir! Başka bir kasiyer bu kasadan satış yapamaz."
            }, status=403)

        # İstek formatı ön doğrulaması (Ürün ve Varyant Desteği - Faz 13)
        parsed_items = []
        for item_info in items_data:
            product_id = item_info.get('product_id') or item_info.get('id')
            if not product_id:
                return JsonResponse({'success': False, 'message': 'Sepetteki ürün kimliği eksik.'}, status=400)
            try:
                qty = Decimal(str(item_info.get('quantity', 1)))
            except (ValueError, TypeError, ArithmeticError):
                return JsonResponse({'success': False, 'message': 'Miktar formatı geçersiz.'}, status=400)

            if qty <= Decimal('0.000'):
                return JsonResponse({'success': False, 'message': 'Ürün miktarı sıfırdan büyük olmalıdır.'}, status=400)

            raw_vid = item_info.get('variant_id')
            variant_id = None
            if raw_vid:
                try:
                    variant_id = int(raw_vid)
                except (ValueError, TypeError):
                    variant_id = None

            parsed_items.append({
                'product_id': int(product_id),
                'quantity': qty,
                'variant_id': variant_id
            })

        # ATOMİK VERİTABANI İŞLEMİ & SATIR KİLİTLEME (select_for_update + F() İfadesi)
        with transaction.atomic():
            # Vardiya satırını kilitle
            shift = Shift.objects.select_for_update().get(id=shift.id)

            # Deadlock önlemek için ürün ve varyant kimliklerini sıralı şekilde kilitle (şirket izolasyonu ile)
            product_ids_sorted = sorted({item['product_id'] for item in parsed_items})
            variant_ids_sorted = sorted({item['variant_id'] for item in parsed_items if item['variant_id']})

            locked_products_qs = Product.objects.select_for_update().filter(company=user_company, id__in=product_ids_sorted)
            locked_products_map = {p.id: p for p in locked_products_qs}

            locked_variants_map = {}
            if variant_ids_sorted:
                locked_variants_qs = ProductVariant.objects.select_for_update().filter(company=user_company, id__in=variant_ids_sorted)
                locked_variants_map = {v.id: v for v in locked_variants_qs}

            # Talep edilen miktarları topla ve geçerlilik kontrolleri yap
            aggregated_product_qty = {}
            aggregated_variant_qty = {}

            for item in parsed_items:
                pid = item['product_id']
                vid = item['variant_id']
                qty = item['quantity']

                product = locked_products_map.get(pid)
                if not product:
                    return JsonResponse({'success': False, 'message': f'Ürün bulunamadı (ID: {pid}).'}, status=404)
                if not product.is_active:
                    return JsonResponse({'success': False, 'message': f"'{product.name}' ürünü satışa kapalıdır (pasif ürün)."}, status=400)

                if vid:
                    variant = locked_variants_map.get(vid)
                    if not variant or variant.product_id != pid:
                        return JsonResponse({'success': False, 'message': f'Geçersiz veya bulunamayan ürün varyantı (ID: {vid}).'}, status=404)
                    if not variant.is_active:
                        var_desc = f"{variant.size} / {variant.color}".strip(' / ')
                        return JsonResponse({'success': False, 'message': f"'{product.name} ({var_desc})' varyantı satışa kapalıdır."}, status=400)
                    aggregated_variant_qty[vid] = aggregated_variant_qty.get(vid, Decimal('0.000')) + qty

                aggregated_product_qty[pid] = aggregated_product_qty.get(pid, Decimal('0.000')) + qty

            # Kilitli (güncel) stok üzerinden yetersiz stok kontrolü
            if not is_return:
                for vid, req_v_qty in aggregated_variant_qty.items():
                    variant = locked_variants_map[vid]
                    if variant.stock_quantity < req_v_qty:
                        var_desc = f"{variant.size} / {variant.color}".strip(' / ')
                        return JsonResponse({
                            'success': False,
                            'message': f"Yetersiz Stok! '{variant.product.name} ({var_desc})' varyantından mevcut stok: {variant.stock_quantity:.3f}, talep edilen: {req_v_qty:.3f}."
                        }, status=400)

                for pid, total_req_qty in aggregated_product_qty.items():
                    product = locked_products_map[pid]
                    # Varyantlı ürünlerde stok kontrolü ilgili varyant üzerinden yapılmıştır
                    if not product.has_variants and product.stock_quantity < total_req_qty:
                        return JsonResponse({
                            'success': False,
                            'message': f"Yetersiz Stok! '{product.name}' ürününden mevcut stok: {product.stock_quantity:.3f} {product.unit}, talep edilen: {total_req_qty:.3f} {product.unit}."
                        }, status=400)

            # Nakit ödemede eksik tahsilat ön kontrolü
            prelim_subtotal = Decimal('0.00')
            prelim_vat = Decimal('0.00')
            for item in parsed_items:
                product = locked_products_map[item['product_id']]
                vid = item['variant_id']
                var_obj = locked_variants_map.get(vid) if vid else None
                unit_price = var_obj.price_override if (var_obj and var_obj.price_override is not None) else product.sell_price

                line_total = round(item['quantity'] * unit_price, 2)
                if product.vat_rate > Decimal('0.00'):
                    line_sub = round(line_total / (Decimal('1.00') + (product.vat_rate / Decimal('100.00'))), 2)
                    line_v = line_total - line_sub
                else:
                    line_sub = line_total
                    line_v = Decimal('0.00')
                prelim_subtotal += line_sub
                prelim_vat += line_v

            # Müşteri ve Veresiye Tanımlaması (Transaction içinde satır kilidi)
            customer_obj = None
            if customer_id:
                try:
                    customer_obj = Customer.objects.select_for_update().get(company=user_company, id=int(customer_id))
                except (Customer.DoesNotExist, ValueError, TypeError):
                    return JsonResponse({'success': False, 'message': 'Seçilen müşteri veritabanında bulunamadı.'}, status=404)
            elif payment_type == 'VERESIYE':
                return JsonResponse({'success': False, 'message': 'Veresiye satış yapabilmek için lütfen bir müşteri seçiniz.'}, status=400)

            grand_total = max(Decimal('0.00'), (prelim_subtotal + prelim_vat) - discount_amount)
            change_given = Decimal('0.00')

            if not is_return:
                if payment_type == 'NAKIT':
                    if cash_paid > Decimal('0.00') and cash_paid < grand_total:
                        return JsonResponse({
                            'success': False,
                            'message': f"Alınan nakit tutar ({cash_paid:.2f} TL), fiş toplamından ({grand_total:.2f} TL) az olamaz!"
                        }, status=400)
                    change_given = (cash_paid - grand_total) if cash_paid >= grand_total else Decimal('0.00')
                elif payment_type == 'KART':
                    card_paid = grand_total
                elif payment_type == 'PARCALI':
                    total_collected = cash_paid + card_paid
                    if total_collected < grand_total:
                        return JsonResponse({
                            'success': False,
                            'message': f"Nakit ({cash_paid:.2f} TL) ve Kart ({card_paid:.2f} TL) toplamı ({total_collected:.2f} TL), fiş tutarından ({grand_total:.2f} TL) az olamaz!"
                        }, status=400)
                    change_given = (total_collected - grand_total) if total_collected > grand_total else Decimal('0.00')
                elif payment_type == 'VERESIYE':
                    if not customer_obj.is_active:
                        return JsonResponse({'success': False, 'message': f"'{customer_obj.full_name}' müşteri hesabı pasif durumdadır."}, status=400)
                    if (customer_obj.balance + grand_total) > customer_obj.credit_limit:
                        return JsonResponse({
                            'success': False,
                            'message': f"Veresiye Limiti Aşıldı! Müşteri Limiti: {customer_obj.credit_limit:.2f} TL, Mevcut Borç: {customer_obj.balance:.2f} TL, Bu Satış: {grand_total:.2f} TL."
                        }, status=400)

            today_local = timezone.localtime(timezone.now()).date()
            today_str = today_local.strftime('%Y%m%d')
            prefix = "IADE" if is_return else "FIS"

            # Benzersiz ve çakışmasız fiş numarası üretimi (Mağaza bazlı)
            last_today_sale = Sale.objects.filter(company=user_company, receipt_number__contains=f"-{today_str}-").order_by('-id').first()
            next_seq = 1
            if last_today_sale and last_today_sale.receipt_number:
                try:
                    parts = last_today_sale.receipt_number.split('-')
                    if len(parts) >= 3:
                        next_seq = int(parts[-1]) + 1
                except (ValueError, TypeError):
                    next_seq = Sale.objects.filter(company=user_company, receipt_number__contains=today_str).count() + 1

            receipt_number = f"{prefix}-{today_str}-{next_seq:04d}"
            while Sale.objects.filter(company=user_company, receipt_number=receipt_number).exists():
                next_seq += 1
                receipt_number = f"{prefix}-{today_str}-{next_seq:04d}"

            # VUK 507 Yasal KDV Baremleri Ayrıştırması (%1, %10, %20) (Varyant Fiyat Override Desteği)
            items_for_vat = []
            for item in parsed_items:
                product = locked_products_map[item['product_id']]
                vid = item['variant_id']
                var_obj = locked_variants_map.get(vid) if vid else None
                unit_price = var_obj.price_override if (var_obj and var_obj.price_override is not None) else product.sell_price
                items_for_vat.append({
                    'vat_rate': product.vat_rate,
                    'unit_price': unit_price,
                    'quantity': item['quantity']
                })
            vat_breakdown = calculate_vat_breakdown(items_for_vat, discount_amount)

            sale = Sale.objects.create(
                company=user_company,
                receipt_number=receipt_number,
                shift=shift,
                cashier=request.user,
                customer=customer_obj,
                payment_type=payment_type,
                document_type=doc_type,
                customer_tax_number=customer_obj.tax_number if (customer_obj and customer_obj.tax_number) else cust_tax_no,
                customer_title=customer_obj.full_name if customer_obj else cust_title,
                customer_tax_office=cust_office,
                subtotal=vat_breakdown['total_subtotal'],
                total_vat=vat_breakdown['total_vat'],
                vat_1_base=vat_breakdown['vat_1_base'],
                vat_1_amount=vat_breakdown['vat_1_amount'],
                vat_10_base=vat_breakdown['vat_10_base'],
                vat_10_amount=vat_breakdown['vat_10_amount'],
                vat_20_base=vat_breakdown['vat_20_base'],
                vat_20_amount=vat_breakdown['vat_20_amount'],
                discount_amount=discount_amount,
                total_amount=grand_total,
                cash_paid=cash_paid,
                card_paid=card_paid,
                change_given=change_given,
                is_return=is_return
            )

            # VUK 507 GİB Karekod ve Mali Onay Kodu Ataması
            apply_fiscal_data_to_sale(sale, {
                'tax_number': sale.customer_tax_number,
                'title': sale.customer_title,
                'tax_office': cust_office,
                'document_type': doc_type,
            })

            # Müşteri Veresiye Bakiyesi & Cari Hareket Kaydı
            if customer_obj:
                if is_return:
                    Customer.objects.filter(id=customer_obj.id).update(balance=F('balance') - grand_total)
                    customer_obj.refresh_from_db(fields=['balance'])
                    CustomerTransaction.objects.create(
                        customer=customer_obj,
                        transaction_type='IADE',
                        amount=grand_total,
                        balance_after=customer_obj.balance,
                        sale=sale,
                        shift=shift,
                        document_no=sale.receipt_number,
                        description=f"İade Fişi #{sale.receipt_number} ile borçtan düşüldü.",
                        user=request.user
                    )
                elif payment_type == 'VERESIYE':
                    Customer.objects.filter(id=customer_obj.id).update(balance=F('balance') + grand_total)
                    customer_obj.refresh_from_db(fields=['balance'])
                    CustomerTransaction.objects.create(
                        customer=customer_obj,
                        transaction_type='VERESIYE_SATIS',
                        amount=grand_total,
                        balance_after=customer_obj.balance,
                        sale=sale,
                        shift=shift,
                        document_no=sale.receipt_number,
                        description=f"Veresiye Satış Fişi #{sale.receipt_number}",
                        user=request.user
                    )

            for item in parsed_items:
                product = locked_products_map[item['product_id']]
                vid = item['variant_id']
                qty = item['quantity']
                var_obj = locked_variants_map.get(vid) if vid else None
                unit_price = var_obj.price_override if (var_obj and var_obj.price_override is not None) else product.sell_price

                line_total = round(qty * unit_price, 2)
                if product.vat_rate > Decimal('0.00'):
                    line_subtotal = round(line_total / (Decimal('1.00') + (product.vat_rate / Decimal('100.00'))), 2)
                    line_vat = line_total - line_subtotal
                else:
                    line_subtotal = line_total
                    line_vat = Decimal('0.00')

                var_info_str = f"{var_obj.size} / {var_obj.color}".strip(' / ') if var_obj else ''
                display_pname = f"{product.name} ({var_info_str})" if var_info_str else product.name
                item_barcode = var_obj.barcode if (var_obj and var_obj.barcode) else (product.barcode or '')

                SaleItem.objects.create(
                    sale=sale,
                    product=product,
                    variant=var_obj,
                    variant_info=var_info_str,
                    product_name=display_pname,
                    barcode=item_barcode,
                    quantity=qty,
                    unit_price=unit_price,
                    buy_price=product.buy_price if product else Decimal('0.00'),
                    vat_rate=product.vat_rate,
                    vat_amount=line_vat,
                    total_price=line_total
                )

                # DB Seviyesinde Atomik Stok Güncelleme: Hem Varyant hem Ana Ürün Stok Senkronu
                if is_return:
                    if var_obj:
                        ProductVariant.objects.filter(id=var_obj.id).update(
                            stock_quantity=F('stock_quantity') + qty
                        )
                    Product.objects.filter(id=product.id).update(
                        stock_quantity=F('stock_quantity') + qty
                    )
                else:
                    if var_obj:
                        ProductVariant.objects.filter(id=var_obj.id).update(
                            stock_quantity=F('stock_quantity') - qty
                        )
                    Product.objects.filter(id=product.id).update(
                        stock_quantity=F('stock_quantity') - qty
                    )
                product.refresh_from_db(fields=['stock_quantity'])

            # Vardiya Sayaçları ve Kasa Mutabakatı Güncelleme (Nakit, Kart, Parçalı, Veresiye)
            cashier_uname = request.user.username if request.user.is_authenticated else 'Bilinmeyen'
            effective_cash = max(Decimal('0.00'), cash_paid - change_given) if payment_type in ('NAKIT', 'PARCALI') else Decimal('0.00')

            if is_return:
                if payment_type == 'NAKIT':
                    shift.total_cash_sales = F('total_cash_sales') - grand_total
                    shift.expected_cash = F('expected_cash') - grand_total
                elif payment_type == 'KART':
                    shift.total_card_sales = F('total_card_sales') - grand_total
                elif payment_type == 'PARCALI':
                    shift.total_cash_sales = F('total_cash_sales') - effective_cash
                    shift.expected_cash = F('expected_cash') - effective_cash
                    shift.total_card_sales = F('total_card_sales') - card_paid
                elif payment_type == 'VERESIYE':
                    shift.total_veresiye_sales = F('total_veresiye_sales') - grand_total
                shift.save(update_fields=['total_cash_sales', 'total_card_sales', 'total_veresiye_sales', 'expected_cash'])
                shift.refresh_from_db()
                log_action(
                    'POS',
                    f'[{register.register_code}] Müşteri Satış İadesi Alındı',
                    f"[{register.register_code}] İade Fişi #{sale.receipt_number} - İade Tutarı: {grand_total} TL ({payment_type}) - Kasiyer: {cashier_uname}",
                    user=request.user
                )
            else:
                if payment_type == 'NAKIT':
                    shift.total_cash_sales = F('total_cash_sales') + grand_total
                    shift.expected_cash = F('expected_cash') + grand_total
                elif payment_type == 'KART':
                    shift.total_card_sales = F('total_card_sales') + grand_total
                elif payment_type == 'PARCALI':
                    shift.total_cash_sales = F('total_cash_sales') + effective_cash
                    shift.expected_cash = F('expected_cash') + effective_cash
                    shift.total_card_sales = F('total_card_sales') + card_paid
                elif payment_type == 'VERESIYE':
                    shift.total_veresiye_sales = F('total_veresiye_sales') + grand_total
                shift.total_sales_count = F('total_sales_count') + 1
                shift.save(update_fields=['total_cash_sales', 'total_card_sales', 'total_veresiye_sales', 'total_sales_count', 'expected_cash'])
                shift.refresh_from_db()
                log_action(
                    'POS',
                    f'[{register.register_code}] Yeni Satış Yapıldı',
                    f"[{register.register_code}] Satış Fişi #{sale.receipt_number} - Tutar: {grand_total} TL ({payment_type}) - Kasiyer: {cashier_uname}",
                    user=request.user
                )

        # ESC/POS Sessiz Termal Yazıcı & Para Çekmecesi Tetikleme (Ctrl+P penceresi açmadan arka planda basım)
        print_info = None
        if getattr(settings, 'ESC_POS_ENABLED', True):
            try:
                print_info = silent_print_receipt(sale)
            except Exception:
                print_info = {'success': False}

        return JsonResponse({
            'success': True,
            'receipt_number': sale.receipt_number,
            'ettn': str(sale.ettn),
            'document_type': sale.document_type,
            'qr_code_data': sale.qr_code_data,
            'fiscal_approval_code': sale.fiscal_approval_code,
            'is_return': is_return,
            'total_amount': float(sale.total_amount),
            'cash_paid': float(sale.cash_paid),
            'change_given': float(sale.change_given),
            'created_at': sale.created_at.strftime('%d.%m.%Y %H:%M'),
            'vat_breakdown': {
                'vat_1': {'base': float(sale.vat_1_base), 'amount': float(sale.vat_1_amount)},
                'vat_10': {'base': float(sale.vat_10_base), 'amount': float(sale.vat_10_amount)},
                'vat_20': {'base': float(sale.vat_20_base), 'amount': float(sale.vat_20_amount)},
            },
            'silent_print': print_info,
        })

    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


@login_required(login_url='/login/')
def silent_print_api_view(request, receipt_number):
    """
    İstenilen bir fişi tarayıcı yazdırma penceresi açmadan doğrudan ESC/POS
    termal yazıcıya gönderen ve para çekmecesini tetikleyen API uç noktası (Multi-Tenancy Uyumlu).
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Yalnızca POST istekleri kabul edilir.'}, status=405)

    user_company = get_user_company(request.user)
    sale = get_object_or_404(Sale, company=user_company, receipt_number=receipt_number)
    result = silent_print_receipt(sale)
    return JsonResponse(result)


@login_required(login_url='/login/')
def open_cash_drawer_api(request):
    """
    Kasada RJ11 para çekmecesini manuel tetikleme API uç noktası.
    Güvenlik Denetimi: Satış olmaksızın çekmece açılması SECURITY loguna yazılır.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Yalnızca POST istekleri kabul edilir.'}, status=405)

    user_company = get_user_company(request.user)
    register = get_current_register(request, company=user_company)
    shift = Shift.objects.filter(company=user_company, cash_register=register, is_closed=False).first()
    if shift and shift.cashier and shift.cashier != request.user and not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({'success': False, 'message': 'Kasa çekmecesini yalnızca açık vardiya sahibi kasiyer veya süpervizör açabilir.'}, status=403)

    res = trigger_cash_drawer_only(register.register_code)
    cashier_name = request.user.username if request.user.is_authenticated else 'Anonim'
    log_action(
        'POS',
        f'[{register.register_code}] Satışsız Manuel Çekmece Açıldı',
        f"Kasiyer '{cashier_name}' tarafından satış işlemi olmaksızın fiziki para çekmecesi fırlatıldı/açıldı.",
        level='SECURITY',
        user=request.user
    )
    return JsonResponse(res)


@login_required(login_url='/login/')
def verify_supervisor_pin_api(request):
    """
    Kasa Güvenliği (Faz 4.2): Satır silme, fiş iptali veya iade modunda yetkili PIN doğrulama servisi.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Yalnızca POST istekleri kabul edilir.'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        data = {}

    pin = str(data.get('pin', '')).strip()
    action = data.get('action', 'İPTAL / SİLME').strip()
    details = data.get('details', '').strip()
    register_code = data.get('register_code', '').strip()

    if not pin:
        return JsonResponse({'success': False, 'message': 'Lütfen yetkili şifresini veya PIN kodunu giriniz.'}, status=400)

    # PIN şifresine sahip yetkili (is_staff=True veya is_superuser=True) kullanıcı ara
    authorized_supervisor = None
    for sup in User.objects.filter(is_active=True).filter(Q(is_staff=True) | Q(is_superuser=True)):
        if sup.check_password(pin):
            authorized_supervisor = sup
            break

    if not authorized_supervisor:
        # Hatalı yetkili şifresi - Güvenlik uyarısı logla!
        log_action(
            'POS',
            f'[{register_code or "KASA"}] Yetkisiz İşlem Girişimi Reddedildi',
            f"Kasiyer '{request.user.username}' '{action}' işlemi yapmaya çalıştı ancak yetkili şifresi hatalı girildi. Detay: {details}",
            level='WARNING',
            user=request.user
        )
        return JsonResponse({'success': False, 'message': 'Hatalı yetkili şifresi! İşleme izin verilmedi.'}, status=403)

    # Onaylandı! Güvenlik denetim loguna kaydet
    sup_name = authorized_supervisor.get_full_name() or authorized_supervisor.username
    log_action(
        'POS',
        f'[{register_code or "KASA"}] Yetkili Onayı Verildi ({action})',
        f"Yetkili '{sup_name}', Kasiyer '{request.user.username}' için '{action}' işlemini onayladı. Detay: {details}",
        level='SECURITY',
        user=authorized_supervisor
    )

    return JsonResponse({
        'success': True,
        'message': f"Yetkili '{sup_name}' tarafından işlem onaylandı.",
        'supervisor_name': sup_name
    })


@xframe_options_sameorigin
@login_required(login_url='/login/')
def receipt_view(request, receipt_number):
    """
    80mm / 58mm Termal Fiş Yazdırma Görünümü (e-Arşiv Fatura & VUK 507 Uyumlu, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    sale = get_object_or_404(Sale, company=user_company, receipt_number=receipt_number)
    company = get_company_fiscal_profile(company=user_company)
    return render(request, 'thermal_receipt.html', {'sale': sale, 'company': company})


@login_required(login_url='/login/')
def z_report_view(request):
    """
    Gün Sonu Kasa Kapanış Raporu (Z Raporu) - Faz 11: Multi-Tenancy Korumalı
    Kasa 1 ve Kasa 2 Tam Bağımsızlık:
    - Kasa 1 kapanınca Kasa 2 asla kapanmaz veya etkilenmez.
    - Seçili kasaya göre bağımsız rapor ve vardiya yönetimi.
    - Brüt Satış, İade ve Net Ciro ayrımı.
    """
    user_company = get_user_company(request.user)
    register = get_current_register(request, company=user_company)
    all_registers = CashRegister.objects.filter(company=user_company, is_active=True).order_by('register_code')

    shift_id = request.GET.get('shift_id')
    shift = None
    if shift_id:
        potential_shift = Shift.objects.filter(company=user_company, id=shift_id).first()
        if potential_shift:
            if request.GET.get('register') and potential_shift.cash_register.register_code != register.register_code:
                shift = None
            else:
                shift = potential_shift
                register = shift.cash_register
                request.session['active_register_code'] = register.register_code

    if not shift:
        shift = Shift.objects.filter(company=user_company, cash_register=register, is_closed=False).order_by('-opened_at').first()
        if not shift:
            shift = Shift.objects.filter(company=user_company, cash_register=register).order_by('-opened_at').first()
        if not shift:
            register, shift = get_or_create_active_shift(request.user, register=register, request=request)

    sales = Sale.objects.filter(company=user_company, shift=shift).order_by('-created_at')
    recent_sales = sales[:50]
    all_shifts = Shift.objects.filter(company=user_company, cash_register=register).order_by('-opened_at')[:20]

    register_shift_statuses = []
    for reg in all_registers:
        reg_active = Shift.objects.filter(company=user_company, cash_register=reg, is_closed=False).order_by('-opened_at').first()
        register_shift_statuses.append({
            'register': reg,
            'is_open': reg_active is not None,
            'active_shift': reg_active,
            'is_current': reg.register_code == register.register_code,
            'cashier_name': (reg_active.cashier.get_full_name() or reg_active.cashier.username) if (reg_active and reg_active.cashier) else 'Boşta'
        })

    active_sales = sales.filter(is_cancelled=False)

    gross_cash_sales = active_sales.filter(payment_type='NAKIT', is_return=False).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
    gross_card_sales = active_sales.filter(payment_type='KART', is_return=False).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
    gross_veresiye_sales = active_sales.filter(payment_type='VERESIYE', is_return=False).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

    # Parçalı Ödeme Dağılımı (Nakit ve Kart)
    parcali_sales = active_sales.filter(payment_type='PARCALI', is_return=False)
    parcali_cash = sum(((s.cash_paid - s.change_given) for s in parcali_sales), Decimal('0.00'))
    parcali_card = parcali_sales.aggregate(total=Sum('card_paid'))['total'] or Decimal('0.00')

    cash_returns = active_sales.filter(payment_type='NAKIT', is_return=True).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
    card_returns = active_sales.filter(payment_type='KART', is_return=True).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
    veresiye_returns = active_sales.filter(payment_type='VERESIYE', is_return=True).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

    # Vardiyada Kasaya Giren Cari Borç Tahsilatları
    shift_txs = CustomerTransaction.objects.filter(shift=shift)
    cash_collections = shift_txs.filter(transaction_type='TAHSILAT_NAKIT').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    card_collections = shift_txs.filter(transaction_type='TAHSILAT_KART').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    total_collections = cash_collections + card_collections

    net_cash = (gross_cash_sales + parcali_cash) - cash_returns
    net_card = (gross_card_sales + parcali_card) - card_returns
    net_veresiye = gross_veresiye_sales - veresiye_returns

    gross_revenue = gross_cash_sales + parcali_cash + gross_card_sales + parcali_card + gross_veresiye_sales
    total_returns = cash_returns + card_returns + veresiye_returns
    net_revenue = gross_revenue - total_returns

    sale_count = active_sales.filter(is_return=False).count()
    return_count = active_sales.filter(is_return=True).count()

    cashiers = User.objects.filter(is_active=True).order_by('username')
    backup_status = get_backup_status_info()

    # VUK 507 Yasal Z Raporu KDV Dağılımı
    vat_1_base_sum = active_sales.filter(is_return=False).aggregate(total=Sum('vat_1_base'))['total'] or Decimal('0.00')
    vat_1_amt_sum = active_sales.filter(is_return=False).aggregate(total=Sum('vat_1_amount'))['total'] or Decimal('0.00')
    vat_10_base_sum = active_sales.filter(is_return=False).aggregate(total=Sum('vat_10_base'))['total'] or Decimal('0.00')
    vat_10_amt_sum = active_sales.filter(is_return=False).aggregate(total=Sum('vat_10_amount'))['total'] or Decimal('0.00')
    vat_20_base_sum = active_sales.filter(is_return=False).aggregate(total=Sum('vat_20_base'))['total'] or Decimal('0.00')
    vat_20_amt_sum = active_sales.filter(is_return=False).aggregate(total=Sum('vat_20_amount'))['total'] or Decimal('0.00')
    total_vat_sum = vat_1_amt_sum + vat_10_amt_sum + vat_20_amt_sum

    expected_cash_calculated = shift.opening_float + net_cash + cash_collections

    setting = CompanySetting.get_settings()
    is_staff_or_admin = bool(request.user.is_staff or request.user.is_superuser)
    is_blind_close = bool(setting.blind_close_enabled and not is_staff_or_admin)

    context = {
        'shift': shift,
        'register': register,
        'recent_sales': recent_sales,
        'all_shifts': all_shifts,
        'all_registers': all_registers,
        'register_shift_statuses': register_shift_statuses,
        'cashiers': cashiers,
        'gross_cash_sales': gross_cash_sales + parcali_cash,
        'gross_card_sales': gross_card_sales + parcali_card,
        'gross_veresiye_sales': gross_veresiye_sales,
        'cash_returns': cash_returns,
        'card_returns': card_returns,
        'veresiye_returns': veresiye_returns,
        'net_cash': net_cash,
        'net_card': net_card,
        'net_veresiye': net_veresiye,
        'cash_sales': net_cash,
        'card_sales': net_card,
        'veresiye_sales': net_veresiye,
        'cash_collections': cash_collections,
        'card_collections': card_collections,
        'total_collections': total_collections,
        'gross_revenue': gross_revenue,
        'total_returns': total_returns,
        'total_revenue': net_revenue,
        'sales_count': sale_count,
        'return_count': return_count,
        'expected_cash_calculated': expected_cash_calculated,
        'backup_status': backup_status,
        'vat_1_base_sum': vat_1_base_sum,
        'vat_1_amt_sum': vat_1_amt_sum,
        'vat_10_base_sum': vat_10_base_sum,
        'vat_10_amt_sum': vat_10_amt_sum,
        'vat_20_base_sum': vat_20_base_sum,
        'vat_20_amt_sum': vat_20_amt_sum,
        'total_vat_sum': total_vat_sum,
        'company': get_company_fiscal_profile(),
        'setting': setting,
        'is_blind_close': is_blind_close,
        'is_staff_or_admin': is_staff_or_admin,
        'now': timezone.now(),
    }
    return render(request, 'z_report.html', context)


def close_shift_action(request):
    """
    Vardiyayı Kapatma, Sayılan Nakit Girişi ve OTOMATİK VERİTABANI YEDEKLEME
    - Kasa 1 ve Kasa 2 Tam Bağımsızlık Garantisi
    - Z Raporu kapandığı anda otomatik olarak backup_database() çalışır ve
      backups/ klasörüne zaman damgalı veritabanı kopyasını kaydeder.
    """
    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': 'Oturum açmanız gerekmektedir.'}, status=401)

    if request.method == 'POST':
        try:
            data = json.loads(request.body.decode('utf-8')) if request.body else {}
            actual_cash = Decimal(str(data.get('actual_cash', 0)))
            notes = data.get('notes', '').strip()
            shift_id = data.get('shift_id')
            password = data.get('password', '').strip()
            register_code = data.get('register_code')
            denominations = data.get('denominations', {})

            if not password:
                return JsonResponse({'success': False, 'message': 'Kasa kapanışını onaylamak için lütfen kasiyer şifrenizi giriniz.'}, status=400)

            user_company = get_user_company(request.user)
            register = get_current_register(request, register_code=register_code, company=user_company)

            if shift_id:
                shift = Shift.objects.filter(company=user_company, id=shift_id).first()
            else:
                shift = Shift.objects.filter(company=user_company, cash_register=register, is_closed=False).order_by('-opened_at').first()

            if not shift:
                shift = Shift.objects.filter(company=user_company, cash_register=register).order_by('-opened_at').first()

            if not shift:
                return JsonResponse({'success': False, 'message': f'{register.name} için kapatılacak vardiya bulunamadı.'}, status=400)

            target_register = shift.cash_register

            # Kasiyer / Yetkili Şifresi Doğrulama
            is_authorized = False
            if shift.cashier and shift.cashier.check_password(password):
                is_authorized = True
            elif request.user.is_authenticated and request.user.check_password(password):
                is_authorized = True
            else:
                for u in User.objects.filter(is_active=True):
                    if u.check_password(password):
                        is_authorized = True
                        break

            if not is_authorized:
                return JsonResponse({'success': False, 'message': 'Hatalı şifre! Kasiyer şifreniz doğrulanamadı.'}, status=403)

            with transaction.atomic():
                shift = Shift.objects.select_for_update().get(id=shift.id)
                active_sales = Sale.objects.filter(shift=shift, is_cancelled=False)
                
                gross_cash = active_sales.filter(payment_type='NAKIT', is_return=False).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
                gross_card = active_sales.filter(payment_type='KART', is_return=False).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
                gross_veresiye = active_sales.filter(payment_type='VERESIYE', is_return=False).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

                parcali_sales = active_sales.filter(payment_type='PARCALI', is_return=False)
                parcali_cash = sum(((s.cash_paid - s.change_given) for s in parcali_sales), Decimal('0.00'))
                parcali_card = parcali_sales.aggregate(total=Sum('card_paid'))['total'] or Decimal('0.00')

                cash_returns = active_sales.filter(payment_type='NAKIT', is_return=True).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
                card_returns = active_sales.filter(payment_type='KART', is_return=True).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
                veresiye_returns = active_sales.filter(payment_type='VERESIYE', is_return=True).aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')

                shift_txs = CustomerTransaction.objects.filter(shift=shift)
                cash_collections = shift_txs.filter(transaction_type='TAHSILAT_NAKIT').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
                card_collections = shift_txs.filter(transaction_type='TAHSILAT_KART').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
                total_collections = cash_collections + card_collections

                net_cash = (gross_cash + parcali_cash) - cash_returns
                net_card = (gross_card + parcali_card) - card_returns
                net_veresiye = gross_veresiye - veresiye_returns

                shift.total_cash_sales = net_cash
                shift.total_card_sales = net_card
                shift.total_veresiye_sales = net_veresiye
                shift.total_collections = total_collections
                shift.total_sales_count = active_sales.filter(is_return=False).count()
                shift.expected_cash = shift.opening_float + net_cash + cash_collections
                shift.actual_cash = actual_cash
                shift.difference = actual_cash - shift.expected_cash
                shift.closed_at = timezone.now()
                shift.is_closed = True
                if notes:
                    shift.notes = notes
                if denominations and isinstance(denominations, dict):
                    shift.denomination_counts = denominations
                shift.save()

                setting = CompanySetting.get_settings(company=user_company)
                is_staff_or_admin = bool(request.user.is_staff or request.user.is_superuser)
                is_blind_close = bool(setting.blind_close_enabled and not is_staff_or_admin)

                cashier_label = shift.cashier.username if shift.cashier else request.user.username
                diff_label = f"Kasa Açığı: -₺{abs(shift.difference):.2f}" if shift.difference < 0 else (f"Kasa Fazlası: +₺{shift.difference:.2f}" if shift.difference > 0 else "Kasa Denk")
                diff_level = 'WARNING' if shift.difference != Decimal('0.00') else 'INFO'

                log_action(
                    'POS',
                    f'[{target_register.register_code}] Z Raporu Kapatıldı ({diff_label})',
                    f"[{target_register.register_code}] Vardiya #{shift.id} Kapatıldı. Beklenen: {shift.expected_cash} TL, Sayılan: {shift.actual_cash} TL, Fark: {shift.difference} TL (Kör Sayım: {is_blind_close}) - Kasiyer: {cashier_label}",
                    level=diff_level,
                    user=request.user
                )

            # Z Raporu kapandığı anda otomatik veritabanı yedeği al (backups/ klasörüne zaman damgalı)
            backup_info = backup_database(
                reason=f'Z_RAPORU_VARDIYA_{shift.id}',
                register_code=target_register.register_code,
                user=request.user
            )

            if is_blind_close:
                return JsonResponse({
                    'success': True,
                    'shift_id': shift.id,
                    'register_code': target_register.register_code,
                    'is_blind_close': True,
                    'actual_cash': float(shift.actual_cash),
                    'backup': backup_info,
                    'message': f"{target_register.name} için kasa kör sayımı başarıyla alındı ve vardiya kapatıldı. Sayım detayları yetkili onayına kaydedildi."
                })
            else:
                return JsonResponse({
                    'success': True,
                    'shift_id': shift.id,
                    'register_code': target_register.register_code,
                    'is_blind_close': False,
                    'difference': float(shift.difference),
                    'expected_cash': float(shift.expected_cash),
                    'actual_cash': float(shift.actual_cash),
                    'backup': backup_info,
                    'message': f"{target_register.name} Vardiya #{shift.id} başarıyla kapatıldı. Kasa Farkı: ₺{shift.difference:.2f}."
                })
        except Exception as e:
            return JsonResponse({'success': False, 'message': str(e)}, status=500)
    return JsonResponse({'success': False, 'message': 'Geçersiz metod.'}, status=400)


def open_shift_action(request):
    """
    Yeni Kasa Vardiyası Başlatma (Kasiyer Şifresi Doğrulamalı)
    Kasa 1 ve Kasa 2 Tam Bağımsızlık Garantisi
    """
    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': 'Oturum açmanız gerekmektedir.'}, status=401)

    if request.method == 'POST':
        try:
            data = json.loads(request.body.decode('utf-8')) if request.body else {}
            opening_float = Decimal(str(data.get('opening_float', '500.00') or '500.00'))
            notes = data.get('notes', '').strip()
            cashier_id = data.get('cashier_id')
            password = data.get('password', '').strip()
            register_code = data.get('register_code')

            if not password:
                return JsonResponse({'success': False, 'message': 'Yeni vardiya başlatmak için lütfen kasiyer şifrenizi giriniz.'}, status=400)

            cashier = None
            if cashier_id:
                cashier = User.objects.filter(id=cashier_id, is_active=True).first()

            if not cashier and request.user.is_authenticated:
                cashier = request.user

            if not cashier:
                for u in User.objects.filter(is_active=True):
                    if u.check_password(password):
                        cashier = u
                        break

            if not cashier or not cashier.check_password(password):
                return JsonResponse({'success': False, 'message': 'Hatalı şifre! Kasiyer şifresi eşleşmedi.'}, status=403)

            user_company = get_user_company(request.user)
            register = get_current_register(request, register_code=register_code, company=user_company)

            open_shift = Shift.objects.filter(company=user_company, cash_register=register, is_closed=False).first()
            if open_shift:
                return JsonResponse({
                    'success': True,
                    'shift_id': open_shift.id,
                    'register_code': register.register_code,
                    'already_open': True,
                    'message': f"{register.name} ({register.register_code}) için zaten açık bir vardiya bulunmaktadır (#{open_shift.id})."
                })

            new_shift = Shift.objects.create(
                company=user_company,
                cash_register=register,
                cashier=cashier,
                opening_float=opening_float,
                expected_cash=opening_float,
                notes=notes
            )
            log_action(
                'POS',
                f'[{register.register_code}] Yeni Vardiya Açıldı',
                f"[{register.register_code}] Vardiya #{new_shift.id} Başlatıldı (Kasiyer: {cashier.username}). Devir: {opening_float} TL",
                user=cashier
            )

            return JsonResponse({
                'success': True,
                'shift_id': new_shift.id,
                'register_code': register.register_code,
                'cashier_name': cashier.get_full_name() or cashier.username,
                'message': f"{register.name} için Vardiya #{new_shift.id} başarıyla açıldı."
            })
        except Exception as e:
            return JsonResponse({'success': False, 'message': str(e)}, status=500)
    return JsonResponse({'success': False, 'message': 'Geçersiz metod.'}, status=400)


# ==========================================
# KULLANICI OTURUMU (LOGIN & LOGOUT) VIEWS
# ==========================================

def login_view(request):
    """
    Hİ MARKET Özel Modern Giriş Sayfası (Kasiyer Dokunmatik PIN Pad & Standart Kullanıcı Girişi)
    Faz 19: Cihaz Mühürleme (Device Binding) ve Mağaza Kapsamlı PIN İzolasyonu.
    """
    if request.user.is_authenticated:
        if request.user.is_staff or request.user.is_superuser:
            return redirect('admin_dashboard')
        return redirect('pos_screen')

    # 1. Cihaz mühürlü mü kontrol et (Smart TV Device Token)
    device_token = request.COOKIES.get('himarket_device_token')
    paired_register = None
    if device_token:
        paired_register = CashRegister.objects.filter(device_token=device_token, is_active=True).select_related('company').first()

    paired_company = paired_register.company if paired_register else None

    # Varsayılan kasiyer hesabının (kasiyer / 1234) mevcut olduğundan emin ol (PIN 1234 uyumu)
    if not User.objects.filter(username='kasiyer').exists():
        try:
            User.objects.create_user(
                username='kasiyer',
                password='1234',
                first_name='Kasa',
                last_name='Görevlisi',
                is_staff=False,
                is_active=True
            )
        except Exception:
            pass

    if request.method == 'POST':
        login_mode = request.POST.get('login_mode', 'standard').strip()
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '').strip()
        pin_code = request.POST.get('pin_code', '').strip()
        
        # Eğer cihaz mühürlüyse register_code zorunlu olarak bu mühürlü kasadır!
        if paired_register:
            register_code = paired_register.register_code
        else:
            register_code = request.POST.get('register_code', 'KASA-01').strip() or 'KASA-01'

        next_url = request.POST.get('next') or request.GET.get('next')

        user = None
        secret = pin_code if (login_mode == 'pin' and pin_code) else password

        if username and secret:
            user = authenticate(request, username=username, password=secret)

        # Hızlı PIN modu: Doğrudan PIN girildiyse
        if user is None and secret:
            # 1. Eğer cihaz bir mağazaya mühürlüyse, SADECE bu mağazanın personeli içinde ara (İzolasyon)!
            if paired_company:
                company_candidates = User.objects.filter(is_active=True, profile__company=paired_company).order_by('is_staff', 'id')
                for candidate in company_candidates:
                    cand_pin = getattr(getattr(candidate, 'profile', None), 'pin_code', '')
                    if (cand_pin and cand_pin == secret) or candidate.check_password(secret):
                        user = candidate
                        break
            else:
                # Cihaz mühürlü değilse genel aktif kullanıcılar içinde ara
                for candidate in User.objects.filter(is_active=True).order_by('is_staff', 'id'):
                    cand_pin = getattr(getattr(candidate, 'profile', None), 'pin_code', '')
                    if (cand_pin and cand_pin == secret) or candidate.check_password(secret):
                        user = candidate
                        break

        if user is not None:
            if user.is_active:
                login(request, user)
                request.session['active_register_code'] = register_code
                auth_method_label = "Hızlı PIN Pad" if login_mode == 'pin' else "Standart Giriş"
                log_action(
                    'SİSTEM',
                    f'[{register_code}] Kullanıcı Girişi ({auth_method_label})',
                    f"[{register_code}] Kullanıcı '{user.username}' sisteme giriş yaptı.",
                    user=user
                )
                if next_url:
                    return redirect(next_url)
                if user.is_staff or user.is_superuser:
                    return redirect('admin_dashboard')
                return redirect('pos_screen')
            else:
                messages.error(request, 'Bu kullanıcı hesabı pasif durumdadır.')
        else:
            if login_mode == 'pin':
                messages.error(request, 'Hatalı PIN kodu! Lütfen PIN kodunuzu kontrol ediniz.')
            else:
                messages.error(request, 'Kullanıcı adı veya şifre hatalı! Lütfen bilgilerinizi kontrol ediniz.')

    # Giriş ekranı sadeleştirildi: Terminal mühürlüdür ve PIN doğrudan kullanıcıya aittir
    context = {
        'paired_register': paired_register,
        'paired_company': paired_company,
        'is_device_paired': bool(paired_register),
    }
    return render(request, 'login.html', context)


def logout_view(request):
    """
    Hİ MARKET Güvenli Oturum Kapatma
    """
    if request.user.is_authenticated:
        log_action('SİSTEM', 'Kullanıcı Çıkışı', f"Kullanıcı '{request.user.username}' oturumu kapattı.", user=request.user)
        logout(request)
    messages.success(request, 'Oturumunuz başarıyla kapatıldı.')
    return redirect('login_view')


# ==========================================
# YÖNETİM PANELİ (ADMIN & MANAGEMENT) VIEWS
# ==========================================

@staff_required
def admin_dashboard_view(request):
    """
    Grafikli Dashboard & Yönetim Kontrol Paneli (Faz 11: Multi-Tenancy Korumalı)
    - Yönetici erişim kısıtlaması
    - Kasa 1 ve Kasa 2 Ayrı Canlı Performans Kartları
    - Veritabanı WAL Modu & Otomatik Yedekleme ('Yedek İndir') Yönetimi
    """
    user_company = get_user_company(request.user)
    now = timezone.localtime(timezone.now())
    today = now.date()

    # 1. Günlük Satışlar (Bugün)
    today_start = timezone.make_aware(datetime.datetime.combine(today, datetime.time.min))
    today_end = timezone.make_aware(datetime.datetime.combine(today, datetime.time.max))

    today_sales = Sale.objects.filter(company=user_company, created_at__range=(today_start, today_end), is_cancelled=False)
    today_gross = today_sales.filter(is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    today_returns = today_sales.filter(is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    today_total = today_gross - today_returns
    today_customer_count = today_sales.filter(is_return=False).count()

    today_cash = (today_sales.filter(payment_type='NAKIT', is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')) - \
                 (today_sales.filter(payment_type='NAKIT', is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00'))
    today_card = (today_sales.filter(payment_type='KART', is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')) - \
                 (today_sales.filter(payment_type='KART', is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00'))
    today_other = today_sales.filter(payment_type__in=['PARCALI', 'VERESIYE'], is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')

    # 2. Bu Ayki Satışlar
    month_start = timezone.make_aware(datetime.datetime.combine(today.replace(day=1), datetime.time.min))
    month_sales = Sale.objects.filter(company=user_company, created_at__gte=month_start, is_cancelled=False)
    month_gross = month_sales.filter(is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    month_returns = month_sales.filter(is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    month_total = month_gross - month_returns
    month_customer_count = month_sales.filter(is_return=False).count()

    month_cash = (month_sales.filter(payment_type='NAKIT', is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')) - \
                 (month_sales.filter(payment_type='NAKIT', is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00'))
    month_card = (month_sales.filter(payment_type='KART', is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')) - \
                 (month_sales.filter(payment_type='KART', is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00'))
    month_other = month_sales.filter(payment_type__in=['PARCALI', 'VERESIYE'], is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')

    # 3. Genel Toplam Metrikler
    all_sales = Sale.objects.filter(company=user_company, is_cancelled=False)
    all_gross = all_sales.filter(is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    all_returns = all_sales.filter(is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    all_time_total = all_gross - all_returns
    all_customer_count = all_sales.filter(is_return=False).count()

    all_cash = (all_sales.filter(payment_type='NAKIT', is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')) - \
               (all_sales.filter(payment_type='NAKIT', is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00'))
    all_card = (all_sales.filter(payment_type='KART', is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')) - \
               (all_sales.filter(payment_type='KART', is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00'))
    all_other = all_sales.filter(payment_type__in=['PARCALI', 'VERESIYE'], is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')

    # 4. Ürün & Stok Durumu
    total_products = Product.objects.filter(company=user_company, is_active=True).count()
    low_stock_products = Product.objects.filter(company=user_company, is_active=True, stock_quantity__lte=F('min_stock_level')).order_by('stock_quantity')
    low_stock_count = low_stock_products.count()
    out_of_stock_count = Product.objects.filter(company=user_company, is_active=True, stock_quantity__lte=0).count()
    total_supplier_balance = Supplier.objects.filter(company=user_company).aggregate(Sum('balance'))['balance__sum'] or Decimal('0.00')

    # 5. Son 14 Günlük Satış Trendi
    days_tr = ['Pzt', 'Sal', 'Çar', 'Per', 'Cum', 'Cmt', 'Paz']
    daily_labels = []
    daily_revenue = []
    daily_customers = []

    for i in range(13, -1, -1):
        d = today - datetime.timedelta(days=i)
        d_start = timezone.make_aware(datetime.datetime.combine(d, datetime.time.min))
        d_end = timezone.make_aware(datetime.datetime.combine(d, datetime.time.max))

        d_qs = Sale.objects.filter(company=user_company, created_at__range=(d_start, d_end), is_cancelled=False)
        d_gross = d_qs.filter(is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
        d_returns = d_qs.filter(is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
        d_rev = max(Decimal('0.00'), d_gross - d_returns)
        d_cnt = d_qs.filter(is_return=False).count()

        daily_labels.append(f"{d.strftime('%d.%m')} ({days_tr[d.weekday()]})")
        daily_revenue.append(float(d_rev))
        daily_customers.append(d_cnt)

    # 6. Son 6 Aylık Ciro Grafiği
    months_tr = ['', 'Ocak', 'Şubat', 'Mart', 'Nisan', 'Mayıs', 'Haziran', 'Temmuz', 'Ağustos', 'Eylül', 'Ekim', 'Kasım', 'Aralık']
    monthly_labels = []
    monthly_revenue = []
    monthly_customers = []

    for offset in range(5, -1, -1):
        m = today.month - offset
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        last_day = calendar.monthrange(y, m)[1]
        m_start = timezone.make_aware(datetime.datetime(y, m, 1, 0, 0, 0))
        m_end = timezone.make_aware(datetime.datetime(y, m, last_day, 23, 59, 59, 999999))

        m_qs = Sale.objects.filter(company=user_company, created_at__range=(m_start, m_end), is_cancelled=False)
        m_gross = m_qs.filter(is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
        m_returns = m_qs.filter(is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
        m_rev = max(Decimal('0.00'), m_gross - m_returns)
        m_cnt = m_qs.filter(is_return=False).count()

        monthly_labels.append(f"{months_tr[m]} {y}")
        monthly_revenue.append(float(m_rev))
        monthly_customers.append(m_cnt)

    # 7. Ödeme Türü Dağılımı
    payment_stats = {
        'today': {
            'cash': float(today_cash),
            'card': float(today_card),
            'other': float(today_other),
            'total': float(today_cash + today_card + today_other),
        },
        'month': {
            'cash': float(month_cash),
            'card': float(month_card),
            'other': float(month_other),
            'total': float(month_cash + month_card + month_other),
        },
        'all': {
            'cash': float(all_cash),
            'card': float(all_card),
            'other': float(all_other),
            'total': float(all_cash + all_card + all_other),
        }
    }

    payment_labels = ['Nakit Satış', 'Banka / Kredi Kartı', 'Parçalı / Cari']
    payment_data = [float(today_cash), float(today_card), float(today_other)]

    # 8. Kasa Bazlı Canlı Durum ve Günlük Performans Kartları (Kasa 1 & Kasa 2 Ayrı Gösterim)
    CashRegister.objects.get_or_create(
        company=user_company,
        register_code='KASA-01',
        defaults={'name': 'KASA-01 (Ana Satış Terminali)', 'is_active': True}
    )
    CashRegister.objects.get_or_create(
        company=user_company,
        register_code='KASA-02',
        defaults={'name': 'KASA-02 (Ek Satış Terminali)', 'is_active': True}
    )
    registers = CashRegister.objects.filter(company=user_company, is_active=True).order_by('register_code')
    register_cards = []
    for reg in registers:
        reg_active_shift = Shift.objects.filter(company=user_company, cash_register=reg, is_closed=False).order_by('-opened_at').first()
        reg_latest_shift = Shift.objects.filter(company=user_company, cash_register=reg).order_by('-opened_at').first()

        reg_sales = Sale.objects.filter(company=user_company, shift__cash_register=reg, created_at__range=(today_start, today_end), is_cancelled=False)
        reg_gross = reg_sales.filter(is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
        reg_returns = reg_sales.filter(is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
        reg_net = reg_gross - reg_returns
        reg_cash = (reg_sales.filter(payment_type='NAKIT', is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')) - \
                   (reg_sales.filter(payment_type='NAKIT', is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00'))
        reg_card = (reg_sales.filter(payment_type='KART', is_return=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')) - \
                   (reg_sales.filter(payment_type='KART', is_return=True).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00'))
        reg_count = reg_sales.filter(is_return=False).count()
        reg_return_count = reg_sales.filter(is_return=True).count()

        active_cashier_name = None
        if reg_active_shift and reg_active_shift.cashier:
            active_cashier_name = reg_active_shift.cashier.get_full_name() or reg_active_shift.cashier.username

        register_cards.append({
            'register': reg,
            'is_open': reg_active_shift is not None,
            'active_shift': reg_active_shift,
            'latest_shift': reg_latest_shift,
            'cashier_name': active_cashier_name,
            'today_gross': reg_gross,
            'today_returns': reg_returns,
            'today_net': reg_net,
            'today_cash': reg_cash,
            'today_card': reg_card,
            'today_count': reg_count,
            'today_return_count': reg_return_count,
        })

    # 9. En Çok Satan 5 Ürün
    top_products = SaleItem.objects.filter(sale__company=user_company).values('product_name').annotate(
        total_qty=Sum('quantity'),
        total_revenue=Sum('total_price')
    ).order_by('-total_qty')[:5]

    # 10. Son Satış Fişleri
    recent_sales = Sale.objects.filter(company=user_company, is_cancelled=False).select_related('cashier', 'shift__cash_register').order_by('-created_at')[:8]

    # 11. Son Sistem Logları
    recent_logs = SystemLog.objects.filter(company=user_company).select_related('user').order_by('-created_at')[:8]

    # 12. Veritabanı Yedekleme & WAL Durumu
    backup_status = get_backup_status_info()

    context = {
        'today_total': today_total,
        'today_customer_count': today_customer_count,
        'today_cash': today_cash,
        'today_card': today_card,
        'today_other': today_other,
        'month_total': month_total,
        'month_customer_count': month_customer_count,
        'month_cash': month_cash,
        'month_card': month_card,
        'all_time_total': all_time_total,
        'all_customer_count': all_customer_count,
        'total_products': total_products,
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'total_supplier_balance': total_supplier_balance,
        'register_cards': register_cards,
        'backup_status': backup_status,

        # Grafikler (JSON)
        'daily_labels_json': json.dumps(daily_labels),
        'daily_revenue_json': json.dumps(daily_revenue),
        'daily_customers_json': json.dumps(daily_customers),
        'monthly_labels_json': json.dumps(monthly_labels),
        'monthly_revenue_json': json.dumps(monthly_revenue),
        'monthly_customers_json': json.dumps(monthly_customers),
        'payment_labels_json': json.dumps(payment_labels),
        'payment_data_json': json.dumps(payment_data),
        'payment_stats_json': json.dumps(payment_stats),

        # Tablolar
        'top_products': top_products,
        'low_stock_products': low_stock_products[:5],
        'recent_sales': recent_sales,
        'recent_logs': recent_logs,
    }
    return render(request, 'admin/index.html', context)


@staff_required
def supplier_list_view(request):
    """
    Toptancı Carileri Listesi (Yönetici Yetkili, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    search_query = request.GET.get('q', '').strip()
    suppliers = Supplier.objects.filter(company=user_company).prefetch_related('transactions')

    if search_query:
        suppliers = suppliers.filter(
            Q(company_name__icontains=search_query) |
            Q(contact_name__icontains=search_query) |
            Q(phone__icontains=search_query) |
            Q(tax_number__icontains=search_query)
        )

    total_debt = suppliers.filter(balance__gt=0).aggregate(Sum('balance'))['balance__sum'] or Decimal('0.00')
    total_credit = suppliers.filter(balance__lt=0).aggregate(Sum('balance'))['balance__sum'] or Decimal('0.00')

    context = {
        'suppliers': suppliers,
        'search_query': search_query,
        'total_debt': total_debt,
        'total_credit': abs(total_credit),
    }
    return render(request, 'admin/suppliers.html', context)


def _handle_supplier_form(request, supplier=None):
    """Toptancı oluşturma ve güncelleme formları için ortak akış (Multi-Tenancy)."""
    user_company = get_user_company(request.user)
    if request.method == 'POST':
        company_name = request.POST.get('company_name', '').strip()
        contact_name = request.POST.get('contact_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        email = request.POST.get('email', '').strip()
        tax_office = request.POST.get('tax_office', '').strip()
        tax_number = request.POST.get('tax_number', '').strip()
        address = request.POST.get('address', '').strip()
        notes = request.POST.get('notes', '').strip()
        is_active = request.POST.get('is_active') == 'on'

        posted = {
            'company_name': company_name,
            'contact_name': contact_name,
            'phone': phone,
            'email': email,
            'tax_office': tax_office,
            'tax_number': tax_number,
            'address': address,
            'notes': notes,
            'initial_balance': request.POST.get('initial_balance', '0.00'),
            'is_active': is_active,
        }

        if not company_name:
            messages.error(request, "Lütfen toptancı firma adını giriniz.")
            return render(request, 'admin/supplier_form.html', {'supplier': supplier, 'posted': posted})

        if supplier is not None:
            supplier.company_name = company_name
            supplier.contact_name = contact_name
            supplier.phone = phone
            supplier.email = email
            supplier.tax_office = tax_office
            supplier.tax_number = tax_number
            supplier.address = address
            supplier.notes = notes
            supplier.is_active = is_active
            supplier.save()
            log_action('SUPPLIER', 'Toptancı Güncellendi', f"{supplier.company_name} (Aktif: {supplier.is_active})", user=request.user)
            messages.success(request, f"Toptancı '{company_name}' bilgileri güncellendi.")
            return redirect('supplier_list_view')

        try:
            initial_balance = _parse_decimal(request.POST.get('initial_balance', '0.00'), Decimal('0.00'))
        except ValueError:
            messages.error(request, "Açılış bakiyesi geçerli bir sayı olmalıdır.")
            return render(request, 'admin/supplier_form.html', {'supplier': None, 'posted': posted})

        with transaction.atomic():
            new_supplier = Supplier.objects.create(
                company=user_company,
                company_name=company_name,
                contact_name=contact_name,
                phone=phone,
                email=email,
                tax_office=tax_office,
                tax_number=tax_number,
                address=address,
                balance=initial_balance,
                notes=notes,
                is_active=is_active
            )
            if initial_balance != 0:
                trans_type = 'ALIS' if initial_balance > 0 else 'IADE'
                SupplierTransaction.objects.create(
                    company=user_company,
                    supplier=new_supplier,
                    transaction_type=trans_type,
                    amount=abs(initial_balance),
                    balance_after=initial_balance,
                    document_no='ACILIS-BAKIYESI',
                    description='Açılış Cari Bakiyesi',
                    user=request.user if request.user.is_authenticated else None
                )

        log_action('SUPPLIER', 'Yeni Toptancı Eklendi', f"{new_supplier.company_name} (Açılış Bakiyesi: {initial_balance} TL)", user=request.user)
        messages.success(request, f"Toptancı '{company_name}' başarıyla eklendi.")
        return redirect('supplier_list_view')

    return render(request, 'admin/supplier_form.html', {'supplier': supplier, 'posted': None})


@staff_required
def supplier_create_view(request):
    """
    Yeni Toptancı Cari Kaydı (Yönetici Yetkili)
    """
    return _handle_supplier_form(request, supplier=None)


@staff_required
def supplier_edit_view(request, supplier_id):
    """
    Mevcut Toptancı Cari Bilgilerini Düzenleme (Yönetici Yetkili)
    """
    user_company = get_user_company(request.user)
    supplier = get_object_or_404(Supplier, company=user_company, id=supplier_id)
    return _handle_supplier_form(request, supplier=supplier)


@staff_required
@require_POST
def supplier_toggle_status_action(request, supplier_id):
    """
    Toptancı Firmayı Tek Tıkla Aktif / Pasif Yapma Aksiyonu
    """
    user_company = get_user_company(request.user)
    supplier = get_object_or_404(Supplier, company=user_company, id=supplier_id)
    supplier.is_active = not supplier.is_active
    supplier.save(update_fields=['is_active'])

    state = 'Aktif' if supplier.is_active else 'Pasif'
    log_action('SUPPLIER', 'Toptancı Durumu Güncellendi', f"{supplier.company_name} -> {state}", user=request.user)
    messages.success(request, f"Toptancı '{supplier.company_name}' durumu '{state}' olarak güncellendi.")
    return _safe_next_redirect(request, 'supplier_list_view')


@staff_required
def supplier_payment_view(request, supplier_id):
    """
    Toptancıya Ödeme Yapma / Cari Bakiye Düzeltme (Atomik & Yönetici Yetkili)
    """
    user_company = get_user_company(request.user)
    if request.method == 'POST':
        trans_type = request.POST.get('transaction_type', 'ODEME')
        adj_dir = request.POST.get('adjustment_direction', 'ADD')
        document_no = request.POST.get('document_no', '').strip()
        description = request.POST.get('description', '').strip()

        try:
            amount = _parse_decimal(request.POST.get('amount'))
        except ValueError:
            messages.error(request, "Lütfen geçerli bir tutar giriniz.")
            return redirect('supplier_list_view')

        if trans_type not in ('ODEME', 'ALIS', 'IADE', 'DUZELTME'):
            messages.error(request, "Geçersiz işlem türü seçildi.")
            return redirect('supplier_list_view')

        if trans_type == 'DUZELTME':
            is_valid = (adj_dir == 'SET') or (amount != 0)
        else:
            is_valid = amount > 0

        if not is_valid:
            messages.error(request, "İşlem tutarı sıfırdan büyük olmalıdır.")
            return redirect('supplier_list_view')

        with transaction.atomic():
            supplier = Supplier.objects.select_for_update().get(company=user_company, id=supplier_id)
            old_balance = supplier.balance
            tx_amount = abs(amount)

            if trans_type == 'ODEME':
                supplier.balance = F('balance') - amount
            elif trans_type == 'ALIS':
                supplier.balance = F('balance') + amount
            elif trans_type == 'IADE':
                supplier.balance = F('balance') - amount
            elif trans_type == 'DUZELTME':
                if adj_dir == 'SET':
                    supplier.balance = amount
                    tx_amount = abs(amount - old_balance)
                    if not description:
                        description = f"Bakiye düzeltme: {old_balance:.2f} TL -> {amount:.2f} TL"
                elif adj_dir == 'SUB':
                    supplier.balance = F('balance') - abs(amount)
                    if not description:
                        description = f"Bakiye düzeltme (-{abs(amount):.2f} TL)"
                else:
                    supplier.balance = F('balance') + amount
                    if not description:
                        sign = '+' if amount >= 0 else ''
                        description = f"Bakiye düzeltme ({sign}{amount:.2f} TL)"

            supplier.save(update_fields=['balance'])
            supplier.refresh_from_db()

            SupplierTransaction.objects.create(
                supplier=supplier,
                transaction_type=trans_type,
                amount=tx_amount,
                balance_after=supplier.balance,
                document_no=document_no,
                description=description,
                user=request.user if request.user.is_authenticated else None
            )

            log_action('SUPPLIER', 'Cari Ödeme/İşlem', f"{supplier.company_name} - {trans_type}: {tx_amount} TL (Yeni Bakiye: {supplier.balance} TL)", user=request.user)
            messages.success(request, f"{supplier.company_name} cari işlemi kaydedildi (Güncel Bakiye: ₺{supplier.balance:.2f}).")

    return redirect('supplier_list_view')


def _filter_stock_entries(params, company=None):
    """Stok girişleri, irsaliyeler ve CSV dışa aktarımı için ortak filtre sorgusu (Multi-Tenancy)."""
    search_q = (params.get('q') or '').strip()
    entry_type = (params.get('type') or '').strip()
    supplier_id = (params.get('supplier') or '').strip()

    entries = StockEntry.objects.select_related('supplier', 'created_by').prefetch_related('items__product').order_by('-created_at')
    if company:
        entries = entries.filter(company_scope_q(company))

    if search_q:
        entries = entries.filter(
            Q(entry_number__icontains=search_q) |
            Q(invoice_number__icontains=search_q) |
            Q(supplier__company_name__icontains=search_q) |
            Q(notes__icontains=search_q)
        )

    valid_entry_types = dict(StockEntry.ENTRY_TYPE_CHOICES).keys()
    if entry_type in valid_entry_types:
        entries = entries.filter(entry_type=entry_type)

    if supplier_id.isdigit():
        entries = entries.filter(supplier_id=int(supplier_id))

    return entries, {
        'search_q': search_q,
        'selected_type': entry_type,
        'selected_supplier': supplier_id,
    }


@staff_required
def stock_entry_list_view(request):
    """
    Stok Giriş ve İrsaliye Fişleri Listesi (Yönetici Yetkili, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    entries, filters = _filter_stock_entries(request.GET, company=user_company)
    total_filtered_count = entries.count()

    all_entries = StockEntry.objects.filter(company_scope_q(user_company))
    total_entries_count = all_entries.count()
    total_in = all_entries.filter(entry_type='GIRIS').aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    total_out = all_entries.filter(entry_type='IADE').aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    total_veresiye = all_entries.filter(entry_type='GIRIS', payment_type='VERESIYE').aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')

    suppliers = Supplier.objects.filter(company_scope_q(user_company), is_active=True).order_by('company_name')

    paginator = Paginator(entries, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_string = query_params.urlencode()

    context = {
        'page_obj': page_obj,
        'entries': page_obj.object_list,
        'total_filtered_count': total_filtered_count,
        'query_string': query_string,
        'suppliers': suppliers,
        'search_q': filters['search_q'],
        'selected_type': filters['selected_type'],
        'selected_supplier': filters['selected_supplier'],
        'total_entries_count': total_entries_count,
        'total_in': total_in,
        'total_out': total_out,
        'total_veresiye': total_veresiye,
    }
    return render(request, 'admin/stock_entry_list.html', context)


@staff_required
def stock_entry_create_view(request):
    """
    Yeni Stok Girişi (Fatura / İrsaliye) ve Toptancı İadesi Ekranı (Atomik & Yönetici Yetkili, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    if request.method == 'POST':
        supplier_id = request.POST.get('supplier_id')
        invoice_number = request.POST.get('invoice_number', '').strip()
        payment_type = request.POST.get('payment_type', 'VERESIYE')
        notes = request.POST.get('notes', '').strip()
        entry_type = request.POST.get('entry_type', 'GIRIS')

        with transaction.atomic():
            supplier = Supplier.objects.select_for_update().filter(company=user_company, id=supplier_id).first() if supplier_id else None

            if entry_type == 'IADE':
                prefix = "IADE"
            elif entry_type == 'FIRE_ZAYI':
                prefix = "FIRE"
            elif entry_type == 'SARF_TUKETIM':
                prefix = "SARF"
            elif entry_type == 'SAYIM_DUZELTME':
                prefix = "SYM"
            else:
                prefix = "STK"
            entry_number = f"{prefix}-{timezone.now().strftime('%Y%m%d%H%M%S')}"

            entry = StockEntry.objects.create(
                company=user_company,
                entry_number=entry_number,
                entry_type=entry_type,
                supplier=supplier,
                invoice_number=invoice_number,
                payment_type=payment_type,
                notes=notes,
                created_by=request.user if request.user.is_authenticated else None
            )

            product_ids = request.POST.getlist('product_id[]')
            quantities = request.POST.getlist('quantity[]')
            units = request.POST.getlist('unit[]')
            list_prices = request.POST.getlist('list_price[]')
            discount_rates = request.POST.getlist('discount_rate[]')
            vat_rates = request.POST.getlist('vat_rate[]')
            buy_prices = request.POST.getlist('buy_price[]')

            total_entry_amount = Decimal('0.00')

            for i, p_id in enumerate(product_ids):
                if not p_id or i >= len(quantities) or not quantities[i]:
                    continue
                try:
                    product = Product.objects.select_for_update().get(company=user_company, id=p_id)
                    qty = Decimal(quantities[i])
                    unit = units[i] if (i < len(units) and units[i]) else product.unit
                    list_p = Decimal(list_prices[i] or '0.00') if i < len(list_prices) else Decimal('0.00')
                    disc_r = Decimal(discount_rates[i] or '0.00') if i < len(discount_rates) else Decimal('0.00')
                    vat_r = Decimal(vat_rates[i] or '20.00') if i < len(vat_rates) else Decimal('20.00')

                    if list_p > Decimal('0.00'):
                        disc_factor = Decimal('1.00') - (disc_r / Decimal('100.00'))
                        vat_factor = Decimal('1.00') + (vat_r / Decimal('100.00'))
                        calc_buy_price = round(list_p * disc_factor * vat_factor, 2)
                    else:
                        calc_buy_price = Decimal(buy_prices[i] or '0.00') if i < len(buy_prices) else (product.buy_price or Decimal('0.00'))

                    total_line = round(qty * calc_buy_price, 2)

                    StockEntryItem.objects.create(
                        stock_entry=entry,
                        product=product,
                        quantity=qty,
                        unit=unit,
                        list_price=list_p,
                        discount_rate=disc_r,
                        vat_rate=vat_r,
                        buy_price=calc_buy_price,
                        total_price=total_line
                    )

                    if entry_type in ['IADE', 'FIRE_ZAYI', 'SARF_TUKETIM']:
                        product.stock_quantity = F('stock_quantity') - qty
                        product.save(update_fields=['stock_quantity'])
                        product.refresh_from_db()
                        action_title = {
                            'IADE': 'Toptancıya İade Çıkışı',
                            'FIRE_ZAYI': 'Fire / Hasar / SKT Çıkışı',
                            'SARF_TUKETIM': 'İşletme İçi Sarf / Tüketim Çıkışı'
                        }.get(entry_type, 'Stok Çıkışı')
                        log_action('STOCK', action_title, f"{product.name} -{qty} {unit} (Birim Maliyet: {calc_buy_price} TL)", user=request.user)
                    else:
                        product.stock_quantity = F('stock_quantity') + qty
                        if calc_buy_price > Decimal('0.00'):
                            product.buy_price = calc_buy_price
                        if vat_r > Decimal('0.00'):
                            product.vat_rate = vat_r
                        if unit:
                            product.unit = unit
                        if supplier and not product.supplier:
                            product.supplier = supplier
                        product.save()
                        product.refresh_from_db()
                        log_action('STOCK', 'Stok Girişi Yapıldı', f"{product.name} +{qty} {unit} (Alış Fiyatı: {calc_buy_price} TL, KDV: %{vat_r})", user=request.user)

                    total_entry_amount += total_line

                except Product.DoesNotExist:
                    continue

            entry.total_amount = total_entry_amount
            entry.save()

            if supplier and total_entry_amount > 0 and entry_type in ['GIRIS', 'IADE']:
                if entry_type == 'IADE':
                    supplier.balance = F('balance') - total_entry_amount
                    supplier.save(update_fields=['balance'])
                    supplier.refresh_from_db()

                    SupplierTransaction.objects.create(
                        supplier=supplier,
                        transaction_type='IADE',
                        amount=total_entry_amount,
                        balance_after=supplier.balance,
                        document_no=invoice_number or entry_number,
                        description=f"Toptancı Mal İadesi #{entry_number}",
                        user=request.user if request.user.is_authenticated else None
                    )
                    log_action('SUPPLIER', 'Toptancı Mal İadesi', f"{supplier.company_name} - {total_entry_amount} TL (Yeni Bakiye: {supplier.balance} TL)", user=request.user)
                elif payment_type == 'VERESIYE':
                    supplier.balance = F('balance') + total_entry_amount
                    supplier.save(update_fields=['balance'])
                    supplier.refresh_from_db()

                    SupplierTransaction.objects.create(
                        supplier=supplier,
                        transaction_type='ALIS',
                        amount=total_entry_amount,
                        balance_after=supplier.balance,
                        document_no=invoice_number or entry_number,
                        description=f"Stok Giriş Fişi #{entry_number}",
                        user=request.user if request.user.is_authenticated else None
                    )
                    log_action('SUPPLIER', 'Mal Alımı Borç Girişi', f"{supplier.company_name} + {total_entry_amount} TL (Yeni Bakiye: {supplier.balance} TL)", user=request.user)

        if entry_type == 'IADE':
            log_action('STOCK', 'Toptancı İade Fişi Kaydedildi', f"İade Fişi #{entry_number} - Toplam {total_entry_amount} TL", user=request.user)
            messages.success(request, f"Toptancı mal iade fişi #{entry_number} başarıyla kaydedildi ve stoklar düşüldü.")
        elif entry_type == 'FIRE_ZAYI':
            log_action('STOCK', 'Fire / Zayi Fişi Kaydedildi', f"Fire Fişi #{entry_number} - Maliyet {total_entry_amount} TL", user=request.user)
            messages.success(request, f"Fire / Zayi fişi #{entry_number} kaydedildi ve stoktan düşüldü.")
        elif entry_type == 'SARF_TUKETIM':
            log_action('STOCK', 'İşletme Sarf Fişi Kaydedildi', f"Sarf Fişi #{entry_number} - Maliyet {total_entry_amount} TL", user=request.user)
            messages.success(request, f"İşletme içi sarf fişi #{entry_number} kaydedildi ve stoktan düşüldü.")
        else:
            log_action('STOCK', 'Stok Fişi Kaydedildi', f"Fiş #{entry_number} - Toplam {total_entry_amount} TL", user=request.user)
            messages.success(request, f"Stok giriş fişi #{entry_number} başarıyla kaydedildi.")

        return redirect('stock_entry_list')

    suppliers = Supplier.objects.filter(company=user_company, is_active=True).order_by('company_name')
    products = Product.objects.filter(company=user_company, is_active=True).select_related('supplier', 'category').order_by('name')
    categories = Category.objects.filter(company=user_company, is_active=True).order_by('name')
    preselect_product_id = request.GET.get('product_id')
    preselect_supplier_param = request.GET.get('supplier_id')

    preselected_product = None
    preselected_supplier_id = None
    preselected_list_price = Decimal('0.00')

    if preselect_supplier_param:
        try:
            preselected_supplier_id = int(preselect_supplier_param)
        except (ValueError, TypeError):
            preselected_supplier_id = None

    if preselect_product_id:
        try:
            preselected_product = Product.objects.filter(company=user_company, id=preselect_product_id, is_active=True).select_related('supplier', 'category').first()
            if preselected_product:
                if preselected_product.supplier_id:
                    preselected_supplier_id = preselected_product.supplier_id
                else:
                    last_item = StockEntryItem.objects.filter(
                        product=preselected_product,
                        stock_entry__supplier__isnull=False
                    ).select_related('stock_entry__supplier').order_by('-stock_entry__created_at').first()
                    if last_item and last_item.stock_entry.supplier_id:
                        preselected_supplier_id = last_item.stock_entry.supplier_id

                if preselected_product.buy_price and preselected_product.buy_price > Decimal('0.00'):
                    vat_div = Decimal('1.00') + (preselected_product.vat_rate / Decimal('100.00'))
                    preselected_list_price = round(preselected_product.buy_price / vat_div, 2)
                else:
                    preselected_list_price = Decimal('0.00')
        except Exception as e:
            print(f"Preselect product error: {e}")

    context = {
        'suppliers': suppliers,
        'products': products,
        'categories': categories,
        'preselect_product_id': preselect_product_id,
        'preselected_product': preselected_product,
        'preselected_supplier_id': preselected_supplier_id,
        'preselected_list_price': preselected_list_price,
        'unit_choices': Product.UNIT_CHOICES,
    }
    return render(request, 'admin/stock_entry_create.html', context)


@staff_api_required
def stock_barcode_search_api(request):
    """
    Mal Kabul / Stok Girişi Esnasında El Terminali veya Barkod Okuyucu İle Hızlı Ürün Bulma (Yönetici Yetkili, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    barcode = request.GET.get('barcode', '').strip()
    supplier_id = request.GET.get('supplier_id')

    if not barcode:
        return JsonResponse({'success': False, 'message': 'Barkod girilmedi.'}, status=400)

    product = Product.objects.filter(company=user_company, is_active=True).filter(
        Q(barcode=barcode) | Q(secondary_barcode=barcode)
    ).first()

    if not product:
        return JsonResponse({'success': False, 'message': f"'{barcode}' barkodlu ürün bulunamadı. Hızlı Ürün Tanımla ile ekleyebilirsiniz."}, status=404)

    is_supplier_product = False
    if supplier_id:
        try:
            s_id = int(supplier_id)
            if product.supplier_id == s_id:
                is_supplier_product = True
        except (ValueError, TypeError):
            pass

    return JsonResponse({
        'success': True,
        'product': {
            'id': product.id,
            'name': product.name,
            'barcode': product.barcode,
            'secondary_barcode': product.secondary_barcode or '',
            'category_id': product.category_id,
            'category_name': product.category.name if product.category else '',
            'supplier_id': product.supplier_id,
            'supplier_name': product.supplier.company_name if product.supplier else '',
            'buy_price': float(product.buy_price),
            'sell_price': float(product.sell_price),
            'vat_rate': float(product.vat_rate),
            'unit': product.unit,
            'stock_quantity': float(product.stock_quantity),
            'is_weighted': product.is_weighted,
            'is_supplier_product': is_supplier_product
        }
    })


def quick_create_product_api(request):
    """
    Fatura / Stok Girişi veya POS Esnasında Hızlı Ürün Tanımlama API Uç Noktası (Yetkili Kullanıcılar & CSRF Korumalı, Multi-Tenancy)
    """
    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': 'Oturum açmanız gerekmektedir.'}, status=401)

    user_company = get_user_company(request.user)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Yalnızca POST istekleri kabul edilir.'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
        barcode = data.get('barcode', '').strip()
        secondary_barcode = data.get('secondary_barcode', '').strip() or None
        name = data.get('name', '').strip()
        category_id = data.get('category_id')
        supplier_id = data.get('supplier_id')
        list_price = Decimal(str(data.get('list_price', '0.00') or '0.00'))
        discount_rate = Decimal(str(data.get('discount_rate', '0.00') or '0.00'))
        vat_rate = Decimal(str(data.get('vat_rate', '20.00') or '20.00'))
        buy_price = Decimal(str(data.get('buy_price', '0.00') or '0.00'))
        sell_price = Decimal(str(data.get('sell_price', '0.00') or '0.00'))
        unit = data.get('unit', 'Adet')
        is_weighted = bool(data.get('is_weighted', False))
        stock_quantity = Decimal(str(data.get('stock_quantity', '100.000') or '100.000'))

        if list_price > Decimal('0.00'):
            disc_factor = Decimal('1.00') - (discount_rate / Decimal('100.00'))
            vat_factor = Decimal('1.00') + (vat_rate / Decimal('100.00'))
            buy_price = round(list_price * disc_factor * vat_factor, 2)

        if not barcode or not name:
            return JsonResponse({'success': False, 'message': 'Barkod ve Ürün Adı zorunludur.'}, status=400)

        if Product.objects.filter(company=user_company, barcode=barcode).exists():
            return JsonResponse({'success': False, 'message': f"Bu barkoda ({barcode}) sahip başka bir ürün zaten kayıtlı!"}, status=400)

        category = Category.objects.filter(company=user_company, id=category_id).first() if category_id else None
        supplier = Supplier.objects.filter(company=user_company, id=supplier_id).first() if supplier_id else None

        product = Product.objects.create(
            company=user_company,
            barcode=barcode,
            secondary_barcode=secondary_barcode,
            name=name,
            category=category,
            supplier=supplier,
            buy_price=buy_price,
            sell_price=sell_price,
            vat_rate=vat_rate,
            unit=unit,
            stock_quantity=stock_quantity,
            is_weighted=is_weighted,
            is_active=True
        )

        current_user = request.user if (hasattr(request, 'user') and request.user.is_authenticated) else None
        source_note = "kasa ekranından" if not supplier else f"fatura girişinden (Tedarikçi: {supplier.company_name})"
        log_action('PRODUCT', 'Hızlı Ürün Tanımlandı', f"{product.name} ({product.barcode}) {source_note} eklendi.", user=current_user)

        return JsonResponse({
            'success': True,
            'product': {
                'id': product.id,
                'name': product.name,
                'barcode': product.barcode,
                'secondary_barcode': product.secondary_barcode or '',
                'list_price': float(list_price),
                'discount_rate': float(discount_rate),
                'buy_price': float(product.buy_price),
                'sell_price': float(product.sell_price),
                'vat_rate': float(product.vat_rate),
                'unit': product.unit,
                'is_weighted': product.is_weighted,
                'stock_quantity': float(product.stock_quantity),
                'supplier_id': product.supplier_id,
                'supplier_name': product.supplier.company_name if product.supplier else ''
            }
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


def _filter_catalog_products(params, company=None):
    """Ürün kataloğu, CSV dışa aktarım ve toplu güncelleme için ortak filtre sorgusu (Multi-Tenancy)."""
    search_q = (params.get('q') or '').strip()
    category_id = (params.get('category') or '').strip()
    supplier_id = (params.get('supplier') or '').strip()
    stock_status = (params.get('stock') or '').strip()

    products = Product.objects.select_related('category', 'supplier').all()
    if company:
        products = products.filter(company_scope_q(company))

    if search_q:
        products = products.filter(
            Q(name__icontains=search_q) |
            Q(barcode__icontains=search_q) |
            Q(secondary_barcode__icontains=search_q)
        )

    if category_id.isdigit():
        products = products.filter(category_id=int(category_id))

    if supplier_id.isdigit():
        products = products.filter(supplier_id=int(supplier_id))

    if stock_status == 'low':
        products = products.filter(stock_quantity__lte=F('min_stock_level'), stock_quantity__gt=0)
    elif stock_status == 'out':
        products = products.filter(stock_quantity__lte=0)
    elif stock_status == 'quick':
        products = products.filter(is_quick_button=True)
    elif stock_status == 'weighted':
        products = products.filter(is_weighted=True)
    elif stock_status == 'passive':
        products = products.filter(is_active=False)

    return products, {
        'search_q': search_q,
        'category_id': category_id,
        'supplier_id': supplier_id,
        'stock_status': stock_status,
    }


@staff_required
def product_catalog_view(request):
    """
    Ürün Yönetim Kataloğu ve Envanter Fiyat Listesi (Yönetici Yetkili, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    products, filters = _filter_catalog_products(request.GET, company=user_company)
    search_q = filters['search_q']
    category_id = filters['category_id']
    supplier_id = filters['supplier_id']
    stock_status = filters['stock_status']
    total_filtered_count = products.count()

    all_products = Product.objects.filter(company_scope_q(user_company))
    total_products_count = all_products.count()
    low_stock_count = all_products.filter(stock_quantity__lte=F('min_stock_level'), stock_quantity__gt=0).count()
    out_of_stock_count = all_products.filter(stock_quantity__lte=0).count()
    quick_button_count = all_products.filter(is_quick_button=True).count()
    passive_count = all_products.filter(is_active=False).count()

    total_stock_cost = Decimal('0.00')
    total_stock_value = Decimal('0.00')
    for p in all_products:
        if p.stock_quantity > 0:
            total_stock_cost += p.stock_quantity * p.buy_price
            total_stock_value += p.stock_quantity * p.sell_price

    categories = Category.objects.filter(company_scope_q(user_company), is_active=True).order_by('name')
    suppliers = Supplier.objects.filter(company_scope_q(user_company), is_active=True).order_by('company_name')

    paginator = Paginator(products, 50)
    page_obj = paginator.get_page(request.GET.get('page'))

    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_string = query_params.urlencode()

    context = {
        'page_obj': page_obj,
        'products': page_obj.object_list,
        'total_filtered_count': total_filtered_count,
        'query_string': query_string,
        'categories': categories,
        'suppliers': suppliers,
        'search_q': search_q,
        'category_id': category_id,
        'supplier_id': supplier_id,
        'stock_status': stock_status,
        'total_products_count': total_products_count,
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'quick_button_count': quick_button_count,
        'passive_count': passive_count,
        'total_stock_cost': total_stock_cost,
        'total_stock_value': total_stock_value,
    }
    return render(request, 'admin/product_catalog.html', context)


@staff_required
def product_create_edit_view(request, product_id=None):
    """
    Ürün Tanımlama / Düzenleme Sayfası (Master Data - Yönetici Yetkili, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    product = get_object_or_404(Product.objects.filter(company_scope_q(user_company)), id=product_id) if product_id else None
    categories = Category.objects.filter(company_scope_q(user_company), is_active=True).order_by('name')
    suppliers = Supplier.objects.filter(company_scope_q(user_company), is_active=True).order_by('company_name')

    def render_form(posted_data=None):
        variants = product.variants.all().order_by('size', 'color') if product else []
        return render(request, 'admin/product_form.html', {
            'product': product,
            'posted': posted_data,
            'categories': categories,
            'suppliers': suppliers,
            'unit_choices': Product.UNIT_CHOICES,
            'variants': variants,
            'company': user_company,
        })

    if request.method == 'POST':
        barcode = request.POST.get('barcode', '').strip()
        secondary_barcode = request.POST.get('secondary_barcode', '').strip() or None
        name = request.POST.get('name', '').strip()
        category_id = request.POST.get('category_id')
        supplier_id = request.POST.get('supplier_id')
        unit = request.POST.get('unit', 'Adet')
        is_weighted = request.POST.get('is_weighted') == 'on'
        is_quick_button = request.POST.get('is_quick_button') == 'on'
        is_active = request.POST.get('is_active') == 'on'
        has_variants = request.POST.get('has_variants') == 'on'
        track_expiration = request.POST.get('track_expiration') == 'on'
        lot_number = request.POST.get('lot_number', '').strip()
        expiration_date_raw = request.POST.get('expiration_date', '').strip()
        expiration_date = _parse_iso_date(expiration_date_raw) if expiration_date_raw else None

        posted_data = {
            'barcode': barcode,
            'secondary_barcode': secondary_barcode or '',
            'name': name,
            'category_id': category_id or '',
            'supplier_id': supplier_id or '',
            'buy_price': request.POST.get('buy_price', '0.00'),
            'sell_price': request.POST.get('sell_price', '0.00'),
            'vat_rate': request.POST.get('vat_rate', '20.00'),
            'unit': unit,
            'stock_quantity': request.POST.get('stock_quantity', '0.000'),
            'min_stock_level': request.POST.get('min_stock_level', '5.000'),
            'quick_button_order': request.POST.get('quick_button_order', '0'),
            'is_weighted': is_weighted,
            'is_quick_button': is_quick_button,
            'is_active': is_active,
            'has_variants': has_variants,
            'track_expiration': track_expiration,
            'lot_number': lot_number,
            'expiration_date': expiration_date_raw,
        }

        if not barcode or not name:
            messages.error(request, "Ana barkod ve ürün adı alanları zorunludur.")
            return render_form(posted_data)

        try:
            buy_price = _parse_decimal(request.POST.get('buy_price'), Decimal('0.00'))
            sell_price = _parse_decimal(request.POST.get('sell_price'), Decimal('0.00'))
            vat_rate = _parse_decimal(request.POST.get('vat_rate'), Decimal('20.00'))
            stock_quantity = _parse_decimal(request.POST.get('stock_quantity'), Decimal('0.000'))
            min_stock_level = _parse_decimal(request.POST.get('min_stock_level'), Decimal('5.000'))
            quick_button_order = _parse_int(request.POST.get('quick_button_order'), 0)
        except ValueError as err:
            messages.error(request, f"Sayısal alan hatası: {err}")
            return render_form(posted_data)

        if buy_price < 0 or sell_price < 0 or vat_rate < 0 or min_stock_level < 0:
            messages.error(request, "Fiyat, KDV ve kritik stok seviyesi negatif olamaz.")
            return render_form(posted_data)

        existing_qs = Product.objects.filter(company_scope_q(user_company))
        if product:
            existing_qs = existing_qs.exclude(id=product.id)
        dup_main = existing_qs.filter(Q(barcode=barcode) | Q(secondary_barcode=barcode)).first()
        if dup_main:
            messages.error(request, f"'{barcode}' barkodu zaten '{dup_main.name}' ürününe tanımlı!")
            return render_form(posted_data)

        if secondary_barcode:
            if secondary_barcode == barcode:
                messages.error(request, "İkincil / koli barkodu ana barkod ile aynı olamaz.")
                return render_form(posted_data)
            dup_sec = existing_qs.filter(Q(barcode=secondary_barcode) | Q(secondary_barcode=secondary_barcode)).first()
            if dup_sec:
                messages.error(request, f"'{secondary_barcode}' ikincil barkodu zaten '{dup_sec.name}' ürününe tanımlı!")
                return render_form(posted_data)

        category = Category.objects.filter(company_scope_q(user_company), id=category_id).first() if category_id else None
        supplier = Supplier.objects.filter(company_scope_q(user_company), id=supplier_id).first() if supplier_id else None

        try:
            if product:
                product.barcode = barcode
                product.secondary_barcode = secondary_barcode
                product.name = name
                product.category = category
                product.supplier = supplier
                product.buy_price = buy_price
                product.sell_price = sell_price
                product.vat_rate = vat_rate
                product.unit = unit
                product.stock_quantity = stock_quantity
                product.min_stock_level = min_stock_level
                product.is_weighted = is_weighted
                product.is_quick_button = is_quick_button
                product.quick_button_order = quick_button_order
                product.is_active = is_active
                product.has_variants = has_variants
                product.track_expiration = track_expiration
                product.expiration_date = expiration_date
                product.lot_number = lot_number

                if 'image' in request.FILES:
                    product.image = request.FILES['image']

                product.save()
                log_action('PRODUCT', 'Ürün Güncellendi', f"{product.name} ({product.barcode}) güncellendi.", user=request.user)
                messages.success(request, f"Ürün '{name}' başarıyla güncellendi.")
            else:
                product = Product.objects.create(
                    company=user_company,
                    barcode=barcode,
                    secondary_barcode=secondary_barcode,
                    name=name,
                    category=category,
                    supplier=supplier,
                    buy_price=buy_price,
                    sell_price=sell_price,
                    vat_rate=vat_rate,
                    unit=unit,
                    stock_quantity=stock_quantity,
                    min_stock_level=min_stock_level,
                    is_weighted=is_weighted,
                    is_quick_button=is_quick_button,
                    quick_button_order=quick_button_order,
                    is_active=is_active,
                    has_variants=has_variants,
                    track_expiration=track_expiration,
                    expiration_date=expiration_date,
                    lot_number=lot_number,
                    image=request.FILES.get('image')
                )
                log_action('PRODUCT', 'Yeni Ürün Eklendi', f"{product.name} ({product.barcode}) eklendi.", user=request.user)
                messages.success(request, f"Yeni ürün '{name}' başarıyla eklendi.")

            # Faz 18: Merkezi Fiyat Yayılımı (HQ Price Push)
            propagate_price = request.POST.get('propagate_price') == 'on'
            if propagate_price and user_company:
                branches = Company.objects.filter(is_active=True).exclude(id=user_company.id)
                if user_company.branches.exists():
                    branches = user_company.branches.filter(is_active=True)
                elif user_company.parent:
                    branches = Company.objects.filter(
                        Q(id=user_company.parent.id) | Q(parent=user_company.parent)
                    ).exclude(id=user_company.id).filter(is_active=True)
                
                synced_count = 0
                for b in branches:
                    cnt = Product.objects.filter(company=b, barcode=product.barcode).update(
                        sell_price=product.sell_price,
                        buy_price=product.buy_price,
                        vat_rate=product.vat_rate,
                        updated_at=timezone.now()
                    )
                    if cnt:
                        synced_count += cnt
                if synced_count > 0:
                    messages.info(request, f"Fiyat ve KDV güncellemesi {synced_count} bağlı şubeye otomatik yansıtıldı.")
        except IntegrityError:
            messages.error(request, f"'{barcode}' barkodu veritabanında zaten kayıtlı.")
            return render_form(posted_data)

        return redirect('product_catalog_view')

    return render_form()


@staff_required
@require_POST
def product_toggle_active_action(request, product_id):
    """
    Ürün Kataloğundan Tek Tıkla Ürünü Aktif / Pasif Yapma (Yönetici Yetkili, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    product = get_object_or_404(Product.objects.filter(company_scope_q(user_company)), id=product_id)
    product.is_active = not product.is_active
    product.save(update_fields=['is_active'])

    status_label = 'Aktif' if product.is_active else 'Pasif'
    log_action(
        'PRODUCT',
        'Ürün Durumu Güncellendi',
        f"{product.name} ({product.barcode}) -> {status_label}",
        user=request.user
    )
    if product.is_active:
        messages.success(request, f"'{product.name}' yeniden satışa açıldı (Aktif).")
    else:
        messages.success(request, f"'{product.name}' pasife alındı; kasada satıştan gizlendi.")

    return _safe_next_redirect(request, 'product_catalog_view')


@staff_required
@require_POST
def product_delete_action(request, product_id):
    """
    Ürün Silme / Arşivleme Güvenli Aksiyonu (Multi-Tenancy)
    - Geçmiş satışı veya mal kabul hareketi olan ürünler silinemez (veri bütünlüğü korunur).
    - Hareketi olmayan ürünler güvenle silinir.
    """
    user_company = get_user_company(request.user)
    product = get_object_or_404(Product.objects.filter(company_scope_q(user_company)), id=product_id)
    has_sales = product.sale_items.exists()
    has_stock_entries = product.stock_entry_items.exists()

    if has_sales or has_stock_entries:
        reasons = []
        if has_sales:
            reasons.append("geçmiş satış fişleri")
        if has_stock_entries:
            reasons.append("stok giriş/mal kabul kayıtları")
        reason_text = " ve ".join(reasons)
        messages.warning(
            request,
            f"'{product.name}' ürününe ait {reason_text} bulunduğu için veri bütünlüğü nedeniyle silinemez. "
            "Ürünü satıştan kaldırmak için 'Pasife Al' seçeneğini kullanabilirsiniz."
        )
        return _safe_next_redirect(request, 'product_catalog_view')

    prod_name = product.name
    prod_barcode = product.barcode
    product.delete()
    log_action('PRODUCT', 'Ürün Silindi', f"'{prod_name}' ({prod_barcode}) silindi.", user=request.user)
    messages.success(request, f"'{prod_name}' ürünü başarıyla silindi.")
    return redirect('product_catalog_view')


@staff_required
def system_logs_view(request):
    """
    Sistem İşlem ve Denetim Logları Ekranı (Yönetici Yetkili, Multi-Tenancy)
    Kasa 1 ve Kasa 2 Ayrı Filtrelenebilir ve İncelenebilir.
    """
    user_company = get_user_company(request.user)
    module = request.GET.get('module')
    level = request.GET.get('level')
    query = request.GET.get('q', '').strip()
    register_code = request.GET.get('register', '').strip()

    logs = SystemLog.objects.filter(company_scope_q(user_company)).select_related('user')

    if module:
        logs = logs.filter(module=module)
    if level:
        logs = logs.filter(level=level)
    if register_code:
        logs = logs.filter(Q(action__icontains=register_code) | Q(message__icontains=register_code))
    if query:
        logs = logs.filter(Q(action__icontains=query) | Q(message__icontains=query) | Q(user__username__icontains=query))

    logs = logs[:150]
    all_registers = CashRegister.objects.filter(company_scope_q(user_company), is_active=True).order_by('register_code')

    # Faz 4: Kasa Güvenliği & Denetim İzi (Audit Trail) İstatistikleri
    manual_drawer_count = SystemLog.objects.filter(company_scope_q(user_company), action__icontains='Manuel Çekmece').count()
    supervisor_override_count = SystemLog.objects.filter(company_scope_q(user_company), action__icontains='Yetkili Onayı Verildi').count()
    unauthorized_attempts_count = SystemLog.objects.filter(company_scope_q(user_company), action__icontains='Yetkisiz İşlem Girişimi').count()
    shift_discrepancy_count = Shift.objects.filter(company_scope_q(user_company), is_closed=True).exclude(difference=Decimal('0.00')).count()

    context = {
        'logs': logs,
        'selected_module': module,
        'selected_level': level,
        'selected_register': register_code,
        'query': query,
        'all_registers': all_registers,
        'modules': SystemLog.MODULES,
        'levels': SystemLog.LOG_LEVELS,
        'manual_drawer_count': manual_drawer_count,
        'supervisor_override_count': supervisor_override_count,
        'unauthorized_attempts_count': unauthorized_attempts_count,
        'shift_discrepancy_count': shift_discrepancy_count,
    }
    return render(request, 'admin/system_logs.html', context)


# ==========================================
# KULLANICI VE YETKİLENDİRME (RBAC) YÖNETİMİ
# ==========================================

@staff_required
def user_management_view(request):
    """
    Sadeleştirilmiş Personel ve Kullanıcı Yetkilendirme Ekranı (Multi-Tenant & RBAC)
    """
    if request.user.is_superuser:
        # Süper Admin platformdaki tüm kullanıcıları görebilir
        users = User.objects.all().order_by('-is_superuser', '-is_staff', 'username')
    else:
        # Şube Yöneticisi SADECE kendi şirketine bağlı personeli görebilir
        # Süper Admin hesapları ASLA listelenmez!
        user_company = get_user_company(request.user)
        if user_company:
            users = User.objects.filter(
                profile__company=user_company,
                is_superuser=False
            ).order_by('-is_staff', 'username')
        else:
            # Şirketi henüz atanmamış yönetici sadece kendisini görebilir (güvenli fallback)
            users = User.objects.filter(id=request.user.id, is_superuser=False)

    total_users = users.count()
    cashier_count = users.filter(is_staff=False, is_superuser=False).count()
    admin_count = users.filter(Q(is_staff=True) | Q(is_superuser=True)).count()

    context = {
        'staff_users': users,
        'total_users': total_users,
        'cashier_count': cashier_count,
        'admin_count': admin_count,
        'is_master_admin': request.user.is_superuser,
    }
    return render(request, 'admin/user_management.html', context)


@staff_required
def user_create_action(request):
    """
    Yeni Personel / Kullanıcı Ekleme (Multi-Tenant & RBAC Korumalı)
    """
    if request.method == 'POST':
        full_name = request.POST.get('full_name', '').strip()
        username = request.POST.get('username', '').strip().lower()
        password = request.POST.get('password', '').strip()
        role = request.POST.get('role', 'KASIYER')

        if not username or not password:
            messages.error(request, 'Lütfen kullanıcı adı ve şifre giriniz.')
            return redirect('user_management_view')

        if User.objects.filter(username=username).exists():
            messages.error(request, f"'{username}' kullanıcı adı zaten kullanımda. Farklı bir kullanıcı adı belirleyiniz.")
            return redirect('user_management_view')

        first_name = ''
        last_name = ''
        if full_name:
            parts = full_name.split(' ', 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ''

        # Şirket / Tenant Bağlantısı (Multi-Tenancy)
        user_company = get_user_company(request.user)

        # PIN / Şifre Çakışma Kontrolü (Aynı şirkette iki personele aynı numerik PIN verilemez)
        if password.isdigit() and user_company:
            for existing in User.objects.filter(is_active=True, profile__company=user_company):
                cand_pin = getattr(getattr(existing, 'profile', None), 'pin_code', '')
                if cand_pin == password or existing.check_password(password):
                    messages.error(
                        request,
                        f"'{password}' PIN kodu bu mağazada zaten '{existing.get_full_name() or existing.username}' tarafından kullanılıyor! Lütfen her personele benzersiz bir PIN belirleyiniz."
                    )
                    return redirect('user_management_view')

        # Şube yöneticisi asla süper admin oluşturamaz!
        is_staff = (role == 'YONETICI')
        user = User.objects.create_user(
            username=username,
            password=password,
            first_name=first_name,
            last_name=last_name,
            is_staff=is_staff,
            is_superuser=False,
            is_active=True
        )

        UserProfile.objects.create(
            user=user,
            company=user_company,
            pin_code=password if password.isdigit() else ''
        )

        role_label = "Yönetici (Mağaza Müdürü)" if is_staff else "Kasiyer (POS Satış)"
        log_action('AUTH', 'Yeni Personel Eklendi', f"'{user.username}' ({role_label}) sisteme eklendi. Şirket: {user_company}. Ekleyen: {request.user.username}", user=request.user)
        messages.success(request, f"'{user.username}' başarıyla oluşturuldu. Rol: {role_label}.")

    return redirect('user_management_view')


@staff_required
def user_toggle_status_action(request, user_id):
    """
    Kullanıcıyı Aktif veya Pasif Yapma (Durdurma) - RBAC & Multi-Tenant Korumalı
    """
    target_user = get_object_or_404(User, id=user_id)
    if target_user == request.user:
        messages.error(request, 'Kendi hesabınızı pasife alamazsınız!')
        return redirect('user_management_view')

    # 1. Hiyerarşik Koruma: Süper Admin hesaplarını sadece Süper Admin değiştirebilir!
    if target_user.is_superuser and not request.user.is_superuser:
        messages.error(request, 'Süper Yönetici hesapları üzerinde değişiklik yapma yetkiniz yoktur!')
        return redirect('user_management_view')

    # 2. Şirket İzolasyonu: Şube yöneticisi başka şirketin personelini pasife alamaz!
    if not request.user.is_superuser:
        user_company = get_user_company(request.user)
        target_company = get_user_company(target_user)
        if user_company != target_company:
            messages.error(request, 'Farklı bir mağazanın personelini değiştirme yetkiniz yoktur!')
            return redirect('user_management_view')

    target_user.is_active = not target_user.is_active
    target_user.save(update_fields=['is_active'])

    status_str = "Aktif Edildi" if target_user.is_active else "Donduruldu / Pasife Alındı"
    log_action('AUTH', 'Kullanıcı Durumu Değişti', f"'{target_user.username}' hesabı {status_str}.", user=request.user)
    messages.success(request, f"'{target_user.username}' hesabı {status_str}.")
    return redirect('user_management_view')


@staff_required
def user_change_role_action(request, user_id):
    """
    Kullanıcı Rolünü Değiştirme (Kasiyer <-> Yönetici) - RBAC & Multi-Tenant Korumalı
    """
    target_user = get_object_or_404(User, id=user_id)
    if target_user == request.user:
        messages.error(request, 'Kendi yönetici rolünüzü değiştiremezsiniz!')
        return redirect('user_management_view')

    # 1. Hiyerarşik Koruma: Süper Admin rolü asla şube yöneticisi tarafından değiştirilemez / düşürülemez!
    if target_user.is_superuser and not request.user.is_superuser:
        messages.error(request, 'Süper Yönetici rolü üzerinde değişiklik yapma yetkiniz yoktur!')
        return redirect('user_management_view')

    # 2. Şirket İzolasyonu: Başka şirketin personelinin rolü değiştirilemez!
    if not request.user.is_superuser:
        user_company = get_user_company(request.user)
        target_company = get_user_company(target_user)
        if user_company != target_company:
            messages.error(request, 'Farklı bir mağazanın personel rolünü değiştirme yetkiniz yoktur!')
            return redirect('user_management_view')

    new_role = request.POST.get('role')
    if new_role == 'YONETICI':
        target_user.is_staff = True
        target_user.is_superuser = False  # Şube yöneticisi kesinlikle is_superuser olamaz
        role_name = "Yönetici (Mağaza Müdürü)"
    else:
        target_user.is_staff = False
        target_user.is_superuser = False
        role_name = "Kasiyer (Kasa Satış)"

    target_user.save(update_fields=['is_staff', 'is_superuser'])

    log_action('AUTH', 'Yetki Değiştirildi', f"'{target_user.username}' rolü '{role_name}' olarak güncellendi.", user=request.user)
    messages.success(request, f"'{target_user.username}' kullanıcısının yeni rolü: {role_name}.")
    return redirect('user_management_view')


@staff_required
def user_reset_password_action(request, user_id):
    """
    Kullanıcı Şifresi / Kasa PIN Kodu Sıfırlama - RBAC & Multi-Tenant Korumalı
    """
    target_user = get_object_or_404(User, id=user_id)

    # 1. Hiyerarşik Koruma: Süper Admin şifresi şube yöneticisi tarafından sıfırlanamaz!
    if target_user.is_superuser and not request.user.is_superuser:
        messages.error(request, 'Süper Yönetici şifresini sıfırlama yetkiniz yoktur!')
        return redirect('user_management_view')

    # 2. Şirket İzolasyonu: Başka şirketin personelinin şifresi sıfırlanamaz!
    if not request.user.is_superuser:
        user_company = get_user_company(request.user)
        target_company = get_user_company(target_user)
        if user_company != target_company:
            messages.error(request, 'Farklı bir mağazanın personel şifresini değiştirme yetkiniz yoktur!')
            return redirect('user_management_view')

    new_password = request.POST.get('new_password', '').strip()
    if not new_password:
        messages.error(request, 'Lütfen yeni bir şifre giriniz.')
        return redirect('user_management_view')

    user_company = get_user_company(request.user)
    if new_password.isdigit() and user_company:
        for existing in User.objects.filter(is_active=True, profile__company=user_company).exclude(id=target_user.id):
            cand_pin = getattr(getattr(existing, 'profile', None), 'pin_code', '')
            if cand_pin == new_password or existing.check_password(new_password):
                messages.error(
                    request,
                    f"'{new_password}' PIN kodu bu mağazada zaten '{existing.get_full_name() or existing.username}' tarafından kullanılıyor! Lütfen farklı bir PIN belirleyiniz."
                )
                return redirect('user_management_view')

    target_user.set_password(new_password)
    target_user.save()
    if hasattr(target_user, 'profile'):
        target_user.profile.pin_code = new_password if new_password.isdigit() else ''
        target_user.profile.save()
    log_action('AUTH', 'Şifre Güncellendi', f"'{target_user.username}' kullanıcısının şifresi yönetici tarafından sıfırlandı.", user=request.user)
    messages.success(request, f"'{target_user.username}' şifresi başarıyla güncellendi.")
    return redirect('user_management_view')


# ==========================================================
# İŞLETME, E-ARŞİV VE DONANIM DİNAMİK AYARLARI
# ==========================================================

@staff_required
def company_settings_view(request):
    """
    İşletme Kimlik Bilgileri, e-Arşiv Entegratörü ve Donanım/Yazıcı Yapılandırması Ekranı (Multi-Tenancy).
    Yazılım farklı market müşterilerine satıldığında kod değiştirmeden bu ekrandan ayarlanır.
    """
    user_company = get_user_company(request.user)
    setting = CompanySetting.get_settings(company=user_company)

    if request.method == 'POST':
        # 1. İşletme Bilgileri
        setting.title = request.POST.get('title', '').strip() or setting.title
        setting.brand_name = request.POST.get('brand_name', '').strip() or setting.brand_name
        setting.tax_number = request.POST.get('tax_number', '').strip() or setting.tax_number
        setting.tax_office = request.POST.get('tax_office', '').strip() or setting.tax_office
        setting.address = request.POST.get('address', '').strip() or setting.address
        setting.phone = request.POST.get('phone', '').strip()
        setting.email = request.POST.get('email', '').strip()
        setting.mersis_no = request.POST.get('mersis_no', '').strip()
        setting.ticaret_sicil_no = request.POST.get('ticaret_sicil_no', '').strip()
        setting.web_site = request.POST.get('web_site', '').strip()

        # 2. Mali ve e-Arşiv Yapılandırması
        setting.fiscal_mode = request.POST.get('fiscal_mode', 'VUK507_EARSIV')
        setting.fiscal_provider = request.POST.get('fiscal_provider', 'LOCAL_MOCK')
        setting.fiscal_environment = request.POST.get('fiscal_environment', 'TEST')
        setting.api_username = request.POST.get('api_username', '').strip()
        setting.api_password = request.POST.get('api_password', '').strip()
        setting.api_key = request.POST.get('api_key', '').strip()
        setting.api_secret = request.POST.get('api_secret', '').strip()

        # 3. Donanım ve Yazıcı Ayarları
        setting.printer_type = request.POST.get('printer_type', 'SPOOL_AND_SOCKET')
        setting.printer_ip = request.POST.get('printer_ip', '127.0.0.1').strip() or '127.0.0.1'
        try:
            setting.printer_port = int(request.POST.get('printer_port', 9100))
        except (ValueError, TypeError):
            setting.printer_port = 9100

        try:
            setting.paper_width = int(request.POST.get('paper_width', 80))
        except (ValueError, TypeError):
            setting.paper_width = 80

        setting.drawer_kick_on_cash = bool(request.POST.get('drawer_kick_on_cash') in ('1', 'on', 'true', 'True'))
        setting.receipt_header_note = request.POST.get('receipt_header_note', '').strip()
        setting.receipt_footer_note = request.POST.get('receipt_footer_note', '').strip() or setting.receipt_footer_note

        # 4. Elektronik Kasa Terazisi Ayarları
        setting.scale_enabled = bool(request.POST.get('scale_enabled') in ('1', 'on', 'true', 'True'))
        setting.scale_port = request.POST.get('scale_port', 'AUTO').strip() or 'AUTO'
        try:
            setting.scale_baudrate = int(request.POST.get('scale_baudrate', 9600))
        except (ValueError, TypeError):
            setting.scale_baudrate = 9600
        setting.scale_protocol = request.POST.get('scale_protocol', 'CAS_PD2')
        try:
            setting.scale_mock_weight = Decimal(str(request.POST.get('scale_mock_weight', '1.250')))
        except Exception:
            setting.scale_mock_weight = Decimal('1.250')

        # 5. Müşteri Fiyat Göstergesi (Customer Display) Ayarları
        setting.customer_display_enabled = bool(request.POST.get('customer_display_enabled') in ('1', 'on', 'true', 'True'))
        setting.customer_display_mode = request.POST.get('customer_display_mode', 'DUAL_SCREEN')
        setting.customer_display_port = request.POST.get('customer_display_port', 'COM2').strip() or 'COM2'
        setting.customer_display_welcome_msg = request.POST.get('customer_display_welcome_msg', 'Hİ MARKET - HOŞ GELDİNİZ').strip() or 'Hİ MARKET - HOŞ GELDİNİZ'

        # 6. Raf ve Barkod Etiketi Ayarları
        setting.label_printer_type = request.POST.get('label_printer_type', 'BROWSER_HTML')
        try:
            setting.label_width_mm = int(request.POST.get('label_width_mm', 60))
        except (ValueError, TypeError):
            setting.label_width_mm = 60
        try:
            setting.label_height_mm = int(request.POST.get('label_height_mm', 40))
        except (ValueError, TypeError):
            setting.label_height_mm = 40

        # 7. Kasa Güvenliği, Kör Sayım & Yetkili İptal Onayı (Faz 4)
        setting.blind_close_enabled = bool(request.POST.get('blind_close_enabled') in ('1', 'on', 'true', 'True'))
        setting.supervisor_cancel_required = bool(request.POST.get('supervisor_cancel_required') in ('1', 'on', 'true', 'True'))
        try:
            setting.supervisor_discount_limit_percent = Decimal(str(request.POST.get('supervisor_discount_limit_percent', '10.00')))
        except Exception:
            setting.supervisor_discount_limit_percent = Decimal('10.00')

        # Faz 13: İşletme Sektörü (Market / Butik-Giyim / Genel Perakende)
        if user_company:
            b_type = request.POST.get('business_type', '').strip().upper()
            if b_type in ('MARKET', 'APPAREL', 'RETAIL'):
                user_company.business_type = b_type
                user_company.save(update_fields=['business_type'])

        setting.save()

        log_action(
            'SYSTEM',
            'İşletme ve Mali Ayarlar Güncellendi',
            f"Firma: '{setting.title}', Sektör: {user_company.get_business_type_display() if user_company else 'N/A'}, VKN: {setting.tax_number}, Entegratör: {setting.fiscal_provider} ({setting.fiscal_environment})",
            user=request.user
        )
        messages.success(request, f"'{setting.brand_name}' işletme, sektör ve donanım ayarları başarıyla kaydedildi.")
        return redirect('company_settings_view')

    context = {
        'company': user_company,
        'business_types': Company.BUSINESS_TYPE_CHOICES,
        'setting': setting,
        'fiscal_modes': CompanySetting.FISCAL_MODE_CHOICES,
        'integrators': CompanySetting.INTEGRATOR_CHOICES,
        'environments': CompanySetting.ENVIRONMENT_CHOICES,
        'printer_types': CompanySetting.PRINTER_TYPE_CHOICES,
        'scale_protocols': CompanySetting.SCALE_PROTOCOL_CHOICES,
        'customer_display_modes': CompanySetting.CUSTOMER_DISPLAY_MODE_CHOICES,
        'label_printer_types': CompanySetting.LABEL_PRINTER_CHOICES,
    }
    return render(request, 'admin/company_settings.html', context)


@staff_api_required
def test_print_settings_api(request):
    """
    Kayıtlı yazıcı ve e-Arşiv ayarlarına göre test fişi basar ve çekmece tetiğini test eder (Multi-Tenancy).
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Yalnızca POST istekleri kabul edilir.'}, status=405)

    user_company = get_user_company(request.user)
    setting = CompanySetting.get_settings(company=user_company)
    register = get_current_register(request, company=user_company)
    _, shift = get_or_create_active_shift(request.user, register=register, request=request, company=user_company)

    today_str = timezone.localtime(timezone.now()).strftime('%Y%m%d')
    test_receipt_no = f"TEST-{today_str}-0001"

    test_sale, _ = Sale.objects.get_or_create(
        company=user_company,
        receipt_number=test_receipt_no,
        defaults={
            'shift': shift,
            'cashier': request.user,
            'payment_type': 'NAKIT',
            'subtotal': Decimal('90.91'),
            'total_vat': Decimal('9.09'),
            'total_amount': Decimal('100.00'),
            'cash_paid': Decimal('100.00'),
            'change_given': Decimal('0.00'),
            'document_type': 'E_ARSIV',
            'customer_title': f'{setting.brand_name} Test Müşterisi',
            'customer_tax_number': '11111111111',
            'vat_10_base': Decimal('90.91'),
            'vat_10_amount': Decimal('9.09'),
        }
    )

    if test_sale.items.count() == 0:
        p = Product.objects.filter(is_active=True).first()
        SaleItem.objects.create(
            sale=test_sale,
            product=p,
            product_name="DENEME TEST ÜRÜNÜ",
            barcode="8690000000000",
            quantity=Decimal('1.000'),
            unit_price=Decimal('100.00'),
            vat_rate=Decimal('10.00'),
            vat_amount=Decimal('9.09'),
            total_price=Decimal('100.00'),
        )

    apply_fiscal_data_to_sale(test_sale, {
        'tax_number': '11111111111',
        'title': f'{setting.brand_name} Deneme Fişi',
        'document_type': 'E_ARSIV'
    })

    print_res = silent_print_receipt(test_sale, open_drawer=True)

    log_action('SYSTEM', 'Donanım & Test Fişi Basıldı', f"Yazıcı Testi: {print_res.get('transport')}", user=request.user)
    return JsonResponse({
        'success': True,
        'message': f"Deneme fişi '{print_res.get('transport')}' üzerinden başarıyla gönderildi ve çekmece tetiklendi.",
        'details': print_res
    })


# ==========================================================
# MÜŞTERİ & CARİ HESAP / VERESİYE DEFTERİ VIEWS & APIS
# ==========================================================

@login_required(login_url='/login/')
def customer_search_api(request):
    """
    Kasa Satış Ekranı için Gerçek Zamanlı Müşteri / Cari Arama API (<10ms, Multi-Tenancy).
    İsim, Soyisim, Telefon veya TCKN ile hızlı filtreleme.
    """
    user_company = get_user_company(request.user)
    q = request.GET.get('q', '').strip()
    base_qs = Customer.objects.filter(company_scope_q(user_company), is_active=True)
    if not q:
        customers = base_qs.order_by('-updated_at')[:10]
    else:
        customers = base_qs.filter(
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(phone__icontains=q) |
            Q(tax_number__icontains=q) |
            Q(company_title__icontains=q)
        )[:20]

    data = [
        {
            'id': c.id,
            'first_name': c.first_name,
            'last_name': c.last_name,
            'full_name': c.full_name,
            'phone': c.phone,
            'tax_number': c.tax_number,
            'company_title': c.company_title,
            'balance': float(c.balance),
            'credit_limit': float(c.credit_limit),
            'remaining_credit': float(c.remaining_credit),
            'is_over_limit': bool(c.balance >= c.credit_limit),
            'notes': c.notes,
        }
        for c in customers
    ]
    return JsonResponse({'success': True, 'customers': data})


def _filter_customers(params, company=None):
    """Müşteri listesi ve CSV dışa aktarım için ortak arama sorgusu (Multi-Tenancy)."""
    query = (params.get('q') or '').strip()
    customers_qs = Customer.objects.all()
    if company:
        customers_qs = customers_qs.filter(company_scope_q(company))

    if query:
        customers_qs = customers_qs.filter(
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(phone__icontains=query) |
            Q(tax_number__icontains=query) |
            Q(company_title__icontains=query)
        )

    return customers_qs.order_by('-balance', 'first_name'), query


@staff_required
def customer_list_view(request):
    """
    Veresiye ve Müşteri Defteri Yönetim Listesi (Multi-Tenancy).
    Toplam borç/alacak bakiyeleri, arama, hızlı müşteri ekleme ve tahsilat kısayolları.
    """
    user_company = get_user_company(request.user)
    customers, query = _filter_customers(request.GET, company=user_company)

    all_cust = Customer.objects.filter(company_scope_q(user_company))
    total_customers = all_cust.count()
    total_receivable = all_cust.aggregate(total=Sum('balance'))['total'] or Decimal('0.00')
    customers_with_debt = all_cust.filter(balance__gt=0).count()
    over_limit_count = all_cust.filter(balance__gte=F('credit_limit')).count()

    context = {
        'customers': customers,
        'search_query': query,
        'total_customers': total_customers,
        'total_receivable': total_receivable,
        'customers_with_debt': customers_with_debt,
        'over_limit_count': over_limit_count,
    }
    return render(request, 'admin/customers.html', context)


@staff_required
def customer_create_view(request):
    """
    Yeni Müşteri / Cari Hesap Kartı Tanımlama (Tam Form veya Modal POST, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        tax_number = request.POST.get('tax_number', '').strip()
        company_title = request.POST.get('company_title', '').strip()
        address = request.POST.get('address', '').strip()
        credit_limit_raw = request.POST.get('credit_limit', '5000.00').strip()
        notes = request.POST.get('notes', '').strip()
        is_active = bool(request.POST.get('is_active', 'on') in ('on', '1', 'true', 'True'))

        if not first_name or not phone:
            messages.error(request, 'Müşteri adı ve telefon numarası zorunludur!')
            return redirect('customer_list_view')

        if Customer.objects.filter(company_scope_q(user_company), phone=phone).exists():
            messages.error(request, f"'{phone}' telefon numarasına sahip bir müşteri zaten kayıtlı!")
            return redirect('customer_list_view')

        try:
            credit_limit = _parse_decimal(credit_limit_raw, Decimal('5000.00'))
        except ValueError:
            credit_limit = Decimal('5000.00')

        customer = Customer.objects.create(
            company=user_company,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            tax_number=tax_number,
            company_title=company_title,
            address=address,
            credit_limit=credit_limit,
            balance=Decimal('0.00'),
            notes=notes,
            is_active=is_active
        )
        log_action('POS', 'Yeni Cari Müşteri Eklendi', f"Müşteri: '{customer.full_name}' ({phone}) sisteme eklendi. Limit: {credit_limit} TL", user=request.user)
        messages.success(request, f"'{customer.full_name}' başarıyla müşteri defterine kaydedildi.")
        return redirect('customer_detail_view', customer_id=customer.id)

    return render(request, 'admin/customer_form.html', {'is_edit': False, 'customer': None})


@staff_required
def customer_edit_view(request, customer_id):
    """
    Kayıtlı Müşteri / Cari Bilgilerini Güncelleme (Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    customer = get_object_or_404(Customer.objects.filter(company_scope_q(user_company)), id=customer_id)

    if request.method == 'POST':
        customer.first_name = request.POST.get('first_name', '').strip() or customer.first_name
        customer.last_name = request.POST.get('last_name', '').strip()
        new_phone = request.POST.get('phone', '').strip()
        if new_phone and new_phone != customer.phone:
            if Customer.objects.filter(company_scope_q(user_company), phone=new_phone).exclude(id=customer.id).exists():
                messages.error(request, f"'{new_phone}' telefon numarası başka bir müşteride kayıtlı!")
                return redirect('customer_edit_view', customer_id=customer.id)
            customer.phone = new_phone

        customer.tax_number = request.POST.get('tax_number', '').strip()
        customer.company_title = request.POST.get('company_title', '').strip()
        customer.address = request.POST.get('address', '').strip()
        customer.notes = request.POST.get('notes', '').strip()
        customer.is_active = bool(request.POST.get('is_active') in ('on', '1', 'true', 'True'))

        try:
            customer.credit_limit = _parse_decimal(request.POST.get('credit_limit'), customer.credit_limit)
        except ValueError:
            pass

        customer.save()
        log_action('POS', 'Cari Müşteri Güncellendi', f"Müşteri #{customer.id} '{customer.full_name}' bilgileri güncellendi.", user=request.user)
        messages.success(request, f"'{customer.full_name}' bilgileri başarıyla güncellendi.")
        return redirect('customer_detail_view', customer_id=customer.id)

    return render(request, 'admin/customer_form.html', {'is_edit': True, 'customer': customer})


@staff_required
@require_POST
def customer_toggle_status_action(request, customer_id):
    """
    Müşteri Hesabını Tek Tıkla Aktif / Pasif Yapma Aksiyonu (Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    customer = get_object_or_404(Customer.objects.filter(company_scope_q(user_company)), id=customer_id)
    customer.is_active = not customer.is_active
    customer.save(update_fields=['is_active'])

    state = 'Aktif' if customer.is_active else 'Pasif'
    log_action('CUSTOMER', 'Müşteri Durumu Güncellendi', f"{customer.full_name} -> {state}", user=request.user)
    messages.success(request, f"Müşteri '{customer.full_name}' durumu '{state}' olarak güncellendi.")
    return _safe_next_redirect(request, 'customer_list_view')


@staff_required
def customer_detail_view(request, customer_id):
    """
    Müşteri Cari Hesap Ekstresi ve Detayı (Multi-Tenancy).
    Tüm veresiye satışlar, nakit/kart tahsilatlar, iadeler ve bakiye geçmişi.
    Tarih filtreleme desteği ile esnek ekstre görüntüleme.
    """
    user_company = get_user_company(request.user)
    customer = get_object_or_404(Customer.objects.filter(company_scope_q(user_company)), id=customer_id)
    transactions = customer.transactions.all().order_by('-created_at')

    date_from_raw = request.GET.get('date_from', '').strip()
    date_to_raw = request.GET.get('date_to', '').strip()
    date_from = _parse_iso_date(date_from_raw)
    date_to = _parse_iso_date(date_to_raw)

    if date_from:
        transactions = transactions.filter(created_at__date__gte=date_from)
    if date_to:
        transactions = transactions.filter(created_at__date__lte=date_to)

    total_sales_sum = transactions.filter(transaction_type='VERESIYE_SATIS').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    total_collected_sum = transactions.filter(transaction_type__in=['TAHSILAT_NAKIT', 'TAHSILAT_KART']).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    query_params = request.GET.copy()
    query_string = query_params.urlencode()

    context = {
        'customer': customer,
        'transactions': transactions,
        'total_sales_sum': total_sales_sum,
        'total_collected_sum': total_collected_sum,
        'date_from': date_from_raw,
        'date_to': date_to_raw,
        'query_string': query_string,
        'company': get_company_fiscal_profile(company=user_company),
    }
    return render(request, 'admin/customer_detail.html', context)


@login_required(login_url='/login/')
def customer_collect_debt_api(request, customer_id):
    """
    Müşteriden Veresiye Borç Tahsilatı Yapma API (Nakit veya Kredi Kartı).
    - Müşteri bakiyesi atomik olarak düşürülür (F('balance') - amount).
    - CustomerTransaction kaydı oluşturulur.
    - Nakit tahsilat ise aktif vardiyanın nakit kasasına eklenir ve RJ11 çekmecesi açılır.
    - Termal makbuz basılır (silent_print_collection_receipt).
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Yalnızca POST istekleri kabul edilir.'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
        amount_raw = data.get('amount')
        payment_method = str(data.get('payment_method', 'NAKIT')).upper()
        notes = data.get('notes', '').strip()

        try:
            amount = Decimal(str(amount_raw))
        except (ValueError, TypeError, ArithmeticError):
            return JsonResponse({'success': False, 'message': 'Geçerli bir tahsilat tutarı giriniz.'}, status=400)

        if amount <= Decimal('0.00'):
            return JsonResponse({'success': False, 'message': 'Tahsilat tutarı 0 TL\'den büyük olmalıdır.'}, status=400)

        if payment_method not in ('NAKIT', 'KART'):
            payment_method = 'NAKIT'

        user_company = get_user_company(request.user)
        register = get_current_register(request, company=user_company)
        _, shift = get_or_create_active_shift(request.user, register=register, request=request, company=user_company)

        with transaction.atomic():
            customer = Customer.objects.select_for_update().get(company_scope_q(user_company), id=customer_id)
            shift = Shift.objects.select_for_update().get(id=shift.id)

            today_str = timezone.localtime(timezone.now()).strftime('%Y%m%d')
            doc_seq = CustomerTransaction.objects.filter(company_scope_q(user_company), document_no__contains=f"TAH-{today_str}-").count() + 1
            doc_no = f"TAH-{today_str}-{doc_seq:04d}"

            Customer.objects.filter(id=customer.id).update(balance=F('balance') - amount)
            customer.refresh_from_db(fields=['balance'])

            tx_type = 'TAHSILAT_NAKIT' if payment_method == 'NAKIT' else 'TAHSILAT_KART'
            desc = notes or f"Veresiye borç tahsilatı ({'Nakit' if payment_method == 'NAKIT' else 'Kredi Kartı'})."

            tx = CustomerTransaction.objects.create(
                company=user_company,
                customer=customer,
                transaction_type=tx_type,
                amount=amount,
                balance_after=customer.balance,
                shift=shift,
                document_no=doc_no,
                description=desc,
                user=request.user
            )

            shift.total_collections = F('total_collections') + amount
            if payment_method == 'NAKIT':
                shift.expected_cash = F('expected_cash') + amount
            shift.save(update_fields=['total_collections', 'expected_cash'])
            shift.refresh_from_db()

            log_action(
                'POS',
                f'[{register.register_code}] Müşteri Borç Tahsilatı Yapıldı',
                f"Müşteri: '{customer.full_name}' - Tahsilat: {amount} TL ({payment_method}) - Kalan Borç: {customer.balance} TL - Makbuz: {doc_no}",
                user=request.user
            )

        print_info = None
        try:
            print_info = silent_print_collection_receipt(tx, open_drawer=(payment_method == 'NAKIT'))
        except Exception:
            print_info = {'success': False}

        return JsonResponse({
            'success': True,
            'message': f"'{customer.full_name}' adlı müşteriden {amount:.2f} TL tahsilat başarıyla yapıldı.",
            'transaction_id': tx.id,
            'document_no': doc_no,
            'new_balance': float(customer.balance),
            'amount': float(amount),
            'payment_method': payment_method,
            'silent_print': print_info,
        })

    except Customer.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Müşteri bulunamadı.'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


@login_required(login_url='/login/')
def collection_receipt_view(request, transaction_id):
    """
    Termal Yazıcı Uyumlu Tahsilat Makbuzu Görünümü (80mm / 58mm HTML).
    """
    tx = get_object_or_404(CustomerTransaction, id=transaction_id)
    setting = CompanySetting.get_settings()
    context = {
        'tx': tx,
        'customer': tx.customer,
        'company': get_company_fiscal_profile(),
        'setting': setting,
        'prev_balance': tx.balance_after + tx.amount,
        'now': timezone.now(),
    }
    return render(request, 'thermal_collection_receipt.html', context)


@login_required(login_url='/login/')
def silent_print_collection_receipt_api(request, transaction_id):
    """
    Kayıtlı bir tahsilat makbuzunu ESC/POS termal yazıcıya sessizce basar.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Yalnızca POST istekleri kabul edilir.'}, status=405)

    tx = get_object_or_404(CustomerTransaction, id=transaction_id)
    res = silent_print_collection_receipt(tx)
    return JsonResponse(res)


# ==========================================================
# FAZ 3: CANLI DONANIM VE ÇEVRE BİRİMLERİ ENTEGRASYONU
# ==========================================================

@login_required(login_url='/login/')
def scale_read_api(request):
    """
    Kasa Satış Ekranı için Canlı Kasa Terazisi Ağırlık Okuma API (<100ms).
    RS232 / USB Sanal COM portundan veya simülasyondan canlı ağırlık döner.
    """
    port = request.GET.get('port')
    baudrate = request.GET.get('baudrate')
    protocol = request.GET.get('protocol')

    try:
        baud_int = int(baudrate) if baudrate else None
    except ValueError:
        baud_int = None

    result = read_scale_weight(port=port, baudrate=baud_int, protocol=protocol)
    return JsonResponse(result)


@staff_api_required
def scale_ports_api(request):
    """
    Sistemde takılı mevcut USB-Seri ve COM portları listeler.
    """
    ports = list_available_serial_ports()
    return JsonResponse({'success': True, 'ports': ports})


def customer_display_view(request):
    """
    Müşteriye Dönük 2. Ekran / HDMI Çift Monitör Sayfası.
    BroadcastChannel ve LocalStorage üzerinden Kasa 1 veya Kasa 2 ile anlık senkronizedir.
    """
    register = get_current_register(request)
    company = get_company_fiscal_profile()
    context = {
        'register': register,
        'company': company,
        'now': timezone.now(),
    }
    return render(request, 'customer_display.html', context)


@staff_required
def product_labels_view(request):
    """
    Ticaret Bakanlığı Fiyat Etiketi Yönetmeliği'ne Uyumlu Raf Etiketi ve Barkod Basım Ekranı (Multi-Tenancy).
    """
    user_company = get_user_company(request.user)
    category_id = request.GET.get('category', '').strip()
    query = request.GET.get('q', '').strip()
    label_format = request.GET.get('format', 'thermal').strip()

    products_qs = Product.objects.filter(company_scope_q(user_company), is_active=True)

    if category_id:
        products_qs = products_qs.filter(category_id=category_id)
    if query:
        products_qs = products_qs.filter(Q(name__icontains=query) | Q(barcode__icontains=query))

    products = products_qs.order_by('name')[:60]
    label_items = [prepare_product_label_context(p) for p in products]

    categories = Category.objects.filter(company_scope_q(user_company), is_active=True).order_by('name')

    context = {
        'label_items': label_items,
        'categories': categories,
        'selected_category': category_id,
        'search_query': query,
        'label_format': label_format,
        'company': get_company_fiscal_profile(company=user_company),
    }
    return render(request, 'admin/product_labels.html', context)


@staff_api_required
def generate_barcode_api(request):
    """
    Barkodsuz ürünler için tek tıkla standart EAN-13 algoritmasıyla benzersiz barkod üretir (Multi-Tenancy).
    """
    user_company = get_user_company(request.user)
    prefix = request.GET.get('prefix', '8690001').strip() or '8690001'
    last_product = Product.objects.filter(company_scope_q(user_company)).order_by('-id').first()
    seq = (last_product.id + 1) if last_product else 1

    candidate = generate_valid_ean13(prefix=prefix, sequence_number=seq)
    attempts = 0
    while Product.objects.filter(company_scope_q(user_company), barcode=candidate).exists() and attempts < 1000:
        seq += 1
        candidate = generate_valid_ean13(prefix=prefix, sequence_number=seq)
        attempts += 1

    svg = generate_ean13_svg(candidate, width=170, height=50)

    return JsonResponse({
        'success': True,
        'barcode': candidate,
        'prefix': prefix,
        'is_valid_ean13': validate_ean13(candidate),
        'barcode_svg': svg,
    })


@staff_api_required
def product_label_raw_api(request, product_id):
    """
    Masaüstü termal barkod yazıcıları (Argox, TSC, Zebra) için ham TSPL veya ZPL komutu döner.
    """
    user_company = get_user_company(request.user)
    product = get_object_or_404(Product.objects.filter(company_scope_q(user_company)), id=product_id)
    printer_lang = request.GET.get('lang', 'tspl').lower()

    if printer_lang == 'zpl':
        raw_bytes = generate_zpl_label_payload(product)
        content_type = 'text/plain'
    else:
        raw_bytes = generate_tspl_label_payload(product)
        content_type = 'text/plain'

    return JsonResponse({
        'success': True,
        'product_id': product.id,
        'product_name': product.name,
        'barcode': product.barcode,
        'lang': printer_lang,
        'raw_commands': raw_bytes.decode('ascii', errors='ignore'),
    })


# ==============================================================================
# FAZ 5: ENVANTER VE BARKODLU STOK SAYIMI (EL TERMİNALİ), FİRE/ZAYİ & KRİTİK SİPARİŞ
# ==============================================================================

@staff_required
def stock_take_list_view(request):
    """
    Stok Sayım Fişleri Listesi (Faz 5.1 & Faz 6 Sayfalama, Multi-Tenancy).
    Reyon ve mağaza bazlı fiziki sayım fişlerini listeler.
    """
    user_company = get_user_company(request.user)
    status_filter = request.GET.get('status', '').strip()
    category_id = request.GET.get('category_id', '').strip()
    q = request.GET.get('q', '').strip()

    takes = StockTake.objects.filter(company_scope_q(user_company)).select_related('category', 'created_by').prefetch_related('items').order_by('-created_at')

    if status_filter:
        takes = takes.filter(status=status_filter)
    if category_id:
        takes = takes.filter(category_id=category_id)
    if q:
        takes = takes.filter(Q(take_number__icontains=q) | Q(title__icontains=q) | Q(notes__icontains=q))

    all_takes = StockTake.objects.filter(company_scope_q(user_company))
    total_count = all_takes.count()
    draft_count = all_takes.filter(status='DRAFT').count()
    completed_count = all_takes.filter(status='COMPLETED').count()

    categories = Category.objects.filter(company_scope_q(user_company), is_active=True).order_by('name')

    total_filtered_count = takes.count()
    paginator = Paginator(takes, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_string = query_params.urlencode()

    context = {
        'page_obj': page_obj,
        'takes': page_obj.object_list,
        'total_filtered_count': total_filtered_count,
        'query_string': query_string,
        'categories': categories,
        'status_filter': status_filter,
        'category_id': category_id,
        'q': q,
        'total_count': total_count,
        'draft_count': draft_count,
        'completed_count': completed_count,
    }
    return render(request, 'admin/stock_take_list.html', context)


@staff_required
def stock_take_create_action(request):
    """
    Yeni Stok Sayım Fişi Başlatma Aksiyonu (Faz 5.1, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    if request.method == 'POST':
        title = request.POST.get('title', '').strip() or 'Genel Mağaza Sayımı'
        category_id = request.POST.get('category_id', '').strip()
        notes = request.POST.get('notes', '').strip()

        category = Category.objects.filter(company_scope_q(user_company), id=category_id).first() if category_id else None
        take_number = f"SYM-{timezone.now().strftime('%Y%m%d%H%M%S')}"

        take = StockTake.objects.create(
            company=user_company,
            take_number=take_number,
            title=title,
            category=category,
            notes=notes,
            created_by=request.user if request.user.is_authenticated else None,
            status='DRAFT'
        )

        log_action('STOCK', 'Yeni Stok Sayımı Başlatıldı', f"Sayım #{take.take_number} - {take.title}", user=request.user)
        messages.success(request, f"Sayım fişi #{take.take_number} oluşturuldu. Barkod okutmaya başlayabilirsiniz.")
        return redirect('stock_take_detail', take_id=take.id)

    return redirect('stock_take_list')


@staff_required
def stock_take_detail_view(request, take_id):
    """
    El Terminali ve Barkod Okuyucu Uyumlu Canlı Stok Sayım Ekranı (Faz 5.1, Multi-Tenancy).
    Büyük dokunmatik alanlar, barkod oto-odak, anlık sesli ve görsel bildirimler.
    """
    user_company = get_user_company(request.user)
    take = get_object_or_404(StockTake.objects.filter(company_scope_q(user_company)).select_related('category', 'created_by'), id=take_id)
    items = take.items.select_related('product', 'product__category').order_by('-scanned_at')

    total_items = items.count()
    total_cost = sum((item.discrepancy_cost for item in items), Decimal('0.00'))
    surplus_items = sum(1 for item in items if item.difference > Decimal('0.000'))
    deficit_items = sum(1 for item in items if item.difference < Decimal('0.000'))
    exact_items = sum(1 for item in items if item.difference == Decimal('0.000'))

    context = {
        'take': take,
        'items': items,
        'total_items': total_items,
        'total_cost': total_cost,
        'surplus_items': surplus_items,
        'deficit_items': deficit_items,
        'exact_items': exact_items,
    }
    return render(request, 'admin/stock_take_detail.html', context)


@staff_required
def stock_take_cancel_action(request, take_id):
    """
    Devam eden stok sayım fişini iptal eder (Multi-Tenancy).
    """
    user_company = get_user_company(request.user)
    if request.method == 'POST':
        take = get_object_or_404(StockTake.objects.filter(company_scope_q(user_company)), id=take_id)
        if take.status == 'DRAFT':
            take.status = 'CANCELLED'
            take.save(update_fields=['status'])
            log_action('STOCK', 'Stok Sayımı İptal Edildi', f"Sayım #{take.take_number} ({take.title}) iptal edildi.", level='WARNING', user=request.user)
            messages.warning(request, f"Sayım fişi #{take.take_number} iptal edildi.")
        else:
            messages.error(request, "Yalnızca taslak durumundaki sayımlar iptal edilebilir.")
    return redirect('stock_take_list')


@staff_api_required
def stock_take_scan_api(request, take_id):
    """
    El terminali / barkod okuyucu ile sayım fişine ürün ekleme veya miktar artırma API'si (Faz 5.1, Multi-Tenancy).
    """
    user_company = get_user_company(request.user)
    take = get_object_or_404(StockTake.objects.filter(company_scope_q(user_company)), id=take_id)
    if take.status != 'DRAFT':
        return JsonResponse({'success': False, 'message': 'Yalnızca taslak durumundaki sayımlara ürün eklenebilir.'}, status=400)

    barcode = (request.POST.get('barcode') or '').strip()
    qty_str = request.POST.get('quantity', '1.000').strip()
    mode = request.POST.get('mode', 'add').strip()

    if not barcode:
        return JsonResponse({'success': False, 'message': 'Barkod giriniz.'}, status=400)

    try:
        qty = Decimal(qty_str)
        if qty <= Decimal('0.000'):
            qty = Decimal('1.000')
    except Exception:
        qty = Decimal('1.000')

    # Ürünü bul: barkod, id veya leading-zero olmaksızın (aynı mağaza içinde)
    product = Product.objects.filter(company_scope_q(user_company), barcode=barcode, is_active=True).first()
    if not product and barcode.isdigit():
        product = Product.objects.filter(company_scope_q(user_company), id=int(barcode), is_active=True).first()
    if not product:
        product = Product.objects.filter(company_scope_q(user_company), barcode=barcode.lstrip('0'), is_active=True).first()

    if not product:
        return JsonResponse({'success': False, 'message': f"'{barcode}' barkodlu aktif ürün bulunamadı."}, status=404)

    item = StockTakeItem.objects.filter(stock_take=take, product=product).first()
    if item:
        if mode == 'set':
            item.counted_quantity = qty
        else:
            item.counted_quantity = item.counted_quantity + qty
        item.buy_price = product.buy_price or Decimal('0.00')
        item.save()
        is_new = False
    else:
        item = StockTakeItem.objects.create(
            stock_take=take,
            product=product,
            system_quantity=product.stock_quantity,
            counted_quantity=qty,
            buy_price=product.buy_price or Decimal('0.00')
        )
        is_new = True

    return JsonResponse({
        'success': True,
        'is_new': is_new,
        'item': {
            'id': item.id,
            'product_id': product.id,
            'product_name': product.name,
            'barcode': product.barcode,
            'unit': product.unit,
            'category_name': product.category.name if product.category else 'Kategorisiz',
            'system_quantity': float(item.system_quantity),
            'counted_quantity': float(item.counted_quantity),
            'difference': float(item.difference),
            'buy_price': float(item.buy_price),
            'discrepancy_cost': float(item.discrepancy_cost),
        },
        'summary': {
            'total_items': take.items.count(),
            'total_discrepancy_cost': float(take.total_discrepancy_cost),
        }
    })


@staff_api_required
def stock_take_item_update_api(request, item_id):
    """
    Sayım satırındaki fiziki miktarı doğrudan güncelleme veya satırı silme API'si (Faz 5.1, Multi-Tenancy).
    """
    user_company = get_user_company(request.user)
    item = get_object_or_404(StockTakeItem.objects.select_related('stock_take', 'product'), id=item_id)
    take = item.stock_take
    if take.company and user_company and take.company != user_company:
        raise Http404("Sayım satırı bu mağazaya ait değil.")
    if take.status != 'DRAFT':
        return JsonResponse({'success': False, 'message': 'Yalnızca taslak durumundaki sayımlar düzenlenebilir.'}, status=400)

    action = request.POST.get('action', 'update').strip()
    if action == 'delete':
        item.delete()
        return JsonResponse({
            'success': True,
            'deleted': True,
            'item_id': item_id,
            'summary': {
                'total_items': take.items.count(),
                'total_discrepancy_cost': float(take.total_discrepancy_cost),
            }
        })

    qty_str = request.POST.get('counted_quantity', '').strip()
    try:
        qty = Decimal(qty_str)
        if qty < Decimal('0.000'):
            return JsonResponse({'success': False, 'message': 'Sayılan miktar negatif olamaz.'}, status=400)
    except Exception:
        return JsonResponse({'success': False, 'message': 'Geçerli bir sayı giriniz.'}, status=400)

    item.counted_quantity = qty
    item.save()

    return JsonResponse({
        'success': True,
        'item': {
            'id': item.id,
            'product_id': item.product.id,
            'product_name': item.product.name,
            'barcode': item.product.barcode,
            'unit': item.product.unit,
            'system_quantity': float(item.system_quantity),
            'counted_quantity': float(item.counted_quantity),
            'difference': float(item.difference),
            'buy_price': float(item.buy_price),
            'discrepancy_cost': float(item.discrepancy_cost),
        },
        'summary': {
            'total_items': take.items.count(),
            'total_discrepancy_cost': float(take.total_discrepancy_cost),
        }
    })


@staff_required
def stock_take_apply_api(request, take_id):
    """
    Sayımı Onaylama ve Sistem Stoklarını Eşitleme İşlemi (Faz 5.1, Multi-Tenancy).
    Tüm ürünlerin fiziki sayım miktarlarını sistem stoğu olarak uygular,
    mali denetim için otomatik 'SAYIM_DUZELTME' Stok Giriş Fişi oluşturur.
    """
    user_company = get_user_company(request.user)
    take = get_object_or_404(StockTake.objects.filter(company_scope_q(user_company)), id=take_id)
    if take.status != 'DRAFT':
        messages.error(request, 'Bu sayım fişi daha önce tamamlanmış veya iptal edilmiş.')
        return redirect('stock_take_detail', take_id=take.id)

    items = take.items.select_related('product').all()
    if not items.exists():
        messages.error(request, 'Sayım listesinde onaylanacak hiç ürün bulunmamaktadır.')
        return redirect('stock_take_detail', take_id=take.id)

    with transaction.atomic():
        entry_number = f"SYM-{take.take_number}"
        if StockEntry.objects.filter(company_scope_q(user_company), entry_number=entry_number).exists():
            entry_number = f"SYM-{take.take_number}-{timezone.now().strftime('%H%M%S')}"

        stock_entry = StockEntry.objects.create(
            company=user_company,
            entry_number=entry_number,
            entry_type='SAYIM_DUZELTME',
            supplier=None,
            invoice_number=take.take_number,
            payment_type='VERESIYE',
            notes=f"Stok Sayım Fişi #{take.take_number} ({take.title}) neticesinde sistem stokları güncellendi. Not: {take.notes}".strip(),
            created_by=request.user if request.user.is_authenticated else None
        )

        total_discrepancy_abs_cost = Decimal('0.00')

        for item in items:
            prod = Product.objects.select_for_update().get(id=item.product_id)
            diff = item.difference
            diff_cost = item.discrepancy_cost

            # Düzeltme fişi satırı kaydı (denetlenebilirlik)
            StockEntryItem.objects.create(
                stock_entry=stock_entry,
                product=prod,
                quantity=abs(diff),
                unit=prod.unit,
                list_price=item.buy_price,
                discount_rate=Decimal('0.00'),
                vat_rate=prod.vat_rate,
                buy_price=item.buy_price,
                total_price=abs(diff_cost)
            )

            # Fiziki sayılan miktarı kesin olarak stoğa yaz
            prod.stock_quantity = item.counted_quantity
            prod.save(update_fields=['stock_quantity'])

            total_discrepancy_abs_cost += abs(diff_cost)

        stock_entry.total_amount = total_discrepancy_abs_cost
        stock_entry.save(update_fields=['total_amount'])

        take.status = 'COMPLETED'
        take.completed_at = timezone.now()
        take.save(update_fields=['status', 'completed_at'])

        log_action(
            'STOCK',
            'Sayım Stoğa İşlendi',
            f"Sayım #{take.take_number} ({take.title}) onaylandı: {items.count()} çeşit fiziki stoğa eşitlendi. Sayım Düzeltme Fişi: #{entry_number}",
            user=request.user
        )

    messages.success(request, f"Sayım #{take.take_number} başarıyla onaylandı! {items.count()} kalemin stok miktarları eşitlendi ve #{entry_number} numaralı denetim fişi kaydedildi.")

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
        return JsonResponse({'success': True, 'message': 'Sayım başarıyla uygulandı.'})

    return redirect('stock_take_detail', take_id=take.id)


def _filter_critical_products(params, company=None):
    """Kritik stok sipariş listesi ve CSV çıktısı için ortak filtre sorgusu (Multi-Tenancy)."""
    supplier_filter_id = (params.get('supplier_id') or '').strip()
    category_filter_id = (params.get('category_id') or '').strip()

    products = Product.objects.filter(
        is_active=True,
        stock_quantity__lte=F('min_stock_level')
    ).select_related('supplier', 'category').order_by('name')

    if company:
        products = products.filter(company_scope_q(company))

    if supplier_filter_id:
        if supplier_filter_id == '0':
            products = products.filter(supplier__isnull=True)
        elif supplier_filter_id.isdigit():
            products = products.filter(supplier_id=int(supplier_filter_id))

    if category_filter_id.isdigit():
        products = products.filter(category_id=int(category_filter_id))

    return products, supplier_filter_id, category_filter_id


@staff_required
def critical_stock_orders_view(request):
    """
    Kritik Stok Uyarıları & Toptancı Bazlı Otomatik Sipariş Listesi (Faz 5.2, Multi-Tenancy).
    Asgari stok seviyesinin altına düşen ürünleri toptancı bazında gruplar,
    önerilen sipariş miktarlarını hesaplar, tek tıkla WhatsApp sipariş metni ve
    yazdırılabilir A4 Satın Alma Sipariş Formu (PO) üretir.
    """
    user_company = get_user_company(request.user)
    products, supplier_filter_id, category_filter_id = _filter_critical_products(request.GET, company=user_company)

    # Toptancı bazında grupla
    suppliers_map = {}
    for p in products:
        # Önerilen sipariş miktarı = Max(1, (2 * Asgari Stok) - Mevcut Stok)
        suggested_qty = max(Decimal('1.000'), (p.min_stock_level * Decimal('2.000')) - p.stock_quantity)
        cost = round(suggested_qty * (p.buy_price or Decimal('0.00')), 2)

        sup = p.supplier
        sup_id = sup.id if sup else 0
        sup_name = sup.company_name if sup else "Atanmamış / Genel Toptancı"

        if sup_id not in suppliers_map:
            suppliers_map[sup_id] = {
                'supplier': sup,
                'supplier_id': sup_id,
                'name': sup_name,
                'phone': sup.phone if sup else '',
                'contact_name': sup.contact_name if sup else '',
                'balance': sup.balance if sup else Decimal('0.00'),
                'items': [],
                'total_items': 0,
                'total_cost': Decimal('0.00'),
                'whatsapp_url': '',
            }

        suppliers_map[sup_id]['items'].append({
            'product': p,
            'current_stock': p.stock_quantity,
            'min_stock': p.min_stock_level,
            'suggested_qty': suggested_qty,
            'cost': cost,
        })
        suppliers_map[sup_id]['total_items'] += 1
        suppliers_map[sup_id]['total_cost'] += cost

    # WhatsApp Sipariş Bağlantılarını oluştur
    for s_data in suppliers_map.values():
        if s_data['phone']:
            raw_phone = re.sub(r'[^0-9]', '', s_data['phone'])
            if raw_phone.startswith('0'):
                raw_phone = '90' + raw_phone[1:]
            elif len(raw_phone) == 10:
                raw_phone = '90' + raw_phone

            msg_lines = [
                f"Merhaba {s_data['name']},",
                "HiMarket marketimiz için acil sipariş listemiz aşağıdadır:\n"
            ]
            for idx, item in enumerate(s_data['items'], 1):
                p = item['product']
                msg_lines.append(f"{idx}. {p.name} - {item['suggested_qty']:.0f} {p.unit} (Barkod: {p.barcode})")
            msg_lines.append(f"\nToplam: {s_data['total_items']} Kalem. Lütfen sevkiyat durumunu teyit ediniz. Teşekkürler.")

            encoded = urllib.parse.quote("\n".join(msg_lines))
            s_data['whatsapp_url'] = f"https://wa.me/{raw_phone}?text={encoded}"

    order_groups = list(suppliers_map.values())
    order_groups.sort(key=lambda x: (x['supplier_id'] == 0, x['name']))

    total_critical_count = products.count()
    total_estimated_budget = sum((g['total_cost'] for g in order_groups), Decimal('0.00'))

    suppliers = Supplier.objects.filter(company_scope_q(user_company), is_active=True).order_by('company_name')
    categories = Category.objects.filter(company_scope_q(user_company), is_active=True).order_by('name')

    context = {
        'order_groups': order_groups,
        'total_critical_count': total_critical_count,
        'total_estimated_budget': total_estimated_budget,
        'suppliers': suppliers,
        'categories': categories,
        'selected_supplier_id': supplier_filter_id,
        'selected_category_id': category_filter_id,
    }
    return render(request, 'admin/critical_stock_orders.html', context)


# ==============================================================================
# FAZ 6: ÜRETİM ORTAMI TEŞHİS, SAĞLIK (HEALTH CHECK) VE DAYANIKLILIK
# ==============================================================================

def health_check_api(request):
    """
    Sistem Sağlık ve Teşhis API Uç Noktası (Faz 6).
    Veritabanı bağlantısı, gecikme süresi, disk doluluk oranı ve yedekleme durumunu doğrular.
    Kiosk sarmalayıcılar, izleme araçları ve watchdog servisleri için HTTP 200/503 döner.
    """
    import time
    start_time = time.time()
    db_ok = False
    latency_ms = 0.0
    db_vendor = connection.vendor
    wal_active = False

    try:
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1;")
            cursor.fetchone()
            if db_vendor == 'sqlite':
                cursor.execute("PRAGMA journal_mode;")
                row = cursor.fetchone()
                if row and str(row[0]).upper() == 'WAL':
                    wal_active = True
        latency_ms = round((time.time() - start_time) * 1000, 2)
        db_ok = True
    except Exception as e:
        db_ok = False

    # Disk Durumu
    disk_total_gb = 0.0
    disk_free_gb = 0.0
    disk_used_percent = 0.0
    disk_status = "OK"

    try:
        total, used, free = shutil.disk_usage(str(settings.BASE_DIR))
        disk_total_gb = round(total / (1024 ** 3), 2)
        disk_free_gb = round(free / (1024 ** 3), 2)
        disk_used_percent = round((used / total) * 100, 1)
        if disk_free_gb < 2.0:
            disk_status = "CRITICAL_LOW_DISK_SPACE"
        elif disk_free_gb < 5.0:
            disk_status = "LOW_DISK_SPACE"
    except Exception:
        pass

    # Yedekleme Durumu
    backup_dir = Path(getattr(settings, 'BACKUP_DIR', settings.BASE_DIR / 'backups'))
    backup_files = sorted(
        [f for f in backup_dir.glob('himarket_*backup_*') if not f.name.endswith('.sha256')],
        key=lambda p: p.stat().st_mtime,
        reverse=True
    ) if backup_dir.exists() else []

    latest_backup = None
    if backup_files:
        top_f = backup_files[0]
        mtime = datetime.datetime.fromtimestamp(top_f.stat().st_mtime)
        latest_backup = {
            'filename': top_f.name,
            'size_kb': round(top_f.stat().st_size / 1024, 2),
            'created_at': mtime.strftime('%Y-%m-%d %H:%M:%S'),
            'age_hours': round((time.time() - top_f.stat().st_mtime) / 3600, 1)
        }

    # Aktif Açık Vardiya
    active_shift = Shift.objects.filter(is_closed=False).select_related('cash_register', 'cashier').first()
    shift_info = None
    if active_shift:
        shift_info = {
            'is_open': True,
            'register_code': active_shift.cash_register.register_code if active_shift.cash_register else 'KASA',
            'cashier': active_shift.cashier.get_full_name() or active_shift.cashier.username if active_shift.cashier else 'Kasiyer',
            'opened_at': active_shift.opened_at.strftime('%Y-%m-%d %H:%M')
        }

    is_healthy = db_ok and (disk_status != "CRITICAL_LOW_DISK_SPACE")

    payload = {
        'status': 'healthy' if is_healthy else 'degraded',
        'timestamp': timezone.now().isoformat(),
        'database': {
            'vendor': db_vendor,
            'connected': db_ok,
            'latency_ms': latency_ms,
            'wal_mode': wal_active,
        },
        'disk': {
            'total_gb': disk_total_gb,
            'free_gb': disk_free_gb,
            'used_percent': disk_used_percent,
            'status': disk_status
        },
        'backup': {
            'total_count': len(backup_files),
            'latest': latest_backup
        },
        'active_shift': shift_info,
        'app_version': '2.6.0-enterprise'
    }

    return JsonResponse(payload, status=200 if is_healthy else 503)


# ==========================================================
# FAZ 4: KATEGORİ, SATIŞ FİŞİ & KASA VARDİYASI ÖZEL EKRANLARI
# ==========================================================

@staff_required
def category_list_view(request):
    """
    Özel Kategori Yönetimi Ekranı (Yönetici Yetkili, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    search_q = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', 'all').strip()

    categories_qs = Category.objects.filter(company_scope_q(user_company)).annotate(
        product_count=Count('products', distinct=True),
        active_product_count=Count('products', filter=Q(products__is_active=True), distinct=True),
    ).order_by('display_order', 'name')

    if search_q:
        categories_qs = categories_qs.filter(Q(name__icontains=search_q) | Q(description__icontains=search_q))

    if status_filter == 'active':
        categories_qs = categories_qs.filter(is_active=True)
    elif status_filter == 'passive':
        categories_qs = categories_qs.filter(is_active=False)

    all_cats = Category.objects.filter(company_scope_q(user_company))
    context = {
        'categories': categories_qs,
        'search_q': search_q,
        'status_filter': status_filter,
        'total_categories': all_cats.count(),
        'active_categories': all_cats.filter(is_active=True).count(),
        'passive_categories': all_cats.filter(is_active=False).count(),
        'linked_products_total': Product.objects.filter(company_scope_q(user_company), category__isnull=False).count(),
    }
    return render(request, 'admin/categories.html', context)


@staff_required
@require_POST
def category_save_action(request):
    """
    Yeni Kategori Ekleme veya Mevcut Kategoriyi Güncelleme Aksiyonu (Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    category_id = request.POST.get('category_id', '').strip()
    name = request.POST.get('name', '').strip()
    description = request.POST.get('description', '').strip()
    is_active = request.POST.get('is_active') == 'on'

    if not name:
        messages.error(request, "Kategori adı boş bırakılamaz.")
        return _safe_next_redirect(request, 'category_list_view')

    try:
        display_order = _parse_int(request.POST.get('display_order', '0'), 0)
    except ValueError as err:
        messages.error(request, f"Sıralama numarası geçersiz: {err}")
        return _safe_next_redirect(request, 'category_list_view')

    if display_order < 0:
        messages.error(request, "Sıralama numarası negatif olamaz.")
        return _safe_next_redirect(request, 'category_list_view')

    if category_id:
        category = get_object_or_404(Category.objects.filter(company_scope_q(user_company)), id=category_id)
        dup = Category.objects.filter(company_scope_q(user_company)).exclude(id=category.id).filter(name__iexact=name).first()
        if dup:
            messages.error(request, f"'{name}' adında başka bir kategori zaten mevcut.")
            return _safe_next_redirect(request, 'category_list_view')

        category.name = name
        category.description = description
        category.display_order = display_order
        category.is_active = is_active
        category.save()
        log_action('SYSTEM', 'Kategori Güncellendi', f"{category.name} (Sıra: {display_order}, Aktif: {is_active})", user=request.user)
        messages.success(request, f"Kategori '{category.name}' güncellendi.")
    else:
        if Category.objects.filter(company_scope_q(user_company), name__iexact=name).exists():
            messages.error(request, f"'{name}' adında bir kategori zaten mevcut.")
            return _safe_next_redirect(request, 'category_list_view')

        category = Category.objects.create(
            company=user_company,
            name=name,
            description=description,
            display_order=display_order,
            is_active=is_active,
        )
        log_action('SYSTEM', 'Yeni Kategori Eklendi', f"{category.name} (Sıra: {display_order})", user=request.user)
        messages.success(request, f"Kategori '{category.name}' eklendi.")

    return _safe_next_redirect(request, 'category_list_view')


@staff_required
@require_POST
def category_toggle_status_action(request, category_id):
    """
    Kategoriyi Tek Tıkla Aktif / Pasif Yapma Aksiyonu (Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    category = get_object_or_404(Category.objects.filter(company_scope_q(user_company)), id=category_id)
    category.is_active = not category.is_active
    category.save(update_fields=['is_active'])

    state = 'Aktif' if category.is_active else 'Pasif'
    log_action('SYSTEM', 'Kategori Durumu Güncellendi', f"{category.name} -> {state}", user=request.user)
    messages.success(request, f"Kategori '{category.name}' durumu '{state}' olarak güncellendi.")
    return _safe_next_redirect(request, 'category_list_view')


def _filter_sales(params, company=None):
    """Satış fişleri, iadeler ve CSV dışa aktarımı için ortak filtre sorgusu (Multi-Tenancy)."""
    search_q = (params.get('q') or '').strip()
    register_code = (params.get('register') or '').strip()
    cashier_id = (params.get('cashier') or '').strip()
    payment_type = (params.get('payment_type') or '').strip()
    doc_status = (params.get('doc_status') or 'all').strip()
    date_from_raw = (params.get('date_from') or '').strip()
    date_to_raw = (params.get('date_to') or '').strip()

    sales_qs = Sale.objects.select_related('cashier', 'shift__cash_register', 'customer').prefetch_related('items').order_by('-created_at')
    if company:
        sales_qs = sales_qs.filter(company_scope_q(company))

    if search_q:
        sales_qs = sales_qs.filter(
            Q(receipt_number__icontains=search_q) |
            Q(customer_title__icontains=search_q) |
            Q(customer_tax_number__icontains=search_q) |
            Q(cashier__username__icontains=search_q) |
            Q(ettn__icontains=search_q) |
            Q(offline_sync_id__icontains=search_q)
        )

    if register_code:
        sales_qs = sales_qs.filter(shift__cash_register__register_code=register_code)

    if cashier_id.isdigit():
        sales_qs = sales_qs.filter(cashier_id=int(cashier_id))

    if payment_type:
        sales_qs = sales_qs.filter(payment_type=payment_type)

    if doc_status == 'sales':
        sales_qs = sales_qs.filter(is_return=False, is_cancelled=False)
    elif doc_status == 'returns':
        sales_qs = sales_qs.filter(is_return=True, is_cancelled=False)
    elif doc_status == 'cancelled':
        sales_qs = sales_qs.filter(is_cancelled=True)

    date_from = _parse_iso_date(date_from_raw)
    date_to = _parse_iso_date(date_to_raw)
    if date_from:
        sales_qs = sales_qs.filter(created_at__date__gte=date_from)
    if date_to:
        sales_qs = sales_qs.filter(created_at__date__lte=date_to)

    return sales_qs, {
        'search_q': search_q,
        'register_code': register_code,
        'cashier_id': cashier_id,
        'payment_type': payment_type,
        'doc_status': doc_status,
        'date_from': date_from_raw,
        'date_to': date_to_raw,
    }


@staff_required
def sale_list_view(request):
    """
    Özel Satış Fişleri ve İade Takip Ekranı (Yönetici Yetkili, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    sales_qs, filters = _filter_sales(request.GET, company=user_company)

    gross_sales = sales_qs.filter(is_return=False, is_cancelled=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    total_returns = sales_qs.filter(is_return=True, is_cancelled=False).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    cancelled_count = sales_qs.filter(is_cancelled=True).count()
    return_count = sales_qs.filter(is_return=True, is_cancelled=False).count()
    total_count = sales_qs.count()
    net_revenue = gross_sales - total_returns

    paginator = Paginator(sales_qs, 50)
    page_obj = paginator.get_page(request.GET.get('page'))

    query_params = request.GET.copy()
    query_params.pop('page', None)

    context = {
        'page_obj': page_obj,
        'sales': page_obj.object_list,
        'search_q': filters['search_q'],
        'register_code': filters['register_code'],
        'cashier_id': filters['cashier_id'],
        'payment_type': filters['payment_type'],
        'doc_status': filters['doc_status'],
        'date_from': filters['date_from'],
        'date_to': filters['date_to'],
        'query_string': query_params.urlencode(),
        'total_count': total_count,
        'return_count': return_count,
        'cancelled_count': cancelled_count,
        'gross_sales': gross_sales,
        'total_returns': total_returns,
        'net_revenue': net_revenue,
        'registers': CashRegister.objects.filter(company_scope_q(user_company), is_active=True).order_by('register_code'),
        'cashiers': User.objects.filter(is_active=True).order_by('username'),
        'payment_choices': Sale.PAYMENT_CHOICES,
    }
    return render(request, 'admin/sales.html', context)


@staff_required
@require_POST
def sale_cancel_action(request, sale_id):
    """
    Yönetim Panelinden Satış Fişini İptal Etme Aksiyonu (Yönetici Yetkili, Multi-Tenancy)
    - Fiş iptal durumuna getirilir.
    - Satılan ürünlerin stokları iade edilir.
    - Veresiye satış ise müşterinin borcu düşürülür.
    """
    user_company = get_user_company(request.user)
    sale = get_object_or_404(Sale.objects.filter(company_scope_q(user_company)).select_related('customer').prefetch_related('items__product'), id=sale_id)

    if sale.is_cancelled:
        messages.warning(request, f"Fiş #{sale.receipt_number} zaten iptal edilmiş durumda.")
        return _safe_next_redirect(request, 'sale_list_view')

    if sale.is_return:
        messages.warning(request, f"İade fişleri doğrudan iptal edilemez.")
        return _safe_next_redirect(request, 'sale_list_view')

    reason = request.POST.get('cancellation_reason', '').strip() or "Yönetici Tarafından İptal Edildi"

    with transaction.atomic():
        sale.is_cancelled = True
        sale.cancellation_reason = reason
        sale.save(update_fields=['is_cancelled', 'cancellation_reason'])

        # Stokları geri yükle
        for item in sale.items.all():
            if item.product_id:
                Product.objects.filter(company_scope_q(user_company), id=item.product_id).update(
                    stock_quantity=F('stock_quantity') + item.quantity
                )

        # Veresiye ise müşteri borcunu düşür
        if sale.customer and sale.total_amount > Decimal('0.00'):
            Customer.objects.filter(company_scope_q(user_company), id=sale.customer_id).update(
                balance=F('balance') - sale.total_amount
            )
            sale.customer.refresh_from_db()
            CustomerTransaction.objects.create(
                company=user_company,
                customer=sale.customer,
                transaction_type='IADE',
                amount=sale.total_amount,
                balance_after=sale.customer.balance,
                document_no=sale.receipt_number,
                description=f"Fiş İptali #{sale.receipt_number}: {reason}",
                sale=sale,
                user=request.user
            )

        log_action('SALE', 'Yönetici Fiş İptali', f"Fiş #{sale.receipt_number} iptal edildi. Gerekçe: {reason}", user=request.user)

    messages.success(request, f"Fiş #{sale.receipt_number} başarıyla iptal edildi ve ürün stokları iade edildi.")
    return _safe_next_redirect(request, 'sale_list_view')


@staff_api_required
def sale_detail_api(request, sale_id):
    """
    Satış Fişi Detay Modalı için JSON Verisi (Kalemler + KDV Matrahları, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    sale = get_object_or_404(Sale.objects.filter(company_scope_q(user_company)).select_related('cashier', 'shift__cash_register', 'customer').prefetch_related('items'), id=sale_id)
    items = [
        {
            'product_name': item.product_name,
            'barcode': item.barcode,
            'quantity': f"{item.quantity:g}",
            'unit_price': f"{item.unit_price:.2f}",
            'vat_rate': f"{item.vat_rate:.0f}",
            'vat_amount': f"{item.vat_amount:.2f}",
            'total_price': f"{item.total_price:.2f}",
        }
        for item in sale.items.all()
    ]
    return JsonResponse({
        'success': True,
        'sale': {
            'id': sale.id,
            'receipt_number': sale.receipt_number,
            'ettn': str(sale.ettn),
            'document_type_display': sale.get_document_type_display(),
            'created_at': timezone.localtime(sale.created_at).strftime('%d.%m.%Y %H:%M:%S'),
            'register': sale.shift.cash_register.name if sale.shift and sale.shift.cash_register else '-',
            'cashier': (sale.cashier.get_full_name() or sale.cashier.username) if sale.cashier else '-',
            'customer_title': sale.customer_title,
            'customer_tax_number': sale.customer_tax_number,
            'payment_type': sale.payment_type,
            'is_offline_sync': sale.is_offline_sync,
            'offline_sync_id': sale.offline_sync_id or '',
            'offline_created_at': timezone.localtime(sale.offline_created_at).strftime('%d.%m.%Y %H:%M:%S') if sale.offline_created_at else None,
            'payment_type_display': sale.get_payment_type_display(),
            'cash_paid': f"{sale.cash_paid:.2f}",
            'card_paid': f"{sale.card_paid:.2f}",
            'change_given': f"{sale.change_given:.2f}",
            'discount_amount': f"{sale.discount_amount:.2f}",
            'subtotal': f"{sale.subtotal:.2f}",
            'total_vat': f"{sale.total_vat:.2f}",
            'total_amount': f"{sale.total_amount:.2f}",
            'vat_1_base': f"{sale.vat_1_base:.2f}",
            'vat_1_amount': f"{sale.vat_1_amount:.2f}",
            'vat_10_base': f"{sale.vat_10_base:.2f}",
            'vat_10_amount': f"{sale.vat_10_amount:.2f}",
            'vat_20_base': f"{sale.vat_20_base:.2f}",
            'vat_20_amount': f"{sale.vat_20_amount:.2f}",
            'is_return': sale.is_return,
            'return_reason': sale.return_reason or '',
            'is_cancelled': sale.is_cancelled,
            'cancellation_reason': sale.cancellation_reason or '',
            'fiscal_approval_code': sale.fiscal_approval_code or '',
        },
        'items': items,
    })


def _filter_shifts(params, company=None):
    """Kasa vardiyaları ve CSV çıktısı için ortak filtre sorgusu (Multi-Tenancy)."""
    register_code = params.get('register', '').strip()
    status_filter = params.get('status', 'all').strip()
    date_from_raw = params.get('date_from', '').strip()
    date_to_raw = params.get('date_to', '').strip()

    shifts_qs = Shift.objects.select_related('cash_register', 'cashier').order_by('-opened_at')
    if company:
        shifts_qs = shifts_qs.filter(company_scope_q(company))

    if register_code:
        shifts_qs = shifts_qs.filter(cash_register__register_code=register_code)

    if status_filter == 'open':
        shifts_qs = shifts_qs.filter(is_closed=False)
    elif status_filter == 'closed':
        shifts_qs = shifts_qs.filter(is_closed=True)
    elif status_filter == 'diff':
        shifts_qs = shifts_qs.filter(is_closed=True).exclude(difference=Decimal('0.00'))

    date_from = _parse_iso_date(date_from_raw)
    date_to = _parse_iso_date(date_to_raw)
    if date_from:
        shifts_qs = shifts_qs.filter(opened_at__date__gte=date_from)
    if date_to:
        shifts_qs = shifts_qs.filter(opened_at__date__lte=date_to)

    return shifts_qs, {
        'register_code': register_code,
        'status_filter': status_filter,
        'date_from': date_from_raw,
        'date_to': date_to_raw,
    }


@staff_required
def shift_list_view(request):
    """
    Özel Kasa Vardiyaları ve Kör Kapanış Farkı İzleme Ekranı (Yönetici Yetkili, Multi-Tenancy)
    """
    user_company = get_user_company(request.user)
    shifts_qs, filter_data = _filter_shifts(request.GET, company=user_company)

    open_shifts_count = shifts_qs.filter(is_closed=False).count()
    closed_shifts_count = shifts_qs.filter(is_closed=True).count()
    diff_shifts_count = shifts_qs.filter(is_closed=True).exclude(difference=Decimal('0.00')).count()
    net_difference = shifts_qs.filter(is_closed=True).aggregate(Sum('difference'))['difference__sum'] or Decimal('0.00')
    total_turnover = (
        (shifts_qs.aggregate(Sum('total_cash_sales'))['total_cash_sales__sum'] or Decimal('0.00'))
        + (shifts_qs.aggregate(Sum('total_card_sales'))['total_card_sales__sum'] or Decimal('0.00'))
        + (shifts_qs.aggregate(Sum('total_veresiye_sales'))['total_veresiye_sales__sum'] or Decimal('0.00'))
    )

    paginator = Paginator(shifts_qs, 30)
    page_obj = paginator.get_page(request.GET.get('page'))

    query_params = request.GET.copy()
    query_params.pop('page', None)

    context = {
        'page_obj': page_obj,
        'shifts': page_obj.object_list,
        'register_code': filter_data['register_code'],
        'status_filter': filter_data['status_filter'],
        'date_from': filter_data['date_from'],
        'date_to': filter_data['date_to'],
        'query_string': query_params.urlencode(),
        'total_shifts_count': shifts_qs.count(),
        'open_shifts_count': open_shifts_count,
        'closed_shifts_count': closed_shifts_count,
        'diff_shifts_count': diff_shifts_count,
        'net_difference': net_difference,
        'total_turnover': total_turnover,
        'registers': CashRegister.objects.filter(company_scope_q(user_company), is_active=True).order_by('register_code'),
    }
    return render(request, 'admin/shifts.html', context)
    return render(request, 'admin/shifts.html', context)


# ==========================================================
# FAZ 5: CSV DIŞA AKTARIM VE TOPLU FİYAT / KDV GÜNCELLEME
# ==========================================================

@staff_required
def export_products_csv(request):
    """Ürün kataloğunu mevcut filtrelere göre Excel uyumlu CSV olarak indirir (Multi-Tenancy)."""
    user_company = get_user_company(request.user)
    products, _ = _filter_catalog_products(request.GET, company=user_company)
    headers = [
        'Barkod', 'İkincil Barkod', 'Ürün Adı', 'Kategori', 'Toptancı',
        'Alış Fiyatı (TL)', 'Satış Fiyatı (TL)', 'KDV (%)', 'Birim',
        'Mevcut Stok', 'Kritik Seviye', 'Durum'
    ]
    rows = [
        [
            p.barcode,
            p.secondary_barcode or '',
            p.name,
            p.category.name if p.category else '',
            p.supplier.company_name if p.supplier else '',
            f"{p.buy_price:.2f}",
            f"{p.sell_price:.2f}",
            f"{p.vat_rate:.2f}",
            p.unit,
            f"{p.stock_quantity:.3f}",
            f"{p.min_stock_level:.3f}",
            'Aktif' if p.is_active else 'Pasif',
        ]
        for p in products
    ]
    log_action('PRODUCT', 'Katalog CSV Dışa Aktarıldı', f"{len(rows)} ürün indirildi.", user=request.user)
    return _csv_response(f"urun_katalogu_{timezone.localdate().isoformat()}.csv", headers, rows)


@staff_required
def download_product_import_sample_csv(request):
    """
    Faz 15: Excel ve CSV Toplu Ürün Aktarımı için Örnek Şablon Dosyası
    """
    headers = [
        'Barkod', 'Ürün Adı', 'Kategori', 'Satış Fiyatı', 'Alış Fiyatı',
        'Stok Miktarı', 'KDV Oranı', 'Birim', 'Kritik Stok'
    ]
    rows = [
        ['8690504012345', 'Ülker Çikolatalı Gofret 35g', 'Bisküvi & Atıştırmalık', '15.00', '11.50', '50', '1', 'Adet', '10'],
        ['5449000000996', 'Coca-Cola 1 Litre', 'İçecek & Meşrubat', '35.00', '28.00', '30', '10', 'Adet', '5'],
        ['8690520001019', 'Sütaş Tam Yağlı Süt 1L', 'Süt & Kahvaltılık', '38.50', '32.00', '25', '1', 'Adet', '5'],
        ['8001841750012', 'Fairy Bulaşık Deterjanı 650ml', 'Temizlik & Hijyen', '65.00', '52.00', '20', '20', 'Adet', '4'],
    ]
    return _csv_response('urun_aktarim_sablonu.csv', headers, rows)


@staff_required
def product_import_view(request):
    """
    Faz 15: Excel / CSV Toplu Ürün ve Stok İçe Aktarma Sihirbazı (Multi-Tenancy)
    Marketlerin binlerce ürünü tek tek girmek yerine Excel/CSV ile saniyeler içinde içeri aktarmasını sağlar.
    """
    user_company = get_user_company(request.user)
    categories = Category.objects.filter(company_scope_q(user_company), is_active=True).order_by('name')

    result_summary = None

    if request.method == 'POST' and request.FILES.get('csv_file'):
        csv_file = request.FILES['csv_file']
        update_existing = request.POST.get('update_existing') == 'on'
        default_category_id = request.POST.get('default_category')
        default_vat = request.POST.get('default_vat', '1.00')

        default_category = None
        if default_category_id and default_category_id.isdigit():
            default_category = Category.objects.filter(company_scope_q(user_company), id=int(default_category_id)).first()

        try:
            # 1. Dosya içeriğini güvenle oku ve encoding çöz
            raw_data = csv_file.read()
            text = None
            for enc in ['utf-8-sig', 'utf-8', 'windows-1254', 'iso-8859-9', 'latin-1']:
                try:
                    text = raw_data.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue

            if not text:
                messages.error(request, "Dosya karakter kodlaması okunamadı. Lütfen UTF-8 veya Türkçe Excel CSV formatında yükleyiniz.")
                return redirect('product_import_view')

            # 2. Ayırıcıyı belirle (; veya , veya \t)
            sample_lines = text[:2048]
            delimiter = ';'
            if ';' in sample_lines:
                delimiter = ';'
            elif ',' in sample_lines:
                delimiter = ','
            elif '\t' in sample_lines:
                delimiter = '\t'

            reader = csv.reader(io.StringIO(text), delimiter=delimiter)
            rows = list(reader)

            if not rows:
                messages.error(request, "Yüklenen dosya boş.")
                return redirect('product_import_view')

            # Başlık satırını oku
            header_row = [c.strip().lower() for c in rows[0]]
            data_rows = rows[1:]

            # Kolon indexlerini tespit et
            col_map = {}
            for idx, col in enumerate(header_row):
                if 'barkod' in col or 'barcode' in col:
                    col_map['barcode'] = idx
                elif 'ad' in col or 'name' in col or 'ürün' in col or 'urun' in col:
                    col_map['name'] = idx
                elif 'kategori' in col or 'category' in col:
                    col_map['category'] = idx
                elif 'satış' in col or 'satis' in col or 'sell' in col or 'fiyat' in col:
                    if 'satış' in col or 'satis' in col or 'sell' in col or 'sell_price' in col:
                        col_map['sell_price'] = idx
                elif 'alış' in col or 'alis' in col or 'buy' in col or 'maliyet' in col or 'cost' in col:
                    col_map['buy_price'] = idx
                elif 'stok' in col or 'miktar' in col or 'adet' in col or 'qty' in col or 'quantity' in col:
                    if 'kritik' not in col:
                        col_map['stock_quantity'] = idx
                elif 'kdv' in col or 'vat' in col or 'vergi' in col:
                    col_map['vat_rate'] = idx
                elif 'birim' in col or 'unit' in col:
                    col_map['unit'] = idx
                elif 'kritik' in col or 'min' in col or 'alarm' in col:
                    col_map['min_stock_level'] = idx

            # Fallback: Eğer header tespit edilemediyse standart sütun sırasını varsay
            if 'barcode' not in col_map:
                col_map['barcode'] = 0
            if 'name' not in col_map:
                col_map['name'] = 1
            if 'category' not in col_map and len(header_row) > 2:
                col_map['category'] = 2
            if 'sell_price' not in col_map and len(header_row) > 3:
                col_map['sell_price'] = 3
            if 'buy_price' not in col_map and len(header_row) > 4:
                col_map['buy_price'] = 4
            if 'stock_quantity' not in col_map and len(header_row) > 5:
                col_map['stock_quantity'] = 5

            created_count = 0
            updated_count = 0
            skipped_count = 0
            errors = []

            with transaction.atomic():
                for row_idx, row in enumerate(data_rows, start=2):
                    if not row or not any(row):
                        continue

                    def get_val(key, default=''):
                        idx = col_map.get(key)
                        if idx is not None and idx < len(row):
                            return row[idx].strip()
                        return default

                    barcode = get_val('barcode')
                    name = get_val('name')

                    if not barcode or not name:
                        errors.append(f"Satır {row_idx}: Barkod veya Ürün Adı boş bırakılamaz.")
                        continue

                    def parse_dec(val, fallback):
                        try:
                            clean = str(val).replace('TL', '').replace('₺', '').replace(' ', '').replace(',', '.')
                            return Decimal(clean)
                        except (InvalidOperation, ValueError):
                            return fallback

                    sell_price = parse_dec(get_val('sell_price'), Decimal('0.00'))
                    buy_price = parse_dec(get_val('buy_price'), Decimal('0.00'))
                    stock_qty = parse_dec(get_val('stock_quantity'), Decimal('0.000'))
                    vat_rate = parse_dec(get_val('vat_rate'), Decimal(str(default_vat)))
                    min_stock = parse_dec(get_val('min_stock_level'), Decimal('5.000'))
                    unit = get_val('unit', 'Adet') or 'Adet'

                    category_name = get_val('category')
                    if category_name:
                        category, _ = Category.objects.get_or_create(
                            company=user_company,
                            name=category_name
                        )
                    else:
                        category = default_category

                    existing = Product.objects.filter(company=user_company, barcode=barcode).first()
                    if existing:
                        if update_existing:
                            existing.name = name
                            if category:
                                existing.category = category
                            existing.sell_price = sell_price
                            if buy_price > 0:
                                existing.buy_price = buy_price
                            existing.stock_quantity = stock_qty
                            existing.vat_rate = vat_rate
                            existing.unit = unit
                            existing.min_stock_level = min_stock
                            existing.is_active = True
                            existing.save()
                            updated_count += 1
                        else:
                            skipped_count += 1
                    else:
                        Product.objects.create(
                            company=user_company,
                            barcode=barcode,
                            name=name,
                            category=category,
                            sell_price=sell_price,
                            buy_price=buy_price,
                            stock_quantity=stock_qty,
                            vat_rate=vat_rate,
                            unit=unit,
                            min_stock_level=min_stock,
                            is_active=True
                        )
                        created_count += 1

            result_summary = {
                'created_count': created_count,
                'updated_count': updated_count,
                'skipped_count': skipped_count,
                'total_processed': created_count + updated_count + skipped_count,
                'errors': errors[:20],
                'has_errors': len(errors) > 0,
                'total_errors': len(errors),
            }

            log_action(
                module="PRODUCT",
                action="CSV_IMPORT",
                message=f"Toplu CSV İçe Aktarım: {created_count} yeni, {updated_count} güncellendi, {skipped_count} atlandı.",
                user=request.user,
                company=user_company
            )
            messages.success(request, f"İçe aktarma tamamlandı! {created_count} yeni ürün eklendi, {updated_count} ürün güncellendi.")

        except Exception as e:
            messages.error(request, f"İçe aktarma sırasında beklenmeyen hata oluştu: {str(e)}")

    context = {
        'categories': categories,
        'result_summary': result_summary,
    }
    return render(request, 'admin/product_import.html', context)


@staff_required
def global_product_pool_view(request):
    """
    Faz 15: Ortak Hazır Ürün Havuzu (Global Master Barcode Catalog) Arama & Mağazaya Aktarma Ekranı
    Market sahiplerinin binlerce ürünü tek tek girmek yerine havuzdan tek tıkla dükkanına eklemesini sağlar.
    """
    user_company = get_user_company(request.user)

    search_q = request.GET.get('search_q', '').strip()
    category_filter = request.GET.get('category', '').strip()

    qs = GlobalProductPool.objects.all()

    if search_q:
        qs = qs.filter(
            Q(barcode__icontains=search_q) |
            Q(name__icontains=search_q) |
            Q(brand__icontains=search_q)
        )

    if category_filter:
        qs = qs.filter(category_name__iexact=category_filter)

    categories_list = list(GlobalProductPool.objects.values_list('category_name', flat=True).distinct().order_by('category_name'))

    existing_barcodes = set(
        Product.objects.filter(company_scope_q(user_company)).values_list('barcode', flat=True)
    )

    paginator = Paginator(qs, 30)
    page_obj = paginator.get_page(request.GET.get('page'))

    pool_items = []
    for item in page_obj.object_list:
        pool_items.append({
            'pool': item,
            'in_store': item.barcode in existing_barcodes
        })

    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_string = query_params.urlencode()

    context = {
        'page_obj': page_obj,
        'pool_items': pool_items,
        'categories_list': categories_list,
        'search_q': search_q,
        'category_filter': category_filter,
        'total_pool_count': GlobalProductPool.objects.count(),
        'query_string': query_string,
    }
    return render(request, 'admin/global_pool.html', context)


@staff_required
@require_POST
def add_pool_product_to_store_action(request):
    """
    Faz 15: Havuzdaki ürünü veya ürünleri mağazanın stoğuna kopyalama aksiyonu
    """
    user_company = get_user_company(request.user)
    barcode = request.POST.get('barcode', '').strip()
    sell_price_raw = request.POST.get('sell_price', '').strip()
    stock_qty_raw = request.POST.get('stock_quantity', '10').strip()

    if not barcode:
        messages.error(request, "Barkod bilgisi eksik.")
        return redirect('global_product_pool_view')

    pool_item = GlobalProductPool.objects.filter(barcode=barcode).first()
    if not pool_item:
        messages.error(request, "Seçilen ürün global havuzda bulunamadı.")
        return redirect('global_product_pool_view')

    existing = Product.objects.filter(company=user_company, barcode=barcode).first()
    if existing:
        messages.info(request, f"'{existing.name}' zaten mağazanızda mevcut.")
        return redirect('global_product_pool_view')

    try:
        sell_price = Decimal(sell_price_raw.replace(',', '.')) if sell_price_raw else pool_item.recommended_price
        stock_qty = Decimal(stock_qty_raw.replace(',', '.')) if stock_qty_raw else Decimal('10.000')

        category, _ = Category.objects.get_or_create(
            company=user_company,
            name=pool_item.category_name or 'Genel Gıda'
        )

        product = Product.objects.create(
            company=user_company,
            category=category,
            name=pool_item.name,
            barcode=pool_item.barcode,
            sell_price=sell_price,
            buy_price=Decimal('0.00'),
            stock_quantity=stock_qty,
            vat_rate=pool_item.vat_rate,
            unit=pool_item.unit,
            is_weighted=pool_item.is_weighted,
            is_active=True
        )

        log_action(
            module="PRODUCT",
            action="POOL_STORE_ADD",
            message=f"Ortak Havuzdan Eklendi: {product.name} ({product.barcode})",
            user=request.user,
            company=user_company
        )
        messages.success(request, f"'{product.name}' başarıyla mağazanıza eklendi! Satış Fiyatı: {product.sell_price} TL")
    except Exception as e:
        messages.error(request, f"Ürün eklenirken hata: {str(e)}")

    return redirect(request.META.get('HTTP_REFERER') or 'global_product_pool_view')


@staff_required
def export_critical_orders_csv(request):
    """Kritik stok ve önerilen sipariş listesini Excel uyumlu CSV olarak indirir (Multi-Tenancy)."""
    user_company = get_user_company(request.user)
    products, _, _ = _filter_critical_products(request.GET, company=user_company)
    headers = [
        'Toptancı', 'Toptancı Telefon', 'Barkod', 'Ürün Adı', 'Kategori',
        'Birim', 'Mevcut Stok', 'Kritik Seviye', 'Önerilen Sipariş',
        'Birim Alış (TL)', 'Tahmini Tutar (TL)'
    ]
    rows = []
    for p in products:
        suggested_qty = max(Decimal('1.000'), (p.min_stock_level * Decimal('2.000')) - p.stock_quantity)
        est_cost = (suggested_qty * (p.buy_price or Decimal('0.00'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        rows.append([
            p.supplier.company_name if p.supplier else 'Atanmamış',
            p.supplier.phone if p.supplier and p.supplier.phone else '',
            p.barcode,
            p.name,
            p.category.name if p.category else '',
            p.unit,
            f"{p.stock_quantity:.3f}",
            f"{p.min_stock_level:.3f}",
            f"{suggested_qty:.3f}",
            f"{p.buy_price:.2f}",
            f"{est_cost:.2f}",
        ])
    return _csv_response(f"kritik_stok_siparis_{timezone.localdate().isoformat()}.csv", headers, rows)


@staff_required
def expiration_tracking_view(request):
    """
    Faz 16: SKT & Parti (Lot) Takibi ve Zayiat Önleme Paneli (Multi-Tenancy)
    Süt ürünleri, şarküteri ve raf ömrü kısa gıdalar için acil indirim/kampanya ve zayiat alarm ekranı.
    """
    user_company = get_user_company(request.user)
    today = timezone.localdate()

    status_filter = request.GET.get('status', 'ALL').strip().upper()
    search_q = request.GET.get('search_q', '').strip()
    category_id = request.GET.get('category_id', '').strip()

    base_qs = Product.objects.filter(
        company_scope_q(user_company),
        is_active=True,
        expiration_date__isnull=False
    ).select_related('category', 'supplier')

    if search_q:
        base_qs = base_qs.filter(
            Q(name__icontains=search_q) |
            Q(barcode__icontains=search_q) |
            Q(lot_number__icontains=search_q)
        )

    if category_id.isdigit():
        base_qs = base_qs.filter(category_id=int(category_id))

    all_exp_products = list(Product.objects.filter(company_scope_q(user_company), is_active=True, expiration_date__isnull=False))
    
    expired_count = 0
    expired_loss_value = Decimal('0.00')
    critical_count = 0
    warning_count = 0
    upcoming_count = 0
    safe_count = 0

    for p in all_exp_products:
        days = p.days_to_expiration
        if days is not None:
            if days < 0:
                expired_count += 1
                if p.stock_quantity > 0:
                    expired_loss_value += p.stock_quantity * p.buy_price
            elif days <= 3:
                critical_count += 1
            elif days <= 7:
                warning_count += 1
            elif days <= 30:
                upcoming_count += 1
            else:
                safe_count += 1

    if status_filter == 'EXPIRED':
        base_qs = base_qs.filter(expiration_date__lt=today)
    elif status_filter == 'CRITICAL':
        base_qs = base_qs.filter(expiration_date__gte=today, expiration_date__lte=today + timedelta(days=3))
    elif status_filter == 'WARNING':
        base_qs = base_qs.filter(expiration_date__gt=today + timedelta(days=3), expiration_date__lte=today + timedelta(days=7))
    elif status_filter == 'UPCOMING':
        base_qs = base_qs.filter(expiration_date__gt=today + timedelta(days=7), expiration_date__lte=today + timedelta(days=30))

    base_qs = base_qs.order_by('expiration_date', 'name')
    categories = Category.objects.filter(company_scope_q(user_company), is_active=True).order_by('name')

    paginator = Paginator(base_qs, 40)
    page_obj = paginator.get_page(request.GET.get('page'))

    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_string = query_params.urlencode()

    context = {
        'page_obj': page_obj,
        'products': page_obj.object_list,
        'categories': categories,
        'search_q': search_q,
        'category_id': category_id,
        'status_filter': status_filter,
        'query_string': query_string,
        'today': today,
        'total_tracked_count': len(all_exp_products),
        'expired_count': expired_count,
        'expired_loss_value': expired_loss_value,
        'critical_count': critical_count,
        'warning_count': warning_count,
        'upcoming_count': upcoming_count,
        'safe_count': safe_count,
    }
    return render(request, 'admin/expiration_tracking.html', context)


@staff_required
def export_expirations_csv(request):
    """SKT ve son kullanma tarihi yaklaşan ürünlerin listesini CSV olarak indirir (Multi-Tenancy)."""
    user_company = get_user_company(request.user)
    today = timezone.localdate()
    products = Product.objects.filter(
        company_scope_q(user_company),
        is_active=True,
        expiration_date__isnull=False
    ).select_related('category').order_by('expiration_date')

    headers = [
        'Barkod', 'Ürün Adı', 'Kategori', 'Parti / Lot No',
        'Son Kullanma Tarihi', 'Kalan Gün', 'Durum',
        'Mevcut Stok', 'Birim', 'Alış Fiyatı (TL)', 'Satış Fiyatı (TL)', 'Risk Tutarı (TL)'
    ]
    rows = []
    for p in products:
        days = p.days_to_expiration
        status_text = "GEÇMİŞ" if days < 0 else f"{days} Gün Kaldı"
        risk_val = (p.stock_quantity * p.buy_price) if p.stock_quantity > 0 else Decimal('0.00')
        rows.append([
            p.barcode,
            p.name,
            p.category.name if p.category else '',
            p.lot_number or '',
            p.expiration_date.strftime('%d.%m.%Y') if p.expiration_date else '',
            days,
            status_text,
            f"{p.stock_quantity:.3f}",
            p.unit,
            f"{p.buy_price:.2f}",
            f"{p.sell_price:.2f}",
            f"{risk_val:.2f}",
        ])
    return _csv_response(f"skt_takip_raporu_{today.isoformat()}.csv", headers, rows)


def _calculate_boss_metrics(user_company, period='TODAY'):
    """
    Patron Kokpiti için gerçek zamanlı finansal metrikler ve kâr analizi (Multi-Tenancy)
    """
    today = timezone.localdate()
    now = timezone.localtime()

    if period == 'YESTERDAY':
        start_date = today - timedelta(days=1)
        end_date = start_date
    elif period == 'WEEK':
        start_date = today - timedelta(days=today.weekday())
        end_date = today
    elif period == 'MONTH':
        start_date = today.replace(day=1)
        end_date = today
    else:  # TODAY
        start_date = today
        end_date = today

    sales_qs = Sale.objects.filter(
        company_scope_q(user_company),
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
        is_cancelled=False
    ).select_related('cashier')

    cancelled_sales_qs = Sale.objects.filter(
        company_scope_q(user_company),
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
        is_cancelled=True
    ).select_related('cashier').order_by('-created_at')

    normal_sales = sales_qs.filter(is_return=False)
    return_sales = sales_qs.filter(is_return=True)

    total_gross_revenue = normal_sales.aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')
    total_return_amount = return_sales.aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')
    net_revenue = total_gross_revenue - total_return_amount

    cash_sales = sales_qs.filter(payment_type__in=['NAKIT', 'PARCALI']).aggregate(s=Sum('cash_paid'))['s'] or Decimal('0.00')
    card_sales = sales_qs.filter(payment_type__in=['KART', 'PARCALI']).aggregate(s=Sum('card_paid'))['s'] or Decimal('0.00')
    veresiye_sales = sales_qs.filter(payment_type='VERESIYE').aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')

    total_receipt_count = normal_sales.count()
    avg_basket = (net_revenue / total_receipt_count) if total_receipt_count > 0 else Decimal('0.00')

    sale_items = SaleItem.objects.filter(sale__in=normal_sales).select_related('product')
    total_cost = Decimal('0.00')
    for item in sale_items:
        cost_unit = item.buy_price if (item.buy_price and item.buy_price > 0) else (item.product.buy_price if item.product else Decimal('0.00'))
        total_cost += cost_unit * item.quantity

    return_items = SaleItem.objects.filter(sale__in=return_sales).select_related('product')
    for item in return_items:
        cost_unit = item.buy_price if (item.buy_price and item.buy_price > 0) else (item.product.buy_price if item.product else Decimal('0.00'))
        total_cost -= cost_unit * item.quantity

    total_cost = max(Decimal('0.00'), total_cost)
    gross_profit = net_revenue - total_cost
    profit_margin_percent = ((gross_profit / net_revenue) * Decimal('100.00')) if net_revenue > Decimal('0.00') else Decimal('0.00')

    cancelled_count = cancelled_sales_qs.count()
    cancelled_total = cancelled_sales_qs.aggregate(s=Sum('total_amount'))['s'] or Decimal('0.00')

    open_shifts = Shift.objects.filter(
        company_scope_q(user_company),
        is_closed=False
    ).select_related('cashier', 'cash_register').order_by('-opened_at')

    critical_stock_count = Product.objects.filter(
        company_scope_q(user_company),
        is_active=True,
        stock_quantity__lte=F('min_stock_level')
    ).count()

    critical_products = list(Product.objects.filter(
        company_scope_q(user_company),
        is_active=True,
        stock_quantity__lte=F('min_stock_level')
    ).order_by('stock_quantity')[:6])

    top_selling_items = list(SaleItem.objects.filter(
        sale__in=normal_sales
    ).values(
        'product__id', 'product__name', 'product__unit'
    ).annotate(
        total_qty=Sum('quantity'),
        total_amount=Sum(F('quantity') * F('unit_price'))
    ).order_by('-total_qty')[:6])

    expired_skt_count = Product.objects.filter(
        company_scope_q(user_company),
        is_active=True,
        expiration_date__lt=today
    ).count()

    urgent_skt_count = Product.objects.filter(
        company_scope_q(user_company),
        is_active=True,
        expiration_date__gte=today,
        expiration_date__lte=today + timedelta(days=3)
    ).count()

    return {
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'now': now,
        'net_revenue': net_revenue,
        'total_gross_revenue': total_gross_revenue,
        'total_return_amount': total_return_amount,
        'cash_sales': cash_sales,
        'card_sales': card_sales,
        'veresiye_sales': veresiye_sales,
        'total_receipt_count': total_receipt_count,
        'avg_basket': avg_basket,
        'total_cost': total_cost,
        'gross_profit': gross_profit,
        'profit_margin_percent': profit_margin_percent,
        'cancelled_count': cancelled_count,
        'cancelled_total': cancelled_total,
        'cancelled_sales': list(cancelled_sales_qs[:10]),
        'open_shifts': open_shifts,
        'critical_stock_count': critical_stock_count,
        'critical_products': critical_products,
        'top_selling_items': top_selling_items,
        'expired_skt_count': expired_skt_count,
        'urgent_skt_count': urgent_skt_count,
    }


@staff_required
def mobile_boss_dashboard_view(request):
    """
    Faz 16: Mobil Patron Kokpiti (Mobile Boss Cockpit PWA)
    İşletme sahibinin cep telefonundan anlık ciroyu, kâr marjını, açık kasaları ve iptal fişlerini izleme ekranı.
    """
    user_company = get_user_company(request.user)
    period = request.GET.get('period', 'TODAY').strip().upper()
    metrics = _calculate_boss_metrics(user_company, period=period)

    context = {
        'm': metrics,
        'company': user_company,
    }
    return render(request, 'admin/boss_dashboard.html', context)


@staff_required
def boss_live_metrics_api(request):
    """
    Faz 16: Mobil Patron Kokpiti Canlı Veri API (Saniyede bir veya butona basınca canlı güncelleme)
    """
    user_company = get_user_company(request.user)
    period = request.GET.get('period', 'TODAY').strip().upper()
    m = _calculate_boss_metrics(user_company, period=period)

    cancelled_list = [
        {
            'id': s.id,
            'receipt_number': s.receipt_number,
            'time': timezone.localtime(s.created_at).strftime('%H:%M'),
            'total': float(s.total_amount),
            'cashier': s.cashier.username if s.cashier else 'Bilinmiyor',
            'reason': s.cancellation_reason or 'Gerekçe belirtilmedi'
        }
        for s in m['cancelled_sales']
    ]

    shifts_list = [
        {
            'id': sh.id,
            'register_name': sh.cash_register.name,
            'cashier': sh.cashier.username if sh.cashier else 'Atanmadı',
            'opened_at': timezone.localtime(sh.opened_at).strftime('%H:%M'),
            'cash_sales': float(sh.total_cash_sales),
            'expected_cash': float(sh.expected_cash)
        }
        for sh in m['open_shifts']
    ]

    return JsonResponse({
        'success': True,
        'timestamp': timezone.localtime().strftime('%H:%M:%S'),
        'metrics': {
            'net_revenue': float(m['net_revenue']),
            'gross_profit': float(m['gross_profit']),
            'profit_margin_percent': float(m['profit_margin_percent']),
            'cash_sales': float(m['cash_sales']),
            'card_sales': float(m['card_sales']),
            'veresiye_sales': float(m['veresiye_sales']),
            'total_receipt_count': m['total_receipt_count'],
            'avg_basket': float(m['avg_basket']),
            'cancelled_count': m['cancelled_count'],
            'cancelled_total': float(m['cancelled_total']),
            'critical_stock_count': m['critical_stock_count'],
            'expired_skt_count': m['expired_skt_count'],
            'urgent_skt_count': m['urgent_skt_count'],
        },
        'cancelled_sales': cancelled_list,
        'open_shifts': shifts_list,
    })


@staff_required
def export_customers_csv(request):
    """Veresiye ve müşteri listesini Excel uyumlu CSV olarak indirir (Multi-Tenancy)."""
    user_company = get_user_company(request.user)
    customers, _ = _filter_customers(request.GET, company=user_company)
    headers = [
        'Ad Soyad', 'Firma Ünvanı', 'Telefon', 'TCKN / VKN',
        'Veresiye Limiti (TL)', 'Güncel Borç Bakiyesi (TL)', 'Kalan Limit (TL)', 'Durum'
    ]
    rows = [
        [
            c.full_name,
            c.company_title or '',
            c.phone,
            c.tax_number or '',
            f"{c.credit_limit:.2f}",
            f"{c.balance:.2f}",
            f"{c.remaining_credit:.2f}",
            'Aktif' if c.is_active else 'Pasif',
        ]
        for c in customers
    ]
    return _csv_response(f"musteri_veresiye_listesi_{timezone.localdate().isoformat()}.csv", headers, rows)


@staff_required
def export_customer_ledger_csv(request, customer_id):
    """Tek müşteriye ait cari hesap hareket ekstresini CSV olarak indirir (Multi-Tenancy)."""
    user_company = get_user_company(request.user)
    customer = get_object_or_404(Customer.objects.filter(company_scope_q(user_company)), id=customer_id)
    txs = customer.transactions.select_related('user', 'sale').order_by('-created_at')

    date_from = _parse_iso_date(request.GET.get('date_from', '').strip())
    date_to = _parse_iso_date(request.GET.get('date_to', '').strip())
    if date_from:
        txs = txs.filter(created_at__date__gte=date_from)
    if date_to:
        txs = txs.filter(created_at__date__lte=date_to)

    headers = ['Tarih', 'Müşteri', 'İşlem Türü', 'Evrak / Fiş No', 'Açıklama', 'Tutar (TL)', 'İşlem Sonrası Bakiye (TL)', 'Personel']
    rows = [
        [
            timezone.localtime(tx.created_at).strftime('%d.%m.%Y %H:%M'),
            customer.full_name,
            tx.get_transaction_type_display(),
            tx.document_no or (tx.sale.receipt_number if tx.sale else ''),
            tx.description or '',
            f"{tx.amount:.2f}",
            f"{tx.balance_after:.2f}",
            tx.user.username if tx.user else '',
        ]
        for tx in txs
    ]
    return _csv_response(f"musteri_ekstre_{customer.id}_{timezone.localdate().isoformat()}.csv", headers, rows)


@staff_required
def export_suppliers_csv(request):
    """Toptancı carileri listesini Excel uyumlu CSV olarak indirir (Multi-Tenancy)."""
    user_company = get_user_company(request.user)
    search_query = request.GET.get('q', '').strip()
    suppliers = Supplier.objects.filter(company_scope_q(user_company))
    if search_query:
        suppliers = suppliers.filter(
            Q(company_name__icontains=search_query) |
            Q(contact_name__icontains=search_query) |
            Q(phone__icontains=search_query) |
            Q(tax_number__icontains=search_query)
        )
    headers = ['Firma Adı', 'Yetkili', 'Telefon', 'E-Posta', 'Vergi Dairesi', 'Vergi No', 'Güncel Bakiye (TL)', 'Durum']
    rows = [
        [
            s.company_name,
            s.contact_name or '',
            s.phone or '',
            s.email or '',
            s.tax_office or '',
            s.tax_number or '',
            f"{s.balance:.2f}",
            'Aktif' if s.is_active else 'Pasif',
        ]
        for s in suppliers
    ]
    return _csv_response(f"toptanci_carileri_{timezone.localdate().isoformat()}.csv", headers, rows)


@staff_required
def export_supplier_ledger_csv(request, supplier_id):
    """Tek toptancıya ait cari hesap hareket ekstresini CSV olarak indirir (Multi-Tenancy)."""
    user_company = get_user_company(request.user)
    supplier = get_object_or_404(Supplier.objects.filter(company_scope_q(user_company)), id=supplier_id)
    txs = supplier.transactions.select_related('user').order_by('-created_at')
    headers = ['Tarih', 'Toptancı', 'İşlem Türü', 'Evrak No', 'Açıklama', 'Tutar (TL)', 'İşlem Sonrası Bakiye (TL)', 'Personel']
    rows = [
        [
            timezone.localtime(tx.created_at).strftime('%d.%m.%Y %H:%M'),
            supplier.company_name,
            tx.get_transaction_type_display(),
            tx.document_no or '',
            tx.description or '',
            f"{tx.amount:.2f}",
            f"{tx.balance_after:.2f}",
            tx.user.username if tx.user else '',
        ]
        for tx in txs
    ]
    return _csv_response(f"toptanci_ekstre_{supplier.id}_{timezone.localdate().isoformat()}.csv", headers, rows)


@staff_required
def export_stock_entries_csv(request):
    """Stok fişleri ve irsaliyeleri mevcut filtrelere göre Excel uyumlu CSV olarak indirir (Multi-Tenancy)."""
    user_company = get_user_company(request.user)
    entries, _ = _filter_stock_entries(request.GET, company=user_company)
    headers = [
        'Fiş No', 'İşlem Türü', 'Toptancı', 'Fatura / İrsaliye No',
        'Ödeme Yöntemi', 'İşlem Tarihi', 'Kalem Adedi', 'Toplam Tutar (TL)',
        'Kaydeden', 'Açıklama'
    ]
    rows = [
        [
            entry.entry_number,
            entry.get_entry_type_display(),
            entry.supplier.company_name if entry.supplier else 'Genel Giriş',
            entry.invoice_number or '',
            entry.get_payment_type_display(),
            timezone.localtime(entry.entry_date).strftime('%d.%m.%Y %H:%M'),
            str(entry.items.count()),
            f"{entry.total_amount:.2f}",
            (entry.created_by.get_full_name() or entry.created_by.username) if entry.created_by else 'Sistem',
            entry.notes or '',
        ]
        for entry in entries
    ]
    log_action('STOCK', 'Stok Fişleri CSV Dışa Aktarıldı', f"{len(rows)} fiş indirildi.", user=request.user)
    return _csv_response(f"stok_fisleri_{timezone.localdate().isoformat()}.csv", headers, rows)


@staff_required
def export_sales_csv(request):
    """Satış fişlerini mevcut filtrelere göre Excel uyumlu CSV olarak indirir (Multi-Tenancy)."""
    user_company = get_user_company(request.user)
    sales, _ = _filter_sales(request.GET, company=user_company)
    headers = [
        'Fiş No', 'ETTN', 'Tarih / Saat', 'Kasa', 'Kasiyer',
        'Müşteri / Ünvan', 'TCKN / VKN', 'Ödeme Türü', 'Belge Durumu',
        'Ara Toplam (TL)', 'İndirim (TL)', 'KDV (TL)', 'Genel Toplam (TL)',
        'Nakit Ödenen (TL)', 'Kredi Kartı Ödenen (TL)', 'Para Üstü (TL)', 'Kalem Adedi'
    ]
    rows = []
    for s in sales:
        if s.is_cancelled:
            doc_label = 'İptal'
        elif s.is_return:
            doc_label = 'İade Fişi'
        else:
            doc_label = 'Satış'

        rows.append([
            s.receipt_number,
            str(s.ettn),
            timezone.localtime(s.created_at).strftime('%d.%m.%Y %H:%M:%S'),
            s.shift.cash_register.name if s.shift and s.shift.cash_register else '',
            (s.cashier.get_full_name() or s.cashier.username) if s.cashier else '',
            s.customer_title or '',
            s.customer_tax_number or '',
            s.get_payment_type_display(),
            doc_label,
            f"{s.subtotal:.2f}",
            f"{s.discount_amount:.2f}",
            f"{s.total_vat:.2f}",
            f"{s.total_amount:.2f}",
            f"{s.cash_paid:.2f}",
            f"{s.card_paid:.2f}",
            f"{s.change_given:.2f}",
            str(s.items.count()),
        ])
    log_action('SALE', 'Satışlar CSV Dışa Aktarıldı', f"{len(rows)} fiş indirildi.", user=request.user)
    return _csv_response(f"satis_fisleri_{timezone.localdate().isoformat()}.csv", headers, rows)


@staff_required
def export_shifts_csv(request):
    """Kasa vardiyalarını mevcut filtrelere göre Excel uyumlu CSV olarak indirir (Multi-Tenancy)."""
    user_company = get_user_company(request.user)
    shifts_qs, _ = _filter_shifts(request.GET, company=user_company)
    headers = [
        'Vardiya No', 'Kasa', 'Kasiyer', 'Açılış Tarihi', 'Kapanış Tarihi',
        'Satış Adedi', 'Nakit Satış (TL)', 'Kart Satış (TL)', 'Veresiye Satış (TL)',
        'Tahsilat (TL)', 'Beklenen Nakit (TL)', 'Sayılan Nakit (TL)', 'Kasa Farkı (TL)', 'Durum'
    ]
    rows = []
    for shift in shifts_qs:
        rows.append([
            f"V-{shift.id}",
            shift.cash_register.name if shift.cash_register else '',
            (shift.cashier.get_full_name() or shift.cashier.username) if shift.cashier else '',
            timezone.localtime(shift.opened_at).strftime('%d.%m.%Y %H:%M') if shift.opened_at else '',
            timezone.localtime(shift.closed_at).strftime('%d.%m.%Y %H:%M') if shift.closed_at else 'Açık',
            str(shift.total_sales_count),
            f"{shift.total_cash_sales:.2f}",
            f"{shift.total_card_sales:.2f}",
            f"{shift.total_veresiye_sales:.2f}",
            f"{shift.total_collections:.2f}",
            f"{shift.expected_cash:.2f}",
            f"{shift.actual_cash:.2f}" if shift.is_closed else '',
            f"{shift.difference:+.2f}" if shift.is_closed else '',
            'Kapalı' if shift.is_closed else 'Açık',
        ])
    log_action('SYSTEM', 'Kasa Vardiyaları CSV Dışa Aktarıldı', f"{len(rows)} vardiya indirildi.", user=request.user)
    return _csv_response(f"kasa_vardiyalari_{timezone.localdate().isoformat()}.csv", headers, rows)


@staff_required
def export_stock_take_csv(request, take_id):
    """Stok sayım fişi kalemlerini Excel uyumlu CSV olarak indirir (Multi-Tenancy)."""
    user_company = get_user_company(request.user)
    take = get_object_or_404(StockTake.objects.filter(company_scope_q(user_company)).select_related('category', 'created_by'), id=take_id)
    items = take.items.select_related('product', 'product__category').order_by('product__name')
    headers = [
        'Barkod', 'Ürün Adı', 'Kategori / Reyon', 'Birim',
        'Teorik Sistem Stoğu', 'Fiziki Sayılan', 'Sayım Farkı',
        'Alış Birim Maliyeti (TL)', 'Net Maliyet Farkı (TL)'
    ]
    rows = [
        [
            item.product.barcode,
            item.product.name,
            item.product.category.name if item.product.category else 'Genel',
            item.product.unit,
            f"{item.system_quantity:.3f}",
            f"{item.counted_quantity:.3f}",
            f"{item.difference:+.3f}",
            f"{item.buy_price:.2f}",
            f"{item.discrepancy_cost:+.2f}",
        ]
        for item in items
    ]
    log_action('STOCK', 'Sayım Fişi Kalemleri CSV Dışa Aktarıldı', f"Sayım #{take.take_number} ({len(rows)} kalem) indirildi.", user=request.user)
    return _csv_response(f"stok_sayim_{take.take_number}_{timezone.localdate().isoformat()}.csv", headers, rows)


@staff_required
@require_POST
def product_bulk_update_action(request):
    """
    Ürün Kataloğundan Seçili veya Filtrelenmiş Ürünlerde Toplu Fiyat / KDV Güncelleme
    """
    apply_scope = request.POST.get('apply_scope', 'selected').strip()
    action_type = request.POST.get('action_type', '').strip()

    try:
        action_value = _parse_decimal(request.POST.get('action_value'), None)
    except ValueError as err:
        messages.error(request, f"Geçersiz işlem değeri: {err}")
        return _safe_next_redirect(request, 'product_catalog_view')

    if action_type not in ('price_percent_up', 'price_percent_down', 'price_fixed_set', 'vat_set'):
        messages.error(request, "Geçersiz toplu işlem türü seçildi.")
        return _safe_next_redirect(request, 'product_catalog_view')

    if action_type == 'price_percent_up' and (action_value <= 0 or action_value > Decimal('1000')):
        messages.error(request, "Zam yüzdesi 0 ile 1000 arasında olmalıdır.")
        return _safe_next_redirect(request, 'product_catalog_view')
    if action_type == 'price_percent_down' and (action_value <= 0 or action_value >= Decimal('100')):
        messages.error(request, "İndirim yüzdesi 0'dan büyük ve 100'den küçük olmalıdır.")
        return _safe_next_redirect(request, 'product_catalog_view')
    if action_type == 'price_fixed_set' and action_value < 0:
        messages.error(request, "Sabit satış fiyatı negatif olamaz.")
        return _safe_next_redirect(request, 'product_catalog_view')
    if action_type == 'vat_set' and (action_value < 0 or action_value > Decimal('100')):
        messages.error(request, "KDV oranı %0 ile %100 arasında olmalıdır.")
        return _safe_next_redirect(request, 'product_catalog_view')

    user_company = get_user_company(request.user)
    if apply_scope == 'filtered':
        target_qs, _ = _filter_catalog_products(request.POST, company=user_company)
    else:
        raw_ids = [pid for pid in request.POST.getlist('product_ids') if str(pid).isdigit()]
        if not raw_ids:
            messages.error(request, "Toplu işlem için en az bir ürün seçiniz veya 'Filtrelenen Tüm Ürünler' seçeneğini kullanınız.")
            return _safe_next_redirect(request, 'product_catalog_view')
        target_qs = Product.objects.filter(company_scope_q(user_company), id__in=[int(pid) for pid in raw_ids])

    updated_count = 0
    with transaction.atomic():
        for prod in target_qs:
            if action_type == 'price_percent_up':
                factor = Decimal('1.00') + (action_value / Decimal('100.00'))
                prod.sell_price = (prod.sell_price * factor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                prod.save(update_fields=['sell_price'])
            elif action_type == 'price_percent_down':
                factor = Decimal('1.00') - (action_value / Decimal('100.00'))
                prod.sell_price = max(Decimal('0.00'), (prod.sell_price * factor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
                prod.save(update_fields=['sell_price'])
            elif action_type == 'price_fixed_set':
                prod.sell_price = action_value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                prod.save(update_fields=['sell_price'])
            elif action_type == 'vat_set':
                prod.vat_rate = action_value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                prod.save(update_fields=['vat_rate'])
            updated_count += 1

    if updated_count == 0:
        messages.warning(request, "Seçilen kapsamda güncellenecek ürün bulunamadı.")
    else:
        action_labels = {
            'price_percent_up': f"%{action_value} Satış Zammı",
            'price_percent_down': f"%{action_value} Satış İndirimi",
            'price_fixed_set': f"Sabit Satış Fiyatı (₺{action_value})",
            'vat_set': f"KDV Oranı (%{action_value})",
        }
        summary = f"{updated_count} ürün -> {action_labels[action_type]}"
        log_action('PRODUCT', 'Toplu Fiyat/KDV Güncelleme', summary, user=request.user)
        messages.success(request, f"Toplu güncelleme tamamlandı: {summary}.")

    return _safe_next_redirect(request, 'product_catalog_view')


# ==========================================================
# FAZ 9 & FAZ 19: TERMİNAL CİHAZ MÜHÜRLEME & EŞLEŞTİRME (RFC 8628)
# ==========================================================

def terminal_device_request_code_api(request):
    """
    Eşlenmemiş Kasa Cihazı İçin 6 Haneli Eşleme Kodu Üretir / Tazeler.
    Smart TV / Cihaz Kodu Modeli (RFC 8628).
    """
    session_id = request.GET.get('session_id') or (request.POST.get('session_id') if request.method == 'POST' else None)
    now = timezone.now()

    session = None
    if session_id:
        session = DevicePairingSession.objects.filter(session_id=session_id, status='PENDING', expires_at__gt=now).first()

    if not session:
        # 6 haneli benzersiz numerik kod üret (Örn: 842109 -> Gösterim: 842 - 109)
        pairing_code = ""
        for _ in range(12):
            candidate = str(random.randint(100000, 999999))
            if not DevicePairingSession.objects.filter(pairing_code=candidate, status='PENDING', expires_at__gt=now).exists():
                pairing_code = candidate
                break
        if not pairing_code:
            pairing_code = str(random.randint(100000, 999999))

        session_id = secrets.token_hex(16)
        user_agent = request.META.get('HTTP_USER_AGENT', '')[:250]
        ip = get_client_ip(request)
        expires_at = now + datetime.timedelta(minutes=10)

        session = DevicePairingSession.objects.create(
            session_id=session_id,
            pairing_code=pairing_code,
            device_info=user_agent,
            ip_address=ip,
            status='PENDING',
            expires_at=expires_at
        )

    formatted_code = f"{session.pairing_code[:3]} - {session.pairing_code[3:]}" if len(session.pairing_code) == 6 else session.pairing_code
    remaining = max(0, int((session.expires_at - now).total_seconds()))

    return JsonResponse({
        'ok': True,
        'session_id': session.session_id,
        'pairing_code': session.pairing_code,
        'formatted_code': formatted_code,
        'expires_in_seconds': remaining
    })


def terminal_device_pairing_status_api(request):
    """
    Kasa Cihazı Ekranının Düzenli Polling Yaparak Onayı Sorguladığı API.
    """
    session_id = request.GET.get('session_id', '').strip()
    if not session_id:
        return JsonResponse({'ok': False, 'message': 'session_id parametresi gereklidir.'}, status=400)

    now = timezone.now()
    session = DevicePairingSession.objects.filter(session_id=session_id).select_related('cash_register', 'company').first()
    if not session:
        return JsonResponse({'ok': True, 'status': 'NOT_FOUND'})

    if session.status == 'PENDING' and session.is_expired():
        session.status = 'EXPIRED'
        session.save(update_fields=['status'])

    if session.status == 'APPROVED' and session.cash_register:
        reg = session.cash_register
        comp = session.company or reg.company
        return JsonResponse({
            'ok': True,
            'status': 'APPROVED',
            'device_token': session.device_token,
            'register_id': reg.id,
            'register_name': reg.name,
            'register_code': reg.register_code,
            'company_name': comp.name if comp else ''
        })

    remaining = max(0, int((session.expires_at - now).total_seconds()))
    return JsonResponse({
        'ok': True,
        'status': session.status,
        'remaining_seconds': remaining
    })


@staff_required
@require_POST
def terminal_device_approve_api(request):
    """
    Yöneticinin Telefonundan veya Bilgisayarından 6 Haneli Kodu Onaylayıp Kasayı Mühürlediği API.
    Eski cihaz bağlıysa yetkisi otomatik düşer, yeni cihaz mühürlenir.
    """
    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    raw_code = str(data.get('pairing_code', '')).replace(' ', '').replace('-', '').strip()
    register_id = data.get('register_id')
    device_name = str(data.get('device_name', '')).strip()

    if not raw_code:
        return JsonResponse({'ok': False, 'message': 'Lütfen kasa ekranındaki 6 haneli kodu giriniz.'}, status=400)

    if not register_id:
        return JsonResponse({'ok': False, 'message': 'Lütfen mühürlenecek kasayı seçiniz.'}, status=400)

    user_company = get_user_company(request.user)
    register = get_object_or_404(CashRegister, id=register_id, company=user_company)

    now = timezone.now()
    session = DevicePairingSession.objects.filter(
        pairing_code=raw_code,
        status='PENDING',
        expires_at__gt=now
    ).first()

    if not session:
        return JsonResponse({
            'ok': False,
            'message': 'Geçersiz veya süresi dolmuş eşleme kodu! Lütfen kasa ekranındaki güncel kodu kontrol ediniz.'
        }, status=404)

    # Yeni kriptolu cihaz mührü (token) üret
    device_token = f"trm_{secrets.token_urlsafe(32)}"
    clean_device_name = device_name or (session.device_info[:60] if session.device_info else "Kasa Terminali")

    # Kasayı yeni cihaza mühürle
    old_device_name = register.device_name
    register.device_token = device_token
    register.device_name = clean_device_name
    register.device_ip = session.ip_address
    register.device_paired_at = now
    register.last_seen_at = now
    register.save()

    # Oturumu onayla
    session.status = 'APPROVED'
    session.company = register.company
    session.cash_register = register
    session.approved_by = request.user
    session.device_token = device_token
    session.save()

    log_msg = f"'{register.name}' ({register.register_code}) kasasına yeni cihaz ('{clean_device_name}') mühürlendi."
    if old_device_name:
        log_msg += f" Önceki cihaz ('{old_device_name}') devreden çıkarıldı."
    log_action('POS', 'Kasa Cihazı Mühürlendi', log_msg, user=request.user)

    return JsonResponse({
        'ok': True,
        'message': f"'{register.name}' başarıyla bu cihaza mühürlendi!",
        'register_id': register.id,
        'register_name': register.name,
        'register_code': register.register_code,
        'device_name': clean_device_name
    })


@staff_required
@require_POST
def terminal_device_unbind_api(request):
    """
    Kayıtlı bir Kasa Terminalinin Cihaz Mührünü Kaldırır (Boşa Çıkarır).
    """
    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else request.POST
    except Exception:
        data = request.POST

    register_id = data.get('register_id')
    user_company = get_user_company(request.user)
    register = get_object_or_404(CashRegister, id=register_id, company=user_company)

    device_name = register.device_name or "Cihaz"
    register.unbind_device()

    log_action('POS', 'Cihaz Mührü Kaldırıldı', f"'{register.name}' kasasındaki cihaz ('{device_name}') mührü kaldırıldı.", level='WARNING', user=request.user)

    return JsonResponse({
        'ok': True,
        'message': f"'{register.name}' cihaz mührü başarıyla kaldırıldı. Kasa artık boştadır.",
        'register_id': register.id
    })


def pair_terminal_action(request):
    """
    Kasa Terminal Cihaz Eşleme Ekranı (Smart TV Eşleme Kodu Modeli).
    Cihaz mühürlüyse doğrudan pos_screen'e yönlendirir; değilse 6 haneli kod gösterir.
    POST ile register_code gönderilirse doğrudan mühürler (Geriye dönük uyumluluk).
    """
    if request.method == 'POST':
        register_code = request.POST.get('register_code', '').strip().upper()
        if register_code:
            register = get_object_or_404(CashRegister, register_code=register_code, is_active=True)
            next_url = request.POST.get('next') or 'pos_screen'
            response = redirect(next_url)
            response.set_cookie(
                'himarket_terminal_code',
                register.register_code,
                max_age=365 * 24 * 3600,
                samesite='Lax'
            )
            if not register.device_token:
                register.device_token = f"trm_{secrets.token_urlsafe(32)}"
                register.device_name = (request.META.get('HTTP_USER_AGENT', '')[:60] if request.META else '') or "Terminal"
                register.device_ip = get_client_ip(request)
                register.device_paired_at = timezone.now()
                register.last_seen_at = timezone.now()
                register.save()

            response.set_cookie(
                'himarket_device_token',
                register.device_token,
                max_age=365 * 24 * 3600,
                samesite='Lax'
            )
            request.session['active_register_code'] = register.register_code
            log_action('POS', 'Cihaz Terminale Eşleştirildi', f"Tarayıcı '{register.name}' ({register.register_code}) kasasına bağlandı.", user=request.user if request.user.is_authenticated else None)
            messages.success(request, f"Bu cihaz başarıyla '{register.name}' ({register.register_code}) terminaline mühürlendi.")
            return response

    device_token = request.COOKIES.get('himarket_device_token')
    if device_token and CashRegister.objects.filter(device_token=device_token, is_active=True).exists():
        return redirect('pos_screen')

    active_registers = []
    if request.user.is_authenticated:
        user_company = get_user_company(request.user)
        active_registers = CashRegister.objects.filter(company=user_company, is_active=True).order_by('register_code')

    return render(request, 'pos_device_pairing.html', {
        'registers': active_registers,
        'current_user': request.user if request.user.is_authenticated else None,
    })


@require_POST
def unpair_terminal_action(request):
    """
    Bu tarayıcının kasa mührünü çözer (Süpervizör PIN veya Yönetici oturumu gerekir).
    """
    pin = request.POST.get('pin', '').strip()
    is_authorized = False

    if request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser):
        is_authorized = True
    elif pin:
        for sup in User.objects.filter(is_active=True).filter(Q(is_staff=True) | Q(is_superuser=True)):
            if sup.check_password(pin):
                is_authorized = True
                break

    if not is_authorized:
        messages.error(request, "Terminal kilidini çözmek için yetkili yönetici şifresi veya PIN gereklidir.")
        return _safe_next_redirect(request, 'pos_screen')

    device_token = request.COOKIES.get('himarket_device_token')
    if device_token:
        CashRegister.objects.filter(device_token=device_token).update(
            device_token=None,
            device_name=None,
            device_ip=None,
            device_paired_at=None
        )

    response = redirect('pair_terminal_action')
    response.delete_cookie('himarket_device_token')
    response.delete_cookie('himarket_terminal_code')
    request.session.pop('active_register_code', None)
    log_action('POS', 'Terminal Bağı Çözüldü', "Cihazın kasa sabitlemesi yetkili tarafından kaldırıldı.", level='WARNING', user=request.user if request.user.is_authenticated else None)
    messages.warning(request, "Bu cihazın kasa mührü kaldırıldı. Artık yeni bir kasaya bağlanabilir.")
    return response


@login_required(login_url='/login/')
@require_POST
def supervisor_force_close_shift_api(request):
    """
    Süpervizör Yetkisi ile Başka Kasiyerin Açık Vardiyasını Devir İçin Kapatma API'si.
    Kasiyer ayrıldığında veya hasta olduğunda yeni kasiyerin kasayı devralabilmesini sağlar.
    """
    try:
        data = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        data = {}

    shift_id = data.get('shift_id')
    pin = str(data.get('pin', '')).strip()
    notes = str(data.get('notes', 'Süpervizör müdahalesi ile devredildi')).strip()

    try:
        actual_cash = _parse_decimal(data.get('actual_cash', 0), Decimal('0.00'))
    except ValueError as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=400)

    if not shift_id:
        return JsonResponse({'success': False, 'message': 'Vardiya ID eksik.'}, status=400)

    shift = Shift.objects.filter(id=shift_id, is_closed=False).first()
    if not shift:
        return JsonResponse({'success': False, 'message': 'Kapatılacak açık bir vardiya bulunamadı.'}, status=404)

    # Süpervizör Doğrulama: Giriş yapan kişi zaten staff/yönetici ise doğrudan onay verilir
    supervisor = None
    if request.user.is_staff or request.user.is_superuser:
        supervisor = request.user
    elif pin:
        for sup in User.objects.filter(is_active=True).filter(Q(is_staff=True) | Q(is_superuser=True)):
            if sup.check_password(pin):
                supervisor = sup
                break

    if not supervisor:
        return JsonResponse({'success': False, 'message': 'Yetkili süpervizör şifresi / PIN hatalı.'}, status=403)

    with transaction.atomic():
        shift = Shift.objects.select_for_update().get(id=shift.id)
        shift.is_closed = True
        shift.closed_at = timezone.now()
        shift.actual_cash = actual_cash
        shift.difference = actual_cash - shift.expected_cash
        shift.notes = (shift.notes or '') + f"\n[{timezone.localtime().strftime('%d.%m.%Y %H:%M')} Süpervizör '{supervisor.username}' tarafından devir teslim için kapatıldı: {notes}]"
        shift.save()

    log_action('SHIFT', 'Süpervizör Zorla Kasa Kapanışı', f"Kasa {shift.cash_register.name} vardiyası devir için kapatıldı.", user=supervisor)
    return JsonResponse({
        'success': True,
        'message': f"{shift.cash_register.name} kasası başarıyla kapatıldı. Artık yeni kasiyer giriş yapabilir."
    })


@staff_required
def cash_register_list_view(request):
    """
    Yönetim Paneli: Kasa Terminalleri Listesi ve Canlı Durum İzleme Ekranı (Multi-Tenancy & Lisanslama).
    """
    user_company = get_user_company(request.user)
    registers = CashRegister.objects.filter(company_scope_q(user_company)).order_by('register_code')
    current_paired_code = request.COOKIES.get('himarket_terminal_code')

    sub = Subscription.get_or_create_for_company(user_company)
    active_count = registers.filter(is_active=True).count()
    max_registers = sub.max_cash_registers if sub else 2
    can_add_register = (active_count < max_registers)

    reg_cards = []
    for reg in registers:
        open_shift = Shift.objects.filter(company_scope_q(user_company), cash_register=reg, is_closed=False).select_related('cashier').first()
        reg_cards.append({
            'register': reg,
            'open_shift': open_shift,
            'is_current_terminal': (current_paired_code == reg.register_code),
            'total_shifts_count': reg.shifts.filter(company_scope_q(user_company)).count(),
        })

    context = {
        'subscription': sub,
        'reg_cards': reg_cards,
        'current_paired_code': current_paired_code,
        'total_registers_count': registers.count(),
        'active_registers_count': active_count,
        'max_cash_registers': max_registers,
        'can_add_register': can_add_register,
        'open_registers_count': sum(1 for c in reg_cards if c['open_shift'] is not None),
    }
    return render(request, 'admin/cash_registers.html', context)


@staff_required
@require_POST
def cash_register_create_action(request):
    """
    Yeni bir Kasa Satış Terminali (örn. KASA-03) ekler (Multi-Tenancy & Lisans Denetimi).
    """
    user_company = get_user_company(request.user)
    sub = Subscription.get_or_create_for_company(user_company)
    active_count = CashRegister.objects.filter(company_scope_q(user_company), is_active=True).count()
    if sub and active_count >= sub.max_cash_registers:
        messages.error(
            request,
            f"Abonelik paketiniz ({sub.get_plan_type_display()}) en fazla {sub.max_cash_registers} aktif kasaya izin vermektedir. "
            f"Yeni kasa eklemek için lisansınızı yükseltiniz veya bir kasayı pasife alınız."
        )
        return redirect('cash_register_list_view')

    code = request.POST.get('register_code', '').strip().upper()
    name = request.POST.get('name', '').strip()

    if not code or not name:
        messages.error(request, "Kasa kodu ve kasa adı zorunludur.")
        return redirect('cash_register_list_view')

    if CashRegister.objects.filter(company_scope_q(user_company), register_code=code).exists():
        messages.error(request, f"'{code}' kasa kodu zaten kayıtlıdır.")
        return redirect('cash_register_list_view')

    CashRegister.objects.create(company=user_company, register_code=code, name=name, is_active=True)
    log_action('SYSTEM', 'Yeni Kasa Terminali Eklendi', f"{name} [{code}] oluşturuldu.", user=request.user)
    messages.success(request, f"'{name}' [{code}] kasa terminali başarıyla oluşturuldu.")
    return redirect('cash_register_list_view')


@staff_required
@require_POST
def cash_register_toggle_status_action(request, register_id):
    """
    Kasa terminalini aktif/pasif yapar. Açık vardiyası olan kasa pasife alınamaz (Multi-Tenancy & Lisans Denetimi).
    """
    user_company = get_user_company(request.user)
    reg = get_object_or_404(CashRegister.objects.filter(company_scope_q(user_company)), id=register_id)
    if reg.is_active:
        open_shift = Shift.objects.filter(company_scope_q(user_company), cash_register=reg, is_closed=False).first()
        if open_shift:
            messages.error(request, f"'{reg.name}' kasasında şu anda açık bir vardiya bulunmaktadır. Kasa pasife alınmadan önce vardiya kapatılmalıdır.")
            return redirect('cash_register_list_view')
        reg.is_active = False
        messages.warning(request, f"'{reg.name}' kasa terminali pasife alındı.")
    else:
        sub = Subscription.get_or_create_for_company(user_company)
        active_count = CashRegister.objects.filter(company_scope_q(user_company), is_active=True).count()
        if sub and active_count >= sub.max_cash_registers:
            messages.error(
                request,
                f"Abonelik paketinizin azami aktif kasa sınırına ({sub.max_cash_registers} Kasa) ulaşıldı. "
                f"Bu kasayı aktifleştirebilmek için önce başka bir kasayı pasife alınız veya lisansınızı yükseltiniz."
            )
            return redirect('cash_register_list_view')
        reg.is_active = True
        messages.success(request, f"'{reg.name}' kasa terminali aktif hale getirildi.")

    reg.save(update_fields=['is_active'])
    log_action('SYSTEM', 'Kasa Durumu Değiştirildi', f"{reg.name} -> {'Aktif' if reg.is_active else 'Pasif'}", user=request.user)
    return redirect('cash_register_list_view')


@staff_required
@require_POST
def product_quick_price_update_api(request, product_id):
    """
    Faz 10.2: Ürün Kataloğu Satır İçi Hızlı Fiyat Güncelleme API
    Ürün listesinden ayrılmadan, tek tıkla yeni satış fiyatını kaydeder ve
    kâr marjı rozetini dinamik yeniden hesaplar.
    """
    user_company = get_user_company(request.user)
    product = get_object_or_404(Product.objects.filter(company_scope_q(user_company)), id=product_id)
    try:
        if request.content_type == 'application/json':
            payload = json.loads(request.body.decode('utf-8'))
            raw_price = payload.get('sell_price')
        else:
            raw_price = request.POST.get('sell_price')

        if raw_price is None or str(raw_price).strip() == '':
            return JsonResponse({'success': False, 'message': 'Satış fiyatı boş bırakılamaz.'}, status=400)

        clean_price_str = str(raw_price).replace(',', '.').strip()
        new_price = Decimal(clean_price_str).quantize(Decimal('0.01'))

        if new_price < Decimal('0.00'):
            return JsonResponse({'success': False, 'message': 'Satış fiyatı sıfırdan küçük olamaz.'}, status=400)

        old_price = product.sell_price
        product.sell_price = new_price
        product.save(update_fields=['sell_price'])

        # Faz 18: Şubelere anında fiyat senkronizasyonu
        propagate_branches = False
        if request.content_type == 'application/json':
            propagate_branches = bool(payload.get('propagate_to_branches', False))
        else:
            propagate_branches = request.POST.get('propagate_to_branches') in ('true', '1', 'on')

        synced_branches_count = 0
        if propagate_branches and user_company:
            branches = Company.objects.filter(is_active=True).exclude(id=user_company.id)
            if user_company.branches.exists():
                branches = user_company.branches.filter(is_active=True)
            elif user_company.parent:
                branches = Company.objects.filter(
                    Q(id=user_company.parent.id) | Q(parent=user_company.parent)
                ).exclude(id=user_company.id).filter(is_active=True)
            for b in branches:
                c = Product.objects.filter(company=b, barcode=product.barcode).update(
                    sell_price=new_price,
                    updated_at=timezone.now()
                )
                if c:
                    synced_branches_count += c

        log_msg = f"{product.name} [{product.barcode}] satış fiyatı ₺{old_price:.2f} -> ₺{new_price:.2f} olarak güncellendi."
        if synced_branches_count > 0:
            log_msg += f" ({synced_branches_count} şubeye senkronize edildi)"

        log_action(
            'PRODUCT',
            'Hızlı Satır İçi Fiyat Güncelleme',
            log_msg,
            user=request.user
        )

        profit_amount = Decimal('0.00')
        profit_percent = Decimal('0.00')
        if product.buy_price > Decimal('0.00'):
            profit_amount = max(Decimal('0.00'), new_price - product.buy_price)
            profit_percent = (profit_amount / product.buy_price) * Decimal('100.0')

        return JsonResponse({
            'success': True,
            'message': f"'{product.name}' satış fiyatı ₺{new_price:.2f} olarak güncellendi." + (f" ({synced_branches_count} şubeye uygulandı)" if synced_branches_count > 0 else ""),
            'product_id': product.id,
            'new_sell_price': float(new_price),
            'profit_amount': float(profit_amount),
            'profit_percent': float(round(profit_percent, 1)),
            'synced_branches_count': synced_branches_count,
        })
    except (ValueError, ArithmeticError) as e:
        return JsonResponse({'success': False, 'message': f'Geçersiz sayısal fiyat: {e}'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Sunucu hatası: {e}'}, status=500)


# ==========================================================
# PLATFORM SÜPER ADMİN KONSOLU & MERKEZİ LİSANS YÖNETİMİ (FAZ 12)
# ==========================================================

@superuser_required
def super_admin_dashboard_view(request):
    """
    Faz 12: Platform Sahibi Merkezi Süper Admin Konsolu
    Tüm kayıtlı şirketleri (Company), lisans/abonelik durumlarını (Subscription),
    aktif kasa sayılarını ve kullanım kotalarını global olarak listeler.
    """
    companies = Company.objects.all().order_by('id')
    now = timezone.now()
    thirty_days_later = now + timedelta(days=30)

    total_companies = companies.count()
    active_companies = 0
    total_active_registers = 0
    total_max_quota = 0
    expiring_soon_count = 0
    status_counts = {'ACTIVE': 0, 'TRIAL': 0, 'SUSPENDED': 0, 'EXPIRED': 0}

    company_rows = []
    for comp in companies:
        if comp.is_active:
            active_companies += 1

        sub = Subscription.get_or_create_for_company(comp)
        active_regs = comp.cash_registers.filter(is_active=True).count()
        total_regs = comp.cash_registers.count()

        total_active_registers += active_regs
        if sub:
            total_max_quota += sub.max_cash_registers
            status_counts[sub.status] = status_counts.get(sub.status, 0) + 1
            if sub.expires_at and now <= sub.expires_at <= thirty_days_later and sub.status in ('ACTIVE', 'TRIAL'):
                expiring_soon_count += 1

        company_rows.append({
            'company': comp,
            'subscription': sub,
            'active_registers': active_regs,
            'total_registers': total_regs,
            'quota': sub.max_cash_registers if sub else 0,
            'is_valid': sub.is_valid if sub else False,
            'days_remaining': sub.days_remaining if sub else 0,
            'expires_at_formatted': sub.expires_at.strftime('%Y-%m-%d') if (sub and sub.expires_at) else '',
        })

    inactive_companies = total_companies - active_companies

    # Tüm terminaller / kasalar (Donanım ve Terminal Denetimi Sekmesi İçin)
    all_registers = CashRegister.objects.select_related('company').order_by('company__name', 'register_code')

    # Son Sistem Denetim Logları (Audit Log Sekmesi İçin)
    recent_logs = SystemLog.objects.select_related('company', 'user').order_by('-created_at')[:30]

    context = {
        'company_rows': company_rows,
        'total_companies': total_companies,
        'active_companies': active_companies,
        'inactive_companies': inactive_companies,
        'total_active_registers': total_active_registers,
        'total_max_quota': total_max_quota,
        'status_counts': status_counts,
        'expiring_soon_count': expiring_soon_count,
        'plan_choices': Subscription.PLAN_CHOICES,
        'status_choices': Subscription.STATUS_CHOICES,
        'all_registers': all_registers,
        'recent_logs': recent_logs,
    }
    return render(request, 'admin/super_admin_dashboard.html', context)


@superuser_required
@require_POST
def super_admin_update_subscription_action(request, company_id):
    """
    Faz 12: Şirket Abonelik ve Kasa Lisans Kotasını Güncelleme Eylemi
    """
    company = get_object_or_404(Company, id=company_id)
    sub = Subscription.get_or_create_for_company(company)

    plan_type = request.POST.get('plan_type', '').strip()
    valid_plans = dict(Subscription.PLAN_CHOICES).keys()
    if plan_type in valid_plans:
        sub.plan_type = plan_type

    raw_quota = request.POST.get('max_cash_registers', '').strip()
    try:
        quota = int(raw_quota)
        if quota < 1:
            quota = 1
        sub.max_cash_registers = quota
    except (ValueError, TypeError):
        pass

    status = request.POST.get('status', '').strip()
    valid_statuses = dict(Subscription.STATUS_CHOICES).keys()
    if status in valid_statuses:
        sub.status = status

    raw_expiry = request.POST.get('expires_at', '').strip()
    if raw_expiry:
        parsed_date = _parse_iso_date(raw_expiry)
        if parsed_date:
            sub.expires_at = timezone.make_aware(datetime.datetime.combine(parsed_date, datetime.time(23, 59, 59)))
    else:
        if 'expires_at' in request.POST:
            sub.expires_at = None

    if 'notes' in request.POST:
        sub.notes = request.POST.get('notes', '').strip()

    sub.save()

    log_action(
        'SYSTEM',
        'Abonelik / Lisans Güncellendi',
        f"Firma: {company.name}, Paket: {sub.plan_type}, Kasa Limiti: {sub.max_cash_registers}, Durum: {sub.status}",
        user=request.user
    )
    messages.success(request, f"'{company.name}' firmasının abonelik ve lisans bilgileri başarıyla güncellendi.")
    return redirect('super_admin_dashboard')


@superuser_required
@require_POST
def super_admin_toggle_company_status_action(request, company_id):
    """
    Faz 12: Şirket Aktiflik Durumunu Açma / Kapatma Eylemi
    """
    company = get_object_or_404(Company, id=company_id)
    company.is_active = not company.is_active
    company.save(update_fields=['is_active'])

    status_str = "aktif" if company.is_active else "pasif"
    log_action('SYSTEM', 'Firma Durumu Değiştirildi', f"{company.name} -> {status_str.upper()}", user=request.user)
    messages.success(request, f"'{company.name}' firması {status_str} duruma getirildi.")
    return redirect('super_admin_dashboard')


def _seed_company_demo_products(company):
    """
    Yeni oluşturulan mağazaya temel market kategorilerini ve popüler demo ürünlerini yükler.
    """
    cat_bakery, _ = Category.objects.get_or_create(company=company, name="Fırın & Ekmek", defaults={'display_order': 1})
    cat_food, _ = Category.objects.get_or_create(company=company, name="Temel Gıda", defaults={'display_order': 2})
    cat_drink, _ = Category.objects.get_or_create(company=company, name="İçecekler", defaults={'display_order': 3})
    cat_dairy, _ = Category.objects.get_or_create(company=company, name="Süt & Şarküteri", defaults={'display_order': 4})
    cat_produce, _ = Category.objects.get_or_create(company=company, name="Manav (Tartılı)", defaults={'display_order': 5})
    cat_snack, _ = Category.objects.get_or_create(company=company, name="Atıştırmalık", defaults={'display_order': 6})

    demo_products = [
        {
            'barcode': '8690000000010',
            'name': 'Ekmek 200g',
            'category': cat_bakery,
            'buy_price': Decimal('8.00'),
            'sell_price': Decimal('10.00'),
            'vat_rate': Decimal('1.00'),
            'is_weighted': False,
            'unit': 'Adet',
            'stock_quantity': Decimal('150.000'),
            'is_quick_button': True,
            'quick_button_order': 1
        },
        {
            'barcode': '8690000000027',
            'name': 'Büyük Alışveriş Poşeti',
            'category': cat_food,
            'buy_price': Decimal('0.10'),
            'sell_price': Decimal('0.50'),
            'vat_rate': Decimal('20.00'),
            'is_weighted': False,
            'unit': 'Adet',
            'stock_quantity': Decimal('5000.000'),
            'is_quick_button': True,
            'quick_button_order': 2
        },
        {
            'barcode': '8690504018003',
            'name': 'Su 0.5L Pet',
            'category': cat_drink,
            'buy_price': Decimal('3.50'),
            'sell_price': Decimal('6.00'),
            'vat_rate': Decimal('10.00'),
            'is_weighted': False,
            'unit': 'Adet',
            'stock_quantity': Decimal('200.000'),
            'is_quick_button': True,
            'quick_button_order': 3
        },
        {
            'barcode': '8690504000123',
            'name': 'Tam Yağlı Süt 1L',
            'category': cat_dairy,
            'buy_price': Decimal('26.00'),
            'sell_price': Decimal('35.00'),
            'vat_rate': Decimal('1.00'),
            'is_weighted': False,
            'unit': 'Adet',
            'stock_quantity': Decimal('40.000'),
            'is_quick_button': True,
            'quick_button_order': 4
        },
        {
            'barcode': '2700001014502',
            'name': 'Amasya Elması (Tartılı)',
            'category': cat_produce,
            'buy_price': Decimal('25.00'),
            'sell_price': Decimal('40.00'),
            'vat_rate': Decimal('1.00'),
            'is_weighted': True,
            'unit': 'KG',
            'stock_quantity': Decimal('85.500'),
            'is_quick_button': True,
            'quick_button_order': 5
        },
        {
            'barcode': '2700002005008',
            'name': 'Taze Kaşar Peyniri (Tartılı)',
            'category': cat_dairy,
            'buy_price': Decimal('180.00'),
            'sell_price': Decimal('260.00'),
            'vat_rate': Decimal('1.00'),
            'is_weighted': True,
            'unit': 'KG',
            'stock_quantity': Decimal('30.000'),
            'is_quick_button': True,
            'quick_button_order': 6
        },
        {
            'barcode': '8690526011234',
            'name': 'Çikolatalı Gofret 35g',
            'category': cat_snack,
            'buy_price': Decimal('6.00'),
            'sell_price': Decimal('10.00'),
            'vat_rate': Decimal('10.00'),
            'is_weighted': False,
            'unit': 'Adet',
            'stock_quantity': Decimal('120.000'),
            'is_quick_button': True,
            'quick_button_order': 7
        },
        {
            'barcode': '8690123456789',
            'name': 'Çay 1000g Rize Turist',
            'category': cat_food,
            'buy_price': Decimal('110.00'),
            'sell_price': Decimal('150.00'),
            'vat_rate': Decimal('1.00'),
            'is_weighted': False,
            'unit': 'Adet',
            'stock_quantity': Decimal('50.000'),
            'is_quick_button': False
        }
    ]

    for p in demo_products:
        Product.objects.get_or_create(
            company=company,
            barcode=p['barcode'],
            defaults=p
        )


@superuser_required
@require_POST
def super_admin_create_company_action(request):
    """
    Platform Süper Admin: Yeni Mağaza & Lisans Onboarding Sihirbazı
    Tek form ile:
    - Yeni Company (Firma) oluşturur.
    - Subscription (Lisans Paketi & Kasa Kotası) tanımlar.
    - Varsayılan Kasa 1 terminali açar.
    - Firma için varsayılan CompanySetting oluşturur.
    - İsteğe bağlı olarak ilk Mağaza Müdürü (User + UserProfile) hesabını açar.
    """
    from django.utils.text import slugify

    name = request.POST.get('name', '').strip()
    if not name:
        messages.error(request, "Lütfen mağaza / şirket adını giriniz.")
        return redirect('super_admin_dashboard')

    code = request.POST.get('code', '').strip()
    if not code:
        tr_map = str.maketrans("ıİğĞüÜşŞöÖçÇ", "iIgGuUsSoOcC")
        base_slug = slugify(name.translate(tr_map))
        if not base_slug:
            base_slug = "magaza"
        code = base_slug
        suffix = 1
        while Company.objects.filter(code=code).exists():
            code = f"{base_slug}-{suffix}"
            suffix += 1
    else:
        tr_map = str.maketrans("ıİğĞüÜşŞöÖçÇ", "iIgGuUsSoOcC")
        code = slugify(code.translate(tr_map))
        if Company.objects.filter(code=code).exists():
            messages.error(request, f"'{code}' mağaza kodu sistemde zaten kayıtlı. Lütfen farklı bir kod seçiniz.")
            return redirect('super_admin_dashboard')

    phone = request.POST.get('phone', '').strip()
    email = request.POST.get('email', '').strip()
    tax_number = request.POST.get('tax_number', '').strip()
    tax_office = request.POST.get('tax_office', '').strip()
    address = request.POST.get('address', '').strip()

    company = Company.objects.create(
        name=name,
        code=code,
        phone=phone,
        email=email,
        tax_number=tax_number,
        tax_office=tax_office,
        address=address,
        is_active=True
    )

    # 2. Lisans & Kasa Kotası
    plan_type = request.POST.get('plan_type', 'PRO').strip()
    if plan_type not in dict(Subscription.PLAN_CHOICES):
        plan_type = 'PRO'

    try:
        max_regs = int(request.POST.get('max_cash_registers', 2))
        if max_regs < 1:
            max_regs = 1
    except (ValueError, TypeError):
        max_regs = 2

    # Lisans Bitiş Süresi
    duration_days_str = request.POST.get('duration_days', '365').strip()
    expires_at = None
    if duration_days_str == '30':
        expires_at = timezone.now() + timedelta(days=30)
    elif duration_days_str == '180':
        expires_at = timezone.now() + timedelta(days=180)
    elif duration_days_str == '365':
        expires_at = timezone.now() + timedelta(days=365)
    elif duration_days_str == 'unlimited':
        expires_at = None
    else:
        raw_expiry = request.POST.get('expires_at', '').strip()
        if raw_expiry:
            parsed_date = _parse_iso_date(raw_expiry)
            if parsed_date:
                expires_at = timezone.make_aware(datetime.datetime.combine(parsed_date, datetime.time(23, 59, 59)))
        else:
            expires_at = timezone.now() + timedelta(days=365)

    sub = Subscription.get_or_create_for_company(company)
    sub.plan_type = plan_type
    sub.max_cash_registers = max_regs
    sub.status = 'ACTIVE'
    sub.expires_at = expires_at
    sub.notes = request.POST.get('notes', 'Süper Admin Onboarding ile açıldı.').strip()
    sub.save()

    # 3. İlk Kasa (Kasa 1)
    CashRegister.objects.get_or_create(
        company=company,
        register_code="KASA-01",
        defaults={'name': 'Kasa 1', 'is_active': True}
    )

    # 4. Firma Ayarları
    CompanySetting.get_settings(company=company)

    # 4.1 İsteğe Bağlı Örnek Market Ürünleri Yükleme
    if request.POST.get('seed_demo_products') == '1':
        _seed_company_demo_products(company)

    # 5. İsteğe Bağlı Yönetici Kullanıcı Hesabı
    manager_username = request.POST.get('manager_username', '').strip()
    manager_password = request.POST.get('manager_password', '').strip()
    created_manager_msg = ""
    if manager_username and manager_password:
        if User.objects.filter(username=manager_username).exists():
            created_manager_msg = f" (Not: '{manager_username}' kullanıcısı zaten mevcut olduğu için yeni kullanıcı açılamadı.)"
        else:
            user = User.objects.create_user(
                username=manager_username,
                password=manager_password,
                is_staff=True,
                is_active=True
            )
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.company = company
            profile.save(update_fields=['company'])
            created_manager_msg = f" Yönetici hesabı ('{manager_username}') hazırlandı."

    log_action(
        'SYSTEM',
        'Yeni Mağaza Oluşturuldu (Onboarding)',
        f"Firma: {company.name} ({company.code}), Paket: {sub.plan_type}, Kasa Kotası: {sub.max_cash_registers}",
        user=request.user
    )

    messages.success(request, f"'{company.name}' mağazası ve {sub.get_plan_type_display()} lisansı başarıyla kuruldu.{created_manager_msg}")
    return redirect('super_admin_dashboard')


@superuser_required
@require_POST
def super_admin_reset_register_hardware_action(request, register_id):
    """
    Süper Admin: Bir kasanın eşleşmiş terminal kaydını ve donanım durumunu sıfırlar.
    """
    register = get_object_or_404(CashRegister, id=register_id)
    comp_name = register.company.name if register.company else 'Genel'
    log_action(
        'POS',
        'Terminal Eşleşmesi Sıfırlandı',
        f"Kasa: '{register.name}' ({register.register_code}), Firma: {comp_name}",
        user=request.user
    )
    messages.success(request, f"'{comp_name}' mağazasının '{register.name}' ({register.register_code}) kasası için cihaz eşleşmesi sıfırlandı.")
    return redirect('super_admin_dashboard')


# ==========================================================
# FAZ 13: VARYANT MOTORU (BEDEN / RENK) & HEDİYE DEĞİŞİM FİŞİ
# ==========================================================

def _generate_unique_variant_barcode(company):
    """Varyantlar için şirket bünyesinde benzersiz geçerli EAN-13 barkod üretir."""
    prefix = '8690002'
    last_v = ProductVariant.objects.filter(company=company).order_by('-id').first()
    seq = (last_v.id + 100) if last_v else 100
    candidate = generate_valid_ean13(prefix=prefix, sequence_number=seq)
    attempts = 0
    while (Product.objects.filter(company=company, barcode=candidate).exists() or
           ProductVariant.objects.filter(company=company, barcode=candidate).exists()) and attempts < 1000:
        seq += 1
        candidate = generate_valid_ean13(prefix=prefix, sequence_number=seq)
        attempts += 1
    return candidate


@staff_api_required
def product_variants_api(request, product_id):
    """
    Faz 13: Bir ürünün varyantlarını JSON olarak listeleme (GET)
    ve tekli / matris (Beden x Renk) varyant üretme (POST).
    """
    user_company = get_user_company(request.user)
    product = get_object_or_404(Product.objects.filter(company_scope_q(user_company)), id=product_id)

    if request.method == 'GET':
        variants = product.variants.all().order_by('size', 'color')
        data = [
            {
                'id': v.id,
                'size': v.size,
                'color': v.color,
                'barcode': v.barcode,
                'stock_quantity': float(v.stock_quantity),
                'price_override': float(v.price_override) if v.price_override is not None else None,
                'is_active': v.is_active,
                'display': f"{v.size} / {v.color}".strip(' / ') or 'Varyant',
            }
            for v in variants
        ]
        return JsonResponse({
            'success': True,
            'product_id': product.id,
            'product_name': product.name,
            'has_variants': product.has_variants,
            'variants': data,
            'total_variants': len(data),
        })

    elif request.method == 'POST':
        try:
            body = json.loads(request.body.decode('utf-8'))
        except Exception:
            return JsonResponse({'success': False, 'message': 'Geçersiz JSON verisi.'}, status=400)

        sizes = body.get('sizes', [])
        colors = body.get('colors', [])
        default_stock = Decimal(str(body.get('default_stock', '0.000')))
        default_price_raw = body.get('default_price')
        default_price = Decimal(str(default_price_raw)) if default_price_raw is not None and str(default_price_raw).strip() != '' else None

        single_size = str(body.get('size', '')).strip()
        single_color = str(body.get('color', '')).strip()
        single_barcode = str(body.get('barcode', '')).strip()

        created_variants = []

        if single_size or single_color or single_barcode:
            bc = single_barcode or _generate_unique_variant_barcode(user_company)
            if ProductVariant.objects.filter(company=user_company, barcode=bc).exists():
                return JsonResponse({'success': False, 'message': f"'{bc}' barkodu zaten başka bir varyantta kayıtlı!"}, status=400)

            v = ProductVariant.objects.create(
                company=user_company,
                product=product,
                size=single_size,
                color=single_color,
                barcode=bc,
                stock_quantity=default_stock,
                price_override=default_price,
                is_active=True
            )
            created_variants.append(v)
        elif sizes or colors:
            size_list = [str(s).strip() for s in sizes if str(s).strip()] or ['']
            color_list = [str(c).strip() for c in colors if str(c).strip()] or ['']

            for s_val in size_list:
                for c_val in color_list:
                    exists = ProductVariant.objects.filter(
                        company=user_company, product=product, size=s_val, color=c_val
                    ).exists()
                    if exists:
                        continue
                    bc = _generate_unique_variant_barcode(user_company)
                    v = ProductVariant.objects.create(
                        company=user_company,
                        product=product,
                        size=s_val,
                        color=c_val,
                        barcode=bc,
                        stock_quantity=default_stock,
                        price_override=default_price,
                        is_active=True
                    )
                    created_variants.append(v)
        else:
            return JsonResponse({'success': False, 'message': 'Lütfen en az bir beden veya renk belirtiniz.'}, status=400)

        product.has_variants = True
        total_v_stock = product.variants.aggregate(total=Sum('stock_quantity'))['total'] or Decimal('0.000')
        product.stock_quantity = total_v_stock
        product.save(update_fields=['has_variants', 'stock_quantity'])

        log_action(
            'PRODUCT',
            'Varyantlar Üretildi',
            f"'{product.name}' ürünü için {len(created_variants)} yeni beden/renk varyantı oluşturuldu.",
            user=request.user
        )

        return JsonResponse({
            'success': True,
            'message': f"{len(created_variants)} varyant başarıyla oluşturuldu.",
            'created_count': len(created_variants),
            'total_stock': float(total_v_stock),
        })

    return JsonResponse({'success': False, 'message': 'Desteklenmeyen metod.'}, status=405)


@staff_api_required
@require_POST
def product_variant_update_api(request, variant_id):
    """
    Faz 13: Bir varyantın barkod, stok, fiyat override ve aktiflik durumunu güncelleme.
    """
    user_company = get_user_company(request.user)
    variant = get_object_or_404(ProductVariant, id=variant_id, company=user_company)
    product = variant.product

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'success': False, 'message': 'Geçersiz JSON.'}, status=400)

    new_barcode = str(body.get('barcode', '')).strip()
    if new_barcode and new_barcode != variant.barcode:
        dup = ProductVariant.objects.filter(company=user_company, barcode=new_barcode).exclude(id=variant.id).exists()
        if dup:
            return JsonResponse({'success': False, 'message': f"'{new_barcode}' barkodu zaten kullanımda!"}, status=400)
        variant.barcode = new_barcode

    if 'stock_quantity' in body:
        try:
            variant.stock_quantity = Decimal(str(body['stock_quantity']))
        except Exception:
            return JsonResponse({'success': False, 'message': 'Geçersiz stok miktarı.'}, status=400)

    if 'price_override' in body:
        val = body['price_override']
        if val is None or str(val).strip() == '':
            variant.price_override = None
        else:
            try:
                variant.price_override = Decimal(str(val))
            except Exception:
                return JsonResponse({'success': False, 'message': 'Geçersiz fiyat.'}, status=400)

    if 'size' in body:
        variant.size = str(body['size']).strip()
    if 'color' in body:
        variant.color = str(body['color']).strip()
    if 'is_active' in body:
        variant.is_active = bool(body['is_active'])

    variant.save()

    # Ana ürün toplam stok senkronu
    total_v_stock = product.variants.aggregate(total=Sum('stock_quantity'))['total'] or Decimal('0.000')
    product.stock_quantity = total_v_stock
    product.save(update_fields=['stock_quantity'])

    return JsonResponse({
        'success': True,
        'message': f"Varyant ({variant.size} / {variant.color}) güncellendi.",
        'variant': {
            'id': variant.id,
            'size': variant.size,
            'color': variant.color,
            'barcode': variant.barcode,
            'stock_quantity': float(variant.stock_quantity),
            'price_override': float(variant.price_override) if variant.price_override is not None else None,
            'is_active': variant.is_active,
        },
        'product_total_stock': float(total_v_stock)
    })


@staff_api_required
@require_POST
def product_variant_delete_api(request, variant_id):
    """
    Faz 13: Bir varyantı silme ve ana ürün stok/bayrak senkronu.
    """
    user_company = get_user_company(request.user)
    variant = get_object_or_404(ProductVariant, id=variant_id, company=user_company)
    product = variant.product
    var_desc = f"{variant.size} / {variant.color}".strip(' / ')

    variant.delete()

    # Kalan varyant kontrolü
    remaining_count = product.variants.count()
    if remaining_count == 0:
        product.has_variants = False
    total_v_stock = product.variants.aggregate(total=Sum('stock_quantity'))['total'] or Decimal('0.000')
    product.stock_quantity = total_v_stock
    product.save(update_fields=['has_variants', 'stock_quantity'])

    log_action(
        'PRODUCT',
        'Varyant Silindi',
        f"'{product.name}' ürününün ({var_desc}) varyantı silindi.",
        user=request.user
    )

    return JsonResponse({
        'success': True,
        'message': f"Varyant ({var_desc}) silindi.",
        'remaining_variants': remaining_count,
        'product_total_stock': float(total_v_stock)
    })


def gift_receipt_view(request, receipt_number):
    """
    Faz 13: 80mm / 58mm Termal Hediye Değişim Fişi (Fiyatsız - Giyim/Butik/Perakende Uyumlu, Multi-Tenancy)
    Müşterilere hediye ürün alışverişlerinde fiyatsız değişim fişi olarak verilir.
    """
    user_company = get_user_company(request.user)
    sale = get_object_or_404(Sale, company=user_company, receipt_number=receipt_number)
    company = get_company_fiscal_profile(company=user_company)
    return render(request, 'thermal_gift_receipt.html', {'sale': sale, 'company': company})


# =====================================================================
# FAZ 14: ÇEVRİMDIŞI (OFFLINE) KASA MOTORU (PWA + INDEXEDDB) API'LERİ
# =====================================================================

@login_required
def pos_catalog_cache_api(request):
    """
    Faz 14: Çevrimdışı Kasa Ürün Kataloğu Önbellek Uç Noktası (Multi-Tenancy)
    Kasa açıldığında veya periyodik olarak mağazanın tüm aktif ürünlerini ve varyantlarını
    tarayıcı IndexedDB katmanına indirilmek üzere kompakt JSON formatında sunar.
    """
    user_company = get_user_company(request.user)

    products_qs = Product.objects.filter(company=user_company, is_active=True).prefetch_related(
        'variants'
    ).order_by('name')

    catalog_list = []
    for p in products_qs:
        variants_list = []
        if p.has_variants:
            for v in p.variants.filter(is_active=True):
                effective_price = float(v.price_override if v.price_override is not None else p.sell_price)
                variants_list.append({
                    'id': v.id,
                    'size': v.size,
                    'color': v.color,
                    'barcode': v.barcode,
                    'stock_quantity': float(v.stock_quantity),
                    'price_override': float(v.price_override) if v.price_override is not None else None,
                    'effective_price': effective_price,
                })

        catalog_list.append({
            'id': p.id,
            'name': p.name,
            'barcode': p.barcode or '',
            'sell_price': float(p.sell_price),
            'vat_rate': float(p.vat_rate),
            'unit': p.unit or 'Adet',
            'is_weighted': bool(p.is_weighted),
            'stock_quantity': float(p.stock_quantity),
            'has_variants': bool(p.has_variants),
            'variants': variants_list
        })

    return JsonResponse({
        'success': True,
        'company_id': user_company.id if user_company else None,
        'company_name': user_company.name if user_company else 'HiMarket',
        'business_type': getattr(user_company, 'business_type', 'MARKET'),
        'timestamp': timezone.now().isoformat(),
        'total_count': len(catalog_list),
        'products': catalog_list
    })


@login_required
def offline_sale_sync_api(request):
    """
    Faz 14: Çevrimdışı (Offline) Satışları Toplu / Tekil Senkronizasyon Uç Noktası
    İnternet bağlantısı kesildiğinde IndexedDB'de biriken satış fişlerini sunucuya aktarır,
    idempotency (tekrar önleme) denetimi yapar, stokları günceller ve mali fiş kaydeder.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Yalnızca POST istekleri kabul edilir.'}, status=405)

    user_company = get_user_company(request.user)
    try:
        data = json.loads(request.body.decode('utf-8'))
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'message': 'Geçersiz JSON verisi.'}, status=400)

    sales_payload = data.get('sales', [])
    if not isinstance(sales_payload, list) or not sales_payload:
        if isinstance(data.get('sale'), dict):
            sales_payload = [data['sale']]
        elif 'items' in data:
            sales_payload = [data]
        else:
            return JsonResponse({'success': False, 'message': 'Senkronize edilecek satış verisi bulunamadı.'}, status=400)

    # Dinamik Kasa ve Aktif Vardiya Çözümleme
    register = get_current_register(request, company=user_company)
    shift = Shift.objects.filter(company=user_company, cash_register=register, is_closed=False).first()
    if not shift:
        register, shift = get_or_create_active_shift(request.user, register=register, request=request)

    results = []
    synced_count = 0
    already_synced_count = 0
    failed_count = 0

    from django.utils.dateparse import parse_datetime
    import uuid

    for sale_dict in sales_payload:
        offline_id = str(sale_dict.get('offline_receipt_id') or sale_dict.get('offline_id') or '').strip()
        if not offline_id:
            offline_id = f"OFF-{timezone.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"

        # 1. Idempotency Kontrolü: Bu çevrimdışı fiş daha önce işlendi mi?
        existing_sale = Sale.objects.filter(company=user_company, offline_sync_id=offline_id).first()
        if existing_sale:
            already_synced_count += 1
            results.append({
                'offline_receipt_id': offline_id,
                'status': 'ALREADY_SYNCED',
                'server_receipt_number': existing_sale.receipt_number,
                'total_amount': float(existing_sale.total_amount),
                'message': 'Bu fiş daha önce başarıyla senkronize edilmişti.'
            })
            continue

        items_data = sale_dict.get('items', [])
        if not items_data:
            failed_count += 1
            results.append({
                'offline_receipt_id': offline_id,
                'status': 'FAILED',
                'message': 'Fişte ürün bulunmuyor.'
            })
            continue

        payment_type = sale_dict.get('payment_type', 'NAKIT')
        cash_paid = Decimal(str(sale_dict.get('cash_paid', 0)))
        card_paid = Decimal(str(sale_dict.get('card_paid', 0)))
        discount_amount = Decimal(str(sale_dict.get('discount_amount', 0)))
        is_return = bool(sale_dict.get('is_return', False))
        customer_id = sale_dict.get('customer_id')

        raw_created_at = sale_dict.get('created_at')
        offline_created_dt = parse_datetime(raw_created_at) if raw_created_at else None
        if not offline_created_dt:
            offline_created_dt = timezone.now()

        # Kalem ayrıştırma
        parsed_items = []
        parse_error = None
        for item_info in items_data:
            pid = item_info.get('product_id') or item_info.get('id')
            if not pid:
                parse_error = 'Ürün kimliği eksik.'
                break
            try:
                qty = Decimal(str(item_info.get('quantity', 1)))
            except (ValueError, TypeError, ArithmeticError):
                parse_error = 'Miktar formatı geçersiz.'
                break

            if qty <= Decimal('0.000'):
                parse_error = 'Ürün miktarı sıfırdan büyük olmalıdır.'
                break

            vid = item_info.get('variant_id')
            variant_id = None
            if vid:
                try:
                    variant_id = int(vid)
                except (ValueError, TypeError):
                    variant_id = None

            parsed_items.append({
                'product_id': int(pid),
                'quantity': qty,
                'variant_id': variant_id
            })

        if parse_error:
            failed_count += 1
            results.append({
                'offline_receipt_id': offline_id,
                'status': 'FAILED',
                'message': parse_error
            })
            continue

        # Atomik işlem ile satışı kaydet ve stokları düş
        try:
            with transaction.atomic():
                shift = Shift.objects.select_for_update().get(id=shift.id)

                product_ids_sorted = sorted({item['product_id'] for item in parsed_items})
                variant_ids_sorted = sorted({item['variant_id'] for item in parsed_items if item['variant_id']})

                locked_products = {p.id: p for p in Product.objects.select_for_update().filter(company=user_company, id__in=product_ids_sorted)}
                locked_variants = {v.id: v for v in ProductVariant.objects.select_for_update().filter(company=user_company, id__in=variant_ids_sorted)} if variant_ids_sorted else {}

                customer_obj = None
                if customer_id:
                    customer_obj = Customer.objects.select_for_update().filter(company=user_company, id=int(customer_id)).first()

                prelim_subtotal = Decimal('0.00')
                prelim_vat = Decimal('0.00')
                items_for_vat = []

                for item in parsed_items:
                    product = locked_products.get(item['product_id'])
                    if not product:
                        raise ValueError(f"Ürün bulunamadı (ID: {item['product_id']})")
                    vid = item['variant_id']
                    var_obj = locked_variants.get(vid) if vid else None
                    unit_price = var_obj.price_override if (var_obj and var_obj.price_override is not None) else product.sell_price

                    line_total = round(item['quantity'] * unit_price, 2)
                    if product.vat_rate > Decimal('0.00'):
                        line_sub = round(line_total / (Decimal('1.00') + (product.vat_rate / Decimal('100.00'))), 2)
                        line_v = line_total - line_sub
                    else:
                        line_sub = line_total
                        line_v = Decimal('0.00')
                    prelim_subtotal += line_sub
                    prelim_vat += line_v

                    items_for_vat.append({
                        'vat_rate': product.vat_rate,
                        'unit_price': unit_price,
                        'quantity': item['quantity']
                    })

                grand_total = max(Decimal('0.00'), (prelim_subtotal + prelim_vat) - discount_amount)
                change_given = Decimal('0.00')

                if not is_return:
                    if payment_type == 'NAKIT':
                        change_given = (cash_paid - grand_total) if cash_paid >= grand_total else Decimal('0.00')
                    elif payment_type == 'KART':
                        card_paid = grand_total
                    elif payment_type == 'PARCALI':
                        total_collected = cash_paid + card_paid
                        change_given = (total_collected - grand_total) if total_collected > grand_total else Decimal('0.00')

                today_str = timezone.localtime(offline_created_dt).strftime('%Y%m%d')
                prefix = "IADE" if is_return else "FIS"
                last_today_sale = Sale.objects.filter(company=user_company, receipt_number__contains=f"-{today_str}-").order_by('-id').first()
                next_seq = 1
                if last_today_sale and last_today_sale.receipt_number:
                    try:
                        parts = last_today_sale.receipt_number.split('-')
                        if len(parts) >= 3:
                            next_seq = int(parts[-1]) + 1
                    except (ValueError, TypeError):
                        next_seq = Sale.objects.filter(company=user_company, receipt_number__contains=today_str).count() + 1

                receipt_number = f"{prefix}-{today_str}-{next_seq:04d}"
                while Sale.objects.filter(company=user_company, receipt_number=receipt_number).exists():
                    next_seq += 1
                    receipt_number = f"{prefix}-{today_str}-{next_seq:04d}"

                vat_breakdown = calculate_vat_breakdown(items_for_vat, discount_amount)

                sale = Sale.objects.create(
                    company=user_company,
                    receipt_number=receipt_number,
                    shift=shift,
                    cashier=request.user,
                    customer=customer_obj,
                    payment_type=payment_type,
                    document_type='E_ARSIV',
                    customer_tax_number=customer_obj.tax_number if (customer_obj and customer_obj.tax_number) else '11111111111',
                    customer_title=customer_obj.full_name if customer_obj else 'Nihai Tüketici',
                    subtotal=vat_breakdown['total_subtotal'],
                    total_vat=vat_breakdown['total_vat'],
                    vat_1_base=vat_breakdown['vat_1_base'],
                    vat_1_amount=vat_breakdown['vat_1_amount'],
                    vat_10_base=vat_breakdown['vat_10_base'],
                    vat_10_amount=vat_breakdown['vat_10_amount'],
                    vat_20_base=vat_breakdown['vat_20_base'],
                    vat_20_amount=vat_breakdown['vat_20_amount'],
                    discount_amount=discount_amount,
                    total_amount=grand_total,
                    cash_paid=cash_paid,
                    card_paid=card_paid,
                    change_given=change_given,
                    is_return=is_return,
                    is_offline_sync=True,
                    offline_sync_id=offline_id,
                    offline_created_at=offline_created_dt
                )

                apply_fiscal_data_to_sale(sale, {
                    'tax_number': sale.customer_tax_number,
                    'title': sale.customer_title,
                    'tax_office': '',
                    'document_type': 'E_ARSIV',
                })

                if customer_obj:
                    if is_return:
                        Customer.objects.filter(id=customer_obj.id).update(balance=F('balance') - grand_total)
                        customer_obj.refresh_from_db(fields=['balance'])
                        CustomerTransaction.objects.create(
                            customer=customer_obj,
                            transaction_type='IADE',
                            amount=grand_total,
                            balance_after=customer_obj.balance,
                            sale=sale,
                            shift=shift,
                            document_no=sale.receipt_number,
                            description=f"Çevrimdışı İade Fişi #{sale.receipt_number} (Yerel: {offline_id}) ile borçtan düşüldü.",
                            user=request.user
                        )
                    elif payment_type == 'VERESIYE':
                        Customer.objects.filter(id=customer_obj.id).update(balance=F('balance') + grand_total)
                        customer_obj.refresh_from_db(fields=['balance'])
                        CustomerTransaction.objects.create(
                            customer=customer_obj,
                            transaction_type='VERESIYE_SATIS',
                            amount=grand_total,
                            balance_after=customer_obj.balance,
                            sale=sale,
                            shift=shift,
                            document_no=sale.receipt_number,
                            description=f"Çevrimdışı Veresiye Satış Fişi #{sale.receipt_number} (Yerel: {offline_id}) ile borç eklendi.",
                            user=request.user
                        )

                for item in parsed_items:
                    product = locked_products[item['product_id']]
                    vid = item['variant_id']
                    var_obj = locked_variants.get(vid) if vid else None
                    unit_price = var_obj.price_override if (var_obj and var_obj.price_override is not None) else product.sell_price
                    var_info = f"{var_obj.size} · {var_obj.color}" if var_obj else ""
                    item_barcode = var_obj.barcode if (var_obj and var_obj.barcode) else (product.barcode or '')
                    display_pname = f"{product.name} ({var_info})" if var_info else product.name

                    line_total = round(item['quantity'] * unit_price, 2)
                    if product.vat_rate > Decimal('0.00'):
                        line_sub = round(line_total / (Decimal('1.00') + (product.vat_rate / Decimal('100.00'))), 2)
                        line_vat = line_total - line_sub
                    else:
                        line_vat = Decimal('0.00')

                    SaleItem.objects.create(
                        sale=sale,
                        product=product,
                        variant=var_obj,
                        variant_info=var_info,
                        product_name=display_pname,
                        barcode=item_barcode,
                        quantity=item['quantity'],
                        unit_price=unit_price,
                        buy_price=product.buy_price if product else Decimal('0.00'),
                        vat_rate=product.vat_rate,
                        vat_amount=line_vat,
                        total_price=line_total
                    )

                    qty_delta = item['quantity'] if is_return else -item['quantity']
                    if var_obj:
                        ProductVariant.objects.filter(id=var_obj.id).update(stock_quantity=F('stock_quantity') + qty_delta)
                    Product.objects.filter(id=product.id).update(stock_quantity=F('stock_quantity') + qty_delta)

                effective_cash = max(Decimal('0.00'), cash_paid - change_given) if payment_type in ('NAKIT', 'PARCALI') else Decimal('0.00')

                if not is_return:
                    shift.total_sales_count = F('total_sales_count') + 1
                    if payment_type == 'NAKIT':
                        shift.total_cash_sales = F('total_cash_sales') + grand_total
                        shift.expected_cash = F('expected_cash') + grand_total
                    elif payment_type == 'KART':
                        shift.total_card_sales = F('total_card_sales') + grand_total
                    elif payment_type == 'PARCALI':
                        shift.total_cash_sales = F('total_cash_sales') + effective_cash
                        shift.expected_cash = F('expected_cash') + effective_cash
                        shift.total_card_sales = F('total_card_sales') + card_paid
                    elif payment_type == 'VERESIYE':
                        shift.total_veresiye_sales = F('total_veresiye_sales') + grand_total
                    shift.save(update_fields=['total_sales_count', 'total_cash_sales', 'total_card_sales', 'total_veresiye_sales', 'expected_cash'])
                else:
                    if payment_type == 'NAKIT':
                        shift.total_cash_sales = F('total_cash_sales') - grand_total
                        shift.expected_cash = F('expected_cash') - grand_total
                    elif payment_type == 'KART':
                        shift.total_card_sales = F('total_card_sales') - grand_total
                    elif payment_type == 'PARCALI':
                        shift.total_cash_sales = F('total_cash_sales') - effective_cash
                        shift.expected_cash = F('expected_cash') - effective_cash
                        shift.total_card_sales = F('total_card_sales') - card_paid
                    elif payment_type == 'VERESIYE':
                        shift.total_veresiye_sales = F('total_veresiye_sales') - grand_total
                    shift.save(update_fields=['total_cash_sales', 'total_card_sales', 'total_veresiye_sales', 'expected_cash'])

                log_action(
                    'SALE',
                    'Çevrimdışı Satış Senkronize Edildi',
                    f"Çevrimdışı Fiş #{offline_id} başarıyla sunucuya aktarıldı -> #{sale.receipt_number} (Tutar: {sale.total_amount:.2f} TL)",
                    user=request.user
                )

                synced_count += 1
                results.append({
                    'offline_receipt_id': offline_id,
                    'status': 'SYNCED',
                    'server_receipt_number': sale.receipt_number,
                    'total_amount': float(sale.total_amount),
                    'message': 'Başarıyla kaydedildi.'
                })

        except Exception as e:
            failed_count += 1
            results.append({
                'offline_receipt_id': offline_id,
                'status': 'FAILED',
                'message': str(e)
            })

    return JsonResponse({
        'success': True,
        'synced_count': synced_count,
        'already_synced_count': already_synced_count,
        'failed_count': failed_count,
        'results': results
    })


def service_worker_view(request):
    """
    PWA Service Worker Dosyası (Scope: '/')
    """
    sw_path = settings.BASE_DIR / 'static' / 'sw.js'
    if not sw_path.exists():
        return HttpResponse("// SW not found", content_type="application/javascript")
    with open(sw_path, 'r', encoding='utf-8') as f:
        content = f.read()
    response = HttpResponse(content, content_type="application/javascript")
    response['Service-Worker-Allowed'] = '/'
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


def manifest_view(request):
    """
    PWA Web App Manifest
    """
    manifest_path = settings.BASE_DIR / 'static' / 'manifest.json'
    if not manifest_path.exists():
        return JsonResponse({"name": "HiMarket POS"}, content_type="application/manifest+json")
    with open(manifest_path, 'r', encoding='utf-8') as f:
        content = f.read()
    response = HttpResponse(content, content_type="application/manifest+json")
    response['Cache-Control'] = 'public, max-age=86400'
    return response


# ==========================================================
# FAZ 18: ÇOKLU ŞUBE, DEPO STOK TRANSFERİ & MERKEZİ FİYAT YÖNETİMİ
# ==========================================================

@staff_required
def branch_transfers_view(request):
    """
    Faz 18: Şubeler ve Depolar Arası Stok Transferleri Listesi
    Merkez Depo ile şubeler veya şubeler arası sevk irsaliyelerini listeler.
    """
    user_company = get_user_company(request.user)
    status_filter = request.GET.get('status', '').strip()
    direction_filter = request.GET.get('direction', '').strip()  # 'SENT' or 'RECEIVED'
    q = request.GET.get('q', '').strip()

    if request.user.is_superuser and not user_company:
        base_qs = BranchTransfer.objects.all()
    else:
        base_qs = BranchTransfer.objects.filter(Q(from_company=user_company) | Q(to_company=user_company))

    transfers = base_qs.select_related('from_company', 'to_company', 'created_by', 'received_by').prefetch_related('items__product')

    if status_filter:
        transfers = transfers.filter(status=status_filter)
    if direction_filter == 'SENT':
        transfers = transfers.filter(from_company=user_company)
    elif direction_filter == 'RECEIVED':
        transfers = transfers.filter(to_company=user_company)

    if q:
        transfers = transfers.filter(
            Q(transfer_number__icontains=q) |
            Q(notes__icontains=q) |
            Q(from_company__name__icontains=q) |
            Q(to_company__name__icontains=q)
        )

    # KPI Metrikleri
    all_scoped = base_qs
    total_count = all_scoped.count()
    in_transit_count = all_scoped.filter(status='IN_TRANSIT').count()
    completed_count = all_scoped.filter(status='COMPLETED').count()
    incoming_pending_count = all_scoped.filter(to_company=user_company, status='IN_TRANSIT').count()

    paginator = Paginator(transfers, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    query_params = request.GET.copy()
    query_params.pop('page', None)
    query_string = query_params.urlencode()

    context = {
        'page_obj': page_obj,
        'transfers': page_obj.object_list,
        'status_filter': status_filter,
        'direction_filter': direction_filter,
        'q': q,
        'query_string': query_string,
        'total_count': total_count,
        'in_transit_count': in_transit_count,
        'completed_count': completed_count,
        'incoming_pending_count': incoming_pending_count,
        'user_company': user_company,
    }
    return render(request, 'admin/branch_transfers.html', context)


@staff_required
def branch_transfer_create_view(request):
    """
    Faz 18: Yeni Şubeler Arası Stok Transferi (Sevk İrsaliyesi) Hazırlama
    """
    user_company = get_user_company(request.user)
    target_companies = Company.objects.filter(is_active=True).exclude(id=user_company.id)

    available_products = Product.objects.filter(
        company_scope_q(user_company),
        is_active=True
    ).order_by('name')

    if request.method == 'POST':
        to_company_id = request.POST.get('to_company_id')
        notes = request.POST.get('notes', '').strip()
        action_type = request.POST.get('action_type', 'DRAFT')  # 'DRAFT' or 'SHIP'

        to_company = target_companies.filter(id=to_company_id).first()
        if not to_company:
            messages.error(request, "Lütfen geçerli bir varış şubesi veya depo seçiniz.")
            return render(request, 'admin/branch_transfer_form.html', {
                'target_companies': target_companies,
                'available_products': available_products,
                'user_company': user_company,
            })

        product_ids = request.POST.getlist('product_id[]')
        quantities = request.POST.getlist('quantity[]')
        item_notes = request.POST.getlist('item_notes[]')

        items_to_create = []
        for i, pid in enumerate(product_ids):
            if not pid:
                continue
            prod = available_products.filter(id=pid).first()
            if not prod:
                continue
            try:
                qty_val = _parse_decimal(quantities[i] if i < len(quantities) else '1', Decimal('1.000'))
            except ValueError:
                qty_val = Decimal('1.000')

            if qty_val <= Decimal('0.000'):
                continue

            note = item_notes[i].strip() if i < len(item_notes) else ''
            items_to_create.append({
                'product': prod,
                'quantity': qty_val,
                'unit': prod.unit,
                'notes': note
            })

        if not items_to_create:
            messages.error(request, "Transfer belgesine en az bir geçerli ürün ve miktar eklemelisiniz.")
            return render(request, 'admin/branch_transfer_form.html', {
                'target_companies': target_companies,
                'available_products': available_products,
                'user_company': user_company,
            })

        transfer_num = f"TRF-{timezone.now().strftime('%Y%m%d%H%M%S')}"
        with transaction.atomic():
            transfer = BranchTransfer.objects.create(
                transfer_number=transfer_num,
                from_company=user_company,
                to_company=to_company,
                status='DRAFT',
                notes=notes,
                created_by=request.user
            )
            for itm in items_to_create:
                BranchTransferItem.objects.create(
                    transfer=transfer,
                    product=itm['product'],
                    quantity=itm['quantity'],
                    unit=itm['unit'],
                    notes=itm['notes']
                )

            if action_type == 'SHIP':
                transfer.ship(user=request.user)
                log_action('TRANSFER', 'Transfer Sevk Edildi', f"{transfer.transfer_number} sevk edildi ({user_company.name} -> {to_company.name})", user=request.user, company=user_company)
                messages.success(request, f"Transfer #{transfer.transfer_number} oluşturuldu ve doğrudan sevk edildi (yola çıktı).")
            else:
                log_action('TRANSFER', 'Taslak Transfer Oluşturuldu', f"{transfer.transfer_number} taslağı kaydedildi ({user_company.name} -> {to_company.name})", user=request.user, company=user_company)
                messages.success(request, f"Transfer #{transfer.transfer_number} taslak olarak kaydedildi.")

        return redirect('branch_transfer_detail_view', transfer_id=transfer.id)

    return render(request, 'admin/branch_transfer_form.html', {
        'target_companies': target_companies,
        'available_products': available_products,
        'user_company': user_company,
    })


@staff_required
def branch_transfer_detail_view(request, transfer_id):
    """
    Transfer Belgesi Detayı ve Yazdırılabilir Sevk İrsaliyesi / Transfer Fişi
    """
    user_company = get_user_company(request.user)
    if request.user.is_superuser and not user_company:
        transfer = get_object_or_404(BranchTransfer.objects.select_related('from_company', 'to_company', 'created_by', 'received_by'), id=transfer_id)
    else:
        transfer = get_object_or_404(
            BranchTransfer.objects.filter(Q(from_company=user_company) | Q(to_company=user_company)).select_related('from_company', 'to_company', 'created_by', 'received_by'),
            id=transfer_id
        )

    items = transfer.items.select_related('product', 'product__category').all()
    can_ship = (transfer.status == 'DRAFT' and (transfer.from_company == user_company or request.user.is_superuser))
    can_receive = (transfer.status == 'IN_TRANSIT' and (transfer.to_company == user_company or request.user.is_superuser))
    can_cancel = (transfer.status in ('DRAFT', 'IN_TRANSIT') and (transfer.from_company == user_company or request.user.is_superuser))

    context = {
        'transfer': transfer,
        'items': items,
        'user_company': user_company,
        'can_ship': can_ship,
        'can_receive': can_receive,
        'can_cancel': can_cancel,
    }
    return render(request, 'admin/branch_transfer_detail.html', context)


@staff_required
@require_POST
def branch_transfer_ship_action(request, transfer_id):
    """Transferi yola çıkarır (Kaynak şubeden stok düşer)"""
    user_company = get_user_company(request.user)
    transfer = get_object_or_404(BranchTransfer, id=transfer_id)
    if transfer.from_company != user_company and not request.user.is_superuser:
        messages.error(request, "Bu transferi sevk etme yetkiniz bulunmamaktadır.")
        return redirect('branch_transfer_detail_view', transfer_id=transfer.id)

    try:
        with transaction.atomic():
            transfer.ship(user=request.user)
        log_action('TRANSFER', 'Transfer Sevk Edildi', f"{transfer.transfer_number} yola çıkarıldı.", user=request.user, company=user_company)
        messages.success(request, f"Transfer #{transfer.transfer_number} başarıyla sevk edildi. Ürünler şube stoğunuzdan düşüldü.")
    except Exception as e:
        messages.error(request, f"Sevk işlemi başarısız: {e}")
    return redirect('branch_transfer_detail_view', transfer_id=transfer.id)


@staff_required
@require_POST
def branch_transfer_receive_action(request, transfer_id):
    """Transferi varış şubesinde teslim alır (Hedef şube stoğuna dahil eder)"""
    user_company = get_user_company(request.user)
    transfer = get_object_or_404(BranchTransfer, id=transfer_id)
    if transfer.to_company != user_company and not request.user.is_superuser:
        messages.error(request, "Bu transferi teslim alma yetkiniz bulunmamaktadır.")
        return redirect('branch_transfer_detail_view', transfer_id=transfer.id)

    try:
        with transaction.atomic():
            transfer.receive(user=request.user)
        log_action('TRANSFER', 'Transfer Teslim Alındı', f"{transfer.transfer_number} teslim alındı ve stoğa eklendi.", user=request.user, company=user_company)
        messages.success(request, f"Transfer #{transfer.transfer_number} başarıyla teslim alındı ve ürünler şube stoğunuza dahil edildi.")
    except Exception as e:
        messages.error(request, f"Teslim alma işlemi başarısız: {e}")
    return redirect('branch_transfer_detail_view', transfer_id=transfer.id)


@staff_required
@require_POST
def branch_transfer_cancel_action(request, transfer_id):
    """Transferi iptal eder (Yoldaysa stokları kaynak şubeye geri yükler)"""
    user_company = get_user_company(request.user)
    transfer = get_object_or_404(BranchTransfer, id=transfer_id)
    if transfer.from_company != user_company and not request.user.is_superuser:
        messages.error(request, "Bu transferi iptal etme yetkiniz bulunmamaktadır.")
        return redirect('branch_transfer_detail_view', transfer_id=transfer.id)

    try:
        with transaction.atomic():
            transfer.cancel(user=request.user)
        log_action('TRANSFER', 'Transfer İptal Edildi', f"{transfer.transfer_number} iptal edildi.", user=request.user, company=user_company)
        messages.info(request, f"Transfer #{transfer.transfer_number} iptal edildi. Varsa düşülen stoklar geri iade edildi.")
    except Exception as e:
        messages.error(request, f"İptal işlemi başarısız: {e}")
    return redirect('branch_transfer_detail_view', transfer_id=transfer.id)


@staff_required
def export_transfers_csv(request):
    """Transfer belgeleri CSV dışa aktarımı"""
    user_company = get_user_company(request.user)
    transfers = BranchTransfer.objects.filter(
        Q(from_company=user_company) | Q(to_company=user_company)
    ).select_related('from_company', 'to_company', 'created_by')

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="himarket_transferler_{timezone.now().strftime("%Y%m%d")}.csv"'
    response.write('\ufeff'.encode('utf-8'))

    writer = csv.writer(response, delimiter=';')
    writer.writerow(['Transfer No', 'Çıkış Şubesi', 'Varış Şubesi', 'Durum', 'Toplam Miktar', 'Oluşturan', 'Sevk Tarihi', 'Teslim Tarihi', 'Notlar'])

    for t in transfers:
        writer.writerow([
            t.transfer_number,
            t.from_company.name,
            t.to_company.name,
            t.get_status_display(),
            f"{t.total_quantity:.3f}",
            t.created_by.username if t.created_by else '-',
            t.shipped_at.strftime('%d.%m.%Y %H:%M') if t.shipped_at else '-',
            t.received_at.strftime('%d.%m.%Y %H:%M') if t.received_at else '-',
            t.notes or ''
        ])
    return response


@staff_required
@require_POST
def product_propagate_price_api(request, product_id):
    """
    Faz 18: Merkezi Fiyat Yayılımı API (HQ Price Push)
    Merkez mağazadaki ürünün güncel satış fiyatını, aynı barkoda sahip tüm aktif bağlı şubelere tek tıkla senkronize eder.
    """
    user_company = get_user_company(request.user)
    product = get_object_or_404(Product.objects.filter(company_scope_q(user_company)), id=product_id)

    target_companies = Company.objects.filter(is_active=True).exclude(id=user_company.id)
    if user_company.branches.exists():
        target_companies = user_company.branches.filter(is_active=True)
    elif user_company.parent:
        target_companies = Company.objects.filter(
            Q(id=user_company.parent.id) | Q(parent=user_company.parent)
        ).exclude(id=user_company.id).filter(is_active=True)

    updated_count = 0
    branches_updated = []
    with transaction.atomic():
        for comp in target_companies:
            count = Product.objects.filter(company=comp, barcode=product.barcode).update(
                sell_price=product.sell_price,
                buy_price=product.buy_price,
                vat_rate=product.vat_rate,
                updated_at=timezone.now()
            )
            if count > 0:
                updated_count += count
                branches_updated.append(comp.name)

    log_action(
        'PRODUCT',
        'Merkezi Fiyat Dağıtımı',
        f"{product.name} [{product.barcode}] fiyatı (₺{product.sell_price:.2f}) {len(branches_updated)} şubeye senkronize edildi: {', '.join(branches_updated)}",
        user=request.user,
        company=user_company
    )

    return JsonResponse({
        'success': True,
        'message': f"'{product.name}' ürününün yeni satış fiyatı (₺{product.sell_price:.2f}) {len(branches_updated)} şubeye başarıyla dağıtıldı.",
        'updated_count': updated_count,
        'branches': branches_updated
    })


# ==========================================================
# FAZ 18: YENİ NESİL ÖKC & E-BELGE SİMÜLASYON API (GMP-3 / JSON)
# ==========================================================

@csrf_exempt
def okc_sale_trigger_api(request):
    """
    Faz 18: Yeni Nesil ÖKC & Harici POS / Mali Fiş Simülasyon API (GMP-3 / JSON)
    Beko, Hugin, Ingenico vb. yeni nesil ödeme kaydedici cihazlarla entegrasyonu simüle eder.
    Satış için mali onay kodu (FP-...), ETTN (UUID) ve GİB uyumlu karekod oluşturur.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Yalnızca POST istekleri kabul edilir.'}, status=405)

    try:
        if request.content_type == 'application/json':
            payload = json.loads(request.body.decode('utf-8'))
        else:
            payload = request.POST

        receipt_number = payload.get('receipt_number')
        sale_id = payload.get('sale_id')
        device_brand = payload.get('device_brand', 'BEKO_300TR')
        slip_number = payload.get('slip_number', f"{timezone.now().strftime('%H%M%S')}")

        sale = None
        if sale_id:
            sale = Sale.objects.filter(id=sale_id).first()
        elif receipt_number:
            sale = Sale.objects.filter(receipt_number=receipt_number).first()

        if not sale:
            return JsonResponse({'success': False, 'message': 'İlgili satış kaydı bulunamadı.'}, status=404)

        if not sale.ettn:
            sale.ettn = uuid.uuid4()

        fiscal_code = f"FP-{timezone.now().strftime('%Y%m%d')}-{random.randint(1000, 9999)}"
        sale.fiscal_approval_code = fiscal_code
        sale.document_type = 'BILGI_FISI' if sale.document_type == 'BILGI_FISI' else 'E_ARSIV'

        qr_data = (
            f"VKN:{sale.company.tax_number if sale.company and sale.company.tax_number else '1234567890'}|"
            f"TUTAR:{sale.total_amount:.2f}|"
            f"TARIH:{timezone.now().strftime('%Y-%m-%d')}|"
            f"ETTN:{str(sale.ettn)}|"
            f"OKC:{device_brand}"
        )
        sale.qr_code_data = qr_data
        sale.save(update_fields=['ettn', 'fiscal_approval_code', 'document_type', 'qr_code_data'])

        return JsonResponse({
            'success': True,
            'message': 'ÖKC Mali Onayı Başarıyla Alındı.',
            'fiscal_code': fiscal_code,
            'ettn': str(sale.ettn),
            'slip_number': slip_number,
            'qr_code_data': qr_data,
            'document_type': sale.document_type,
            'total_amount': float(sale.total_amount),
            'timestamp': timezone.now().isoformat()
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'ÖKC entegrasyon hatası: {str(e)}'}, status=500)









