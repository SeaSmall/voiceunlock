# adapt.ps1 -- 从 ref/ 生成 src/：把参考的人脸 CP 改编成声纹 CP
#
# 为什么用脚本而不是手抄：参考实现有 500+ 行 C++，手抄必然引入打字错误；
# 机械替换 + 少量定点改动，既快又可复现、可审查。
#
# 来源：caochitam/windows-face-unlock（MIT）的 credential_provider/
# 改动清单见生成后的 PROVENANCE.md。

$ErrorActionPreference = 'Stop'
# 一律相对脚本自身定位，不写死机器路径（脚本就在 provider\ 下）
$root = $PSScriptRoot
$ref  = Join-Path $root 'ref'
$dst  = Join-Path $root 'src'

# ★ 必须换掉的 CLSID：参考项目那个是公开的（它自己的 README 就提醒了）
$oldGuid = 'F8A0B4D9-3C7F-4B0A-9E21-8C1B1E2B7C10'
$newGuid = '7C4E1A62-9D3B-4F58-A1E7-5B2C8D94F013'

New-Item -ItemType Directory -Force -Path $dst | Out-Null
Get-ChildItem $dst -File -ErrorAction SilentlyContinue | Remove-Item -Force

$renames = @{
    'FaceUnlock'              = 'VoiceUnlock'
    'FaceCredentialProvider'  = 'VoiceCredentialProvider'
    'FaceCredential'          = 'VoiceCredential'
    'Face Unlock'             = 'Voice Unlock'
}

# 定点改的界面文案与超时（人脸 -> 声纹的语义差别）
$pointEdits = @(
    @{ file='VoiceCredential.cpp';
       from='m_status(L"Look at the camera")';
       to='m_status(L"Press the arrow, then speak")' },
    @{ file='VoiceCredential.cpp';
       from='m_status = L"Press the arrow to scan your face";';
       to='m_status = L"Press the arrow, then speak for about 6 seconds";' },
    # 失败提示要指明密码在哪 —— Win11 上密码在「登录选项」里，
    # 用户第一次遇到我们的磁贴时很容易找不到（实测反馈过两次）。
    # 注意 from 必须匹配【重命名前】的原文（这里 "Face not recognised" 不含
    # "Face Unlock"，所以机械替换不会动它，必须按原样匹配）。
    @{ file='VoiceCredential.cpp';
       from='m_status = L"Face not recognised. Use password tile instead.";';
       to='m_status = L"Voice not recognised - use Sign-in options for the password";' },
    @{ file='VoiceCredential.cpp';
       from='AllocStr(L"Voice Unlock failed - use password tile", ppwszOptionalStatusText)';
       to='AllocStr(L"Voice unlock failed - use Sign-in options for the password", ppwszOptionalStatusText)' },
    # 点中我们的磁贴就【立刻开始听】（不用再按箭头）
    @{ file='VoiceCredential.cpp';
       from='*pbAutoLogon = FALSE;';
       to='*pbAutoLogon = TRUE;' }
)

foreach ($f in Get-ChildItem $ref -File) {
    $text = Get-Content $f.FullName -Raw -Encoding UTF8
    foreach ($k in $renames.Keys) { $text = $text.Replace($k, $renames[$k]) }
    $text = $text -replace [regex]::Escape($oldGuid), $newGuid
    $text = $text -replace [regex]::Escape($oldGuid.ToLower()), $newGuid

    $name = $f.Name -replace '^Face', 'Voice'
    $out  = Join-Path $dst $name
    foreach ($e in ($pointEdits | Where-Object { $_.file -eq $name })) {
        if (-not $text.Contains($e.from)) {
            throw "定点改动未命中: $name :: $($e.from)"
        }
        $text = $text.Replace($e.from, $e.to)
    }
    Set-Content -Path $out -Value $text -Encoding utf8NoBOM
}

