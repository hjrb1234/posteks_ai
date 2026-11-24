"""Local, page-preserving document ingestion and deterministic knowledge search.

The search below is deliberately lexical. It is useful without an API key and does
not claim to be semantic retrieval or a model-generated answer.
"""
from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
import re
import warnings
import zipfile
from xml.etree import ElementTree

from sqlalchemy import delete, select

from .config import settings
from .db import SessionLocal
from .models import Document, DocumentPage, KnowledgeChunk


MAX_PDF_PAGES = 400
MAX_OCR_PAGES = 16
MAX_IMAGE_PIXELS = 24_000_000
MAX_TEXT_CHARS = 8_000_000
MAX_ARCHIVE_BYTES = 160 * 1024 * 1024
MAX_SHEET_CELLS = 150_000
CHUNK_SIZE = 1400
CHUNK_OVERLAP = 180

# These substitutions are used only to recognize model designations, never to
# alter Russian document text. One-to-one mappings preserve character offsets.
_CONFUSABLES = str.maketrans({
    "Р": "P", "р": "p", "Е": "E", "е": "e", "А": "A", "а": "a",
    "З": "Z", "з": "z",
})
_PE_MODEL = re.compile(
    r"(?<![A-ZА-Я0-9])PE\s*[-_.]?\s*(\d{1,5})"
    r"(?:(?:\s*[-_/.,]\s*|\s+)(\d{1,4}))?(?!\d)", re.I,
)
_AZ_MODEL = re.compile(r"(?<![A-ZА-Я0-9])AZ\s*[-_.]?\s*(\d{1,3})(?!\d)", re.I)
_BARE_VARIANT = re.compile(r"(?<![A-ZА-Я0-9])38(?:\s*[-_/.,]\s*|\s+)(\d{1,3})(?!\d)", re.I)


class IndexingError(ValueError):
    """An actionable, safe error suitable for the document status screen."""
    def __init__(self, message: str, code: str = 'indexing_failed'):
        self.code = code
        super().__init__(message)


def _model_matches(value: str):
    prepared = str(value or "").translate(_CONFUSABLES)
    found = []
    for match in _PE_MODEL.finditer(prepared):
        number, variant = match.group(1), match.group(2)
        # PE104.000.000 is an assembly drawing number, not a new PE104 model.
        model = "PE" + str(int(number))
        if variant and number != "104":
            model += "-" + str(int(variant))
        found.append((match.start(), match.end(), model))
    for match in _AZ_MODEL.finditer(prepared):
        found.append((match.start(), match.end(), "AZ" + match.group(1).zfill(2)))
    for match in _BARE_VARIANT.finditer(prepared):
        found.append((match.start(), match.end(), "PE38-" + match.group(1)))
    # A filename revision such as ...лотков...5310_3 does not make a PE5310-3
    # variant. Recognize the tray machine only in an equipment context.
    if re.search(r'лотк|формировател|модел', str(value or ''), re.I):
        for match in re.finditer(r'(?<![A-ZА-Я0-9])5310(?!\d)', prepared, re.I):
            if not any(start <= match.start() < end for start, end, _ in found):
                found.append((match.start(), match.end(), 'PE5310'))
    return sorted(found)


def detect_models(text: str) -> list[str]:
    """Recognize exact equipment variants, including Cyrillic PE lookalikes."""
    return list(dict.fromkeys(model for _, _, model in _model_matches(text)))


def normalize_model(value: str) -> str:
    models = detect_models(value)
    if models:
        return models[0]
    return re.sub(r"[\s_]+", "-", str(value or "").strip().upper())


def document_dict(document: Document) -> dict:
    return {
        "id": document.id,
        "title": document.title,
        "filename": document.filename,
        "category": document.category,
        "models": document.models or [],
        "visibility": document.visibility,
        "answer_access": getattr(document, 'answer_access', 'staff_only'),
        "status": document.status,
        "pages": document.pages,
        "chunks": document.chunks,
        "size_bytes": document.size_bytes,
        "mime_type": document.mime_type,
        "version": document.version,
        "error": document.error,
        "created_at": document.created_at.isoformat() + "Z" if document.created_at else None,
        "searchable": document.status == "indexed" and not document.deleted,
        "url": f"/api/documents/{document.id}/file",
        "indexing": document.indexing_details or {},
    }


