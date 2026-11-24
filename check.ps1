param([switch]$Offline, [switch]$NoOpen, [switch]$Live)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'
if ($Offline -and $Live) {
    Write-Host 'Выберите один режим (-Offline или -Live).' -ForegroundColor Red
    exit 1
}
$Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $Python)) {
    Write-Host 'Python-среда проекта не найдена. Сначала запустите install.bat в этой папке.' -ForegroundColor Red
    exit 1
}
$Reports = Join-Path $PSScriptRoot 'reports'
$Latest = Join-Path $Reports 'LATEST_REPORT.txt'
$Previous = ''
if (Test-Path -LiteralPath $Latest) { $Previous = ([string](Get-Content -LiteralPath $Latest -Raw -Encoding UTF8)).Trim() }
$CheckArgs = @('-m', 'scripts.selfcheck')
if ($Live) {
    Write-Host 'До 20 запросов к OpenAI на тестовых данных. Используется ваш локальный ключ и расходуется квота.'
    $CheckArgs += @('--live', '--synthetic-only')
} else {
    Write-Host 'Проверка выполняется локально. Для проверки с запросами к OpenAI запустите .\check.bat -Live.'
}
Write-Host 'Проверка может занять несколько минут. Окно start.bat можно оставить открытым.'
& $Python @CheckArgs | ForEach-Object {
    if ($_ -notmatch '^(Отчёт|Для поддержки): ') { $_ }
}
$CheckExit = $LASTEXITCODE
$Current = ''
if (Test-Path -LiteralPath $Latest) { $Current = ([string](Get-Content -LiteralPath $Latest -Raw -Encoding UTF8)).Trim() }
$ReportDirectory = if ($Current) { Split-Path -Parent $Current } else { '' }
if ($Current -and $Current -ne $Previous -and (Split-Path -Leaf $Current) -eq 'REPORT.html' -and
    (Split-Path -Parent $ReportDirectory) -eq $Reports -and (Test-Path -LiteralPath $Current -PathType Leaf) -and
    (Test-Path -LiteralPath (Join-Path $ReportDirectory 'support_report.json') -PathType Leaf)) {
    $RelativeDirectory = Join-Path 'reports' (Split-Path -Leaf $ReportDirectory)
    Write-Host ('Новый отчёт: ' + (Join-Path $RelativeDirectory 'REPORT.html'))
    Write-Host ('Для разбора отправьте только ' + (Join-Path $RelativeDirectory 'support_report.json'))
    if (!$NoOpen) {
        try { Invoke-Item -LiteralPath $Current }
        catch { Write-Warning 'Откройте REPORT.html из указанной выше папки вручную.' }
    }
} else {
    Write-Host 'Новый отчёт не создан. Скопируйте только текст ошибки из этого окна, без ключей и паролей.' -ForegroundColor Red
    if ($CheckExit -eq 0) { $CheckExit = 1 }
}
exit $CheckExit
