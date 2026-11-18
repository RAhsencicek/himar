"""
Hİ MARKET - ESC/POS Sessiz Termal Yazıcı, RJ11 Para Çekmecesi ve Donanım Servisi
--------------------------------------------------------------------------------
Tarayıcı tabanlı 'window.print()' (Ctrl+P) penceresi yerine arka planda doğrudan
termal yazıcıya ESC/POS bayt dizisi gönderen, kağıdı otomatik kesen ve
nakit tahsilatlarda RJ11 para çekmecesini fırlatan yerel donanım katmanı.
"""

import socket
from pathlib import Path
from decimal import Decimal
from django.conf import settings
from django.utils import timezone
from .fiscal_service import get_company_fiscal_profile, build_escpos_qr_command

# Standart ESC/POS Komut Sabitleri
ESC = b'\x1b'
GS = b'\x1d'
ESCPOS_INIT = ESC + b'@'                        # Yazıcıyı sıfırla / başlat
ESCPOS_ALIGN_LEFT = ESC + b'a\x00'              # Sola hizala
ESCPOS_ALIGN_CENTER = ESC + b'a\x01'            # Ortala
ESCPOS_ALIGN_RIGHT = ESC + b'a\x02'             # Sağa hizala
ESCPOS_BOLD_ON = ESC + b'E\x01'                 # Kalın yazı aç
ESCPOS_BOLD_OFF = ESC + b'E\x00'                # Kalın yazı kapat
ESCPOS_DOUBLE_HEIGHT_ON = GS + b'!\x11'         # Büyük punto aç
ESCPOS_DOUBLE_HEIGHT_OFF = GS + b'!\x00'        # Normal puntoya dön
ESCPOS_CUT_PAPER = GS + b'V\x41\x03'            # Otomatik giyotin kağıt kesme
ESCPOS_DRAWER_KICK_PIN2 = ESC + b'p\x00\x19\xfa'  # RJ11 Para Çekmecesi Tetik (Pin 2)
ESCPOS_DRAWER_KICK_PIN5 = ESC + b'p\x01\x19\xfa'  # RJ11 Para Çekmecesi Tetik (Pin 5)


def _tr_to_ascii(text: str) -> str:
    """
    Standart ESC/POS kod sayfası uyumsuzluğu olan yazıcılarda Türkçe karakterlerin
    bozuk çıkmaması için temiz dönüşüm sağlar.
    """
    tr_map = str.maketrans({
        'ç': 'c', 'Ç': 'C',
        'ğ': 'g', 'Ğ': 'G',
        'ı': 'i', 'İ': 'I',
        'ö': 'o', 'Ö': 'O',
        'ş': 's', 'Ş': 'S',
        'ü': 'u', 'Ü': 'U',
        '₺': 'TL',
    })
    return str(text or '').translate(tr_map)


def get_printer_config(company=None) -> dict:
    """
    Termal yazıcı ve donanım ayarlarını dinamik CompanySetting modelinden okur (Multi-Tenancy Uyumlu).
    Veritabanında yoksa settings.py ve .env varsayılanlarına döner.
    """
    try:
        from .models import CompanySetting
        setting = CompanySetting.get_settings(company=company)
        return {
            'host': setting.printer_ip or getattr(settings, 'PRINTER_HOST', '127.0.0.1'),
            'port': int(setting.printer_port or getattr(settings, 'PRINTER_PORT', 9100)),
            'mode': setting.printer_type or getattr(settings, 'PRINTER_MODE', 'SPOOL_AND_SOCKET'),
            'drawer_kick_on_cash': setting.drawer_kick_on_cash,
            'footer_note': setting.receipt_footer_note or 'Bizi tercih ettiğiniz için teşekkür ederiz.\nİyi günler dileriz!',
        }
    except Exception:
        return {
            'host': getattr(settings, 'PRINTER_HOST', '127.0.0.1'),
            'port': int(getattr(settings, 'PRINTER_PORT', 9100)),
            'mode': getattr(settings, 'PRINTER_MODE', 'SPOOL_AND_SOCKET'),
            'drawer_kick_on_cash': getattr(settings, 'CASH_DRAWER_KICK_ON_CASH', True),
            'footer_note': 'Bizi tercih ettiğiniz için teşekkür ederiz.\nİyi günler dileriz!',
        }