def _clean_text(text: str) -> str:
    text = str(text or "").replace("\x00", "").replace("\r\n", "\n")
    text = re.sub(r"[^\S\n]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _checked_archive(path: Path) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(path)
    infos = archive.infolist()
    if len(infos) > 3000 or sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
        archive.close()
        raise IndexingError("Архив документа слишком велик после распаковки. Разделите файл.")
    if any(info.flag_bits & 0x1 for info in infos):
        archive.close()
        raise IndexingError("Зашифрованный архив документа не поддерживается.")
    return archive


def _ocr_languages() -> str:
    try:
        from .ocr import language_configuration, OCRSetupError
        import pytesseract
        binary, languages = language_configuration()
        pytesseract.pytesseract.tesseract_cmd = binary
        return languages
    except ImportError as exc:
        raise IndexingError('Не установлен pytesseract. Повторите install.bat.', 'python_dependency_missing') from exc
    except OCRSetupError as exc:
        raise IndexingError(str(exc), exc.code) from exc


def _ocr_image(image) -> str:
    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise IndexingError("Изображение слишком большое для OCR (максимум 24 мегапикселя).")
    try:
        languages = _ocr_languages()
        import pytesseract
        return pytesseract.image_to_string(image, lang=languages, timeout=20)
    except IndexingError:
        raise
    except RuntimeError as exc:
        if 'timeout' in str(exc).lower():
            raise IndexingError("OCR превысил лимит 20 секунд на страницу. Уменьшите размер скана.", 'ocr_timeout') from exc
        raise IndexingError('Tesseract не завершил распознавание страницы. Проверьте языки и повторите обработку.', 'ocr_failed') from exc
    except Exception as exc:
        raise IndexingError('Не удалось распознать страницу. Выполните python -m scripts.install_ocr --check и повторите обработку; текстовые страницы сохранены.', 'ocr_failed') from exc


def _pdf_pages(path: Path, progress=None, layouts=None) -> list[tuple[int, str]]:
    import pymupdf as fitz
    result = []
    ocr_pages = 0
    with fitz.open(path) as document:
        if not document.is_pdf:
            raise IndexingError("Содержимое файла не соответствует формату PDF.")
        if document.needs_pass:
            raise IndexingError("PDF защищён паролем. Загрузите незашифрованную копию.")
        if len(document) > MAX_PDF_PAGES:
            raise IndexingError(f"PDF содержит больше {MAX_PDF_PAGES} страниц. Разделите документ.")
        total_chars = 0
        for index, page in enumerate(document):
            if progress:
                progress('extracting', index, len(document))
            text = page.get_text("text", sort=True)
            layout = {'width': page.rect.width, 'height': page.rect.height, 'tables': [], 'images': [], 'blocks': []}
            for block in page.get_text('blocks')[:1000]:
                if len(block) >= 7:
                    layout['blocks'].append({'bbox': list(block[:4]), 'kind': 'text' if block[6] == 0 else 'image'})
            for image in page.get_image_info()[:150]:
                layout['images'].append({'bbox': list(image['bbox']), 'width': image.get('width'), 'height': image.get('height')})
            # Retain cells, coordinates and headers rather than trusting a
            # flattened column order in a technical specification table.
            try:
                tables = page.find_tables()
                for table in tables.tables[:30]:
                    rows = table.extract()
                    layout['tables'].append({'bbox': list(table.bbox), 'rows': rows[:500],
                                             'cells': [list(cell) if cell else None for cell in table.cells[:5000]],
                                             'headers': table.header.names})
            except Exception:
                layout['table_extraction'] = 'unavailable; use original page'
            if layouts is not None:
                layouts[index + 1] = layout
            if len(re.sub(r"\W", "", text)) < 25 and page.get_images():
                ocr_pages += 1
                try:
                    if ocr_pages > MAX_OCR_PAGES:
                        raise IndexingError(f'Превышен лимит OCR {MAX_OCR_PAGES} страниц за обработку. Разделите полный скан на части или загрузите PDF с текстовым слоем.', 'ocr_page_limit')
                    from PIL import Image
                    area = max(float(page.rect.width * page.rect.height), 1)
                    scale = min(2.0, math.sqrt(6_000_000 / area))
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
                    with Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples) as image:
                        if progress:
                            progress('ocr', index, len(document))
                        recognized = _ocr_image(image)
                    if recognized.strip():
                        text = recognized
                        layout['ocr_status'] = 'recognized'
                    else:
                        layout.update(ocr_status='no_text', ocr_error='OCR не обнаружил текст; проверьте изображение исходной страницы.', ocr_code='ocr_no_text')
                except IndexingError as exc:
                    # A missing Windows executable must not discard 100 good
                    # text-layer pages because one illustration needed OCR.
                    layout.update(ocr_status='unavailable', ocr_error=str(exc), ocr_code=exc.code)
                except Exception:
                    layout.update(ocr_status='unavailable',
                        ocr_error='Не удалось подготовить изображение страницы для OCR. Проверьте исходную страницу и повторите обработку.',
                        ocr_code='ocr_render_failed')
            else:
                layout['ocr_status'] = 'not_required'
            for table in layout['tables']:
                headers = table.get('headers') or []
                text += '\n\nТаблица (проверяйте модель по заголовку столбца и оригиналу страницы):\n'
                for row in table['rows']:
                    text += ' | '.join(f'{headers[i] or "Столбец " + str(i + 1)}: {cell or ""}' if i < len(headers) else str(cell or '') for i, cell in enumerate(row)) + '\n'
            text = _clean_text(text)
            total_chars += len(text)
            if total_chars > MAX_TEXT_CHARS:
                raise IndexingError("Извлечённый текст слишком большой. Разделите документ.")
            result.append((index + 1, text))
            if progress:
                progress('extracting', index + 1, len(document))
    return result


