"""Indexable public support information; no company files or personal data."""
from html import escape
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from .config import settings

router = APIRouter()

PAGES = {
    'service': {
        'title': 'Сервис оборудования ПОСТЭКС — техническая поддержка',
        'description': 'Сервисный помощник ПОСТЭКС: поиск по опубликованным инструкциям, подготовка обращения и помощь в уточнении модели упаковочного оборудования.',
        'eyebrow': 'ПОДДЕРЖКА ВАШЕГО ОБОРУДОВАНИЯ',
        'heading': 'Сервис начинается с понятного вопроса.',
        'lead': 'ПОСТЭКС AI помогает найти нужную информацию по формирователям и заклейщикам гофрокоробов и подготовить данные для сервисной команды.',
        'sections': [
            ('Найдите нужный раздел инструкции', 'Укажите точную модель оборудования и опишите ситуацию. Помощник ищет в документах, разрешённых компанией для публичного использования. Рядом с результатами доступны источник и номер страницы.'),
            ('Подготовьте обращение специалисту', 'Сообщите модель, серийный номер, текст ошибки и наблюдаемые симптомы. Не проводите регулировку или ремонт без проверки руководства и требований безопасности.'),
            ('Проверьте технические сведения', 'PE38-12, PE38-17, PE38-25 и семейство AZ01 отличаются. Характеристики и совместимость деталей требуют подтверждения по подходящей документации. При недостатке данных помощник предлагает уточнение.'),
        ],
    },
    'support': {
        'title': 'Как обратиться в техническую поддержку ПОСТЭКС',
        'description': 'Как описать неисправность оборудования ПОСТЭКС, найти модель и серийный номер и подготовить информацию для сервисного обращения.',
        'eyebrow': 'ПОМЕНЬШЕ НЕОПРЕДЕЛЁННОСТИ',
        'heading': 'Подготовим обращение вместе.',
        'lead': 'Хорошее описание помогает быстрее понять, какая инструкция подходит и какие сведения потребуются сервисному специалисту.',
        'sections': [
            ('1. Уточните оборудование', 'Найдите модель и заводской номер на шильде оборудования. Если обозначение нечёткое или неизвестно, сообщите об этом. Не заменяйте его приблизительно похожей моделью.'),
            ('2. Опишите наблюдения', 'Когда появилась неисправность? Какие действия ей предшествовали? Что указано на панели? Приведите точный текст ошибки и отметьте, остановлено ли оборудование.'),
            ('3. Укажите требуемую деталь', 'Для запасных частей нужны сведения о машине, маркировка, назначение и необходимые размеры. Если ранее поставленная деталь не подошла, важно описать требуемые параметры, а не только размеры полученной детали.'),
            ('4. Соблюдайте безопасность', 'Не снимайте защиту, не обходите блокировки и не выполняйте работы под напряжением. Если для проверки требуется доступ в опасную зону, остановите оборудование и обратитесь к квалифицированному специалисту.'),
        ],
    },
}


def render_page(slug: str) -> str:
    item = PAGES[slug]
    canonical = f'<link rel="canonical" href="{escape(settings.public_base_url + "/" + slug, quote=True)}"/>' if settings.public_base_url.startswith('https://') else ''
    sections = ''.join(f'<section><span class="number">0{index}</span><div><h2>{escape(title)}</h2><p>{escape(body)}</p></div></section>' for index, (title, body) in enumerate(item['sections'], 1))
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(item['title'])}</title><meta name="description" content="{escape(item['description'], quote=True)}"><meta name="theme-color" content="#ed681b"><meta property="og:title" content="{escape(item['title'], quote=True)}"><meta property="og:description" content="{escape(item['description'], quote=True)}"><meta property="og:type" content="website">{canonical}<link rel="icon" href="/icon.svg"><style>
*{{box-sizing:border-box}}body{{margin:0;background:#fcfbf9;color:#242424;font:16px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}a{{color:inherit}}header,main,footer{{max-width:1000px;margin:auto;padding:28px 32px}}header{{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #e9e7e3}}header img{{width:170px;height:auto}}nav{{display:flex;gap:24px;font-size:14px}}nav a{{text-decoration:none;color:#64625e}}.eyebrow{{font-size:13px;letter-spacing:.04em;font-weight:600;color:#a34711}}h1{{font-size:clamp(34px,5.5vw,60px);line-height:1.14;letter-spacing:-.04em;font-weight:550;max-width:750px;margin:24px 0}}.lead{{max-width:650px;color:#77736c;font-size:18px;margin-bottom:32px}}.cta{{display:inline-flex;align-items:center;gap:24px;background:#ed681b;color:white;padding:12px 22px;text-decoration:none;border-radius:11px;font-size:14px}}.hero{{padding:60px 0}}section{{display:flex;gap:30px;padding:32px 0;border-top:1px solid #e6e3dd}}.number{{color:#d66a29;font-size:13px;padding-top:5px}}h2{{font-size:22px;line-height:1.35;font-weight:550;margin:0 0 12px}}section p{{max-width:780px;margin:0;color:#76716b}}footer{{border-top:1px solid #e6e3dd;color:#68615a;font-size:13px;display:flex;justify-content:space-between;gap:30px}}footer a{{text-decoration:none}}@media(max-width:600px){{header,main,footer{{padding:22px}}header img{{width:125px}}nav{{gap:14px;font-size:12px}}.hero{{padding:38px 0}}section{{gap:20px}}footer{{display:block}}}}
</style></head><body><header><a href="/" aria-label="Сервисный помощник ПОСТЭКС"><img src="/brand/posteks-logo.png" alt="ПОСТЭКС"></a><nav aria-label="Информация о сервисе"><a href="/service">О сервисе</a><a href="/support">Как обратиться</a></nav></header><main><div class="hero"><span class="eyebrow">{escape(item['eyebrow'])}</span><h1>{escape(item['heading'])}</h1><p class="lead">{escape(item['lead'])}</p><a class="cta" href="/">Открыть помощника <span aria-hidden="true">↗</span></a></div>{sections}</main><footer><span>ПОСТЭКС</span><span>Помощник может ошибаться. Проверяйте рекомендации по руководству.</span><a href="/">Вернуться в чат</a></footer></body></html>'''


@router.get('/service', response_class=HTMLResponse)
def service():
    return render_page('service')


@router.get('/support', response_class=HTMLResponse)
def support():
    return render_page('support')
