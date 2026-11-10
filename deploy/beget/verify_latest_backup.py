#!/usr/bin/env python3
"""Periodic job for the OFFLINE/TRUSTED recovery host, not the production VPS."""
import argparse,json
from pathlib import Path
from backup_retention import PATTERN,digest
from verify_backup import verify

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source-directory',type=Path,required=True);p.add_argument('--receipt-directory',type=Path,required=True);p.add_argument('--private-key',type=Path);a=p.parse_args()
    try:
        copies=sorted((f for f in a.source_directory.iterdir() if PATTERN.fullmatch(f.name) and f.is_file() and not f.is_symlink()),key=lambda f:f.stat().st_mtime,reverse=True)
        if not copies:raise ValueError('No copy')
        a.receipt_directory.mkdir(mode=0o700,parents=True,exist_ok=True);latest=copies[0];receipt=a.receipt_directory/(latest.name+'.verified.json')
        if receipt.exists():
            value=json.loads(receipt.read_text())
            if value.get('restored') is not True or value.get('sha256')!=digest(latest):raise ValueError('Previous receipt mismatch')
        else:verify(latest,receipt,a.private_key)
    except Exception:p.exit(1,'Периодическая проверка не завершена; ключ и данные не выводятся.\n')
    print('Последняя копия проверена восстановлением; подтверждение сохранено.')
