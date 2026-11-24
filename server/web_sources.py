"""Find public manufacturer documentation without sending private chat context to web search."""
from dataclasses import dataclass
from html.parser import HTMLParser
import re
from time import monotonic
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .config import settings
from .knowledge import detect_models


_DOMAIN = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}\Z')
_COMPONENT = re.compile(r'VEICHI (SD[0-9]{2,4}[A-Z]{0,3})\Z', re.I)
_ALARM = re.compile(r'(?:ERR?|AL)[ ._-]?(\d{2,5})\Z', re.I)
_TOPICS = (
    (r'энкодер|encoder', 'encoder alarm troubleshooting', r'энкодер|encoder'),
    (r'останавлива|остановк|stop', 'stop and alarm diagnostics', r'останавлива|остановк|fault|stop|alarm'),
    (r'настрой|регулиров|размер|setting', 'setup operating instructions', r'настрой|регулиров|размер|setting|setup'),
    (r'запчаст|спецификац|детал|parts', 'parts manual', r'запчаст|спецификац|детал|parts'),
    (r'руководств|инструкц|manual', 'operation manual', r'руководств|инструкц|manual'),
)
MAX_SEARCH_OUTPUT = 900
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024


def official_domains() -> tuple[str, ...]:
    return tuple(dict.fromkeys(host for entry in settings.web_search_domains.split(',')
        if (host := entry.strip().lower()) and _DOMAIN.fullmatch(host)))[:20]