def _docx_pages(path: Path) -> list[tuple[int, str]]:
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with _checked_archive(path) as archive:
        try:
            content = archive.read("word/document.xml")
        except KeyError as exc:
            raise IndexingError("DOCX не содержит основной части word/document.xml.") from exc
        if len(content) > MAX_TEXT_CHARS * 4:
            raise IndexingError("XML документа слишком велик. Разделите документ.")
    root = ElementTree.fromstring(content)
    pages: list[list[str]] = [[]]
    for paragraph in root.iter(namespace + "p"):
        text = ""
        for element in paragraph.iter():
            if element.tag == namespace + "t":
                text += element.text or ""
            elif element.tag == namespace + "tab":
                text += " | "
            elif element.tag == namespace + "br":
                if element.get(namespace + "type") == "page":
                    pages[-1].append(text)
                    pages.append([])
                    text = ""
                else:
                    text += "\n"
            elif element.tag == namespace + "lastRenderedPageBreak":
                pages[-1].append(text)
                pages.append([])
                text = ""
        pages[-1].append(text)
        if len(pages) > MAX_PDF_PAGES:
            raise IndexingError("DOCX содержит слишком много разрывов страниц. Разделите файл.")
    return [(index + 1, _clean_text("\n".join(lines))) for index, lines in enumerate(pages)]


def _xlsx_pages(path: Path) -> list[tuple[int, str]]:
    from openpyxl import load_workbook
    with _checked_archive(path):
        pass
    workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    result = []
    cell_count = 0
    text_count = 0
    try:
        if len(workbook.worksheets) > MAX_PDF_PAGES:
            raise IndexingError("В XLSX слишком много листов.")
        for index, worksheet in enumerate(workbook.worksheets):
            lines = ["Лист: " + worksheet.title]
            for row in worksheet.iter_rows(values_only=True):
                cell_count += len(row)
                if cell_count > MAX_SHEET_CELLS:
                    raise IndexingError("В XLSX больше 150 000 ячеек. Разделите таблицу.")
                if not any(value is not None for value in row):
                    continue
                line = " | ".join("" if value is None else str(value) for value in row)
                text_count += len(line)
                if text_count > MAX_TEXT_CHARS:
                    raise IndexingError("Текст таблицы слишком велик. Разделите файл.")
                lines.append(line)
            result.append((index + 1, _clean_text("\n".join(lines))))
    finally:
        workbook.close()
    return result


