# 免责声明与安全边界

> **一句话**：这个程序**没有后门**（不联网、不上传、无遥测，下面给了你自己核验的方法），
> 但它替你保管的那份 Windows 登录凭据**存在被解出的风险**，而且**声纹本身可以被冒用**。
> 请读完再决定是否使用。

---

## 1. 无后门 —— 这部分你不用信我，可以自己核验

| 项 | 事实 | 怎么自己验 |
|---|---|---|
| 联网 | **没有**任何网络代码（`socket` / `urllib` / `requests` / `http.client` / `winhttp` / `wininet` 一个都没有） | 见下方「核验 1」 |
| 上传音频 | **没有**。音频只在内存里算成 192 维向量，不落盘、不外发 | 见「核验 4」 |
| 遥测 / 统计 | **没有** | 同上 |
| 自动更新 | **没有**。程序不会自己下载或替换任何文件 | 同「核验 1」 |
| 隐藏账号 / 隐藏服务 | **没有**。它只做两件事：注册锁屏凭据提供者、注册一个当前用户的登录计划任务 | `VoiceUnlock.exe paths` 会把这些路径全打出来 |
| 源码 | 全部公开，可自行编译比对 | 见「核验 3」 |

**核验 1（搜网络调用）** —— 只有两个入口和 `src/` 会被打进安装包：

```bash
rg -n "socket|urllib|requests\.|http\.client|httpx|winhttp|wininet|InternetOpen" src packaging/vu_cli.py packaging/vu_agent.py
# 预期：0 条。（tools/ 是开发工具，不随安装包分发）
```

**核验 2（看依赖）**：`requirements.txt` 只有 `numpy` / `onnxruntime` / `kaldi-native-fbank` /
`SoundCard` / `cffi`，没有任何 HTTP 客户端或云 SDK。

**核验 3（自己构建比对）**：

```powershell
pwsh -File packaging\build_installer.ps1        # 会先跑冻结版自检，不过就中止
dist\VoiceUnlock\VoiceUnlock.exe parity --ref logs\parity_ref.npy
# 预期：cos = 1.00000000 —— 冻结版与源码版必须算出完全相同的向量
```

**核验 4（运行时观察）**：给 `VoiceUnlock.exe` 和 `VoiceUnlockAgent.exe` 加一条**出站全阻断**
的防火墙规则，或者用 Process Monitor / `netstat -b` 观察 —— 预期**零连接**。
也可以搜它有没有写音频文件：`rg -n "writeframes|soundfile|\.wav'" src`（预期 0 条；
唯一的 `wave.` 是**读**示例音频）。

**核验 5（原生 DLL）**：`provider\build\Release\VoiceCredentialProvider.dll` 的导入表里
没有 `WS2_32.dll` / `winhttp` / `wininet`。

> 本文件写作前，核验 1、2、5 已跑过：**全部零命中**。3、4 你随时能自己跑一遍。

---

## 2. ⚠ 凭据被解出的风险（最重要的一条）

它**必须**保存你的 Windows 登录密码 —— 否则没法在锁屏上"替你交密码"。
存法：Windows 自带的 **DPAPI 用户作用域**加密，密文在
`%LOCALAPPDATA%\VoiceUnlock\credential.bin`。

**它防得住**：拿到磁盘文件的人、同一台机器上的其他用户。
**它防不住**：

* **已经能以你的身份运行代码的人** —— 那种情况下对方本来就能读进程内存
  （这条边界写在 `src/dpapi.py` 的注释里，不是事后补的）；
* **本机管理员 / SYSTEM** —— 可以替换程序、注入进程、直接读内存；
* **离线爆破（这就是"密钥有被破解风险"的具体含义）**：
  DPAPI 主密钥由你的 **Windows 口令**派生。任何拿到 `credential.bin` 的人
  （偷硬盘、翻备份、同步网盘、VM 快照、旧磁盘）都可以**离线**对你的口令做
  字典/暴力攻击 —— **口令越弱，破开越快**。

**降低风险的做法**：用长口令或密码短语、开启 **BitLocker**（让"拿到文件"本身变难）、
不要把这个用户目录同步到网盘、不要给来路不明的程序管理员权限。

---

## 3. ⚠ 声纹可以被冒用

* **没有活体检测**：没有随机短语挑战、没有 ASR 校验、也没有"拒绝重复片段"。
* 现有的缓解只有：设备白名单（默认**不信任**虚拟/回环/混音设备）、质量闸
  （活跃比例与时长）、失败熔断（连续 3 次失败锁 5 分钟）、每设备独立阈值。
