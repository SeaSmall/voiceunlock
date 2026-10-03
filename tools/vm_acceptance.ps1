# =====================================================================
#  vm_acceptance.ps1 - run the 5 acceptance checks against the new VM
#  Every command is launched with a SINGLE-STRING -ArgumentList so that
#  the empty "":  -gp ""  survives CreateProcess argument parsing.
#  (PowerShell's array form / Start-Process -ArgumentList @(...) drops it.)
# =====================================================================
$ErrorActionPreference = 'Continue'

# vmrun / VMX 一律从环境变量取，脚本里不写死任何机器路径：
#   $env:VU_VMRUN   = 'C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe'
#   $env:VU_VM_VMX  = '<VM目录>\Win10Auto\Win10Auto.vmx'
#   $env:VU_VM_USER = 'Administrator'   （可选）
#   $env:VU_VM_PASS = '<客户机口令>'    （可选；不设就用空口令，与原来一致）
#   $env:VU_LOG_DIR = '<日志目录>'      （可选；默认 <仓库根>\logs）
$Vmrun = $env:VU_VMRUN
if (-not $Vmrun) { throw '缺少 VU_VMRUN：请设置为 vmrun.exe 的完整路径' }
if (-not (Test-Path $Vmrun)) { throw "找不到 vmrun：$Vmrun" }
$Vmx = $env:VU_VM_VMX
if (-not $Vmx) { throw '缺少 VU_VM_VMX：请设置为 .vmx 文件的完整路径' }
$User = $env:VU_VM_USER
if (-not $User) { $User = 'Administrator' }
$LogDir = $env:VU_LOG_DIR
if (-not $LogDir) { $LogDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'logs' }
$Log   = Join-Path $LogDir 'vm_provision_acceptance.log'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if ($env:ACC_LOG_RESET -eq '1') { Remove-Item $Log -ErrorAction SilentlyContinue }

function Log([string]$s) {
    Write-Host $s
    Add-Content -Path $Log -Value $s -Encoding utf8
}

function VR {
    param(
        [Parameter(Mandatory=$true)][string]$ArgLine,
        [string]$Title = ''
    )
    $tmpO = [System.IO.Path]::GetTempFileName()
    $tmpE = [System.IO.Path]::GetTempFileName()
    Log ''
    Log ('#' * 72)
    if ($Title) { Log "# $Title" }
    Log ('$ "' + $Vmrun + '" ' + $ArgLine)
    Log ('#' * 72)
    $p = Start-Process -FilePath $Vmrun -ArgumentList $ArgLine -NoNewWindow -Wait -PassThru `
                       -RedirectStandardOutput $tmpO -RedirectStandardError $tmpE
    $o = ''
    $e = ''
    if (Test-Path $tmpO) { $o = (Get-Content $tmpO -Raw -ErrorAction SilentlyContinue) }
    if (Test-Path $tmpE) { $e = (Get-Content $tmpE -Raw -ErrorAction SilentlyContinue) }
    if ($o) { Log '--- stdout ---'; Log ($o.TrimEnd()) }
    if ($e) { Log '--- stderr ---'; Log ($e.TrimEnd()) }
    Log "--- exit code: $($p.ExitCode) ---"
    Remove-Item $tmpO,$tmpE -Force -ErrorAction SilentlyContinue
    return $p.ExitCode
}

# 客户机口令：默认空（与原来一致）；需要口令就设 $env:VU_VM_PASS，不要写进脚本。
if ($env:VU_VM_PASS) { $common = "-T ws -gu $User -gp `"$($env:VU_VM_PASS)`"" }
else { $common = "-T ws -gu $User -gp `"`"" }

Log ''
Log ('=' * 72)
Log " ACCEPTANCE RUN  $(Get-Date -Format o)"
Log " VMX = $Vmx"
Log ('=' * 72)

# ---- 3. VMware Tools state -------------------------------------------------
VR -Title 'AC-3  checkToolsState' -ArgLine "-T ws checkToolsState `"$Vmx`""

# ---- 4a. listDirectoryInGuest ---------------------------------------------
VR -Title 'AC-4a listDirectoryInGuest C:\' -ArgLine "$common listDirectoryInGuest `"$Vmx`" C:\"

# ---- 4c. runScriptInGuest --------------------------------------------------
VR -Title 'AC-4c runScriptInGuest -> C:\probe.txt' `
   -ArgLine "$common runScriptInGuest `"$Vmx`" `"`" `"cmd.exe /c echo hello > C:\probe.txt`""

VR -Title 'AC-4c verify C:\probe.txt exists' -ArgLine "$common fileExistsInGuest `"$Vmx`" C:\probe.txt"
VR -Title 'AC-4c read back C:\probe.txt' -ArgLine "$common runScriptInGuest `"$Vmx`" `"`" `"cmd.exe /c type C:\probe.txt`""

# ---- 4b. captureScreen -----------------------------------------------------
VR -Title 'AC-4b captureScreen -> C:\shot.png' `
   -ArgLine "$common captureScreen `"$Vmx`" C:\shot.png"
VR -Title 'AC-4b size of C:\shot.png in guest' `
   -ArgLine "$common runScriptInGuest `"$Vmx`" `"`" `"cmd.exe /c dir C:\shot.png`""

# ---- 5. guest OS info ------------------------------------------------------
VR -Title 'AC-5  wmic os get Caption,OSArchitecture,Version' `
   -ArgLine "$common runScriptInGuest `"$Vmx`" `"`" `"cmd.exe /c wmic os get Caption,OSArchitecture,Version /value`""
VR -Title 'AC-5  systeminfo summary' `
   -ArgLine "$common runScriptInGuest `"$Vmx`" `"`" `"cmd.exe /c systeminfo`""

Log ''
Log '=== acceptance run finished ==='