def extract_pages(path: Path, filename: str | None = None, *, progress=None, layouts=None) -> list[tuple[int, str]]:
    """Read supported formats without executing macros, links or document code."""
    if not path.is_file():
        raise IndexingError("Исходный файл отсутствует в хранилище.")
    if path.stat().st_size > settings.max_file_bytes:
        raise IndexingError("Файл превышает ограничение 25 МБ.")
    suffix = Path(filename or path.name).suffix.lower()
    layouts = layouts if layouts is not None else {}
    try:
        if suffix == ".pdf":
            pages = _pdf_pages(path, progress, layouts)
        elif suffix == ".docx":
            pages = _docx_pages(path)
        elif suffix == ".xlsx":
            pages = _xlsx_pages(path)
        elif suffix in {".txt", ".md"}:
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp1251")
            pages = [(1, _clean_text(text))]
        elif suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            from PIL import Image
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if image.width * image.height > MAX_IMAGE_PIXELS:
                        raise IndexingError("Изображение превышает ограничение 24 мегапикселя.")
                    with image.convert("RGB") as rgb:
                        pages = [(1, _clean_text(_ocr_image(rgb)))]
        elif suffix in {".doc", ".xls"}:
            from .legacy_office import converted_document
            with converted_document(path, suffix) as converted:
                pages = _docx_pages(converted) if suffix == '.doc' else _xlsx_pages(converted)
        else:
            raise IndexingError("Неподдерживаемый формат документа.")
    except IndexingError:
        raise
    except ImportError as exc:
        raise IndexingError("Не установлен необходимый обработчик формата. Выполните установку зависимостей.") from exc
    except Exception as exc:
        raise IndexingError("Файл повреждён или не соответствует заявленному формату.") from exc
    if sum(len(text) for _, text in pages) > MAX_TEXT_CHARS:
        raise IndexingError("Извлечённый текст слишком большой. Разделите документ.")
    if (not any(text.strip() for _, text in pages) or
        (any(layout.get('ocr_error') for layout in layouts.values()) and
         not any(len(re.sub(r'\W', '', text)) >= 25 for _, text in pages))):
        failures = [layout.get('ocr_error') for layout in (layouts or {}).values() if layout.get('ocr_error')]
        raise IndexingError('В документе не найден текст. ' + (failures[0] if failures else 'Для сканов требуется OCR.'), 'no_extractable_text')
    return pages


def _chunks(text: str):
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n", start + CHUNK_SIZE // 2, end), text.rfind(". ", start + CHUNK_SIZE // 2, end))
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            yield chunk
        if end >= len(text):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)


