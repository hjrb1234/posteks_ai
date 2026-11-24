"""Answer permission is separate from publishing a document's original file.

Nothing is enabled implicitly: an administrator may allow bounded excerpts in
client answers while the original, full pages and page images remain private.
"""
from sqlalchemy import or_, select
from sqlalchemy.orm import object_session

from .models import AppSetting, Document

ANSWER_ACCESS = {'staff_only', 'client_answers'}


def unreviewed_history(document, session=None):
    if not document:
        return False
    category = (document.category or '').casefold()
    if ('непроверен' in category or 'история переписки' in category
            or (document.indexing_details or {}).get('source_kind') == 'unreviewed_history'):
        return True
    session = session or object_session(document)
    return bool(session and session.scalar(select(AppSetting.key).where(
        AppSetting.key.startswith('history_import:'),
        AppSetting.value['document_id'].as_integer() == document.id).limit(1)))


def can_use_for_answer(document, include_internal=False):
    if not document or document.deleted or document.status not in {'indexed', 'partial'}:
        return False
    if include_internal:
        return True
    return bool((document.visibility == 'public'
                 or getattr(document, 'answer_access', 'staff_only') == 'client_answers')
                and not unreviewed_history(document))


def client_answer_predicate():
    """Initial SQL filter; callers still check fresh document provenance."""
    return or_(Document.visibility == 'public', Document.answer_access == 'client_answers')


def source_access_metadata(document, include_internal=False):
    original_private = document.visibility != 'public'
    return {'answer_access': getattr(document, 'answer_access', 'staff_only'),
            'original_private': original_private,
            'file_available': bool(include_internal or not original_private)}


def client_source_view(source, document):
    """Avoid carrying a private full-page/layout payload into public responses."""
    allowed = {'document_id', 'chunk_id', 'page', 'title', 'filename', 'model', 'models',
               'source_type', 'category', 'score', 'record_version', 'source_fingerprint'}
    value = {key: item for key, item in source.items() if key in allowed}
    value.update(source_access_metadata(document))
    value['text'] = str(source.get('text') or source.get('excerpt') or '')[:2200]
    value['excerpt'] = str(source.get('excerpt') or value['text'])[:1200]
    value['url'] = source.get('url') if value['file_available'] else None
    return value
