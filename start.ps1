$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (!(Test-Path '.venv\Scripts\python.exe') -or !(Test-Path 'frontend\dist\index.html')) {
    throw 'Сначала выполните powershell -ExecutionPolicy Bypass -File .\install.ps1'
}
& .\.venv\Scripts\python.exe -m scripts.manage init-db
if ($LASTEXITCODE -ne 0) { throw 'Ошибка миграций' }
& .\.venv\Scripts\python.exe -m scripts.manage has-admin
if ($LASTEXITCODE -eq 1) {
    & .\.venv\Scripts\python.exe -m scripts.manage create-admin
    if ($LASTEXITCODE -ne 0) { throw 'Администратор не создан' }
} elseif ($LASTEXITCODE -ne 0) { throw 'Не удалось проверить наличие администратора' }
Write-Host 'Клиентский сайт: http://127.0.0.1:8080'
Write-Host 'Админка: http://127.0.0.1:8080/admin/knowledge'
Write-Host 'Остановка: Ctrl+C. Сеть Интернет не открывается автоматически.'
& .\.venv\Scripts\python.exe -m uvicorn server.main:app --host 127.0.0.1 --port 8080
exit $LASTEXITCODE
