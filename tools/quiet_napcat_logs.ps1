# ============================================================================
#  降低 NapCat 的日志啰嗦程度
#
#  背景：之前为了排查"收到消息但不上报"，把日志级别调成了 debug，
#        结果每条消息都会打印整个 RawMessage 和 OB11Message（几百行 JSON）。
#
#  本脚本把它调回 info：
#      * 保留  接收 <- 私聊 / 发送 -> 私聊 / WebSocket服务已启动 这些有用的行
#      * 去掉  [debug] 收到新消息 RawMessage {...}   这一整块
#      * 去掉  [debug] 转化为 OB11Message {...}      这一整块
#      * 关闭文件日志（之前排查时打开的，会在 logs\ 下不断堆积）
#
#  运行：powershell -NoProfile -ExecutionPolicy Bypass -File quiet_napcat_logs.ps1
# ============================================================================

$ErrorActionPreference = 'Stop'

# 两个可能的 NapCat 安装位置，谁存在就改谁
$Candidates = @(
    'C:\Users\<你的用户名>\qq930\NapCat\config',
    'C:\Users\<你的用户名>\qq932-example\NapCat\config'
)

function Set-LogLevel {
    param([string]$Path, [string]$Level)

    $changed = @()

    # 账号级配置（NapCat 实际读取的就是这个）
    $acct = Join-Path $Path 'napcat_1234567890.json'
    if (Test-Path $acct) {
        $j = Get-Content $acct -Raw -Encoding UTF8 | ConvertFrom-Json
        $j.consoleLogLevel = $Level
        $j.fileLogLevel = $Level
        $j.fileLog = $false          # 不再往 logs\ 写文件
        $j.consoleLog = $true
        ($j | ConvertTo-Json -Depth 10) |
            Set-Content $acct -Encoding UTF8
        $changed += $acct
    }

    # 全局配置（有些版本会读它）
    $g = Join-Path $Path 'napcat.json'
    if (Test-Path $g) {
        $j2 = Get-Content $g -Raw -Encoding UTF8 | ConvertFrom-Json
        $j2.consoleLogLevel = $Level
        $j2.fileLogLevel = $Level
        $j2.fileLog = $false
        $j2.consoleLog = $true
        ($j2 | ConvertTo-Json -Depth 10) |
            Set-Content $g -Encoding UTF8
        $changed += $g
    }

    return $changed
}

$done = @()
foreach ($dir in $Candidates) {
    if (Test-Path $dir) {
        foreach ($f in (Set-LogLevel -Path $dir -Level 'info')) {
            Write-Host "[OK] 已改为 info: $f" -ForegroundColor Green
            $done += $f
        }
    }
}

if ($done.Count -eq 0) {
    Write-Host "[ERROR] 没找到 NapCat 配置目录，请检查路径：" -ForegroundColor Red
    $Candidates | ForEach-Object { Write-Host "        $_" }
    exit 1
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ' 改完了。重启 NapCat 生效：' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host '   cd C:\Users\<你的用户名>\qq930\NapCat'
Write-Host "   `$env:LUXUN_QQ_PATH = 'C:\Users\<你的用户名>\qq930\QQ.exe'"
Write-Host '   .\launcher-napcat.bat'
Write-Host ''
Write-Host ' 之后终端只会显示这类简洁的行：'
Write-Host '   余烬 | 接收 <- 私聊 (1987654321) 在吗' -ForegroundColor Gray
Write-Host '   余烬 | 发送 -> 私聊 (1234567890) 【待你选择】...' -ForegroundColor Gray
Write-Host '   余烬 | [Notice] [输入状态] 1987654321 对方正在输入...' -ForegroundColor Gray
Write-Host ''
Write-Host ' 不再出现几百行的 RawMessage / OB11Message JSON 转储。' -ForegroundColor Gray
Write-Host ''
Write-Host ' 想更安静（连"输入状态"都不显示）：把 info 改成 warn' -ForegroundColor DarkGray
