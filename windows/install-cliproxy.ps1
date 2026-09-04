# install-cliproxy.ps1 — 装 cliproxy（Windows）：下载 → 校验 → 解压 → 生成配置。
# 常驻注册在 register-tasks.ps1（第 6 个任务），装完跑一次它即可。
# 资产名/下载地址/校验和/配置骨架**一律问 scripts\hub_bootstrap.py**，本脚本不重复这些知识
# （各写一遍 = 改一个字段要改三处，漏掉这里 = Windows 用户静默拿到错配置）。
# 接缝：HUB_CLIPROXY_LOCAL_PKG=<zip 路径> 用本地包代替下载，校验流程照跑。
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $PSScriptRoot
$env:PYTHONUTF8 = '1'  # hub_bootstrap 读写 hub.env 走 UTF-8，防 cp936
$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command python3).Source }
$BS = Join-Path $RepoDir "scripts\hub_bootstrap.py"

function Ask($Name) {
    $v = & $Py $BS get $Name
    if ($LASTEXITCODE -ne 0) { throw "hub_bootstrap get $Name 失败" }
    return "$v".Trim()
}

$Ver = Ask "version"; $Asset = Ask "asset"; $Url = Ask "url"; $Want = Ask "sha256"
$Dir = Ask "dir"; $Bin = Ask "bin"
if ($Asset -notlike "CLIProxyAPI_*_windows_*.zip") { throw "资产名不像 Windows 包：$Asset" }
if ($Url -notlike "https://github.com/*") { throw "bad_download_host: $Url" }
$Stamp = Join-Path $Dir ".installed-version"

if ((Test-Path $Bin) -and (Test-Path $Stamp) -and ((Get-Content -Raw -Encoding UTF8 $Stamp).Trim() -eq $Ver)) {
    Write-Host "已是 $Ver，跳过下载"
} else {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $Tmp = Join-Path $env:TEMP ("cliproxy-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
    try {
        $Pkg = Join-Path $Tmp $Asset
        if ($env:HUB_CLIPROXY_LOCAL_PKG) {
            Copy-Item $env:HUB_CLIPROXY_LOCAL_PKG $Pkg
            Write-Host "用本地包 $env:HUB_CLIPROXY_LOCAL_PKG（校验照跑）"
        } else {
            Write-Host "下载 $Asset（约 20 MB，解压后约 57 MB）…"
            Invoke-WebRequest -Uri $Url -OutFile $Pkg -UseBasicParsing
        }
        # 上游没有 GPG 签名，sha256 是唯一防线：**校验先于解压**，不匹配就什么都不留
        $Got = (Get-FileHash -Algorithm SHA256 -Path $Pkg).Hash.ToLower()
        if ($Got -ne $Want) { throw "checksum_mismatch: $Asset 校验和对不上，已丢弃" }
        Write-Host "sha256 校验通过"
        # 防归档路径穿越：绝对路径或 .. 一律拒
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::OpenRead($Pkg)
        try {
            foreach ($e in $zip.Entries) {
                if ($e.FullName -match '^([A-Za-z]:|[\\/])' -or $e.FullName -match '\.\.[\\/]') {
                    throw "unsafe_archive_path: $($e.FullName)"
                }
            }
        } finally { $zip.Dispose() }
        $Stage = Join-Path $Tmp "stage"
        Expand-Archive -Path $Pkg -DestinationPath $Stage -Force
        $Src = Get-ChildItem -Path $Stage -Recurse -Filter "cli-proxy-api.exe" | Select-Object -First 1
        if (-not $Src) { throw "包里没找到 cli-proxy-api.exe" }
        Copy-Item $Src.FullName $Bin -Force
        Set-Content -Path $Stamp -Value $Ver -Encoding ascii
        Write-Host "已装到 $Bin"
    } finally { Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue }
}

& $Py $BS
if ($LASTEXITCODE -ne 0) { throw "hub_bootstrap 失败" }
Write-Host ""
Write-Host "cliproxy 就绪。注册开机自启（含守护循环）："
Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\register-tasks.ps1`""
