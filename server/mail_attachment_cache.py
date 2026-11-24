"""Rebuild a missing private attachment cache from the retained original MIME."""
from email import policy
from email.parser import BytesParser
import hashlib
import os
from pathlib import Path, PurePosixPath
import re

from .config import settings


def restore_missing_attachment(session, attachment):
    from .workflows import InboxMessage
    from .mail_sync import _attachment_allowed
    inbox = session.get(InboxMessage, attachment.inbox_id)
    if not inbox or not inbox.raw_mime or attachment.status != 'available':
        return None
    message = BytesParser(policy=policy.default).parsebytes(inbox.raw_mime)
    for part in message.walk():
        if part.is_multipart() or not (part.get_content_disposition() == 'attachment' or part.get_filename()):
            continue
        filename = PurePosixPath(str(part.get_filename() or 'attachment').replace('\\', '/')).name[:240]
        filename = re.sub(r'[\x00-\x1f\x7f]', '_', filename) or 'attachment'
        if filename != attachment.filename:
            continue
        payload = part.get_payload(decode=True) or b''
        if len(payload) != attachment.size or hashlib.sha256(payload).hexdigest() != attachment.sha256:
            continue
        _, reason = _attachment_allowed(filename, payload)
        if reason:
            return None
        directory = (settings.storage_dir / 'mail_attachments').resolve()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (attachment.sha256 + Path(filename).suffix.casefold())
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Never overwrite an existing file; the download endpoint checks
            # its location, length and hash before serving it.
            return target
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
        return target
    return None