def ingest_document(document_id: int) -> None:
    """Index in a background worker; persist useful failures in document status."""
    with SessionLocal() as session:
        document = session.get(Document, document_id)
        if document is None or document.deleted:
            return
        previous_pages = {page.page: (page.text, page.layout) for page in session.scalars(
            select(DocumentPage).where(DocumentPage.document_id == document_id)).all()}
        previous_index = bool(document.chunks and previous_pages)
        layouts = {}
        document.status = "processing"
        document.error = None
        document.indexing_details = {**(document.indexing_details or {}), 'stage': 'validating', 'completed': 0, 'total': None}
        session.commit()
        try:
            path = (settings.documents_dir / document.storage_name).resolve()
            if path.parent != settings.documents_dir.resolve():
                raise IndexingError("Недопустимое имя файла хранилища.")
            def progress(stage, completed, total):
                document.indexing_details = {**(document.indexing_details or {}), 'stage': stage, 'completed': completed, 'total': total}
                session.commit()
            pages = extract_pages(path, document.filename, progress=progress, layouts=layouts)
            progress('building_index', len(pages), len(pages))
            # Reindexing the same immutable original must not replace an earlier
            # useful OCR result with an empty page while Tesseract is unavailable.
            restored = []
            for page, text in pages:
                layout = layouts.get(page, {})
                old = previous_pages.get(page)
                if layout.get('ocr_error') and old and old[0].strip() and len(old[0]) > len(text):
                    text = old[0]
                    layout['previous_text_preserved'] = True
                    restored.append(page)
                pages[page - 1] = (page, text)
            declared = [normalize_model(model) for model in (document.models or []) if str(model).strip()]
            inferred = detect_models(document.title + " " + document.filename)
            # Filename/manual metadata identifies the machine. Incidental
            # references to other machines inside a manual must not broaden it.
            models = list(dict.fromkeys(declared or inferred or detect_models("\n".join(text for _, text in pages[:3]))))
            session.refresh(document)
            if document.deleted:
                return
            session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
            session.execute(delete(DocumentPage).where(DocumentPage.document_id == document.id))
            count = 0
            for page, text in pages:
                session.add(DocumentPage(document_id=document.id, page=page, text=text, layout=layouts.get(page, {})))
                for chunk in _chunks(text):
                    local_models = [m for m in detect_models(chunk) if m in models]
                    # On pages naming one variant, do not attach the other
                    # variant from a combined manual to that specific passage.
                    chunk_models = local_models or models
                    session.add(KnowledgeChunk(document_id=document.id, page=page, text=chunk, models=chunk_models))
                    count += 1
            document.models = models
            document.pages = len(pages)
            document.chunks = count
            document.status = "indexed"
            document.error = None
            warnings = [{'page': page, 'code': layout.get('ocr_code', 'ocr_failed'), 'message': layout['ocr_error'],
                         'previous_text_preserved': bool(layout.get('previous_text_preserved'))}
                        for page, layout in layouts.items() if layout.get('ocr_error')]
            document.indexing_details = {**(document.indexing_details or {}), 'stage': 'complete', 'completed': len(pages), 'total': len(pages),
                                         'tables': sum(len(layout.get('tables', [])) for layout in layouts.values()),
                                         'images': sum(len(layout.get('images', [])) for layout in layouts.values()),
                                         'warnings': warnings, 'extraction_complete': not warnings,
                                         'unread_pages': [warning['page'] for warning in warnings if not warning['previous_text_preserved']],
                                         'pages_with_text': sum(bool(text.strip()) and
                                            (not layouts.get(page, {}).get('ocr_error') or
                                             layouts.get(page, {}).get('previous_text_preserved') or
                                             len(re.sub(r'\W', '', text)) >= 25) for page, text in pages),
                                         'index_preserved': bool(restored), 'last_error': None}
            session.commit()
        except Exception as exc:
            session.rollback()
            document = session.get(Document, document_id)
            if document is not None and not document.deleted:
                message = str(exc)[:500] if isinstance(exc, IndexingError) else "Ошибка индексации документа. Проверьте формат и повторите обработку."
                warnings = [{'page': page, 'code': layout.get('ocr_code', 'ocr_failed'), 'message': layout['ocr_error']}
                            for page, layout in layouts.items() if layout.get('ocr_error')]
                document.status = 'indexed' if previous_index else 'error'
                document.indexing_details = {**(document.indexing_details or {}),
                    'failed_stage': (document.indexing_details or {}).get('stage'), 'stage': 'reindex_failed' if previous_index else 'error',
                    'warnings': warnings, 'extraction_complete': False,
                    'unread_pages': [item['page'] for item in warnings], 'total': len(layouts) or None,
                    'index_preserved': previous_index, 'last_error': message if previous_index else None}
                if not previous_index and layouts:
                    document.pages = len(layouts)
                document.error = None if previous_index else message
                session.commit()


_STOPWORDS = set("а без бы был была были было в вам вас ваш ваша ваши вы где да для до его ее если есть ещё еще же за и из или их к как ко когда кто ли мне мой мы на над нам нас не него нее нет но о об от по под при про с со так такой там то у уже что чтобы это этой этот я the a an and or of to in is for on".split())
_RUSSIAN_SUFFIX = re.compile(r"(иями|ями|ами|ого|ему|ому|ыми|ими|ение|ения|ений|ость|ости|ать|ять|ить|ться|ется|ются|ется|ов|ев|ий|ый|ой|ая|яя|ое|ее|ые|ие|ам|ям|ах|ях|ом|ем|ую|юю|ы|и|а|я|у|ю|е|о)$")


