# ============================================================================
#  NapCat 工作区外部署脚本（请在普通 PowerShell 里运行，不要用管理员）
#
#  为什么必须离开 E:\code：
#      E:\code 是 DSH 会话工作区，被注入了
#          Everyone : Deny  DeleteSubdirectoriesAndFiles
#      这条拒绝规则会阻止程序删除/替换自己目录里的临时文件。
#      QQ 与 NapCat 启动时都要做这件事，被拒绝后初始化失败、静默退出。
#      （实测三个 QQ 版本在工作区内都失败，搬到 C 盘后 QQ 双击即可用。）
#
#  本脚本做四件事：
#      1. 复制 NapCat 到 C:\Users\<你的用户名>\qq932-example\NapCat
#      2. 生成启动脚本 launcher-napcat.bat（QQ 路径写死为 C 盘的 9.9.32）
#      3. 预写 config\webui.json 与 config\onebot11.json（省掉手工配 WebUI）
#      4. 打印后续两步
#
#  运行方式（复制本文件内容，或直接执行）：
#      powershell -NoProfile -ExecutionPolicy Bypass -File deploy_napcat.ps1
# ============================================================================

$ErrorActionPreference = 'Stop'

$Src = 'E:\code\luxun-qq\_runtime\napcat\napcat'
$Dst = 'C:\Users\<你的用户名>\qq932-example\NapCat'
$Qq  = 'C:\Users\<你的用户名>\qq932-example\QQ.exe'

Write-Host '=== NapCat 工作区外部署 ===' -ForegroundColor Cyan

if (-not (Test-Path $Src)) { throw "源目录不存在: $Src" }
if (-not (Test-Path $Qq))  { throw "找不到 QQ: $Qq" }

# 检查残留进程
$p = Get-Process -Name 'NapCatWinBootMain','QQ' -ErrorAction SilentlyContinue
if ($p) {
    Write-Host '检测到残留进程，正在停止…' -ForegroundColor Yellow
    $p | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# 1) 复制
if (Test-Path $Dst) {
    Write-Host "目标已存在，先清空: $Dst" -ForegroundColor Yellow
    Remove-Item $Dst -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "复制 NapCat …"
Copy-Item -Path $Src -Destination $Dst -Recurse -Force
$n = (Get-ChildItem $Dst -Recurse -File -ErrorAction SilentlyContinue).Count
Write-Host "  完成，共 $n 个文件" -ForegroundColor Green

# 2) 生成启动脚本（纯 ASCII + CRLF）
$bat = @"
@echo off
chcp 65001 >nul
setlocal
REM NapCat launcher (kept outside the DSH workspace on purpose).
REM Workspace E:\code has a sandbox Deny-Delete ACL that blocks
REM QQ/NapCat from replacing their own temp files at startup.
REM QQ is pinned to the 9.9.32 build (NapCat supports up to 9.9.32).

set "QQPath=$Qq"
if defined LUXUN_QQ_PATH set "QQPath=%LUXUN_QQ_PATH%"

if not exist "%QQPath%" (
    echo [ERROR] QQ not found: %QQPath%
    pause
    exit /b 1
)

set "NAPCAT_PATCH_PACKAGE=%cd%\qqnt.json"
set "NAPCAT_LOAD_PATH=%cd%\loadNapCat.js"
set "NAPCAT_INJECT_PATH=%cd%\NapCatWinBootHook.dll"
set "NAPCAT_LAUNCHER_PATH=%cd%\NapCatWinBootMain.exe"
set "NAPCAT_MAIN_PATH=%cd%\napcat.mjs"

if not exist "%NAPCAT_INJECT_PATH%" goto :missing
if not exist "%NAPCAT_LAUNCHER_PATH%" goto :missing
if not exist "%NAPCAT_MAIN_PATH%" goto :missing
if not exist "%NAPCAT_PATCH_PACKAGE%" goto :missing

set "NAPCAT_MAIN_PATH=%NAPCAT_MAIN_PATH:\=/%"
> "%NAPCAT_LOAD_PATH%" echo (async () =^> {await import("file:///%NAPCAT_MAIN_PATH%")})()

echo ============================================================
echo  QQ     : %QQPath%
echo  NapCat : %cd%
echo ============================================================
echo  Scan the QR code with the phone QQ app to log in.
echo  WebUI  : http://127.0.0.1:6099  (token: luxun-qq-local)
echo ============================================================
echo.

"%NAPCAT_LAUNCHER_PATH%" "%QQPath%" "%NAPCAT_INJECT_PATH%" %*

echo.
echo NapCat exited with code %ERRORLEVEL%
pause
exit /b %ERRORLEVEL%

:missing
echo [ERROR] Missing NapCat files in: %cd%
pause
exit /b 1
"@
$batPath = Join-Path $Dst 'launcher-napcat.bat'
[System.IO.File]::WriteAllText($batPath, ($bat -replace "`r`n","`n" -replace "`n","`r`n"), [System.Text.Encoding]::ASCII)
Write-Host "  已生成 $batPath" -ForegroundColor Green

# 3) 预写配置
$cfgDir = Join-Path $Dst 'config'
New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null

$webui = @'
{
  "host": "0.0.0.0",
  "port": 6099,
  "prefix": "",
  "token": "luxun-qq-local",
  "loginRate": 3,
  "autoLoginAccount": ""
}
'@
[System.IO.File]::WriteAllText((Join-Path $cfgDir 'webui.json'), $webui, (New-Object System.Text.UTF8Encoding($false)))

$onebot = @'
{
  "http": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 3000,
    "enableCors": true,
    "enableWebsocket": false,
    "messagePostFormat": "array",
    "token": "",
    "debug": false,
    "reportSelfMessage": false
  },
  "httpSse": { "enabled": false },
  "httpPost": {
    "enabled": true,
    "urls": ["http://127.0.0.1:8848/"],
    "messagePostFormat": "array",
    "token": "",
    "reportSelfMessage": false
  },
  "ws": { "enabled": false },
  "wsReverse": { "enabled": false },
  "plugin": { "enabled": true }
}
'@
[System.IO.File]::WriteAllText((Join-Path $cfgDir 'onebot11.json'), $onebot, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "  已预写 config\webui.json 和 config\onebot11.json" -ForegroundColor Green

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ' 部署完成。接下来两步：' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host ' 1) 启动 NapCat（会打印二维码，用手机 QQ 扫码登录 1234567890）：'
Write-Host "      cd $Dst" -ForegroundColor White
Write-Host '      .\launcher-napcat.bat' -ForegroundColor White
Write-Host ''
Write-Host ' 2) 登录后，新开一个终端启动回复服务：'
Write-Host '      cd E:\code\luxun-qq' -ForegroundColor White
Write-Host '      $env:PYTHONPATH="src"' -ForegroundColor White
Write-Host '      python -m luxun_qq serve' -ForegroundColor White
Write-Host ''
Write-Host ' 配置已预置，正常无需再进 WebUI 手动修改。' -ForegroundColor Green