def build_escpos_receipt_payload(sale, kick_drawer: bool = False) -> tuple[bytes, str]:
    """
    Satış (Sale) nesnesinden 80mm (42 kolon) standart ESC/POS ham bayt dizisini (bytes)
    ve okunabilir metin kopyasını üretir.
    VUK 507, GİB e-Arşiv Fatura ve 2D Karekod komutlarını eksiksiz içerir.
    """
    sale_company = getattr(sale, 'company', None)
    company = get_company_fiscal_profile(company=sale_company)
    lines = []
    width = 42

    reg_code = sale.shift.cash_register.register_code if (sale.shift and sale.shift.cash_register) else 'KASA-01'
    cashier_name = (sale.cashier.get_full_name() or sale.cashier.username) if sale.cashier else 'Kasiyer'
    local_dt = timezone.localtime(sale.created_at).strftime('%d.%m.%Y %H:%M:%S')

    if sale.is_return:
        doc_title = "MUSTERI IADE FISI (GIDER PUSULASI)"
    elif getattr(sale, 'document_type', 'E_ARSIV') == 'E_FATURA':
        doc_title = "e-FATURA (VUK 507)"
    else:
        doc_title = "e-ARSIV FATURA (VUK 507)"

    # 1. RESMİ İŞLETME VE MALİ BAŞLIK
    lines.append("=" * width)
    lines.append(_tr_to_ascii(company['company_title'])[:width].center(width))
    lines.append(f"VKN: {company['tax_number']} | {_tr_to_ascii(company['tax_office'])}".center(width)[:width])
    lines.append(_tr_to_ascii(company['address'])[:width].center(width))
    lines.append(f"Mersis No: {company['mersis_no']}".center(width))
    lines.append("=" * width)
    lines.append(doc_title.center(width))
    lines.append("=" * width)

    # 2. MALİ BELGE VE MÜŞTERİ BİLGİLERİ
    lines.append(f"Fis No  : {sale.receipt_number}")
    lines.append(f"ETTN    : {getattr(sale, 'ettn', '')}")
    lines.append(f"Tarih   : {local_dt}")
    lines.append(f"Terminal: {reg_code} | Kasiyer: {_tr_to_ascii(cashier_name)}")

    cust_title = getattr(sale, 'customer_title', 'Nihai Tuketici') or 'Nihai Tuketici'
    cust_tax = getattr(sale, 'customer_tax_number', '11111111111') or '11111111111'
    lines.append(f"Musteri : {_tr_to_ascii(cust_title)[:22]} (VKN/TC:{cust_tax})")
    lines.append("-" * width)
    lines.append(f"{'URUN':<22}{'MIKTAR':>8}{'TUTAR':>12}")
    lines.append("-" * width)

    for item in sale.items.all():
        name_clean = _tr_to_ascii(item.product_name)[:21]
        qty_str = f"{float(item.quantity):.3f}".rstrip('0').rstrip('.')
        total_str = f"{float(item.total_price):.2f} TL"
        lines.append(f"{name_clean:<22}{qty_str:>8}{total_str:>12}")
        unit_info = f"  ({float(item.unit_price):.2f} TL x {qty_str} | KDV %{int(item.vat_rate)})"
        lines.append(unit_info[:width])

    lines.append("-" * width)
    lines.append(f"{'ARA TOPLAM (KDV HARIC):':<26}{float(sale.subtotal):>13.2f} TL")
    lines.append(f"{'TOPLAM KDV:':<26}{float(sale.total_vat):>13.2f} TL")
    if sale.discount_amount and sale.discount_amount > Decimal('0.00'):
        lines.append(f"{'ISKONTO:':<26}-{float(sale.discount_amount):>12.2f} TL")

    # 3. YASAL KDV AYRIŞTIRMA TABLOSU
    lines.append("-" * width)
    lines.append("KDV ORANI          MATRAH          KDV TUTARI")
    lines.append("-" * width)
    if getattr(sale, 'vat_1_base', Decimal('0.00')) > Decimal('0.00') or getattr(sale, 'vat_1_amount', Decimal('0.00')) > Decimal('0.00'):
        lines.append(f" %1 KDV       {float(sale.vat_1_base):>12.2f} TL {float(sale.vat_1_amount):>12.2f} TL")
    if getattr(sale, 'vat_10_base', Decimal('0.00')) > Decimal('0.00') or getattr(sale, 'vat_10_amount', Decimal('0.00')) > Decimal('0.00'):
        lines.append(f"%10 KDV       {float(sale.vat_10_base):>12.2f} TL {float(sale.vat_10_amount):>12.2f} TL")
    if getattr(sale, 'vat_20_base', Decimal('0.00')) > Decimal('0.00') or getattr(sale, 'vat_20_amount', Decimal('0.00')) > Decimal('0.00'):
        lines.append(f"%20 KDV       {float(sale.vat_20_base):>12.2f} TL {float(sale.vat_20_amount):>12.2f} TL")

    lines.append("=" * width)
    lines.append(f"{'GENEL TOPLAM:':<24}{float(sale.total_amount):>15.2f} TL")
    lines.append("=" * width)
    lines.append(f"ODEME TIPI : {_tr_to_ascii(sale.payment_type)}")
    if sale.payment_type == 'NAKIT' and not sale.is_return:
        lines.append(f"ALINAN NAKIT: {float(sale.cash_paid):.2f} TL")
        lines.append(f"PARA USTU   : {float(sale.change_given):.2f} TL")
    elif sale.payment_type == 'PARCALI':
        lines.append(f"NAKIT ODENEN: {float(sale.cash_paid):.2f} TL")
        lines.append(f"KART ODENEN : {float(sale.card_paid):.2f} TL")
        if sale.change_given and sale.change_given > Decimal('0.00'):
            lines.append(f"PARA USTU   : {float(sale.change_given):.2f} TL")
    elif sale.payment_type == 'VERESIYE':
        cust_name = sale.customer.full_name if sale.customer else (sale.customer_title or 'Cari Musteri')
        lines.append(f"CARI HESAP  : {_tr_to_ascii(cust_name)[:26]}")
        if sale.customer:
            lines.append(f"GUNCEL BORC : {float(sale.customer.balance):.2f} TL")

    approval_code = getattr(sale, 'fiscal_approval_code', '')
    if approval_code:
        lines.append(f"Mali Onay No: {approval_code}")

    # Özel Fiş Altı Teşekkür ve İletişim Notu (Dinamik Ayar)
    cfg = get_printer_config()
    footer_note = cfg.get('footer_note', '')
    if footer_note:
        lines.append("-" * width)
        for fl in footer_note.splitlines():
            if fl.strip():
                lines.append(_tr_to_ascii(fl.strip())[:width].center(width))

    lines.append("-" * width)
    lines.append("509 Sira No.lu VUK Genel Tebligi".center(width))
    lines.append("uyarinca e-Arsiv Fatura olarak".center(width))
    lines.append("elektronik ortamda duzenlenmistir.".center(width))
    lines.append("=" * width)

    readable_text = "\n".join(lines) + "\n"

    # ESC/POS Ham Bayt Akışı Oluşturma
    buf = bytearray()
    buf.extend(ESCPOS_INIT)
    if kick_drawer:
        buf.extend(ESCPOS_DRAWER_KICK_PIN2)

    buf.extend(ESCPOS_ALIGN_CENTER)
    buf.extend(ESCPOS_BOLD_ON)
    buf.extend(company['company_title'][:32].encode('ascii', errors='ignore') + b"\n")
    buf.extend(doc_title.encode('ascii', errors='ignore') + b"\n")
    buf.extend(ESCPOS_BOLD_OFF)
    buf.extend(ESCPOS_ALIGN_LEFT)
    buf.extend(readable_text.encode('ascii', errors='replace'))

    # 4. GİB KAREKOD (QR CODE) BASKISI
    qr_data = getattr(sale, 'qr_code_data', None)
    if qr_data:
        buf.extend(ESCPOS_ALIGN_CENTER)
        buf.extend(b"\n[ GIB e-ARSIV KAREKOD ]\n")
        buf.extend(build_escpos_qr_command(qr_data))
        buf.extend(b"\n")
        buf.extend(ESCPOS_ALIGN_LEFT)

    buf.extend(b"\n\n\n")
    buf.extend(ESCPOS_CUT_PAPER)

    return bytes(buf), readable_text


