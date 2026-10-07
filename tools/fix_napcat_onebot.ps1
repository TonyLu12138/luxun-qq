# ============================================================================
#  修复 NapCat 的 OneBot11 事件上报配置
#
#  问题：
#      NapCat 实际读取的是账号专属配置
#          C:\Users\<你的用户名>\qq932-example\NapCat\config\onebot11_1234567890.json
#      而该文件里 httpServers / httpClients 都是空数组，
#      所以消息事件没有被推送到 luxun-qq 服务（127.0.0.1:8848）。
#
#  本脚本：
#      1. 备份原配置
#      2. 写入正确的 HTTP 服务器(3000) + HTTP 事件上报(8848) 配置
#
#  运行（普通 PowerShell 即可）：
#      powershell -NoProfile -ExecutionPolicy Bypass -File fix_napcat_onebot.ps1
# ============================================================================

$ErrorActionPreference = 'Stop'

$CfgDir = 'C:\Users\<你的用户名>\qq932-example\NapCat\config'
$Target = Join-Path $CfgDir 'onebot11_1234567890.json'
$Backup = Join-Path $CfgDir ('onebot11_1234567890.json.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))

if (-not (Test-Path $CfgDir)) { throw "配置目录不存在: $CfgDir" }

# 备份
if (Test-Path $Target) {
    Copy-Item $Target $Backup -Force
    Write-Host "已备份原配置 -> $Backup" -ForegroundColor Green
}

# 正确的账号级 OneBot11 配置
$json = @'
{
  "network": {
    "httpServers": [
      {
        "name": "luxun-qq-http",
        "enable": true,
        "host": "127.0.0.1",
        "port": 3000,
        "enableCors": true,
        "enableWebsocket": false,
        "messagePostFormat": "array",
        "token": "",
        "debug": false,
        "reportSelfMessage": false
      }
    ],
    "httpSseServers": [],
    "httpClients": [
      {
        "name": "luxun-qq-report",
        "enable": true,
        "url": "http://127.0.0.1:8848/",
        "messagePostFormat": "array",
        "reportSelfMessage": true,
        "token": "",
        "debug": false
      }
    ],
    "websocketServers": [],
    "websocketClients": [],
    "plugins": []
  },
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": false,
  "imageDownloadProxy": "",
  "timeout": {
    "baseTimeout": 10000,
    "uploadSpeedKBps": 256,
    "downloadSpeedKBps": 256,
    "maxTimeout": 1800000
  }
}
'@

[System.IO.File]::WriteAllText($Target, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "已写入 $Target" -ForegroundColor Green
Write-Host ''
Write-Host '配置内容：' -ForegroundColor Cyan
Write-Host '  HTTP 服务器   : 127.0.0.1:3000  （供 luxun-qq 调用发消息）'
Write-Host '  HTTP 事件上报 : http://127.0.0.1:8848/  （把消息推给 luxun-qq）'
Write-Host '  reportSelfMessage: true  （这样你给 1234567890 的审批指令也会上报）'
Write-Host ''
Write-Host '接下来：' -ForegroundColor Yellow
Write-Host '  1. 重启 NapCat（关闭窗口，重新运行 launcher-napcat.bat）'
Write-Host '  2. 重启 luxun-qq 服务'
Write-Host '  3. 用 1987654321 发一条消息测试'
Write-Host ''
Write-Host '如果 NapCat 的 WebUI 里能看到「网络配置」，
也可以直接在 WebUI 里核对这两个条目，效果相同。' -ForegroundColor Gray