* **所以**：在真实麦克风旁**播放一段你的录音**，或用高保真**语音克隆**，
  仍**有可能**骗过判定。请把声纹当作"便捷因素"，不要当作唯一防线 ——
  密码框一直都在，就是为这种情况留的。

---

## 4. 声纹档案本身也是敏感数据

`profiles.json` 里是 192 维声纹向量（生物特征）。**生物特征不可撤销**：
口令泄露了可以改，声纹泄露了改不了。别把它上传、分享或提交到任何仓库。

---

## 5. 误判与失效（会让你进不去，但不会把你锁死）

* 环境噪声、换麦克风、感冒、语速变化都会影响判定，可能解锁失败 —— 这时用密码。
* **防锁死**：每次自动提交密码之前，程序都会先用 Windows 自己 `LogonUser` 真登一次；
  一旦发现库里存的密码已经登录不了（比如你改过密码），它**一次都不提交**。
  所以它不会因为闷头提交错误密码而把你的账户锁死。
* 与第三方主题登录界面共存时，若锁屏出现异常，**先跑 `VoiceUnlock.exe panic`**
  退回原版锁屏，再去归因是谁的问题。

---

## 6. 责任限制

* 本程序按 **"现状"（AS IS）** 提供，**不提供任何明示或暗示的担保**，
  包括但不限于可商用性、特定用途适用性、以及不侵权。
* 因下载、安装、使用或**无法使用**本程序造成的任何直接或间接损失
  （包括但不限于：无法登录、数据丢失、账户被冒用、隐私泄露、合规风险、业务中断），
  **作者不承担任何责任**。
* 是否使用、装在哪些机器上、用谁的账户，由**你自行判断并承担后果**。

---

## 7. 使用边界与合规

* 只用于**你自己拥有、或已获得明确授权**的设备。
  不得用于规避他人设备的访问控制。
* 声纹属于**生物识别信息**。如果你在他人机器/他人账户上部署，或采集他人声纹，
  必须事先取得对方**知情同意**，并遵守当地法律
  （例如中国《个人信息保护法》、欧盟 GDPR 对生物识别数据的特别规定）。
* 请遵守你所在单位/学校的安全策略 —— 有些环境明确禁止改动锁屏认证链。

---

## 8. 用之前建议照做的加固清单

- [ ] 用**长口令/密码短语**（直接决定离线爆破成本）
- [ ] 开启 **BitLocker**
- [ ] 不要把 `%LOCALAPPDATA%\VoiceUnlock\` 同步到网盘/备份到共享盘
- [ ] 保留桌面上的「★ 紧急关闭声纹解锁」快捷方式（出事一键退回原版锁屏）
- [ ] 先在**测试机**上装一遍、验一遍锁屏，再上生产机器
- [ ] 装了第三方主题登录界面的话，记住它的更新可能影响锁屏，出问题先 `panic` 再归因
- [ ] 定期更换 Windows 口令，并在**当前登录会话里**重新跑一次 `setup-password`

---

## English summary

**No backdoor, but read this.** VoiceUnlock contains **no network code at all** — it does not
upload audio, has no telemetry, no auto-update, and no hidden accounts; the shipped code is
small enough to audit yourself (see the verification commands in §1). However:

1. It must store your Windows logon password to submit it at the lock screen. The ciphertext
   is protected with **DPAPI (user scope)**, which protects against other users and against
   someone who only has your disk — **not** against code already running as you, not against
   local administrators/SYSTEM, and **not** against **offline brute force**: the DPAPI master
   key is derived from your Windows password, so a weak password makes the stored credential
   crackable offline.
2. **The voiceprint can be spoofed.** There is no liveness detection, no random-phrase
   challenge and no duplicate-audio rejection; mitigation is limited to a device allow-list,
   quality gates and a 3-failures/5-minutes lockout. Replaying a recording near a real
   microphone, or using a high-fidelity voice clone, may still pass. Treat the voiceprint as a
   *convenience factor*, never as your only defence.
3. The software is provided **AS IS**, without warranty of any kind; the author accepts **no
   liability** for any damage or loss arising from its use.
4. Use it only on devices you own or are explicitly authorised to access. Voiceprints are
   **biometric data**: obtain informed consent and comply with applicable law (e.g. GDPR, PIPL)
   before deploying it on anyone else's machine or account.
