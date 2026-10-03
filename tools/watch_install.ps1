# Long-running install monitor: polls VMware Tools state and snapshots the console.
#
# vmrun / VMX 从环境变量取，脚本里不写死机器路径：
#   $env:VU_VMRUN  = 'C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe'
#   $env:VU_VM_VMX = '<VM目录>\Win10Auto\Win10Auto.vmx'
param(
    [int]$Iterations = 70,
    [int]$IntervalSec = 60,
    [string]$OutDir = ''
)
$Vmrun = $env:VU_VMRUN
if (-not $Vmrun) { throw '缺少 VU_VMRUN：请设置为 vmrun.exe 的完整路径' }
if (-not (Test-Path $Vmrun)) { throw "找不到 vmrun：$Vmrun" }
$Vmx = $env:VU_VM_VMX
if (-not $Vmx) { throw '缺少 VU_VM_VMX：请设置为 .vmx 文件的完整路径' }
$root = Split-Path $PSScriptRoot -Parent
if (-not $OutDir) { $OutDir = Join-Path $root 'logs\shots' }
$Log = Join-Path $root 'logs\vm_provision_monitor.log'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

for ($i = 0; $i -lt $Iterations; $i++) {
    $ts = Get-Date -Format 'HH:mm:ss'
    $state = (& $Vmrun -T ws checkToolsState $Vmx 2>&1) -join ' '
    $running = (& $Vmrun -T ws list 2>&1) -match [regex]::Escape($Vmx)
    $line = "[$ts] iter=$i tools='$($state.Trim())' running=$([bool]$running)"
    Add-Content -Path $Log -Value $line -Encoding utf8

    $png = Join-Path $OutDir ('shot_{0:d3}.png' -f $i)
    try {
        & python (Join-Path $PSScriptRoot 'vmvnc.py') shot $png 2>$null | Out-Null
    } catch {
        Add-Content -Path $Log -Value "   (vnc shot failed: $($_.Exception.Message))" -Encoding utf8
    }

    if ($state -match 'installed') {
        Add-Content -Path $Log -Value "[$ts] VMware Tools reports INSTALLED - stopping monitor" -Encoding utf8
        break
    }
    Start-Sleep -Seconds $IntervalSec
}
Add-Content -Path $Log -Value "monitor finished $(Get-Date -Format o)" -Encoding utf8