def official_url(value, domains: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or re.search(r'[\\\s\x00-\x1f]', value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.netloc.lower() != host or not domains
                or not any(host == domain or host.endswith('.' + domain) for domain in domains)):
            return None
    except ValueError:
        return None
    return urlunsplit(('https', host, parsed.path or '/', parsed.query, ''))


@dataclass(frozen=True)
class ManualQuery:
    model: str
    topic: str
    keywords: str
    alarm: str = ''

    @property
    def search(self) -> str:
        return ' '.join(value for value in (self.model, f'Er.{self.alarm}' if self.alarm else '', self.topic) if value)

    def model_matches(self, value: str) -> bool:
        parts = re.findall(r'[A-Z0-9]+', self.model.split()[-1].upper())
        pattern = r'[ \t_-]*'.join(re.escape(part) for part in parts)
        return bool(re.search(r'(?<![A-Z0-9])' + pattern + r'(?![A-Z0-9])', value.upper()))

    def anchor(self, value: str):
        if self.alarm:
            return re.search(r'(?<!\w)(?:ERR?|AL)[ ._:-]*' + re.escape(self.alarm) + r'(?!\w|[.,]\d)', value, re.I)
        return re.search(self.keywords, value, re.I)

    def covered_by(self, sources: list[dict]) -> bool:
        for source in sources:
            if source.get('source_type') == 'equipment_registry':
                continue
            text = str(source.get('text') or '')[:2200]
            model_fields = ' '.join(str(source.get(k) or '') for k in ('model', 'title', 'filename'))
            if self.model_matches(model_fields + ' ' + text) and self.anchor(text):
                return True
        return False


def manual_query(context: dict) -> ManualQuery | None:
    turn = context.get('current_turn', {})
    if not turn.get('web_lookup_allowed'):
        return None
    state = context.get('state', {})
    facts = state.get('known_facts', {})
    component = str(turn.get('component_model') or facts.get('component_model') or '')
    component_match = _COMPONENT.fullmatch(component)
    models = detect_models(context['message']) or detect_models(str(facts.get('equipment_model') or ''))
    model = 'VEICHI ' + component_match[1].upper() if component_match else models[0] if len(models) == 1 else ''
    if not model:
        return None
    alarm_code = str(turn.get('alarm_code') or facts.get('alarm_code') or '')
    alarm_match = _ALARM.fullmatch(alarm_code)
    if turn.get('intermittent_stop'):
        # A new stop does not inherit a different incident's code.
        if alarm_code.casefold() not in context['message'].casefold():
            alarm_match = None
        topic, keywords = 'stop and alarm diagnostics', r'останавлива|остановк|fault|stop|alarm'
    else:
        subject = context['message'] + ' ' + str(state.get('goal') or '')[:600]
        for pattern, topic, keywords in _TOPICS:
            if re.search(pattern, subject, re.I):
                break
        else:
            if not alarm_match and not component_match:
                return None
            topic, keywords = 'alarm and troubleshooting manual', r'ошибк|alarm|fault|encoder'
    query = ManualQuery(model, topic, keywords, alarm_match[1] if alarm_match else '')
    return None if query.covered_by(context.get('sources', [])) else query


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def search_urls(provider, model: str, query: ManualQuery, domains: tuple[str, ...]):
    response = provider.responses.create(model=model, input=query.search,
        instructions=('Find the original public manufacturer manual or technical support page for this exact model. '
                      'Prefer official PDFs or manufacturer pages containing the specified alarm/topic. Return the links.'),
        tools=[{'type': 'web_search', 'filters': {'allowed_domains': list(domains)}, 'search_context_size': 'low'}],
        include=['web_search_call.action.sources'],
        tool_choice='required', max_tool_calls=2, max_output_tokens=MAX_SEARCH_OUTPUT, store=False, timeout=22)
    citations, hits = [], []
    for item in _field(response, 'output', []) or []:
        if _field(item, 'type') == 'web_search_call' and _field(item, 'status') == 'completed':
            action = _field(item, 'action')
            hits.extend(_field(row, 'url') for row in _field(action, 'sources', []) or [])
            hits.append(_field(action, 'url'))
        for block in _field(item, 'content', []) or []:
            citations.extend(_field(a, 'url') for a in _field(block, 'annotations', []) or [] if _field(a, 'type') == 'url_citation')
    urls = list(dict.fromkeys(url for raw in citations + hits if (url := official_url(raw, domains))))[:4]
    return urls, _field(response, 'usage')


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript'}:
            self.hidden.append(tag)

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def _get_document(client, url: str, domains: tuple[str, ...], deadline: float):
    for _ in range(3):
        if monotonic() > deadline:
            return None
        with client.stream('GET', url) as response:
            if response.is_redirect:
                url = official_url(urljoin(url, response.headers.get('location', '')), domains)
                if url is None:
                    return None
                continue
            response.raise_for_status()
            mime = response.headers.get('content-type', '').split(';')[0].strip().lower()
            if mime not in {'application/pdf', 'application/octet-stream', 'text/html', 'text/plain'}:
                return None
            limit = MAX_DOCUMENT_BYTES if mime.startswith('application/') else 2 * 1024 * 1024
            buffer = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                if monotonic() > deadline or len(buffer) + len(chunk) > limit:
                    return None
                buffer.extend(chunk)
            if buffer.startswith(b'%PDF-'):
                return url, bytes(buffer)
            if mime in {'text/html', 'text/plain'}:
                text = buffer.decode(response.encoding or 'utf-8', errors='replace')
                if mime == 'text/html':
                    parser = _PageText()
                    parser.feed(text)
                    text = '\n'.join(parser.parts)
                return url, text
            return None
    return None


def _sources_from_document(url: str, body: bytes | str, query: ManualQuery, deadline: float) -> list[dict]:
    def match_pages(pages):
        confirmed_model = query.model_matches(urlsplit(url).path)
        candidates = []
        for page, text in pages:
            if monotonic() > deadline:
                break
            confirmed_model = confirmed_model or query.model_matches(text)
            anchor = query.anchor(text)
            if anchor is not None and len(candidates) < 2:
                begin = max(0, anchor.start() - 350)
                candidates.append((page, text[begin:begin + 2000].strip()))
        if not confirmed_model:
            return []
        host = urlsplit(url).hostname
        return [dict(source_type='official_web', title=f'{query.model} · {host}', url=url,
                     text=text, model=query.model, **({'page': page} if page else {}))
                for page, text in candidates]

    if isinstance(body, str):
        return match_pages([(None, body)])
    import fitz
    with fitz.open(stream=body, filetype='pdf') as document:
        return match_pages((index + 1, document[index].get_text())
                           for index in range(min(document.page_count, 320)))


def read_official_sources(urls, query: ManualQuery, domains: tuple[str, ...]) -> list[dict]:
    deadline = monotonic() + 14
    sources: list[dict] = []
    with httpx.Client(timeout=5, follow_redirects=False, headers={'User-Agent': 'POSTEKS-ManualLookup/1.0'}) as client:
        for url in urls:
            try:
                document = _get_document(client, url, domains, deadline)
                if document:
                    sources.extend(_sources_from_document(*document, query, deadline))
            except (httpx.HTTPError, RuntimeError, ValueError, LookupError, OverflowError):
                continue
            if len(sources) >= 2 or monotonic() > deadline:
                break
    return sources[:2]


def public_web_source(source: dict) -> dict | None:
    url = official_url(source.get('url'), official_domains())
    if source.get('source_type') != 'official_web' or url is None:
        return None
    return {**source, 'url': url}