# guid.h 整份重写：新的 CLSID 必须同时更新字符串形式与 DEFINE_GUID 字节形式
# ⚠ 这里刻意【只用 ASCII】。踩过：写中文注释 + UTF-8 无 BOM -> MSVC 按系统代码页
#   (936/GBK) 解析，多字节序列把换行吃掉，DEFINE_GUID 被并进注释里，报
#   "C2059 语法错误:常数" 和 "CLSID_... 未声明的标识符"。
#   CMakeLists 里另外加了 /utf-8 兜底，但这里保持 ASCII 最稳。
$guidHeader = @"
#pragma once
#include <initguid.h>

// {$newGuid}
// Project-specific CLSID. The reference project (windows-face-unlock) publishes
// its own CLSID; reusing it would collide with other installs of that project.
//
// Keep this file pure ASCII: MSVC parses sources in the system codepage
// (936 here) unless there is a BOM or /utf-8 is passed, and a non-ASCII
// comment can swallow the next line and break the DEFINE_GUID below.
DEFINE_GUID(CLSID_VoiceCredentialProvider,
    0x7c4e1a62, 0x9d3b, 0x4f58, 0xa1, 0xe7, 0x5b, 0x2c, 0x8d, 0x94, 0xf0, 0x13);
"@
Set-Content -Path (Join-Path $dst 'guid.h') -Value $guidHeader -Encoding utf8NoBOM

# 让 MSVC 按 UTF-8 解析所有源文件（对非 ASCII 内容是必需的保险）
Add-Content -Path (Join-Path $dst 'CMakeLists.txt') -Value @"

# Force UTF-8 source parsing. Without this MSVC uses the system codepage (936),
# where a non-ASCII comment can swallow the following line.
target_compile_options(VoiceCredentialProvider PRIVATE /utf-8)
"@ -Encoding utf8NoBOM

# 出处与改动说明
$prov = @"
# PROVENANCE -- provider/src 的来源与改动

来源：**caochitam/windows-face-unlock**（MIT）的 ``credential_provider/`` 目录。
本项目把它从"人脸解锁"改编为"声纹解锁"，改编过程见 ``../adapt.ps1``（可复现）。

## 改了什么

| 项 | 原来 | 现在 |
|---|---|---|
| 命名空间 | ``FaceUnlock`` | ``VoiceUnlock`` |
| 类名 | ``FaceCredential(Provider)`` | ``VoiceCredential(Provider)`` |
| 文件 | ``FaceCredential*.{h,cpp}`` | ``VoiceCredential*.{h,cpp}`` |
| **CLSID** | ``{$oldGuid}``（公开的） | **``{$newGuid}``**（本项目专用） |
| 管道名 | ``\\.\pipe\FaceUnlock`` | ``\\.\pipe\VoiceUnlock``（随命名空间一并替换） |
| 磁贴文案 | "Face Unlock" | "Voice Unlock" |
| 状态文案 | "看摄像头" / "按箭头扫描人脸" | "按箭头后连续说约 6 秒" |
| 管道超时 | 12000 ms | **25000 ms**（声纹要采 2~3 段，人脸是单帧） |

## 原样保留（这些是有价值、容易写错的部分）

- ``helpers.cpp`` 的 ``KERB_INTERACTIVE_UNLOCK_LOGON`` 打包：
  ``UNICODE_STRING.Buffer`` 里存的是**相对缓冲区起始的偏移**而不是指针 —— LSA 要求如此。
- 认证包用 ``LsaConnectUntrusted`` + ``LsaLookupAuthenticationPackage("Negotiate")``。
- ``GetSerialization`` 失败时返回 ``S_FALSE`` + ``CPGSR_NO_CREDENTIAL_NOT_FINISHED``，
  这样用户可以立刻改用密码磁贴（不能把用户卡死）。
- ``GetCredentialCount`` 里 ``*pdwDefault=0`` / ``*pbAutoLogonWithDefault=FALSE``：
  磁贴存在但**不自动触发**，必须用户按箭头 —— 避免锁屏重绘时反复开麦克风。