def _terms(text: str) -> list[str]:
    result = []
    for word in re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е")):
        if len(word) < 2 or word in _STOPWORDS:
            continue
        if re.fullmatch(r'рем(?:ень|ня|ню|нем|не|ни|ней|ням|нями|нях)', word):
            result.append('ремень')
            continue
        if re.search("[а-я]", word) and len(word) > 4:
            candidate = _RUSSIAN_SUFFIX.sub("", word)
            if len(candidate) >= 3:
                word = candidate
        result.append(word)
    return result


def search_knowledge(session, query: str, model: str | None = None,
                     document_id: int | None = None, include_internal: bool = False,
                     limit: int = 8) -> list[dict]:
    """Rank local passages, retaining exact model and visibility boundaries."""
    if not str(query or "").strip():
        return []
    limit = min(max(int(limit), 0), 30)
    if limit == 0:
        return []
    query = str(query)[:4000]
    required_models = [normalize_model(model)] if model else detect_models(query)
    # Strip model names by offsets from the original Russian query. Translating
    # the whole query would corrupt ordinary Russian words containing Р/Е/А.
    query_without_models = list(query)
    for start, end, _ in _model_matches(query):
        query_without_models[start:end] = " " * (end - start)
    terms = set(_terms("".join(query_without_models)))
    if not terms and not required_models:
        return []
    statement = select(KnowledgeChunk, Document).join(Document, KnowledgeChunk.document_id == Document.id).where(
        Document.deleted.is_(False), Document.status == "indexed",
    )
    if not include_internal:
        from .source_access import client_answer_predicate
        statement = statement.where(client_answer_predicate())
    if document_id is not None:
        statement = statement.where(Document.id == document_id)
    ranked = []
    for chunk, document in session.execute(statement).yield_per(200):
        document_models = {normalize_model(item) for item in (document.models or [])}
        # Assembly drawing numbers (e.g. PE3805.441.202) in a parts list are
        # not machine variants. Repair old indexes at read time as well.
        chunk_models = ({normalize_model(item) for item in (chunk.models or [])} & document_models) or document_models
        if required_models and not set(required_models).intersection(document_models):
            continue
        if required_models and chunk_models and not set(required_models).intersection(chunk_models):
            continue
        counts = Counter(_terms(chunk.text))
        title_terms = set(_terms(document.title))
        matched = terms.intersection(counts)
        # A matching machine designation alone is not evidence for a technical
        # question. At least one substantive query term must occur in a passage.
        if terms and not matched:
            continue
        # A coincidental page/drawing number is not relevance evidence for a
        # requested part marking. It must have a matching word as well.
        if terms and not any(not term.isdigit() for term in matched):
            continue
        if terms:
            score = sum(1 + math.log(1 + counts[term]) for term in matched)
            score += 1.5 * len(matched) / len(terms)
            score += 0.25 * len(terms.intersection(title_terms))
            score /= 1 + 0.08 * math.log(1 + len(counts))
        else:
            score = 0.5 + (0.2 if detect_models(chunk.text) else 0)
        ranked.append((score, chunk, document, sorted(chunk_models)))
    ranked.sort(key=lambda item: (-item[0], item[2].id, item[1].page, item[1].id))
    results = []
    seen_pages = set()
    for score, chunk, document, chunk_models in ranked:
        key = (document.id, chunk.page)
        if key in seen_pages:
            continue
        seen_pages.add(key)
        excerpt = chunk.text[:1200]
        results.append({
            "document_id": document.id, "title": document.title,
            "filename": document.filename, "page": chunk.page,
            "record_version": document.version, "source_fingerprint": document.sha256,
            "text": chunk.text, "excerpt": excerpt,
            "model": required_models[0] if len(required_models) == 1 else ", ".join(chunk_models),
            "models": chunk_models, "score": round(score, 4),
            "source_type": "document",
            "category": document.category,
            "url": f"/api/documents/{document.id}/file#page={chunk.page}",
        })
        from .source_access import source_access_metadata
        results[-1].update(source_access_metadata(document, include_internal))
        if len(results) >= limit:
            break
    return results
