"""
Hİ MARKET - EAN-13 Standart Barkod Üretim ve SVG Çizim Servisi
--------------------------------------------------------------
GS1 ve Türkiye Ticaret Standartlarına uygun 13 haneli EAN-13 barkod algoritması.
Mod-10 kontrol basamağı hesaplama, doğrulama ve harici kütüphane gerektirmeyen
saf Python vektörel SVG barkod çizim motoru.
"""

from typing import Tuple


# EAN-13 Standart Kodlama Tabloları (A, B, C Tabloları ve Sol Basamak Eşlikleri)
L_CODE_A = {
    '0': '0001101', '1': '0011001', '2': '0010011', '3': '0111101', '4': '0100011',
    '5': '0110001', '6': '0101111', '7': '0111011', '8': '0110111', '9': '0001011'
}

L_CODE_B = {
    '0': '0100111', '1': '0110011', '2': '0011011', '3': '0100001', '4': '0011101',
    '5': '0111001', '6': '0000101', '7': '0010001', '8': '0001001', '9': '0010111'
}

R_CODE_C = {
    '0': '1110010', '1': '1100110', '2': '1101100', '3': '1000010', '4': '1011100',
    '5': '1001110', '6': '1010000', '7': '1000100', '8': '1001000', '9': '1110100'
}

# 1. Basamağa Göre Sol 6 Karakterin Eşlik Deseni (Parity Pattern)
FIRST_DIGIT_PARITY = {
    '0': 'AAAAAA', '1': 'AABABB', '2': 'AABBAB', '3': 'AABBBA', '4': 'ABAABB',
    '5': 'ABBAAB', '6': 'ABBBAA', '7': 'ABABAB', '8': 'ABABBA', '9': 'ABBABA'
}


def calculate_ean13_checksum(twelve_digits: str) -> int:
    """
    12 haneli sayı dizisi için Modulo 10 EAN-13 kontrol basamağını hesaplar.
    Kural: Çift indisler x3, Tek indisler x1.
    """
    digits = [int(c) for c in twelve_digits if c.isdigit()]
    if len(digits) != 12:
        raise ValueError("Kontrol basamağı için tam 12 adet rakam gereklidir.")

    # EAN-13 kuralı: 1. basamak (tek) x1, 2. basamak (çift) x3 ...
    odd_sum = sum(digits[i] for i in range(0, 12, 2))
    even_sum = sum(digits[i] for i in range(1, 12, 2))
    total = odd_sum + (even_sum * 3)
    mod = total % 10
    return (10 - mod) % 10


def validate_ean13(barcode: str) -> bool:
    """
    13 haneli bir barkodun standart EAN-13 kontrol basamağı kuralına uyup uymadığını doğrular.
    """
    cleaned = ''.join(c for c in str(barcode) if c.isdigit())
    if len(cleaned) != 13:
        return False
    twelve = cleaned[:12]
    expected_check = calculate_ean13_checksum(twelve)
    return int(cleaned[12]) == expected_check


def generate_valid_ean13(prefix: str = '869', sequence_number: int = 1) -> str:
    """
    Verilen ön ek ve sıra numarasına göre standart 13 haneli EAN-13 üretir.
    Örnek: prefix='8690001', sequence=42 -> '869000100042' + check digit.
    """
    prefix_clean = ''.join(c for c in str(prefix) if c.isdigit())
    needed_len = 12 - len(prefix_clean)
    if needed_len < 1:
        prefix_clean = prefix_clean[:11]
        needed_len = 1

    seq_str = str(sequence_number).zfill(needed_len)
    if len(seq_str) > needed_len:
        seq_str = seq_str[-needed_len:]

    twelve = f"{prefix_clean}{seq_str}"
    chk = calculate_ean13_checksum(twelve)
    return f"{twelve}{chk}"


def generate_ean13_bit_pattern(ean13_str: str) -> Tuple[str, str]:
    """
    13 haneli EAN-13 kodunu çizgisel 1/0 bit dizilimine dönüştürür.
    Çıktı: (bit_string, display_text)
    """
    cleaned = ''.join(c for c in str(ean13_str) if c.isdigit()).zfill(13)[:13]
    first_digit = cleaned[0]
    left_digits = cleaned[1:7]
    right_digits = cleaned[7:13]

    parity = FIRST_DIGIT_PARITY.get(first_digit, 'AAAAAA')

    bits = []
    # Sol Başlangıç Koruması (Start Guard: 101)
    bits.append('101')

    # Sol 6 Basamak
    for d, mode in zip(left_digits, parity):
        if mode == 'A':
            bits.append(L_CODE_A[d])
        else:
            bits.append(L_CODE_B[d])

    # Orta Ayırıcı Koruma (Center Guard: 01010)
    bits.append('01010')

    # Sağ 6 Basamak
    for d in right_digits:
        bits.append(R_CODE_C[d])

    # Sağ Bitiş Koruması (Stop Guard: 101)
    bits.append('101')

    return ''.join(bits), f"{first_digit} {left_digits} {right_digits}"


def generate_ean13_svg(barcode_str: str, width: int = 180, height: int = 65, show_text: bool = True) -> str:
    """
    Harici binary bağımlılık olmaksızın saf Python ile yüksek çözünürlüklü SVG barkod üretir.
    Termal etiket yazıcılarında (Argox, Zebra, Xprinter) ve web arayüzünde kayıpsız ölçeklenir.
    """
    # 13 haneli değilse tamamla veya düzelt
    clean_digits = ''.join(c for c in str(barcode_str) if c.isdigit())
    if len(clean_digits) == 12:
        clean_digits += str(calculate_ean13_checksum(clean_digits))
    elif len(clean_digits) != 13:
        clean_digits = generate_valid_ean13('8690001', 1)

    bit_string, formatted_label = generate_ean13_bit_pattern(clean_digits)
    total_modules = len(bit_string)  # 95 modül

    module_width = width / (total_modules + 16)  # Sol ve sağ marjin için +16
    left_offset = 8 * module_width
    bar_height = height - (16 if show_text else 0)

    rects = []
    current_x = left_offset

    for i, bit in enumerate(bit_string):
        if bit == '1':
            # Muhafaza çizgileri (start, center, stop) biraz daha uzundur
            is_guard = (i < 3) or (45 <= i < 50) or (i >= 92)
            h = height - (8 if (show_text and not is_guard) else 2)
            rects.append(
                f'<rect x="{current_x:.2f}" y="2" width="{module_width:.2f}" height="{h:.2f}" fill="#000000" />'
            )
        current_x += module_width

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
        f'<rect width="{width}" height="{height}" fill="#ffffff" />',
        ''.join(rects)
    ]

    if show_text:
        text_y = height - 2
        text_x = width / 2
        svg_parts.append(
            f'<text x="{text_x:.2f}" y="{text_y}" font-family="Courier, monospace" font-size="11" font-weight="bold" fill="#000000" text-anchor="middle">{formatted_label}</text>'
        )

    svg_parts.append('</svg>')
    return '\n'.join(svg_parts)

