"""Download official pinned Tesseract data, verifying SHA-256 before use."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import urllib.request
from dotenv import load_dotenv
from server.ocr import data_directory, diagnostic

ROOT = Path(__file__).resolve().parent.parent
COMMIT = '87416418657359cb625c412a48b6e1d6d41c29bd'
DATA = {
    'rus': 'e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225',
    'eng': '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2',
}

def main():
    parser = argparse.ArgumentParser(description='Языковые данные Tesseract и проверка установки без изменения базы')
    parser.add_argument('--check', action='store_true', help='Проверить программу, пути и языки без скачивания')
    args = parser.parse_args()
    load_dotenv(ROOT / '.env')
    if args.check:
        result = diagnostic()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['available'] else 1
    destination = data_directory()
    destination.mkdir(parents=True, exist_ok=True)
    for language, expected in DATA.items():
        target = destination / (language + '.traineddata')
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            print(language + ': проверен, уже установлен')
            continue
        url = f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{COMMIT}/{language}.traineddata'
        with urllib.request.urlopen(url, timeout=45) as response:
            raw = response.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
            raise RuntimeError(f'Контрольная сумма {language} не совпадает. Данные не установлены.')
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes(raw)
        os.replace(temporary, target)
        print(language + ': установлен, SHA-256 проверен')
    print('Языки подготовлены. Проверка: python -m scripts.install_ocr --check')
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print('Языки OCR не установлены: проверьте сеть, права папки TESSDATA_PREFIX и повторите python -m scripts.install_ocr. PDF с текстовым слоем доступны без OCR. (' + type(exc).__name__ + ')', file=sys.stderr)
        raise SystemExit(1)
