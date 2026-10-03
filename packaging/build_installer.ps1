# build_installer.ps1 -- 一键打包：PyInstaller 冻结 + Inno Setup 生成安装包
#
# 为什么要一个脚本（而不是手敲两条命令）：
#   中间有几步是"忘了就出乱子"的：
#     1) 先跑 parity 参考向量 —— 冻结版必须能复现源码版的向量
#     2) .iss 和 使用说明.txt 必须带 UTF-8 BOM，否则 Inno 按 ANSI 读，中文乱码
#     3) 自定义语言包必须复制进 Inno 的 Languages 目录，compiler: 前缀才找得到
#     4) 打包前清掉旧 dist，避免把上一次的残留一起打进安装包
#
# 用法：
#   pwsh -File packaging\build_installer.ps1
#   产物： dist\VoiceUnlock-Setup-<版本>.exe

[CmdletBinding()]
param(
    [switch]$SkipFreeze,
    [switch]$SkipParity
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$py = Join-Path $root 'venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "找不到 venv python：$py" }

function Step($n, $t) { Write-Host "`n=== [$n] $t ===" -ForegroundColor Cyan }

function Add-Utf8Bom([string]$path) {
    $bytes = [System.IO.File]::ReadAllBytes($path)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        return $false   # 已有 BOM
    }
    $text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $true))
    return $true
}

# ---------------------------------------------------------------- 1. 参考向量
if (-not $SkipParity) {
    Step 1 '生成源码侧 parity 参考向量'
    & $py tools\dump_parity_ref.py
    if ($LASTEXITCODE -ne 0) { throw "dump_parity_ref 失败" }
}

# ---------------------------------------------------------------- 2. 冻结
if (-not $SkipFreeze) {
    Step 2 'PyInstaller 冻结（one-folder，两个 exe）'
    Remove-Item (Join-Path $root 'dist\VoiceUnlock') -Recurse -Force -ErrorAction SilentlyContinue
    & $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\vu.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败，见日志" }
}

$exe = Join-Path $root 'dist\VoiceUnlock\VoiceUnlock.exe'
if (-not (Test-Path $exe)) { throw "没找到冻结产物：$exe" }

# ---------------------------------------------------------------- 3. 冻结版自检
Step 3 '冻结版自检（一致性 + 判定逻辑）'
$ref = Join-Path $root 'logs\parity_ref.npy'
& $exe parity --ref $ref
if ($LASTEXITCODE -ne 0) { throw "冻结版 parity 不一致 —— 打包把推理搞坏了，别往下走" }
& $exe selftest-decision | Out-Null
if ($LASTEXITCODE -ne 0) { throw "冻结版判定自测失败" }
Write-Host '  parity + selftest-decision 均通过'

# ---------------------------------------------------------------- 4. Inno 准备
Step 4 '准备 Inno Setup 资源（BOM + 语言包）'
$iscc = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe'
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    throw ' 没找到 ISCC.exe（Inno Setup）。装一个： winget install --id JRSoftware.InnoSetup --exact'
}
Write-Host "  ISCC = $iscc"

$innoLangs = Join-Path (Split-Path $iscc) 'Languages'
$islSrc = Join-Path $PSScriptRoot 'ChineseSimplified.isl'
$islDst = Join-Path $innoLangs 'ChineseSimplified.isl'
if (-not (Test-Path $islDst)) {
    if (-not (Test-Path $islSrc)) {
        throw "缺中文语言包：$islSrc （下载见 packaging 说明）"
    }
    Copy-Item $islSrc $islDst -Force
    Write-Host "  已安装中文语言包 -> $islDst"
} else {
    Write-Host "  中文语言包已就位"
}

foreach ($f in @((Join-Path $PSScriptRoot 'installer.iss'),
                 (Join-Path $PSScriptRoot '使用说明.txt'))) {
    if (Add-Utf8Bom $f) { Write-Host "  已补 UTF-8 BOM: $(Split-Path $f -Leaf)" }
    else { Write-Host "  已有 UTF-8 BOM: $(Split-Path $f -Leaf)" }
}

# ★ 第三方许可：免安装形态（dist\VoiceUnlock\）也必须带。
#   随包的 campplus.onnx 是 CAM++ 权重（Apache-2.0）的格式转换件，§4(a)(b)(c)
#   要求"给接收者许可正文 + 声明修改 + 保留归属"。安装包那份由 installer.iss 的
#   [Files] 负责；但用户完全可能直接把 dist\VoiceUnlock\ 拷给别人 —— 那条路径
#   也必须合规，否则"免安装形态"就是个漏洞。
foreach ($f in @('THIRD-PARTY-NOTICES.txt', 'LICENSE-Apache-2.0.txt')) {
    Copy-Item (Join-Path $PSScriptRoot $f) (Join-Path $root "dist\VoiceUnlock\$f") -Force
}
Write-Host '  第三方许可已随免安装形态落地（THIRD-PARTY-NOTICES.txt / LICENSE-Apache-2.0.txt）'

# ---------------------------------------------------------------- 5. 编译安装包
Step 5 'Inno Setup 编译安装包'
& $iscc (Join-Path $PSScriptRoot 'installer.iss')
if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败（退出码 $LASTEXITCODE）" }

$setup = Get-ChildItem (Join-Path $root 'dist') -Filter 'VoiceUnlock-Setup-*.exe' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Host ''
Write-Host ('安装包： {0}  ({1:N1} MB)' -f $setup.FullName, ($setup.Length / 1MB)) -ForegroundColor Green
Write-Host '装到别的机器前，记得同时带上：dist\VoiceUnlock\（免安装形态，可直接跑）'
