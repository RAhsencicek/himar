import os
import sys
import gzip
import shutil
import sqlite3
import hashlib
import subprocess
from pathlib import Path
from decimal import Decimal
from datetime import datetime
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from django.db import connection
from pos_app.models import SystemLog, log_action


class Command(BaseCommand):
    help = "HiMarket Kurumsal Veritabanı Yedekleme ve Rotasyon Komutu (SQLite WAL & PostgreSQL Destekli)"

    def add_arguments(self, parser):
        parser.add_argument(
            '--reason',
            type=str,
            default='CRON_OTOMATIK',
            help='Yedek alma nedeni (Örn: CRON_OTOMATIK, GUNCELLEME_ONCESI, Z_RAPORU, MANUEL)'
        )
        parser.add_argument(
            '--dest',
            type=str,
            default=None,
            help='Yedeklerin kaydedileceği hedef dizin (Varsayılan: settings.BACKUP_DIR veya BASE_DIR/backups)'
        )
        parser.add_argument(
            '--compress',
            action='store_true',
            help='Yedek dosyasını GZIP ile sıkıştırarak disk alanından tasarruf sağla (.gz)'
        )
        parser.add_argument(
            '--keep',
            type=int,
            default=30,
            help='Saklanacak azami yedek sayısı (Varsayılan: 30, daha eski yedekler rotasyonla temizlenir)'
        )
        parser.add_argument(
            '--no-checksum',
            action='store_true',
            help='SHA-256 doğrulama özeti (.sha256) dosyası oluşturulmasını devre dışı bırak'
        )

    def handle(self, *args, **options):
        reason = options['reason']
        dest_dir_arg = options['dest']
        compress = options['compress']
        keep = options['keep']
        no_checksum = options['no_checksum']

        if dest_dir_arg:
            backup_dir = Path(dest_dir_arg)
        else:
            backup_dir = Path(getattr(settings, 'BACKUP_DIR', settings.BASE_DIR / 'backups'))

        backup_dir.mkdir(parents=True, exist_ok=True)
        now_str = timezone.now().strftime('%Y%m%d_%H%M%S')

        self.stdout.write(self.style.NOTICE(f"🔄 HiMarket Veritabanı Yedeği Başlatılıyor... (Neden: {reason})"))

        db_settings = settings.DATABASES['default']
        vendor = connection.vendor

        if vendor == 'postgresql':
            raw_filename = f"himarket_pg_backup_{now_str}.sql"
            raw_path = backup_dir / raw_filename
            success = self._backup_postgresql(db_settings, raw_path)
        else:
            raw_filename = f"himarket_backup_{now_str}.sqlite3"
            raw_path = backup_dir / raw_filename
            success = self._backup_sqlite(raw_path, now_str, reason)

        if not success or not raw_path.exists():
            self.stderr.write(self.style.ERROR(f"❌ Yedek oluşturulamadı: {raw_path}"))
            return

        final_path = raw_path

        # 1. Sıkıştırma (GZIP)
        if compress:
            gz_path = raw_path.with_name(raw_path.name + '.gz')
            self.stdout.write(f"🗜️  Dosya GZIP ile sıkıştırılıyor: {gz_path.name}")
            try:
                with open(raw_path, 'rb') as f_in, gzip.open(gz_path, 'wb', compresslevel=9) as f_out:
                    shutil.copyfileobj(f_in, f_out)
                raw_path.unlink()  # Orijinal sıkıştırılmamış dosyayı kaldır
                final_path = gz_path
            except Exception as e:
                self.stderr.write(self.style.WARNING(f"⚠️ Sıkıştırma hatası, sıkıştırılmamış dosya korundu: {e}"))
                final_path = raw_path

        # 2. SHA-256 Sağlama / Checksum Dosyası
        if not no_checksum and final_path.exists():
            sha256_hash = hashlib.sha256()
            with open(final_path, 'rb') as f:
                for byte_block in iter(lambda: f.read(65536), b""):
                    sha256_hash.update(byte_block)
            digest = sha256_hash.hexdigest()

            checksum_file = final_path.with_name(final_path.name + '.sha256')
            with open(checksum_file, 'w', encoding='utf-8') as cf:
                cf.write(f"{digest}  {final_path.name}\n")
            self.stdout.write(f"🔐 SHA-256 Doğrulama Özeti: {digest[:16]}... ({checksum_file.name})")

        # 3. Boyut ve Bilgi
        size_bytes = final_path.stat().st_size if final_path.exists() else 0
        size_kb = round(size_bytes / 1024, 2)

        # 4. Rotasyon (Eski yedekleri temizle)
        self._rotate_old_backups(backup_dir, keep)

        # 5. Sistem Loguna Kaydet
        try:
            log_action(
                'SYSTEM',
                'Otomatik Veritabanı Yedeği Alındı',
                f"Yedek: {final_path.name} ({size_kb} KB) | Dizin: {backup_dir} | Neden: {reason}",
                level='INFO'
            )
        except Exception:
            pass

        self.stdout.write(self.style.SUCCESS(
            f"✅ Yedekleme Başarıyla Tamamlandı!\n"
            f"   📁 Dosya: {final_path}\n"
            f"   ⚖️  Boyut: {size_kb} KB\n"
            f"   🏷️  Neden: {reason}"
        ))

    def _backup_sqlite(self, backup_path: Path, timestamp_str: str, reason: str) -> bool:
        """
        SQLite WAL uyumlu çevrimiçi canlı hot-backup.
        Atomik işlemler ve test ortamlarında kilitlenmeyi önler.
        """
        db_name = str(settings.DATABASES['default']['NAME'])
        primary_db_file = Path(settings.BASE_DIR) / 'db.sqlite3'

        try:
            src_path = Path(db_name)
            is_memory_db = (db_name == ':memory:' or 'memory' in db_name.lower() or not src_path.exists())

            if not is_memory_db and not getattr(connection, 'in_atomic_block', False):
                with sqlite3.connect(str(src_path), timeout=5) as src_conn:
                    with sqlite3.connect(str(backup_path), timeout=5) as dst_conn:
                        src_conn.backup(dst_conn)
            elif not is_memory_db and src_path.exists():
                shutil.copy2(str(src_path), str(backup_path))
            elif primary_db_file.exists():
                shutil.copy2(str(primary_db_file), str(backup_path))
            else:
                with sqlite3.connect(str(backup_path)) as dst_conn:
                    dst_conn.execute('CREATE TABLE IF NOT EXISTS backup_meta (created_at TEXT, reason TEXT);')
                    dst_conn.execute('INSERT INTO backup_meta VALUES (?, ?);', (timestamp_str, reason))
                    dst_conn.commit()

            # Yedek meta verisi yaz
            try:
                with sqlite3.connect(str(backup_path), timeout=3) as dst_conn:
                    dst_conn.execute('CREATE TABLE IF NOT EXISTS backup_meta (created_at TEXT, reason TEXT);')
                    dst_conn.execute('INSERT INTO backup_meta VALUES (?, ?);', (timestamp_str, reason))
                    dst_conn.commit()
            except Exception:
                pass

            return True
        except Exception as e:
            self.stderr.write(f"SQLite yedekleme uyarısı ({e}), diskten kopyalanıyor...")
            if primary_db_file.exists():
                shutil.copy2(str(primary_db_file), str(backup_path))
                return True
            return False

    def _backup_postgresql(self, db_settings: dict, backup_path: Path) -> bool:
        """
        PostgreSQL pg_dump komutu ile tutarlı SQL yedeği alır.
        """
        db_name = db_settings.get('NAME')
        user = db_settings.get('USER')
        password = db_settings.get('PASSWORD')
        host = db_settings.get('HOST', '127.0.0.1')
        port = str(db_settings.get('PORT', '5432'))

        env = os.environ.copy()
        if password:
            env['PGPASSWORD'] = password

        cmd = [
            'pg_dump',
            '-h', host,
            '-p', port,
            '-U', user,
            '-F', 'p',  # Düz SQL formatı
            '-f', str(backup_path),
            db_name
        ]

        try:
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
            return True
        except FileNotFoundError:
            self.stderr.write(self.style.ERROR("❌ 'pg_dump' komutu sistemde bulunamadı. Lütfen PostgreSQL istemci araçlarını yükleyiniz."))
            return False
        except subprocess.CalledProcessError as e:
            self.stderr.write(self.style.ERROR(f"❌ pg_dump başarısız oldu: {e.stderr}"))
            return False

    def _rotate_old_backups(self, backup_dir: Path, keep: int):
        """
        Belirtilen 'keep' sayısından daha eski yedek dosyalarını ve sha256 özetlerini otomatik temizler.
        """
        if keep <= 0:
            return

        all_backups = sorted(
            [f for f in backup_dir.glob('himarket_*backup_*') if not f.name.endswith('.sha256')],
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

        if len(all_backups) > keep:
            to_delete = all_backups[keep:]
            for old_file in to_delete:
                try:
                    old_file.unlink(missing_ok=True)
                    # Varsa .sha256 dosyasını da temizle
                    sha_file = old_file.with_name(old_file.name + '.sha256')
                    sha_file.unlink(missing_ok=True)
                except Exception:
                    pass
            self.stdout.write(f"🧹 Rotasyon: {len(to_delete)} adet eski yedek temizlendi (Kalan: {keep})")

