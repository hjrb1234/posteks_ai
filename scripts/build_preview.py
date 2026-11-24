"""Build a single self-contained HTML design preview using the actual React views."""
from pathlib import Path
import base64
import os
import subprocess

ROOT = Path(__file__).resolve().parent.parent


def main():
    npm = 'npm.cmd' if os.name == 'nt' else 'npm'
    subprocess.run([npm, 'exec', 'vite', 'build', '--', '--config', 'vite.preview.config.ts'], cwd=ROOT / 'frontend', check=True)
    dist = ROOT / 'frontend' / 'preview-dist'
    javascript = (dist / 'preview.js').read_text(encoding='utf-8')
    css = '\n'.join(p.read_text(encoding='utf-8') for p in dist.glob('*.css'))
    for asset, mime in [('brand/posteks-logo.png', 'image/png'), ('icon.svg', 'image/svg+xml')]:
        raw = (ROOT / 'frontend' / 'public' / asset).read_bytes()
        encoded = 'data:' + mime + ';base64,' + base64.b64encode(raw).decode()
        javascript = javascript.replace('"/' + asset + '"', '"' + encoded + '"')
    javascript = javascript.replace('</script', '<\\/script')
    css += '''
html[data-preview=true] .app-shell{height:calc(100dvh - 44px);min-height:0}
html[data-preview=true] .sidebar,html[data-preview=true] .main-shell{height:calc(100dvh - 44px)}
.preview-banner{height:44px;display:flex;gap:16px;align-items:center;justify-content:space-between;padding:0 20px;background:#fff0df;color:#7c3b12;font:12px/1.3 system-ui;border-bottom:1px solid #f7d5af}
.preview-banner strong{letter-spacing:.04em}.preview-banner nav{display:flex;gap:14px}.preview-banner a{color:inherit;text-decoration:underline;text-underline-offset:3px;white-space:nowrap}
@media(max-width:760px){.preview-banner{height:72px;padding:8px 12px;align-items:flex-start;flex-direction:column;gap:5px}.preview-banner .explanation{display:none}html[data-preview=true] .app-shell,html[data-preview=true] .main-shell,html[data-preview=true] .sidebar{height:calc(100dvh - 72px)}html[data-preview=true] .sidebar{top:72px}html[data-preview=true] .mobile-menu{top:91px}html[data-preview=true] .sidebar-backdrop{top:72px}}
'''
    html = '''<!doctype html><html lang="ru" data-preview="true"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>ПОСТЭКС AI · автономный TEST-просмотр</title><style>''' + css + '''</style></head><body><div class="preview-banner"><div><strong>ТЕСТОВЫЙ ПРОСМОТР</strong><span class="explanation"> · Все записи вымышлены. Изменения только в памяти браузера. Python-сервер и AI не подключены.</span></div><nav><a href="#/">Чат</a><a href="#/admin/inbox">Панель</a><a href="#/admin/knowledge">Знания</a><a href="#/admin/cases">Формы</a><a href="https://github.com/hjrb1234/posteks_mvp/raw/refs/heads/codex/posteks-ai/downloads/posteks_ai_windows.zip" target="_blank" rel="noopener">Скачать ZIP</a></nav></div><div id="root"></div><script>''' + javascript + '''</script></body></html>'''
    target = ROOT / 'downloads' / 'posteks-preview.html'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding='utf-8')
    hosted = ROOT / 'docs' / 'index.html'
    hosted.write_text(html, encoding='utf-8')
    (ROOT / 'docs' / '.nojekyll').write_text('')
    print('Self-contained TEST preview:', target.name, target.stat().st_size, 'bytes; no private input files used')

if __name__ == '__main__':
    main()
