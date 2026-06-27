# scripts/build-cn.ps1 — 一键用国内镜像构建 ZLAgent Docker 镜像并启动。
#
# 解决的问题：默认的 docker compose build 在国内会卡在三处外网拉取上：
#   1. apt-get update / install (deb.debian.org, security.debian.org)
#   2. pip install (pypi.org)
#   3. 运行时 npx -y mcporter ... (registry.npmjs.org)
#
# 这个脚本把 APT_MIRROR_URL / PIP_INDEX_URL / NPM_CONFIG_REGISTRY 都设成
# 清华源（默认）然后调用 docker compose build + up -d。Dockerfile 和
# docker-compose.yml 已经在 v0.35 接好这三个变量。
#
# 用法：
#   PowerShell 里在仓库根目录执行：
#     .\scripts\build-cn.ps1                 # 用清华源 build + up
#     .\scripts\build-cn.ps1 -Mirror aliyun  # 用 Aliyun
#     .\scripts\build-cn.ps1 -Mirror ustc    # 用 USTC
#     .\scripts\build-cn.ps1 -BuildOnly      # 只 build，不 up
#     .\scripts\build-cn.ps1 -NoCache        # 强制重 build（rebuild from scratch）

[CmdletBinding()]
param(
    [ValidateSet("tsinghua", "aliyun", "ustc", "tencent")]
    [string]$Mirror = "tsinghua",
    [switch]$BuildOnly,
    [switch]$NoCache
)

$ErrorActionPreference = "Stop"

# 镜像源映射表。每行 4 列：apt / pip / npm / 显示名。
$mirrors = @{
    "tsinghua" = @{
        apt  = "https://mirrors.tuna.tsinghua.edu.cn"
        pip  = "https://pypi.tuna.tsinghua.edu.cn/simple"
        npm  = "https://registry.npmmirror.com/"
        name = "Tsinghua TUNA"
    }
    "aliyun" = @{
        apt  = "https://mirrors.aliyun.com"
        pip  = "https://mirrors.aliyun.com/pypi/simple/"
        npm  = "https://registry.npmmirror.com/"  # Aliyun 没自家 npm，复用 npmmirror
        name = "Aliyun"
    }
    "ustc" = @{
        apt  = "https://mirrors.ustc.edu.cn"
        pip  = "https://pypi.mirrors.ustc.edu.cn/simple"
        npm  = "https://registry.npmmirror.com/"
        name = "USTC"
    }
    "tencent" = @{
        apt  = "https://mirrors.cloud.tencent.com"
        pip  = "https://mirrors.cloud.tencent.com/pypi/simple"
        npm  = "https://mirrors.cloud.tencent.com/npm/"
        name = "Tencent Cloud"
    }
}

$m = $mirrors[$Mirror]

Write-Host ""
Write-Host "===== ZLAgent build-cn.ps1 ====="
Write-Host "  mirror set : $($m.name)"
Write-Host "  apt        : $($m.apt)"
Write-Host "  pip        : $($m.pip)"
Write-Host "  npm        : $($m.npm)"
Write-Host ""

# 把 mirror env 注入当前 PowerShell 进程,docker compose 会从这里读取。
# 注意:这只影响当前脚本进程，不污染你的 shell 全局环境。
$env:APT_MIRROR_URL     = $m.apt
$env:PIP_INDEX_URL      = $m.pip
$env:NPM_CONFIG_REGISTRY = $m.npm

# Dockerfile 里 RUN 命令对 PIP_INDEX_URL 是 [ -n "$X" ] 判断，空串 = 不用。
# 上面三行确保了非空。

$composeArgs = @("compose", "build")
if ($NoCache) {
    $composeArgs += "--no-cache"
}

Write-Host "==> docker $($composeArgs -join ' ')"
& docker @composeArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "docker compose build failed (exit=$LASTEXITCODE). 上面的日志看一下哪个 mirror 不通。"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "[OK] build 完成。"

if ($BuildOnly) {
    Write-Host "  -BuildOnly 已指定，不调用 docker compose up。"
    Write-Host "  下一步手动起服务：docker compose up -d"
    exit 0
}

Write-Host ""
Write-Host "==> docker compose up -d"
& docker compose up -d
if ($LASTEXITCODE -ne 0) {
    Write-Error "docker compose up 失败 (exit=$LASTEXITCODE)。看 docker compose logs 排查。"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "[OK] ZLAgent 已启动。"
Write-Host "  健康检查 : http://localhost:8020/api/health"
Write-Host "  实时日志 : docker compose logs -f zlagent"
Write-Host "  停止     : docker compose down"
