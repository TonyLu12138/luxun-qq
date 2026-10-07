# ============================================================================
#  打开 NapCat 的调试日志，用于定位「收到消息但不上报」的原因
#
#  背景：NapCat 源码里有这个判断（packages/napcat-onebot/index.ts:316）
#        if (this.bootTime > parseInt(m.msgTime)) {
#            logDebug(`消息时间${m.msgTime}早于启动时间${this.bootTime}，忽略上报`);
#            continue;   // 消息被丢弃，不上报
#        }
#    这条 logDebug 只有打开 fileLog 才能看到。
#
#  本脚本把两处 debug 打开：
#    1. napcat_1234567890.json -> fileLog = true（写文件日志）
#    2. onebot11 的 httpClient -> debug = true（打印上报细节）
#
#  运行：powershell -NoProfile -ExecutionPolicy Bypass -File enable_napcat_debug.ps1
# ============================================================================

$ErrorActionPreference = 'Stop'
$CfgDir = 'C:\Users\<你的用户名>\qq930\NapCat\config'

if (-not (Test-Path $CfgDir)) { throw "配置目录不存在: $CfgDir" }

# ---- 1) 打开 napcat 文件日志 ----
$acct = Join-Path $CfgDir 'napcat_1234567890.json'
if (Test-Path $acct) {
    $j = Get-Content $acct -Raw | ConvertFrom-Json
    $j.fileLog = $true
    $j.fileLogLevel = 'debug'
    $j.consoleLogLevel = 'debug'
    $j | ConvertTo-Json -Depth 10 | Set-Content $acct -Encoding UTF8
    Write-Host "[OK] 已打开文件日志: $acct" -ForegroundColor Green
} else {
    Write-Host "[跳过] 不存在: $acct" -ForegroundColor Yellow
}

# ---- 2) 打开 onebot11 httpClient 的 debug ----
foreach ($f in @('onebot11_1234567890.json', 'onebot11.json')) {
    $p = Join-Path $CfgDir $f
    if (-not (Test-Path $p)) { continue }
    $j = Get-Content $p -Raw | ConvertFrom-Json
    $changed = $false
    if ($j.network.httpClients) {
        foreach ($c in $j.network.httpClients) { $c.debug = $true; $changed = $true }
    }
    if ($j.httpPost) { $j.httpPost.debug = $true; $changed = $true }
    if ($changed) {
        $j | ConvertTo-Json -Depth 10 | Set-Content $p -Encoding UTF8
        Write-Host "[OK] 已打开上报调试: $p" -ForegroundColor Green
    }
}

Write-Host ''
Write-Host '接下来：' -ForegroundColor Yellow
Write-Host '  1. 完全关闭 NapCat 窗口（连着 QQ 一起）'
Write-Host '  2. 重新启动：'
Write-Host '       cd C:\Users\<你的用户名>\qq930\NapCat'
Write-Host "       `$env:LUXUN_QQ_PATH = 'C:\Users\<你的用户名>\qq930\QQ.exe'"
Write-Host '       .\launcher-napcat.bat'
Write-Host '  3. 扫码登录后，用 1987654321 发一条消息'
Write-Host '  4. 在 NapCat 目录里找日志文件（napcat/logs 或根目录 *.log）'
Write-Host '     把含「忽略上报」「消息时间」「emit」的行发给我'
Write-Host ''
Write-Host '重点看这句是否出现：' -ForegroundColor Cyan
Write-Host '  消息时间<数字>早于启动时间<数字>，忽略上报'
Write-Host '如果出现，就确认是 NapCat 的时间判断把消息丢了。'
