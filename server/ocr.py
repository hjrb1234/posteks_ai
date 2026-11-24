"""Tesseract discovery and diagnostics shared by ingestion and the installer.

This module deliberately does not initialize the application database, so an
old DATABASE_URL cannot prevent an administrator from repairing OCR.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent


class OCRSetupError(ValueError):
    def __init__(self, message: str, code: str):
        self.code = code
        super().__init__(message)


def project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def data_directory() -> Path:
    if value := os.getenv('TESSDATA_PREFIX', '').strip():
        return project_path(value)
    storage = os.getenv('STORAGE_DIR') or os.getenv('SERVICE_STORAGE_ROOT') or str(ROOT / 'storage')
    return project_path(storage) / 'tessdata'


def tesseract_binary() -> str:
    configured = os.getenv('TESSERACT_CMD', '').strip().strip('"')
    if configured:
        found = shutil.which(configured)
        if not found and ('/' in configured or '\\' in configured):
            path = project_path(configured)
            found = str(path) if path.is_file() else None
        if found:
            return found
        raise OCRSetupError('TESSERACT_CMD указывает на недоступную программу. Укажите полный путь к tesseract.exe в локальном .env и повторите обработку.', 'binary_configuration')
    if found := shutil.which('tesseract'):
        return found
    candidates = []
    for name, fallback in [('PROGRAMFILES', 'C:/Program Files'), ('PROGRAMFILES(X86)', 'C:/Program Files (x86)')]:
        candidates.append(Path(os.getenv(name, fallback)) / 'Tesseract-OCR' / 'tesseract.exe')
    if local := os.getenv('LOCALAPPDATA'):
        candidates.append(Path(local) / 'Programs' / 'Tesseract-OCR' / 'tesseract.exe')
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise OCRSetupError('Tesseract OCR не найден. Установите Tesseract 5 или задайте TESSERACT_CMD=C:/Program Files/Tesseract-OCR/tesseract.exe в локальном .env. PDF с текстом уже доступны.', 'binary_missing')


def language_configuration() -> tuple[str, str]:
    binary = tesseract_binary()
    prefix = data_directory()
    configured_prefix = bool(os.getenv('TESSDATA_PREFIX', '').strip())
    if configured_prefix and not prefix.is_dir():
        raise OCRSetupError('Папка TESSDATA_PREFIX отсутствует. Выполните python -m scripts.install_ocr из виртуальной среды проекта, затем повторите обработку документа.', 'data_missing')
    environment = os.environ.copy()
    if configured_prefix or all((prefix / (language + '.traineddata')).is_file() for language in ('rus', 'eng')):
        environment['TESSDATA_PREFIX'] = str(prefix)
    try:
        result = subprocess.run([binary, '--list-langs'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=10, check=False, env=environment)
    except subprocess.TimeoutExpired:
        raise OCRSetupError('Tesseract не ответил на проверку за 10 секунд. Проверьте установку и TESSERACT_CMD.', 'check_timeout') from None
    except OSError:
        raise OCRSetupError('Tesseract найден, но не запускается. Проверьте TESSERACT_CMD и установку Tesseract 5.', 'binary_unusable') from None
    languages = {line.strip() for line in result.stdout.decode('utf-8', errors='replace').splitlines()
                 if re.fullmatch(r'[a-z][a-z_]{1,30}', line.strip())}
    if result.returncode or not {'rus', 'eng'} <= languages:
        raise OCRSetupError('В Tesseract недоступны языки rus и eng. Выполните python -m scripts.install_ocr; проверьте TESSDATA_PREFIX (папка с rus.traineddata и eng.traineddata).', 'languages_missing')
    # pytesseract inherits this environment when starting the same executable.
    if 'TESSDATA_PREFIX' in environment:
        os.environ['TESSDATA_PREFIX'] = environment['TESSDATA_PREFIX']
    return binary, 'rus+eng'


def diagnostic() -> dict:
    try:
        binary, languages = language_configuration()
        return {'available': True, 'binary': binary, 'data_directory': str(data_directory()),
                'languages': languages.split('+'), 'error': None, 'code': None}
    except OCRSetupError as exc:
        return {'available': False, 'binary': None, 'data_directory': str(data_directory()),
                'languages': [], 'error': str(exc), 'code': exc.code}
