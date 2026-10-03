# register.ps1 -- 注册 / 注销 / 查看 声纹 Credential Provider
#
# 为什么不能只用 regsvr32 就完事：
#   regsvr32 返回成功只代表 DllRegisterServer 被调用了，**不代表注册表真的写对了**。
#   CP 要生效必须同时满足两处注册（一处不对就完全不会出现在锁屏上）：
#     1. HKLM\SOFTWARE\Classes\CLSID\{GUID}\InprocServer32  -> DLL 路径 + Apartment
#     2. HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers\{GUID}
#   所以本脚本注册完会把两处都读出来打印，并给出明确的 OK/FAIL。
#
# ⚠ 救援：如果注册后锁屏出问题（例如磁贴卡住/无法登录），
#   用密码登录后立刻跑：  pwsh -File provider\register.ps1 -Action unregister

param(
    [ValidateSet('register', 'unregister', 'status')]
    [string]$Action = 'register',
    [string]$DllPath
)

$ErrorActionPreference = 'Stop'
$clsid = '{7C4E1A62-9D3B-4F58-A1E7-5B2C8D94F013}'
$clsidKey = "HKLM:\SOFTWARE\Classes\CLSID\$clsid"
$cpKey    = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers\$clsid"

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Find-Dll {
    if ($DllPath -and (Test-Path $DllPath)) { return (Resolve-Path $DllPath).Path }
    # 默认在脚本自己所在的 provider\ 目录下找（相对定位，不写死机器路径）
    $hits = Get-ChildItem $PSScriptRoot -Recurse -Filter 'VoiceCredentialProvider.dll' `
                -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
    if ($hits) { return $hits[0].FullName }
    return $null
}

function Show-State {
    Write-Host "`n--- 注册状态核对 ---"
    $ok1 = Test-Path $clsidKey
    $ok2 = Test-Path $cpKey
    Write-Host ("  1) CLSID\InprocServer32 : {0}" -f $(if ($ok1) { '存在' } else { '缺失' }))
    if ($ok1) {
        $v = Get-ItemProperty "$clsidKey\InprocServer32" -ErrorAction SilentlyContinue
        Write-Host ("       DLL       = {0}" -f $v.'(default)')
        Write-Host ("       线程模型  = {0}" -f $v.ThreadingModel)
    }
    Write-Host ("  2) Credential Providers : {0}" -f $(if ($ok2) { '存在' } else { '缺失' }))
    if ($ok2) {
        $v = Get-ItemProperty $cpKey -ErrorAction SilentlyContinue
        Write-Host ("       名称      = {0}" -f $v.'(default)')
    }
    if ($ok1 -and $ok2) { Write-Host "  => 两处注册齐全（CP 应当能在锁屏上出现）" -ForegroundColor Green }
    else { Write-Host "  => 注册不完整（锁屏上不会出现）" -ForegroundColor Yellow }
}

Write-Host "=== 声纹 CP 注册工具 ==="
Write-Host "CLSID = $clsid"
if (-not (Test-Elevated)) { throw "必须以管理员身份运行（注册写的是 HKLM）" }

switch ($Action) {
    'status' { Show-State }

    'register' {
        $dll = Find-Dll
        if (-not $dll) { throw "没找到 VoiceCredentialProvider.dll —— 先跑 provider\build.ps1" }
        Write-Host "DLL    = $dll"
        Write-Host "`n调用 regsvr32 ..."
        $out = & regsvr32.exe /s $dll 2>&1
        Write-Host "  regsvr32 退出码 = $LASTEXITCODE"
        if ($out) { Write-Host "  $out" }
        Show-State
        Write-Host "`n下一步：按 Win+L 锁屏，看是否出现 'Voice Unlock' 磁贴。"
        Write-Host "若没出现：见方案 §17.5 —— 那就是 Win11 未签名 CP 的加载问题（需要签名 spike）。"
        Write-Host "出问题就立刻撤销： pwsh -File provider\register.ps1 -Action unregister"
    }

    'unregister' {
        $dll = Find-Dll
        if ($dll) {
            & regsvr32.exe /u /s $dll 2>&1 | Out-Host
            Write-Host "已调用 regsvr32 /u（退出码 $LASTEXITCODE）"
        } else {
            Write-Host "没找到 DLL，直接清注册表键"
        }
        # regsvr32 找不到 DLL 时注册表会残留，所以这里再兜一刀
        foreach ($k in @($clsidKey, $cpKey)) {
            if (Test-Path $k) {
                Remove-Item $k -Recurse -Force -ErrorAction SilentlyContinue
                Write-Host "  已删除 $k"
            }
        }
        Show-State
    }
}
