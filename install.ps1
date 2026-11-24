param([switch]$RebuildFrontend)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function Assert-Exit([string]$Stage) {
    if ($LASTEXITCODE -ne 0) { throw "Ошибка этапа: $Stage (код $LASTEXITCODE)" }
}
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
    Assert-Exit 'Проверка Python'
    if (!(Test-Path '.venv\Scripts\python.exe')) { & py -3 -m venv .venv; Assert-Exit 'Создание venv' }
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
    Assert-Exit 'Проверка Python'
    if (!(Test-Path '.venv\Scripts\python.exe')) { & python -m venv .venv; Assert-Exit 'Создание venv' }
} else { throw 'Установите Python 3.11 или новее с python.org (Add Python to PATH).' }
& .\.venv\Scripts\python.exe -m scripts.check_server_stopped
Assert-Exit 'Остановка сервера перед изменением зависимостей'
$ShouldBuildFrontend = $RebuildFrontend -or (Test-Path '.git') -or !(Test-Path 'frontend\dist\index.html')
if ($ShouldBuildFrontend) {
    if (!(Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'Для исходников установите Node.js 22 LTS с nodejs.org. Готовый Windows ZIP содержит собранный интерфейс.' }
    & node -e "if(Number(process.versions.node.split('.')[0])<22)process.exit(1)"
    Assert-Exit 'Node.js 22+'
}
& .\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
Assert-Exit 'Python-зависимости'
if (!(Test-Path '.env')) { Copy-Item '.env.example' '.env' }
# Языки готовятся даже если Tesseract установлен вне PATH. Поиск исполняемого
# файла также учитывает TESSERACT_CMD и Program Files/Tesseract-OCR.
& .\.venv\Scripts\python.exe -m scripts.install_ocr
if ($LASTEXITCODE -ne 0) { Write-Warning 'Языковые данные OCR пока недоступны. Установка приложения продолжается; текстовые страницы PDF сохраняются. Повторите python -m scripts.install_ocr после проверки сети.' }
& .\.venv\Scripts\python.exe -m scripts.install_ocr --check
if ($LASTEXITCODE -ne 0) { Write-Warning 'OCR требует настройки. Прочитайте диагностику выше, установите Tesseract 5 или укажите TESSERACT_CMD в .env. Затем повторите обработку документов в базе знаний.' }
& .\.venv\Scripts\python.exe -m scripts.manage prepare-update
Assert-Exit 'Резервная копия перед обновлением (сервис должен быть остановлен)'
& .\.venv\Scripts\python.exe -m scripts.manage init-db
Assert-Exit 'Миграции новой базы'
& .\.venv\Scripts\python.exe -m scripts.manage import-sources
Assert-Exit 'Импорт документов из настроенного storage/imports'
if ($ShouldBuildFrontend) {
    Push-Location frontend
    try {
        & npm.cmd ci
        Assert-Exit 'Frontend-зависимости'
        & npm.cmd run build
        Assert-Exit 'Сборка frontend'
    } finally { Pop-Location }
} else { Write-Host 'Используется готовая проверенная сборка интерфейса. Для пересборки: install.ps1 -RebuildFrontend.' }
Write-Host 'Установка завершена. Запустите start.bat. При первом старте задайте пароль администратора.'
