# Обновление кода на сервере. Запускать из корня проекта: .\deploy\upload.ps1
# Пароль root спросит дважды (scp и ssh).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$server  = "root@109.71.247.247"
$archive = Join-Path $env:TEMP "pomogaika.tar.gz"

# .env, data/, dialogs/, .venv на сервер не едут: секреты, база и реальные переписки остаются локально
tar --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache `
    --exclude=data --exclude=dialogs --exclude=techlead-archive `
    --exclude=.env --exclude=*.zip --exclude=*.pdf --exclude=.git `
    -czf $archive src ops tests docs deploy tools requirements.txt pytest.ini README.md .env.example

Write-Host "Архив: $([math]::Round((Get-Item $archive).Length/1KB)) KB"

scp $archive "${server}:/tmp/pomogaika.tar.gz"
ssh $server @'
set -e
tar -xzf /tmp/pomogaika.tar.gz -C /opt/pomogaika
rm -f /tmp/pomogaika.tar.gz
# windows-tar распаковывается как rw-rw-rw- — чиним права каждый раз
cd /opt/pomogaika
find . -path ./.venv -prune -o -type d -print0 | xargs -0 chmod 755
find . -path ./.venv -prune -o -type f -print0 | xargs -0 chmod 644
chmod 600 .env
chmod 750 data
chown -R pomogaika:pomogaika /opt/pomogaika
systemctl restart pomogaika
sleep 8
systemctl is-active pomogaika
journalctl -u pomogaika -n 8 --no-pager
'@

Remove-Item $archive
Write-Host "Готово: https://pomogaika.vddapp.ru"
