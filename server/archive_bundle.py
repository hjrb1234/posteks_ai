"""Bounded local archive container, not an official MAX export format.

The importer never follows remote URLs and never extracts ZIP member paths onto
the filesystem. Original names are metadata; stored files use content hashes.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import PurePosixPath
import stat
from zipfile import BadZipFile, ZipFile

MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_MEMBERS = 2000
SCHEMA = 'posteks.archive.v1'


def _relative(value):
    value = str(value or '').replace('\\', '/')
    path = PurePosixPath(value)
    if (not value or '\x00' in value or path.is_absolute() or ':' in value
            or '..' in path.parts or value.endswith('/') or str(path) != value):
        raise ValueError('Недопустимый относительный путь файла в архиве.')
    return value


def parse_bundle(raw, filename):
    if not raw or len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError('Архив должен быть непустым и не превышать 32 МиБ.')
    try:
        archive = ZipFile(io.BytesIO(raw))
    except BadZipFile as exc:
        raise ValueError('Не удалось прочитать ZIP-контейнер истории.') from exc
    with archive:
        members = {}
        expanded = 0
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = _relative(member.filename)
            if name in members:
                raise ValueError('Архив содержит повторяющиеся имена файлов.')
            if stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
                raise ValueError('Ссылки и зашифрованные файлы в контейнере не поддерживаются.')
            expanded += member.file_size
            if len(members) >= MAX_MEMBERS or expanded > MAX_EXPANDED_BYTES:
                raise ValueError('Распакованный архив превышает безопасный лимит.')
            if member.file_size > MAX_ATTACHMENT_BYTES:
                raise ValueError('Отдельный файл архива превышает 25 МиБ.')
            members[name] = member
        if 'manifest.json' not in members:
            raise ValueError('Нужен manifest.json формата posteks.archive.v1; произвольный ZIP не считается экспортом MAX.')
        if members['manifest.json'].file_size > 5 * 1024 * 1024:
            raise ValueError('Манифест превышает 5 МиБ.')
        try:
            manifest = json.loads(archive.read(members['manifest.json']).decode('utf-8-sig'))
        except (ValueError, UnicodeError, BadZipFile, RecursionError) as exc:
            raise ValueError('Некорректный manifest.json.') from exc
        if not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA:
            raise ValueError('Поддерживается только явно указанный schema=posteks.archive.v1.')
        from .history_import import parse_history
        parsed = parse_history(json.dumps({'messages': manifest.get('messages')}, ensure_ascii=False).encode(), 'history.json')
        files, gaps = {}, []
        for message in parsed['messages']:
            for attachment in message['attachments']:
                if not attachment.get('path'):
                    attachment['file_state'] = 'missing'
                    attachment['reason'] = 'local_path_missing'
                    gaps.append({'message_id': message['id'] or None, 'attachment': attachment['filename'], 'reason': attachment['reason']})
                    continue
                path = _relative(attachment['path'])
                attachment['path'] = path
                member = members.get(path)
                if not member:
                    attachment['file_state'] = 'missing'
                    attachment['reason'] = 'file_not_in_archive'
                else:
                    try:
                        payload = archive.read(member)
                    except (OSError, BadZipFile, RuntimeError):
                        attachment['file_state'] = 'missing'
                        attachment['reason'] = 'zip_member_unreadable'
                    else:
                        digest = hashlib.sha256(payload).hexdigest()
                        if attachment.get('sha256') and attachment['sha256'] != digest:
                            attachment['file_state'] = 'missing'
                            attachment['reason'] = 'sha256_mismatch'
                        else:
                            attachment.update(file_state='present', sha256=digest, size_bytes=len(payload))
                            files[path] = payload
                if attachment['file_state'] != 'present':
                    gaps.append({'message_id': message['id'] or None, 'attachment': attachment['filename'], 'reason': attachment['reason']})
        parsed.update(format='zip', sha256=hashlib.sha256(raw).hexdigest(), bundle_files=files,
                      gaps=gaps, coverage={'completeness': 'unverified',
                          'export_claim': manifest.get('coverage') if isinstance(manifest.get('coverage'), dict) else {}})
        parsed['warnings'].append('Наличие ZIP не доказывает полноту чата. Покрытие периода и наличие исходных вложений требуют сверки с официальным клиентом.')
        return parsed