def silent_print_receipt(sale, open_drawer: bool = None) -> dict:
    """
    Tarayıcı Ctrl+P penceresi açmadan arka planda sessizce ESC/POS termal fiş basar
    ve nakit işlemlerde RJ11 para çekmecesini tetikler.
    1. Fiziksel Ağ/USB/Windows yazıcısı varsa doğrudan bayt akışını gönderir.
    2. Her durumda 'backups/printed_receipts/' altına ESC/POS bayt (.bin) ve (.txt)
       kopyasını kaydeder (Spooler güvencesi).
    """
    cfg = get_printer_config(company=getattr(sale, 'company', None))
    if open_drawer is None:
        open_drawer = (
            cfg['drawer_kick_on_cash']
            and sale.payment_type == 'NAKIT'
        )

    raw_bytes, readable_text = build_escpos_receipt_payload(sale, kick_drawer=open_drawer)

    # 1. Yerel Spooler Klasörüne Kayıt (Donanım bağlı olmasa bile %100 kanıtlanabilir çıktı)
    spool_dir = Path(getattr(settings, 'BACKUP_DIR', settings.BASE_DIR / 'backups')) / 'printed_receipts'
    spool_dir.mkdir(parents=True, exist_ok=True)

    safe_receipt_no = "".join(c for c in sale.receipt_number if c.isalnum() or c in ('-', '_'))
    bin_file = spool_dir / f"{safe_receipt_no}.escpos.bin"
    txt_file = spool_dir / f"{safe_receipt_no}.txt"

    bin_file.write_bytes(raw_bytes)
    txt_file.write_text(readable_text, encoding='utf-8')

    hardware_sent = False
    transport_used = 'LOCAL_ESC_POS_SPOOLER'

    # 2. Opsiyonel Ağ Termal Yazıcısı (Raw Socket Port 9100) veya python-escpos gönderimi
    printer_host = cfg['host']
    printer_port = cfg['port']
    printer_mode = cfg['mode']

    if printer_mode in ('SOCKET_ONLY', 'SPOOL_AND_SOCKET') and printer_host:
        try:
            with socket.create_connection((printer_host, printer_port), timeout=0.35) as sock:
                sock.sendall(raw_bytes)
                hardware_sent = True
                transport_used = f'RAW_SOCKET ({printer_host}:{printer_port})'
        except Exception:
            # Fiziki yazıcı o an ağda yoksa sessizce yerel spooler üzerinden tamamla
            pass

    return {
        'success': True,
        'receipt_number': sale.receipt_number,
        'drawer_kicked': open_drawer,
        'hardware_sent': hardware_sent,
        'transport': transport_used,
        'spool_file': str(txt_file.name),
        'bytes_written': len(raw_bytes),
    }


