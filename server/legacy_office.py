"""Optional, bounded legacy Office conversion. Originals are never modified."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def office_binary():
    configured = os.getenv('LIBREOFFICE_PATH', '').strip()
    candidates = [configured] if configured else ['libreoffice', 'soffice']
    if os.name == 'nt' and not configured:
        candidates += [str(Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'LibreOffice/program/soffice.exe')]
    for candidate in candidates:
        if candidate and (resolved := shutil.which(candidate)):
            return resolved
    return None


@contextmanager
def converted_document(path: Path, suffix: str):
    from .knowledge import IndexingError
    binary = office_binary()
    if not binary:
        raise IndexingError('DOC/XLS требуют установленного LibreOffice. Укажите LIBREOFFICE_PATH или сохраните файл как DOCX/XLSX/PDF. Оригинал сохранён.')
    with tempfile.TemporaryDirectory(prefix='posteks-office-') as temporary:
        directory = Path(temporary)
        profile = directory / 'profile'
        (profile / 'user').mkdir(parents=True)
        # An isolated profile with macros disabled and link updates forbidden.
        (profile / 'user' / 'registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
</oor:items>''', encoding='utf-8')
        extension = 'docx' if suffix == '.doc' else 'xlsx'
        # Use a fixed, fresh source name so file names cannot become flags.
        source = directory / ('source' + suffix)
        shutil.copyfile(path, source)
        try:
            result = subprocess.run([binary, '-env:UserInstallation=' + profile.as_uri(), '--headless',
                '--nologo', '--nodefault', '--norestore', '--convert-to', extension,
                '--outdir', str(directory), str(source)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IndexingError('LibreOffice не завершил преобразование за 45 секунд. Сохраните файл как DOCX/XLSX/PDF.') from exc
        output = directory / ('source.' + extension)
        if result.returncode or not output.is_file() or output.stat().st_size > 25 * 1024 * 1024:
            raise IndexingError('LibreOffice не преобразовал документ. Проверьте файл и повторите загрузку.')
        yield output
