"""Smoke-check the isolated HTML preview; no private data or real integrations."""
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
import json,re,os,shutil

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

@contextmanager
def preview_server(directory):
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(directory)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    root=Path(__file__).resolve().parent.parent
    out=root/'artifacts'/'preview-review';out.mkdir(parents=True,exist_ok=True)
    errors=[];network=[]
    with preview_server(root/'downloads') as base:
        with sync_playwright() as p:
         executable=os.environ.get('CHROMIUM_PATH') or shutil.which('chromium')
         launch={'headless':True,'args':['--no-sandbox']}
         if executable: launch['executable_path']=executable
         browser=p.chromium.launch(**launch)
         page=browser.new_page(viewport={'width':1440,'height':1000},locale='ru-RU')
         page.on('pageerror',lambda error:errors.append(str(error)))
         page.on('request',lambda request:network.append(request.url) if request.url.startswith(('http:','https:')) and not request.url.startswith(base + '/') else None)
         page.on('dialog',lambda dialog:dialog.accept())
         page.goto(base + '/posteks-preview.html')
         expect(page.get_by_text('ТЕСТОВЫЙ ПРОСМОТР',exact=True)).to_be_visible()
         expect(page.get_by_role('heading',name='Сервис рядом. Решение — ближе.')).to_be_visible()
         page.screenshot(path=str(out/'chat.png'))
         page.locator('.preview-banner').get_by_role('link',name='Панель',exact=True).click()
         expect(page.get_by_role('heading',name='Входящие',exact=True)).to_be_visible()
         page.locator('.ws-email-row').first.click()
         page.get_by_label('Текст ответа',exact=True).fill('TEST: проверка формы черновика')
         page.get_by_role('button',name='На согласование',exact=True).click()
         expect(page.get_by_text('Черновик сохранён и передан на согласование. Письмо не отправлено.',exact=True)).to_be_visible()
         page.screenshot(path=str(out/'inbox.png'))
         page.locator('.preview-banner').get_by_role('link',name='Знания',exact=True).click()
         expect(page.get_by_role('heading',name=re.compile('^База знаний'))).to_be_visible()
         expect(page.locator('.document-row')).to_have_count(3)
         page.get_by_label('Тестовый вопрос по базе знаний').fill('PE38-12 тестовый вопрос')
         page.get_by_role('button',name='Задать тестовый вопрос').click()
         expect(page.locator('.source-card').first).to_be_visible()
         page.screenshot(path=str(out/'knowledge.png'))
         page.locator('.source-card').first.click()
         expect(page.locator('.source-text')).to_contain_text('ТЕСТОВЫЙ ДОКУМЕНТ')
         page.get_by_role('button',name='Закрыть источник',exact=True).click()
         page.locator('.preview-banner').get_by_role('link',name='Формы',exact=True).click()
         page.get_by_role('button',name='Новое обращение',exact=True).click()
         page.get_by_label('Тема обращения').fill('TEST: обращение из автономного просмотра')
         page.get_by_label('Клиент / организация').fill('TEST')
         page.get_by_label('Описание',exact=True).fill('TEST: проверка сохранения формы в памяти')
         page.get_by_role('button',name='Создать обращение',exact=True).click()
         expect(page.get_by_text('Обращение создано',exact=True)).to_be_visible()
         page.get_by_label('Внутренняя заметка').fill('TEST: заметка')
         page.get_by_role('button',name='Добавить заметку').click()
         expect(page.get_by_text('TEST: заметка',exact=True)).to_be_visible()
         page.get_by_label('Новое поручение').fill('TEST: поручение')
         page.get_by_label('Отдел-исполнитель',exact=True).fill('TEST')
         page.get_by_role('button',name='Создать поручение',exact=True).click()
         expect(page.get_by_text('TEST: поручение',exact=True)).to_be_visible()
         page.screenshot(path=str(out/'case.png'))
         page.get_by_role('button',name='Согласования',exact=True).click()
         page.locator('.ws-select-row').first.click()
         page.get_by_role('button',name='Согласовать',exact=True).click()
         expect(page.get_by_text('Черновик согласован. Отправка не выполнялась.',exact=True)).to_be_visible()
         page.get_by_role('button',name='Настройки',exact=True).click()
         page.get_by_role('button',name='Тёмная',exact=True).click()
         assert page.locator('html').get_attribute('data-theme')=='dark'
         page.set_viewport_size({'width':390,'height':844})
         page.locator('.preview-banner').get_by_role('link',name='Знания',exact=True).click()
         expect(page.locator('.document-row')).to_have_count(3)
         assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
         page.screenshot(path=str(out/'mobile.png'))
         assert not errors,errors
         assert not network,network
         browser.close()
    report={'status':'passed','checks':['single HTML opens over local HTTP','same React menus navigate by hash','draft form stores locally','knowledge sources open','case, note and task created','approval updates','theme switches','390px without overflow','zero external network requests'],'javascript_errors':errors,'external_requests':network}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))

if __name__ == '__main__':
    main()
