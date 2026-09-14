# tools/dispatch_travel_refresh.ps1
# ILANG
# TYPE: script | ROLE: reliable-trigger | PROJECT: branddeals-promo-radar
# ::STATE{@TRIGGER, target:travel-refresh workflow, host:Tencent CVM, why:GitHub 自带 cron 不可靠}
# ::RULE{这台云主机只做"按时触发", 抓取/构建/部署仍交给 GitHub runner}
# ::BOUNDARY{never:把 GitHub token 写进仓库任何文件|scope:file}
#
# 为什么需要（2026-09-14 实测）:
#   GitHub 自带 cron 在这个仓库不可靠 —— 仓库建后 18.6 小时里, 6 小时一次的 cron 只跑了 1 次
#   (迟到 2.6h), 每日 slot 过了 3h 还没跑。所以用这台常开的云主机做可靠触发器。
#
# 设计要点（踩过的坑）:
#   - 计划任务以 SYSTEM 身份跑, $MyInvocation.MyCommand.Path 会是空 -> 一切路径写死, 不推算
#   - token 从本机 ~/.git-credentials 读, 绝不进仓库
#   - 日志写死到 logs\dispatch-<日期>.log（带日期, 每次运行追加一段）
#
# ::RULE{运行记录⇒写进固定路径的带日期文件 全成功 有失败 某家 0 条 三种状态都写}
# 本脚本只能记"派发"这一步的成败; 抓取/构建/部署的成败由 CI 写进 logs\travel-<日期>.log。

$date = Get-Date -Format 'yyyy-MM-dd'
$log = 'C:\Users\Administrator\WorkBuddy AI\Claw\branddeals-promo-radar\logs\dispatch-' + $date + '.log'

function L($m) {
    $line = (Get-Date -Format 's') + '  ' + $m
    try { Add-Content -Path $log -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue } catch { }
    Write-Output $line
}

L '--- dispatch start ---'
try {
    $credFile = 'C:\Users\Administrator\.git-credentials'
    if (-not (Test-Path $credFile)) { throw "credential file not found: $credFile" }
    $cred = Get-Content $credFile | Select-Object -First 1
    if ($cred -match '://[^:]+:([^@]+)@') { $token = $matches[1] }
    else { throw 'cannot parse token from .git-credentials' }

    $headers = @{
        Authorization = 'token ' + $token
        Accept        = 'application/vnd.github+json'
        'User-Agent'  = 'cvm-scheduler'
    }
    $resp = Invoke-WebRequest `
        -Uri 'https://api.github.com/repos/ZiXu000/branddeals-promo-radar/actions/workflows/travel-refresh.yml/dispatches' `
        -Method Post -Headers $headers -Body '{"ref":"main"}' `
        -ContentType 'application/json' -UseBasicParsing -TimeoutSec 60
    L ('dispatch OK  HTTP ' + $resp.StatusCode + '  (204 = triggered)')
    exit 0
} catch {
    L ('dispatch FAILED: ' + $_.Exception.Message)
    exit 1
}
