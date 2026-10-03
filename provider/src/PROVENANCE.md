# PROVENANCE -- provider/src 的来源与改动

来源：**caochitam/windows-face-unlock**（MIT）的 `credential_provider/` 目录。
本项目把它从"人脸解锁"改编为"声纹解锁"，改编过程见 `../adapt.ps1`（可复现）。

> **许可与再分发**：上游是 MIT，其版权与许可声明必须随改编件一起保留 ——
> 见 `../LICENSE-upstream-windows-face-unlock.txt`（含上游版权行与对本目录的归属说明）。
> 上游源码的**原样副本不随本仓库分发**（根目录 `.gitignore` 排除了 `provider/ref/`）。
> 要重跑改编脚本：把上游仓库的 `credential_provider/` 整个拷到 `provider/ref/`，再执行 `adapt.ps1`。

## 改了什么

| 项 | 原来 | 现在 |
|---|---|---|
| 命名空间 | `FaceUnlock` | `VoiceUnlock` |
| 类名 | `FaceCredential(Provider)` | `VoiceCredential(Provider)` |
| 文件 | `FaceCredential*.{h,cpp}` | `VoiceCredential*.{h,cpp}` |
| **CLSID** | `{F8A0B4D9-3C7F-4B0A-9E21-8C1B1E2B7C10}`（公开的） | **`{7C4E1A62-9D3B-4F58-A1E7-5B2C8D94F013}`**（本项目专用） |
| 管道名 | `\\.\pipe\FaceUnlock` | `\\.\pipe\VoiceUnlock`（随命名空间一并替换） |
| 磁贴文案 | "Face Unlock" | "Voice Unlock" |
| 状态文案 | "看摄像头" / "按箭头扫描人脸" | "按箭头后连续说约 6 秒" |
| 管道超时 | 12000 ms | **25000 ms**（声纹要采 2~3 段，人脸是单帧） |

## 原样保留（这些是有价值、容易写错的部分）

- `helpers.cpp` 的 `KERB_INTERACTIVE_UNLOCK_LOGON` 打包：
  `UNICODE_STRING.Buffer` 里存的是**相对缓冲区起始的偏移**而不是指针 —— LSA 要求如此。
- 认证包用 `LsaConnectUntrusted` + `LsaLookupAuthenticationPackage("Negotiate")`。
- `GetSerialization` 失败时返回 `S_FALSE` + `CPGSR_NO_CREDENTIAL_NOT_FINISHED`，
  这样用户可以立刻改用密码磁贴（不能把用户卡死）。
- `GetCredentialCount` 里 `*pdwDefault=0` / `*pbAutoLogonWithDefault=FALSE`：
  磁贴存在但**不自动触发**，必须用户按箭头 —— 避免锁屏重绘时反复开麦克风。
