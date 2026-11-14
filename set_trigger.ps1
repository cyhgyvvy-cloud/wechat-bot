param(
    [ValidateSet('chance', 'mention', 'all')]
    [string]$Mode = 'mention',
    [string]$ConfigPath = 'C:\微信机器人\config.json'
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $ConfigPath)) {
    Write-Host "找不到配置文件：$ConfigPath" -ForegroundColor Red
    Write-Host '如果机器人装在别处，请改本脚本的 $ConfigPath。' -ForegroundColor Yellow
    exit 1
}

$bytes = [IO.File]::ReadAllBytes($ConfigPath)
$hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
$enc = New-Object System.Text.UTF8Encoding($hasBom)
$text = [IO.File]::ReadAllText($ConfigPath, $enc)

$pattern = '("trigger"\s*:\s*)"[^"]*"'
if (-not [regex]::IsMatch($text, $pattern)) {
    Write-Host 'config.json 里没有 trigger 字段，请检查文件内容。' -ForegroundColor Red
    exit 1
}

$new = [regex]::Replace($text, $pattern, ('${1}"' + $Mode + '"'))
[IO.File]::WriteAllText("$ConfigPath.bak_trigger", $text, $enc)
[IO.File]::WriteAllText($ConfigPath, $new, $enc)

Write-Host ''
if ($Mode -eq 'chance') {
    Write-Host '已改成：@ 或提到自己必回，其余按概率回（默认 35%）' -ForegroundColor Green
    Write-Host '到控制台「配置 → 回复方式」可调概率和必回关键词，改完立即生效。' -ForegroundColor Yellow
} elseif ($Mode -eq 'mention') {
    Write-Host '已改成：群里 @机器人 才回复（私聊照常回复）' -ForegroundColor Green
    Write-Host ''
    Write-Host '注意：@ 出来的名字必须和 config.json 的 bot_name 一致。' -ForegroundColor Yellow
    Write-Host '      当前 bot_name = 大肥鱼，请用微信的 @成员列表选人，别手写。' -ForegroundColor Yellow
} else {
    Write-Host '已改成：群里每句话都回复' -ForegroundColor Green
}
Write-Host ''
Write-Host '最后一步：在控制台点「重启」，配置才会生效。' -ForegroundColor Cyan