def trigger_cash_drawer_only(register_code: str = 'KASA-01') -> dict:
    """
    Satış olmadan yalnızca RJ11 para çekmecesini açmak için ESC/POS darbe sinyali gönderir.
    """
    cfg = get_printer_config()
    payload = ESCPOS_INIT + ESCPOS_DRAWER_KICK_PIN2 + ESCPOS_DRAWER_KICK_PIN5
    printer_host = cfg['host']
    printer_port = cfg['port']
    hardware_sent = False

    try:
        with socket.create_connection((printer_host, printer_port), timeout=0.35) as sock:
            sock.sendall(payload)
            hardware_sent = True
    except Exception:
        pass

    return {
        'success': True,
        'register_code': register_code,
        'hardware_sent': hardware_sent,
        'command_hex': payload.hex(),
    }


def build_escpos_collection_receipt_payload(tx, open_drawer: bool = None) -> tuple[bytes, str]:
    """
    Veresiye Borç Tahsilat Makbuzu için ESC/POS bayt akışı ve okunabilir metin üretir.
    """
    tx_company = getattr(tx, 'company', None)
    cfg = get_printer_config(company=tx_company)
    width = 42 if cfg['paper_width'] == 80 else 32
    company = get_company_fiscal_profile(company=tx_company)

    is_cash = (tx.transaction_type == 'TAHSILAT_NAKIT')
    kick_drawer = (open_drawer is True) or (open_drawer is None and cfg['drawer_kick_on_cash'] and is_cash)

    customer = tx.customer
    prev_balance = tx.balance_after + tx.amount

    local_dt = timezone.localtime(tx.created_at).strftime('%d.%m.%Y %H:%M:%S')
    cashier_name = (tx.user.get_full_name() or tx.user.username) if tx.user else 'Kasa Görevlisi'
    reg_code = tx.shift.cash_register.register_code if (tx.shift and tx.shift.cash_register) else 'KASA-01'

    type_label = "NAKIT TAHSILAT" if is_cash else "KART ILE TAHSILAT"

    lines = []
    lines.append("=" * width)
    lines.append(_tr_to_ascii(company['company_title'])[:width].center(width))
    lines.append(f"VKN: {company['tax_number']}".center(width))
    lines.append(_tr_to_ascii(company['address'])[:width].center(width))
    lines.append("=" * width)
    lines.append("CARI BORC TAHSILAT MAKBUZU".center(width))
    lines.append("=" * width)
    lines.append(f"Makbuz No: {tx.document_no or ('MAKBUZ-' + str(tx.id))}")
    lines.append(f"Tarih    : {local_dt}")
    lines.append(f"Terminal : {reg_code} | Personel: {_tr_to_ascii(cashier_name)[:12]}")
    lines.append("-" * width)
    lines.append(f"Musteri  : {_tr_to_ascii(customer.full_name)[:28]}")
    if customer.phone:
        lines.append(f"Telefon  : {customer.phone}")
    if customer.tax_number:
        lines.append(f"TCKN/VKN : {customer.tax_number}")
    lines.append("-" * width)
    lines.append(f"Tahsilat Sekli : {type_label}")
    lines.append(f"Onceki Borc    : {float(prev_balance):>12.2f} TL")
    lines.append(f"TAHSIL EDILEN  : {float(tx.amount):>12.2f} TL")
    lines.append(f"KALAN BORC     : {float(tx.balance_after):>12.2f} TL")
    lines.append("-" * width)
    if tx.description:
        lines.append(f"Not: {_tr_to_ascii(tx.description)[:width]}")
        lines.append("-" * width)

    lines.append("\n")
    lines.append(f"{'Teslim Eden':<18}{'Tahsil Eden':>18}")
    lines.append(f"{'(Imza)':<18}{'(Kasiyer Imza)':>18}")
    lines.append("\n")
    lines.append("=" * width)
    lines.append("Borcunuz basariyla tahsil edilmistir.".center(width))
    lines.append("Tesekkur ederiz.".center(width))
    lines.append("=" * width)

    readable_text = "\n".join(lines) + "\n"

    buf = bytearray()
    buf.extend(ESCPOS_INIT)
    if kick_drawer:
        buf.extend(ESCPOS_DRAWER_KICK_PIN2)

    buf.extend(ESCPOS_ALIGN_CENTER)
    buf.extend(ESCPOS_BOLD_ON)
    buf.extend(company['company_title'][:32].encode('ascii', errors='ignore') + b"\n")
    buf.extend(b"TAHSILAT MAKBUZU\n")
    buf.extend(ESCPOS_BOLD_OFF)
    buf.extend(ESCPOS_ALIGN_LEFT)
    buf.extend(readable_text.encode('ascii', errors='replace'))
    buf.extend(b"\n\n\n")
    buf.extend(ESCPOS_CUT_PAPER)

    return bytes(buf), readable_text


