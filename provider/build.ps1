# build.ps1 -- 编译声纹 Credential Provider
#
# 为什么要走 cmd + vcvars64 而不是直接 cmake：
#   MSVC 的编译/链接需要一整套环境变量（INCLUDE / LIB / PATH 指向 cl.exe、link.exe、
#   Windows SDK 头与库）。vcvars64.bat 是官方唯一可靠的注入方式；
#   直接在 PowerShell 里手拼这些变量极其容易漏，编出来的错还很隐晦。
#
# 用法：pwsh -File provider\build.ps1

$ErrorActionPreference = 'Stop'

$src   = Join-Path $PSScriptRoot 'src'
$build = Join-Path $PSScriptRoot 'build'
$log   = Join-Path (Split-Path $PSScriptRoot -Parent) 'logs\build.log'

Write-Host "=== 1) 定位工具链 ==="
$vsw = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vsw)) { throw "找不到 vswhere.exe —— VS 安装器未就位" }

# 必须带 VC.Tools（只要 SDK 没有编译器是不够的）
$vsPath = & $vsw -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath
if (-not $vsPath) {
    Write-Host "带 C++ 工具集的实例尚未就绪。当前 vswhere 能看到:"
    & $vsw -products * -property installationPath
    throw "缺少 Microsoft.VisualStudio.Component.VC.Tools.x86.x64（安装可能还没完成）"
}
$vsPath = $vsPath.Trim()
Write-Host "  VS 安装路径 : $vsPath"

$vcvars = Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path $vcvars)) { throw "找不到 vcvars64.bat: $vcvars" }
Write-Host "  vcvars64    : $vcvars"

# ⚠ 清理会误导 CMake 的环境变量。
# 踩过：环境里存在 RC=0（RC 是资源编译器专用变量名），CMake 会优先读它，
# 于是报 "Could not find the compiler specified in the environment variable RC: 0"。
# CC / CXX 同理。这些名字太通用，被上层进程污染的概率不低。
foreach ($v in 'RC', 'CC', 'CXX') {
    if (Test-Path "Env:$v") {
        Write-Host ("  清理环境变量 {0}={1}" -f $v, (Get-Item "Env:$v").Value)
        Remove-Item "Env:$v"
    }
}

# cmake 优先用 PATH 上的；没有就用 VS 自带的
$cmake = (Get-Command cmake -ErrorAction SilentlyContinue).Source
if (-not $cmake) {
    $cmake = Join-Path $vsPath 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
}
if (-not (Test-Path $cmake)) { throw "找不到 cmake" }
Write-Host "  cmake       : $cmake"

# 显式指定资源编译器，彻底绕开 RC 环境变量污染。
# 踩过两次：环境里先是有 RC=0；把链写成 `set RC= && ...` 时 cmd 又把值设成了一个空格，
# CMake 仍然认为"用户指定了 RC 编译器"。与其和它斗，不如直接把路径喂给 CMake。
$rcExe = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\rc.exe" `
            -ErrorAction SilentlyContinue | Sort-Object FullName -Descending |
            Select-Object -First 1
$rcOpt = ''
if ($rcExe) {
    # ⚠ 必须用【正斜杠】：反斜杠会被原样写进 CMakeRCCompiler.cmake，
    # 而 CMake 解析时把 \P 之类当非法转义，报 "Invalid character escape '\P'"。
    $rcPath = $rcExe.FullName -replace '\\', '/'
    Write-Host "  rc.exe      : $rcPath"
    $rcOpt = "-DCMAKE_RC_COMPILER=`"$rcPath`""
} else {
    Write-Host "  rc.exe      : 没找到（将依赖 PATH）" -ForegroundColor Yellow
}

Write-Host "`n=== 2) 配置 ==="
Write-Host "  静态链接 CRT（/MT）：这个 DLL 要在 LogonUI 里加载，"
Write-Host "  不想依赖系统 vcruntime140/msvcp140 的版本是否匹配。"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
$cfg = "call `"$vcvars`" >nul 2>&1 && `"$cmake`" -S `"$src`" -B `"$build`" -A x64 $rcOpt -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded"
Write-Host "  $cfg"
cmd /c $cfg 2>&1 | Tee-Object -FilePath $log -Append
if ($LASTEXITCODE -ne 0) { throw "cmake 配置失败（exit=$LASTEXITCODE），详情见 $log" }

Write-Host "`n=== 3) 编译 ==="
$bld = "call `"$vcvars`" >nul 2>&1 && `"$cmake`" --build `"$build`" --config Release"
Write-Host "  $bld"
cmd /c $bld 2>&1 | Tee-Object -FilePath $log -Append
if ($LASTEXITCODE -ne 0) { throw "编译失败（exit=$LASTEXITCODE），详情见 $log" }

Write-Host "`n=== 4) 产物 ==="
$dll = Get-ChildItem "$build\**\VoiceCredentialProvider.dll" -ErrorAction SilentlyContinue |
       Select-Object -First 1
if (-not $dll) {
    Write-Host "在 $build 下没找到 DLL，列出所有产物："
    Get-ChildItem $build -Recurse -Include *.dll, *.lib, *.exp |
        Select-Object FullName | Format-Table -AutoSize
    throw "没有产出 VoiceCredentialProvider.dll"
}
Write-Host ("  {0}  ({1:N1} KB)" -f $dll.FullName, ($dll.Length / 1KB))
Write-Host "`nBUILD OK"
