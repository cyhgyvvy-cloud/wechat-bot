param(
    [string]$Target = 'C:\微信机器人',
    [string]$Source = ''
)

$ErrorActionPreference = 'Stop'
if (-not $Source) { $Source = Join-Path $PSScriptRoot '微信机器人' }

function Say([string]$msg, [string]$color = 'White') {
    Write-Host $msg -ForegroundColor $color
}

Say ''
Say '=== 只更新程序（不重装，约 10MB）===' 'Cyan'
Say "源目录: $Source"
Say "目标目录: $Target"
Say ''

if (-not (Test-Path -LiteralPath $Target)) {
    Say "找不到目标目录：$Target" 'Red'
    Say '如果机器人装在别处，请用记事本改本文件第一行的 $Target。' 'Yellow'
    exit 1
}

$files = @('微信机器人.exe', '机器人控制台.exe')
foreach ($n in $files) {
    $s = Join-Path $Source $n
    if (-not (Test-Path -LiteralPath $s)) {
        Say "缺少源文件：$s" 'Red'
        Say '请确认共享夹里的「微信机器人」文件夹完整。' 'Yellow'
        exit 1
    }
}

Say '1/3  停止正在运行的机器人 / 控制台（不关掉会复制失败）...' 'Yellow'
foreach ($procName in @('微信机器人', '机器人控制台')) {
    $ps = Get-Process -Name $procName -ErrorAction SilentlyContinue
    if ($ps) {
        $ps | Stop-Process -Force -ErrorAction SilentlyContinue
        Say "      已关闭 $(@($ps).Count) 个 $procName 进程" 'DarkGray'
    }
}
Start-Sleep -Milliseconds 1500

Say '2/3  复制两个 exe（_internal / _console 内容没变，不用动）...' 'Yellow'
foreach ($n in $files) {
    $s = Join-Path $Source $n
    $d = Join-Path $Target $n
    Copy-Item -LiteralPath $s -Destination $d -Force
    $mb = [math]::Round((Get-Item -LiteralPath $d).Length / 1MB, 1)
    Say "      已更新 $n  ($mb MB)" 'Green'
}

Say '3/3  把触发方式切成「概率回复」...' 'Yellow'
$cfg = Join-Path $Target 'config.json'
if (Test-Path -LiteralPath $cfg) {
    $bytes = [IO.File]::ReadAllBytes($cfg)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    $enc = New-Object System.Text.UTF8Encoding($hasBom)
    $text = [IO.File]::ReadAllText($cfg, $enc)
    $pattern = '("trigger"\s*:\s*)"[^"]*"'
    if ([regex]::IsMatch($text, $pattern)) {
        $old = [regex]::Match($text, '"trigger"\s*:\s*"([^"]*)"').Groups[1].Value
        # 备份是尽力而为：用户机器上可能存在只读的旧备份，不能让它连累主写入
        try {
            [IO.File]::WriteAllText("$cfg.bak_trigger", $text, $enc)
        } catch {
            Say '      （旧备份写不进去，已跳过，不影响主配置）' 'DarkGray'
        }
        $new = [regex]::Replace($text, $pattern, '${1}"chance"')
        [IO.File]::WriteAllText($cfg, $new, $enc)
        Say "      trigger: $old -> chance（原配置备份为 config.json.bak_trigger）" 'Green'
    } else {
        Say '      config.json 里没找到 trigger，跳过；可在控制台「配置」页改。' 'Yellow'
    }
} else {
    Say '      目标目录里没有 config.json（像是首次安装），只复制了 exe。' 'Yellow'
    Say '      请先跑一次完整「安装.bat」，再执行本脚本。' 'Yellow'
}

Say ''
Say '完成。接下来：' 'Cyan'
Say '  1) 打开「机器人控制台」，点「启动」或「重启」'
Say '  2) 「配置 → 回复方式」可调概率（默认 35%）和必回关键词，保存即生效'
Say '  3) 群里 @机器人 或提到它的名字必定回复'
Say ''
exit 0