def silent_print_collection_receipt(tx, open_drawer: bool = None) -> dict:
    """
    Tahsilat makbuzunu termal yazıcıya sessizce basar ve yerel kuyruk dosyasına yazar.
    """
    raw_bytes, readable_text = build_escpos_collection_receipt_payload(tx, open_drawer=open_drawer)
    cfg = get_printer_config(company=getattr(tx, 'company', None))

    spool_dir = Path(getattr(settings, 'ESCPOS_SPOOL_DIR', settings.BASE_DIR / 'spool'))
    spool_dir.mkdir(parents=True, exist_ok=True)

    today_str = timezone.localtime(tx.created_at).strftime('%Y%m%d')
    doc_safe = (tx.document_no or f"COLLECTION_{tx.id}").replace('/', '_')
    txt_file = spool_dir / f"receipt_{today_str}_{doc_safe}.txt"
    bin_file = spool_dir / f"receipt_{today_str}_{doc_safe}.bin"

    with open(txt_file, 'w', encoding='utf-8') as f:
        f.write(readable_text)
    with open(bin_file, 'wb') as f:
        f.write(raw_bytes)

    hardware_sent = False
    transport_used = 'LOCAL_ESC_POS_SPOOLER'
    printer_host = cfg['host']
    printer_port = cfg['port']
    printer_mode = cfg['mode']

    if printer_mode in ('SOCKET_ONLY', 'SPOOL_AND_SOCKET') and printer_host:
        try:
            with socket.create_connection((printer_host, printer_port), timeout=0.35) as sock:
                sock.sendall(raw_bytes)
                hardware_sent = True
                transport_used = f'RAW_SOCKET ({printer_host}:{printer_port})'
        except Exception:
            pass

    return {
        'success': True,
        'document_no': tx.document_no,
        'drawer_kicked': open_drawer if open_drawer is not None else (tx.transaction_type == 'TAHSILAT_NAKIT'),
        'hardware_sent': hardware_sent,
        'transport': transport_used,
        'spool_file': str(txt_file.name),
        'bytes_written': len(raw_bytes),
    }

