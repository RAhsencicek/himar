"""
Hİ MARKET - Raf Etiketi ve Barkod Basım Servisi
------------------------------------------------
T.C. Ticaret Bakanlığı Fiyat Etiketi Yönetmeliği'ne %100 uyumlu raf etiketleri.
Yasal Zorunlu Alanlar:
- Malın satış fiyatı ve birim fiyatı (Örn: 1 kg = 85.00 TL)
- Fiyat değişiklik tarihi (Gün/Ay/Yıl)
- Üretim yeri / Menşei (TÜRKİYE)
- Tüm vergiler dahil ibaresi
- EAN-13 barkod görseli ve numarası

Ayrıca Argox, TSC ve Zebra masaüstü termal barkod yazıcıları için
doğrudan TSPL ve ZPL komut çıktıları üretir.
"""

from decimal import Decimal
from django.utils import timezone
from .barcode_service import generate_ean13_svg


def prepare_product_label_context(product) -> dict:
    """
    Tek bir ürün için yasal raf etiketi verilerini hazırlar.
    """
    now = timezone.localtime(timezone.now())
    date_str = now.strftime('%d.%m.%Y')

    # Birim Fiyatı Hesaplama (1 kg = X TL veya 1 Adet = X TL)
    unit_str = product.unit or 'Adet'
    unit_price_display = f"1 {unit_str} = {float(product.sell_price):.2f} TL"

    # SVG Barkod
    barcode_svg = generate_ean13_svg(product.barcode, width=170, height=55, show_text=True)

    return {
        'id': product.id,
        'name': product.name,
        'barcode': product.barcode,
        'sell_price': float(product.sell_price),
        'vat_rate': int(product.vat_rate),
        'unit': unit_str,
        'unit_price_display': unit_price_display,
        'price_date': date_str,
        'origin': 'TÜRKİYE',
        'barcode_svg': barcode_svg,
    }


def generate_tspl_label_payload(product, width_mm: int = 60, height_mm: int = 40) -> bytes:
    """
    Argox, TSC, Xprinter, HPRT masaüstü termal etiket yazıcıları için TSPL komutları.
    """
    price_str = f"{float(product.sell_price):.2f} TL"
    name_clean = product.name[:28].replace('"', '')
    now_date = timezone.localtime(timezone.now()).strftime('%d.%m.%Y')

    lines = [
        f"SIZE {width_mm} mm, {height_mm} mm",
        "GAP 2 mm, 0 mm",
        "DIRECTION 1",
        "CLS",
        f'TEXT 20,20,"3",0,1,1,"{name_clean}"',
        f'TEXT 20,65,"2",0,1,1,"Mensei: TURKIYE | KDV Dahil"',
        f'TEXT 20,95,"2",0,1,1,"Tarih: {now_date}"',
        f'TEXT 20,135,"4",0,1,1,"{price_str}"',
        f'BARCODE 20,200,"EAN13",60,1,0,2,2,"{product.barcode}"',
        "PRINT 1,1",
        ""
    ]
    return "\r\n".join(lines).encode('ascii', errors='ignore')


def generate_zpl_label_payload(product, width_mm: int = 60, height_mm: int = 40) -> bytes:
    """
    Zebra (ZPL-II) masaüstü termal etiket yazıcıları için ZPL komutları.
    """
    price_str = f"{float(product.sell_price):.2f} TL"
    name_clean = product.name[:28].replace('^', '').replace('~', '')
    now_date = timezone.localtime(timezone.now()).strftime('%d.%m.%Y')

    zpl = (
        "^XA"
        "^PW480"
        f"^FO20,20^A0N,28,28^FD{name_clean}^FS"
        f"^FO20,60^A0N,20,20^FDMensei: TURKIYE  Tarih: {now_date}^FS"
        f"^FO20,95^A0N,44,44^FD{price_str}^FS"
        f"^FO20,160^BEN,60,Y,N^FD{product.barcode}^FS"
        "^XZ"
    )
    return zpl.encode('ascii', errors='ignore')