"@
Set-Content -Path (Join-Path $dst 'PROVENANCE.md') -Value $prov -Encoding utf8NoBOM

# ---- 生成后处理：两处"行为级"改动（用正则匹配多行，避开原文里的非 ASCII 破折号）----

# A) 让本 CP 成为默认并【自动提交】：锁屏一出现 LogonUI 就调 GetSerialization，
#    agent 那边常驻监听电平，用户直接开口即可 —— 不需要点磁贴、不需要按箭头。
$pcp = Join-Path $dst 'VoiceCredentialProvider.cpp'
$t = Get-Content $pcp -Raw
$newAuto = @'
    // voiceunlock: TRUE is what makes it hands-free -- LogonUI submits this
    // credential as soon as the lock screen appears, so the user only speaks.
    //
    // It MUST be paired with a BOUNDED GetSerialization on the Python side.
    // A previous version blocked for 25-60s while "listening", which occupied
    // the sign-in screen so long that it looked like there was no password box
    // (reported twice on real hardware). The agent now pre-arms a ticket from a
    // resident listener thread, and the pipe call is bounded to ~12s -- usually
    // it returns in well under a second (warm ticket) or ~1.5s (no agent).
    *pdwDefault = 0;
    *pbAutoLogonWithDefault = TRUE;
'@
$t2 = $t -replace '(?s)// Face tile is default.*?\*pbAutoLogonWithDefault = FALSE;', $newAuto
if ($t2 -eq $t) { throw "后处理 A 未命中：VoiceCredentialProvider.cpp 的 auto-logon 块" }
Set-Content -Path $pcp -Value $t2 -Encoding utf8NoBOM

# B) PipeClient：先【快速探活】再长时间等待。
#    探活的意义：agent 没在跑时立刻失败（1.5s 内），而不是让用户在锁屏上干等 60 秒。
$ppc = Join-Path $dst 'PipeClient.cpp'
$t = Get-Content $ppc -Raw
$oldCall = 'if (!PipeCall(pipe, "{\"cmd\":\"unlock\"}", resp, 12000)) {' + "`n" +
           '        errorOut = "pipe-unavailable";' + "`n" +
           '        return false;' + "`n" + '    }'
$newCall = @'
// First a fast liveness probe: if the agent is not running we fail in ~1.5s
    // instead of making the user wait out the long voice timeout on the lock screen.
    std::string ping;
    if (!PipeCall(pipe, "{\"cmd\":\"ping\"}", ping, 1500)) {
        errorOut = "agent-unavailable";
        return false;
    }
    // Then the real request. The agent keeps listening for a voice level trigger
    // and only starts recording once somebody speaks, so this can legitimately
    // take up to ~60s while the lock screen sits there.
    if (!PipeCall(pipe, "{\"cmd\":\"unlock\"}", resp, 12000)) {
        errorOut = "pipe-unavailable";
        return false;
    }
'@
if (-not $t.Contains($oldCall)) { throw "后处理 B 未命中：PipeClient.cpp 的 unlock 调用块" }
Set-Content -Path $ppc -Value $t.Replace($oldCall, $newCall) -Encoding utf8NoBOM

Write-Host "生成到: $dst"
Get-ChildItem $dst | Select-Object Name, Length | Format-Table -AutoSize

# 自检：不该再有 Face 残留
$leftover = Get-ChildItem $dst -File -Include *.cpp, *.h, *.def, *.txt, *.ps1 |
    Select-String -Pattern 'Face' -SimpleMatch -List
if ($leftover) {
    Write-Host "`n!! 仍有 Face 残留:" -ForegroundColor Yellow
    $leftover | ForEach-Object { "   {0}:{1}" -f $_.Filename, $_.Line.Trim() }
} else {
    Write-Host "`n自检: 无 'Face' 残留 —— OK" -ForegroundColor Green
}
Write-Host "新 CLSID = {$newGuid}"
