# ============================================================================
#  让 NapCat 开启 WebSocket 服务器（替代 HTTP 事件上报）
#
#  为什么换 WebSocket：
#      HTTP 事件上报在你的环境里始终只推空 {} 心跳、不推消息事件，
#      而事件在 NapCat 内部明明已经正确构造出来了（日志有「转化为 OB11Message」）。
#      改用 WebSocket 后，收事件与发消息共用同一条连接，
#      由我们的客户端主动连接 NapCat，绕开 httpClient 上报这一层。
#
#  本脚本做两件事：
#      1. 在 onebot11_1234567890.json 里启用 websocketServers（端口 3001）
#      2. 打印后续步骤
#
#  运行：powershell -NoProfile -ExecutionPolicy Bypass -File enable_napcat_ws.ps1
# ============================================================================

$ErrorActionPreference = 'Stop'

$CfgDir = 'C:\Users\<你的用户名>\qq930\NapCat\config'
$Target = Join-Path $CfgDir 'onebot11_1234567890.json'

if (-not (Test-Path $Target)) { throw "找不到配置: $Target" }

# 备份
$backup = "$Target.bak-ws-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
Copy-Item $Target $backup -Force
Write-Host "[OK] 已备份 -> $backup" -ForegroundColor Green

$j = Get-Content $Target -Raw | ConvertFrom-Json

# 启用 WebSocket 服务端
$wsServer = [PSCustomObject]@{
    name              = 'luxun-qq-ws'
    enable            = $true
    host              = '127.0.0.1'
    port              = 3001
    messagePostFormat = 'array'
    reportSelfMessage = $true      # 这样你给 1234567890 发的 1/2/3 也会推过来
    token             = ''
    enableForcePushEvent = $true
    debug             = $false
    heartInterval     = 30000
}
$j.network.websocketServers = @($wsServer)

# 关掉 HTTP 上报（它一直不工作，留着只会干扰）
foreach ($c in $j.network.httpClients) { $c.enable = $false }

# HTTP 服务端保留（发送消息可以用，但我们现在走 WS，留着也无害）
$j | ConvertTo-Json -Depth 12 | Set-Content $Target -Encoding UTF8
Write-Host "[OK] 已启用 WebSocket 服务器: 127.0.0.1:3001" -ForegroundColor Green
Write-Host "[OK] 已关闭 HTTP 事件上报（它一直只推空心跳）" -ForegroundColor Green

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ' 接下来：' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host ' 1) 重启 NapCat（关窗口重开）：'
Write-Host '      cd C:\Users\<你的用户名>\qq930\NapCat' -ForegroundColor White
Write-Host "      `$env:LUXUN_QQ_PATH = 'C:\Users\<你的用户名>\qq930\QQ.exe'" -ForegroundColor White
Write-Host '      .\launcher-napcat.bat' -ForegroundColor White
Write-Host '    日志里应出现：WebSocket服务: 127.0.0.1:3001, : 已启动'
Write-Host ''
Write-Host ' 2) 扫码登录后，启动我们的 WS 客户端（新终端）：'
Write-Host '      cd E:\code\luxun-qq' -ForegroundColor White
Write-Host '      $env:PYTHONPATH="src"' -ForegroundColor White
Write-Host '      python -m luxun_qq serve-ws --ws-url ws://127.0.0.1:3001' -ForegroundColor White
Write-Host ''
Write-Host ' 3) 用 1987654321 发一条消息 —— 这次应该能收到事件了。'
