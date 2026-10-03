# 声纹解锁 —— 交接文档（上下文压缩版）

> 用途：换会话/换人接手时的唯一入口。所有结论都带证据，踩过的坑单独一节。
> 本次更新：2026-10-03 17:2x（这一轮做完了：输出语言自动切换、第三方许可合规、
> 施工方案补写 §19-§25、按新代码重打包并在英文 Win10 虚拟机复验）

---

## 1. 用户的最终要求（以最后几条为准）

1. **给"其他电脑"用的安装包**，不是本机 —— 目标机器上**账户是有密码的**。
2. **原锁屏界面一点都不要动**：不要加自绘磁贴、不要加状态文字、不要改外观。
3. **只在锁屏时静默后台检测**声纹。
4. 合并 ShittimLogon-1.5.0：**用它自己的安装程序装**，我们的包**不含它的任何文件**
   （其 LICENSE §3(a)(d) 禁止再分发/二次打包；用户定性为"仅本机自用不传播"，
   我们仍按其条款操作，且保证不修改它的二进制、随包保留其 LICENSE/NOTICE/SPINE-LICENSE）。
5. 用户本机要**恢复干净**：已注销声纹 CP、清空密码、移除自启（本轮又独立核对一次，见 §8）。
6. 虚拟机开 **Win10** 做验证；紧急退出功能"可以不加"（已做，保留）。

---

## 2. 架构（已定型，细节见施工方案 §19-§21）

```
常驻 agent（用户会话，HKCU Run 自启）
  ├─ 只在锁屏时开麦 → 电平触发 → 声纹判定 → 备一张 40 秒有效的"解锁票据"
  └─ 命名管道 \\.\pipe\VoiceUnlock（SDDL + 只认 logonui.exe）
        ↕
LogonUI 里的 Credential Provider（我们的 DLL）
  ├─ ping 问票据；ticket=false 时【枚举 0 个凭据】→ 界面上完全没有我们
  ├─ ticket=true → CredentialsChanged（经 GIT 封送，跨线程调 STA）→ 凭据出现
  │   → pbAutoLogonWithDefault → LogonUI 自动提交 → 解锁
  └─ 若凭据是"无密码"状态 → 改为在安全桌面 SendInput 注入回车
      （Windows 自己的"按回车即登录"路径，不提交任何凭据，不会触发账户锁定）
```

**两条解锁路径**：有密码机器（主场景）提交 DPAPI 解出的密码（已实测 `4801` 解锁）；
无密码机器（兼容）注入回车（未在真机实测）。
**防锁死**：提交前必用 `LogonUser` 真登一次；不对就一次都不提交
（本机策略 10 次密码错锁 10 分钟，闷头提交会把人锁在门外）。

---

## 3. 已完成并验证

| 项 | 证据 |
|---|---|
| 独立推理内核（无 torch/funasr） | `parity cos=1.00000000`；`SELFTEST OK` |
| 判定逻辑三层覆盖 | `DECISION SELFTEST OK`（含阈值标定、兜底、质量闸断言） |
| 冻结版一致性 | `dist\VoiceUnlock\VoiceUnlock.exe parity --ref logs\parity_ref.npy` → 1.00000000 |
| 安装包（Win10 x64 虚拟机实测） | Inno 日志：`cp-install` exit 0、`install-autostart` exit 0、`Need to restart? No` |
| **锁屏/登录界面完全原样** | VM 抓图：登录界面只有 `admin` + `Password` 框，无我们的任何元素 |
| 凭据可用 | VM 内 `setup-password` → `网络式/交互式登录 : 成功` |
| **票据门控 + 自动提交解锁** | VM 内锁屏 → 自动回到桌面（桌面帧 98KB vs 锁屏帧 900KB+） |
| 卸载干净（先注销 CP 再删文件） | 本轮新包复验：两个 CP 键、自启项、用户数据全清，程序目录只剩 `unins000.exe` |
| 声纹声学部分（真机） | 本机实测：真实语音 0.57 → `4801 自动解锁`，连续两次 |
| **英文系统不再乱码（本轮）** | 英文 Win10 VM 实测（`logs\guest\en2-output.txt`）：`help/ paths/ cp-status/ enroll --list/ enroll --help` 全部英文且纯 ASCII；`logs\guest\lang.txt` 记下该机 `UILanguage=0x0409`、控制台代码页 437 |
| **中文机零回归（本轮）** | `tools\i18n_audit.py --all` 的 [E]：10 条只读命令的中文输出与**改动前的冻结版**逐行一致（0 处不一致） |
| **输出层五项审计（本轮）** | `logs\audit.txt`：A 460 条目录 0 问题；B 462 条用户可见中文 0 缺失；C 158 条模板自渲染 0 问题；D 10 条英文命令 0 残留（6 行是用户名/设备名数据）；E 见上 |
| **第三方许可合规（本轮）** | `logs\licensing-campplus.md` 判定 CAM++ 权重为 Apache-2.0；已补 `packaging\LICENSE-Apache-2.0.txt` + `packaging\THIRD-PARTY-NOTICES.txt`，写进 `installer.iss` 的 `[Files]`，并在 `build_installer.ps1` 里同步拷进免安装形态；VM 装机目录里已列到这两个文件 |
| **★ 对端校验缺陷已修（2026-10-03 晚）** | 依据另一台机器的排障记录：非管理员 agent 读不到 SYSTEM 进程（LogonUI）映像 → 所有锁屏请求被判 `peer-not-allowed`。自启改成**计划任务 + `RunLevel=HighestAvailable`**（`src/install.py`），`installer.iss` 的 `[Run]` 同步去掉 `runasoriginaluser`。本机冻结版实测：`schtasks /Query /XML` 回读 RunLevel/LogonType/Command/Arguments/IgnoreNew/PT0S/电池/重启 全部正确，`paths` 显示"计划任务 VoiceUnlockAgent（最高权限）"，`uninstall-autostart` 干净移除 |
| **`VoiceUnlockAgent.exe` 忽略 argv 已修** | `packaging/vu_agent.py` 原来直接 `Agent().run()`，`--allow-any-peer` 等参数在发布的 exe 里无效；改为交给 `src.agent.main()` |
| **安装器删掉"同时安装 Shittim Logon"选项** | 按用户要求：`[Tasks]` 勾选项、`[Run]` 代跑、`[Code]` 三个函数全部删除；安装包与主题登录界面零耦合 |
| **VM 里验完新自启（真机级证据）** | 新安装包装完后 `schtasks /Query /XML` 回读：`RunLevel=HighestAvailable` / `LogonType=InteractiveToken` / `VoiceUnlockAgent.exe run` / `IgnoreNew` / `PT0S` / 电池不停 / `RestartOnFailure 3/PT1M`；旧 HKCU Run 值**不存在**；`paths` 显示 `autostart = scheduled task VoiceUnlockAgent (highest privileges)` |
| **★ 对端校验修复的对照证据** | 由计划任务启动的 agent + 锁屏 30 秒：`agent.log` **收到请求 87 次 / 拒绝对端 0 次**，每条都是 `cmd='ping' 对端=C:\Windows\System32\LogonUI.exe`（笔记本 上的症状是"几百行拒绝、请求 0 次"）→ 缺陷已消除 |
| **卸载残留缺陷（本轮自查发现并修复）** | VM 实测：旧包卸载后计划任务**仍在**（`Status=Ready`）。已在 `[UninstallRun]` 加 `uninstall-autostart`（计划任务只能这样清，`[Registry]` 段管不着）并重打包 |
| **包内隐私清理（本轮）** | `tools\scan_package_sensitive.py` 四项全绿：① 产物目录无用户数据文件 ② 128 个包内文件按真实敏感串搜 0 命中 ③ 23 个"会进包的源码" 0 命中 ④ **两个 exe 各解出 18 个 `src.*` 冻结模块、遍历字符串常量 0 命中**。清理内容与踩坑见 §6 坑 28-29 |

---

## 4. 关键路径与命令

```
项目根            <项目目录>
冻结产物          dist\VoiceUnlock\（VoiceUnlock.exe 控制台 / VoiceUnlockAgent.exe 无窗口）
安装包            dist\VoiceUnlock-Setup-0.1.0.exe（49.9 MB）
改动前基线        dist\_baseline_pre_i18n\VoiceUnlock\  ← [E] 零回归对比用的老冻结版，别删
一键打包          pwsh -File packaging\build_installer.ps1 [-SkipFreeze]
CP 源码/DLL       provider\src\  →  provider\build\Release\VoiceCredentialProvider.dll
CP 注册脚本       provider\register.ps1 -Action register|unregister|status
施工方案文档      <项目目录>\声纹解锁-施工方案.md（已补 §19-§25，1718 行）
```

**输出语言（本轮新增）**
```
src\i18n.py                    语言探测 + 输出层翻译（print/input/argparse）
src\i18n_catalog.py            460 条中英对照（由工具生成，可手工维护）
tools\i18n_scan.py             穷举用户可见中文 → 出切片 / 覆盖审计
tools\i18n_build_catalog.py    合并分片翻译 → src\i18n_catalog.py（带自检）
tools\i18n_audit.py            五项审计（A 目录 B 覆盖 C 模板 D 运行时 ASCII E 中文零回归）
  python tools\i18n_audit.py --all --out logs\audit.txt     ← 全过才返回 0
强制语言： set VOICEUNLOCK_LANG=zh   /   =en   （探测顺序：环境变量 > 系统 UI 语言）
```

CLI 子命令：`paths` / `parity` / `setup-password` / `agent run|status` / `enroll` /
`cp-install|cp-uninstall|cp-status` / `install-autostart|uninstall-autostart` / `panic`

---

## 5. 测试环境（虚拟机的"自动化通道"）

```
VMware Workstation 26.0     <VMware安装目录>\（vmrun.exe / vmware.exe）
虚拟机                      <VM目录>\Win10Auto\Win10Auto.vmx（2 vCPU / 4GB / BIOS / Win10 Pro x64 19045 英文版）
VM 账户                     admin / <VM测试口令>（密码是我自己设的测试值，非用户真实密码）
VM 的 VNC                  127.0.0.1:5905  密码 <VNC口令>（.vmx 里 RemoteDisplay.vnc.*）
主机在 NAT 网段            192.168.1.1（客户机用它访问主机）
主机 HTTP 桥               python tools\guest_bridge.py --port 8899
                           GET 下发 staging\ 下文件；POST/PUT /upload/<name> 落到 logs\guest\
客户机侧取文件/回传         curl -o x.cmd http://192.168.1.1:8899/x.cmd ; x.cmd
                           curl -s -X POST --data-binary "@out.txt" http://192.168.1.1:8899/upload/out.txt
VNC 控制器                 python tools\vnc_ctl.py --pass <VNC口令> type "..." key enter pause 5 capture out.png
本轮用到的脚本             staging\e2.cmd（英文输出取证）、lang.ps1 + l.cmd（量语言信号）、u.cmd（卸载复验）
```

**这套通道不需要 VMware Tools，也不需要客户机凭据** —— 靠 VNC 打字 + HTTP 传文件。

---

## 6. 踩过的坑（重要，别再踩）

1. **VMware VNC 不转发 Shift**：`:` 会变成 `;`（`C:\g.cmd` → `c;\g.cmd`），命令全废。
   → 必须显式发 `shift-<基础键>`；已封装在 `tools/vnc_ctl.py`（每个字符单独发）。
   另外 vncdotool 的 **Python API 需要 reactor 在跑**，用 `time.sleep` 不会让 Deferred 执行
   → 只把参数翻译好、交给 `vncdo` CLI 跑。CLI 的延迟选项是 `--delay=毫秒`（不是 `-d`）。
2. **不要往 `C:\` 根目录写文件**（非提权 cmd 会被拒）→ 用 `%USERPROFILE%` / 相对路径。
3. **`.cmd` 必须纯 ASCII + CRLF**（UTF-8 中文会被 cmd 按 ANSI 解析、吃掉换行）。
   `.iss` / 说明文件相反：**必须有 UTF-8 BOM**，否则 Inno 按 ANSI 读、中文全乱。
4. **Inno Setup**：`runasoriginaluser` 只在 `[Run]` 有效，`[UninstallRun]` 不支持
   → 自启项清理由 `[Registry]` 的 `ValueType: none + uninsdeletevalue` 惯用法负责。
5. **DPAPI 用户作用域**：会话建立后**改密码**会让老会话解不开凭据（`0x8009000B`）
   → 凭据必须在**当前登录会话**里保存；改密码后要重新保存。
6. **沉默不是"安静"，要判"有没有人声"**：`speech_stats.silent` 只看设备是不是死的
   （峰值<1e-5）。噪声/空麦也能过那关，于是"没人说话"也能建档、写出 0.67 的害人阈值。
   可靠判据是 `active_ratio`（正常说话 0.3~0.7，噪声 0.00），阈值 0.10。
7. **登记用连续语音切片算阈值会偏乐观**（解锁走的是短窗，分布不同）
   → `_runtime_selfcheck` 按判定路径复核并下调阈值。
8. **`embed_best_window` 的 `speech_frames` 不是 10ms 帧**，是 0.265 秒能量块（2 秒窗最多 7~8 个）
   → 质量闸用**比例**，别用绝对帧数。
9. **虚拟机麦克风不是假的（原结论已推翻，2026-10-03 晚更正）**：VM 声卡
   `sound.autodetect=TRUE` 时，**麦克风输入取的是主机的"默认录音设备"**；本机默认录音设备是
   `VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)` —— 一个**虚拟**设备。上一轮测到"峰值 7e-6
   静音"不是麦克风坏，而是**没人往里灌音频**。
   → 正确做法：主机侧用 `tools\feed_audio.py` 往 `VoiceMeeter Input` 播 wav，
   VAIO 驱动**不需要 VoiceMeeter 程序在跑**就会自己环回到 `VoiceMeeter Output`
   （自检：峰值 0.609248 活跃比例 0.97），VM 那边就"有人在说话"了；零副作用
   （不改默认设备、不从扬声器出声）。详见施工方案 §26。
10. **英文版 Windows 控制台代码页 437**，中文全变 `?` —— 本轮已修（非中文系统自动输出英文，
    见 §3 与本文件 §4）。但**中文系统上仍有少量符号会变 `?`**：`✗`（U+2717）在 GBK 里编不出来，
    `paths` 的"凭据可读性 = ✗ …"在纯 GBK 控制台会显示成 `?`（无伤大雅，暂时不动）。

**本轮新踩的（11~18）**

11. **`.cmd` 从交互式提示符里执行 = 在同一个 cmd 进程里跑**。
    脚本里的 `set VOICEUNLOCK_LANG=zh` 会**留在那个 shell 的环境里**，污染下一次"自动探测"的
    测试 —— 我因此得到过一个假的失败结论（英文 VM 上"自动探测成中文"）。
    → 取证脚本第一行先 `set VOICEUNLOCK_LANG=`；量事实（`logs\guest\lang.txt`）再下结论。
12. **cmd 的 `( ... )` 块里不能出现未转义的 `)`**。`powershell -Command "(Get-UICulture).Name"`
    那个 `)` 会提前关掉块，**重定向被静默吞掉** —— 第一次英文取证只存到后面 `>>` 的部分。
    → 块内别放带括号的字符串（或写 `^)`）；实测脚本 `staging\e2.cmd` 已改成无块写法。
13. **冻结版不吃 `PYTHONIOENCODING`**（PyInstaller 的 exe 走控制台代码页 GBK），
    源码版是 UTF-8 → 拿冻结版与源码版逐行比对时必须**各自按自己的编码解码**，
    否则整个 [E] 全是假差异（看起来像"中文输出全变了"）。
14. **审计工具自己的输出不能被自己翻译**：`i18n_audit.py` 必须在 import `src.i18n` **之前**
    把 `VOICEUNLOCK_LANG=zh` 设好，否则报告被翻一遍，满屏"残留中文"的幻觉。
15. **通用模板会整行吃掉别人的消息**：目录里有一条 `"%s（%s）"`（来自 `enroll.py`），
    英文模式下任何 `xxx（yyy）` 形状的行都被它匹配：
    `  --device DEVICE    只登记指定设备（序号或 endpoint id）` 变成
    `... 只登记指定设备 (序号或 endpoint id)`（中文没翻、只有括号变 ASCII）。
    → `_too_generic()`：字面部分不足 2 字符或不含汉字的模板**禁止做整行匹配**，
    降级为"只匹配值"（`_VPATTERNS`）。
16. **模板必须按中文长度降序试**：`"声纹档案      = %s"`（路径）会吞掉
    `"声纹档案      = %d 条"`（条数），英文里漏出 `11 条`。
17. **占位符正则要用 `(.*?)` 不能用 `(.+?)`**：值可能是**空串**
    （`print("凭据文件      = %s %s" % (path, "" if exists else "(还没保存)"))`）——
    一旦保存过凭据，那一行就匹配不上、原样露出中文（只有"没保存过凭据"的机器上是好的）。
18. **翻译的覆盖面不止 print 的第一个参数**：中文常常是**值**
    （`print("状态 = %s" % ("是" if x else "否"))`、`st.info()`、异常文本）。
    只扫第一个参数会留下 `Credential Providers : 缺失`、`Logon autostart = （未登记）`。
    → 扫描器两遍走：第二遍捞**所有中文字面量**（排除文档字符串与日志回调实参）。
19. **PowerShell 里 `$home` 就是只读的 `$HOME`**（变量名不区分大小写）。
    想设临时 HOME 做烟测时写 `$home='<临时目录>'` 只会报 "Cannot overwrite variable HOME"，
    而 `$env:VOICEUNLOCK_HOME=$home` 拿到的仍是 `C:\Users\<用户>` ——
    agent 于是往**用户 profile 根目录**写了 `logs\agent.log`（本轮真踩，
    已删除该目录并核对 profile 根无残留）。用 `$tmpHome=$null; $tmpHome='...'` 之类
    不撞名的变量名。
20. **`(Get-UICulture).Name` 之类的 ( ) 已在坑 12 说过；这里是它的镜像**：
    在 guest 里跑 `.cmd` 时，`set VAR=` 必须在**脚本开头**先清一次 ——
    交互式提示符里跑的 .cmd 与 shell 共用环境（见坑 11）。
21. **锁屏后 40~60 秒屏幕会自己黑**（Windows 的"控制台锁定显示关闭超时"）。
    VNC 截到黑屏帧（约 2.4KB）**不等于解锁成功** —— 按一下键会回到锁屏。
    所以"是否真的解锁"必须用**三条证据交叉**判断，别只看一帧：
    ① 帧大小（桌面 ~110KB / 锁屏 ~1.19MB / 黑屏 ~2.4KB）；
    ② `agent.log` 里有没有"电平触发 → 常驻判定 → 通过"；
    ③ 安全日志 4801（Workstation Unlocked）/ 4624 里 `Logon Process: User32`。
    本轮就吃过这个亏：t=42s 的黑屏帧一度被当成"解锁成功"。
22. **VM 里可能同时跑着两个 agent**（一个手工 `start` 的 + 一个登录自启的）。
    做锁屏测试前先 `tasklist` 确认只有一个；本轮清理时杀掉的是 PID 5872 与 604。
23. **★★ agent 必须以"最高权限"运行，否则整个产品形同不存在**（2026-10-03 晚，
    另一台机器上实测出来的产品缺陷）：
    管道服务端要读**客户端进程的映像名**做对端校验，而锁屏客户端是 `LogonUI.exe`
    （SYSTEM 身份）。**普通权限**下 `OpenProcess` 被拒（err=5）→ 映像取不到 →
    `base=""` → 判 `peer-not-allowed` → **所有锁屏请求都被回绝**。
    症状极具误导性：`agent.log` 里几百行 `拒绝对端（映像='?'）`，
    而 `收到请求` 一条都没有；用户看到的是"声纹识别不灵"。
    → 修法：登录自启必须是**计划任务 + `RunLevel=HighestAvailable`**（`src/install.py`），
    不是 `HKCU\...\Run`。**在 VM 里手工 start 出来的 agent 往往是从提权命令行起的，
    所以"手工测过能用"根本证明不了自启路径可用** —— 这条最容易被骗过去。
24. **`.cmd` 测试脚本会被 Windows Update 打断**：VM 自己重启装更新时，
    VNC 会话、HTTP 桥、正在跑的脚本全断，表现为"命令没反应/上传没出现"。
    先抓一张屏幕（`vnc_ctl.py capture`）看清楚是不是"Working on updates"，
    别误判成脚本写错了。
25. **VNC 截图的"帧体积"不能单独当锁屏判据**：同一个 VM，桌面暗色壁纸约 110 KB、
    Spotlight 锁屏照片约 1.2 MB —— 但桌面换成照片壁纸后也到了 1.2 MB。
    **判"是否锁屏"要 OCR 看内容**（锁屏有日期/时间大字），体积只能当辅助。
26. **`[UninstallRun]` 清不掉计划任务**：`[Registry]` 的 `uninsdeletevalue` 只管注册表值。
    本轮实测：卸载后 `VoiceUnlockAgent` 任务仍然 `Status=Ready`，
    每次登录都会去启动一个已被删掉的 exe。→ 必须显式在 `[UninstallRun]` 里调
    `uninstall-autostart`（本轮已修）。**凡是安装时创建的系统级对象，都要在
    [UninstallRun] 里配一条删除命令**，并真的卸载一次核对。
27. **`.cmd` 里绝不能出现中文**（坑 3 的复现）：本轮我在 `t2.cmd` 里用
    `powershell -Command "... '收到请求' ..."` 做正则计数，写 ASCII 时中文被抹成 `????`，
    PowerShell 直接报 `Quantifier {x,y} following nothing`。
    结论：**统计/断言这类活挪到主机侧做**（把原始日志传回来，在主机上数）。
28. **★★ 想查"exe 里有没有某段源码字符串"，按字节 grep 是**查不到**的**：
    PyInstaller 的 PYZ 是**压缩**的 —— 实测连 `peer-not-allowed` 这种源码字面量
    都不在 exe 的可见字节里（搜出来是 False）。所以"我 grep 过 exe，干净"这种结论
    是假的安心。正确做法（已封装进 `tools\scan_package_sensitive.py`）：
    用 `PyInstaller.archive.readers.CArchiveReader` 把 `PYZ.pyz` 取出来 →
    `ZlibArchiveReader` 解每个模块 → 反序列化成 code →
    **递归遍历 `co_consts`**（字符串常量才是真正随包出去的东西）。
29. **注释不进包，docstring 进包**（`optimize=0`）：
    清隐私时不要只搜文件全文 —— `#` 注释里的东西编译后会被丢掉，
    而 **模块/函数 docstring 会**原样进 .pyc。
    本轮真实命中：`src\config.py` / `src\enroll.py` / `src\selftest_decision.py`
    的 docstring 里写着 `<项目目录>`（这三个模块都在包内）；
    `src\selftest.py` 里还写死了 `r"<上游模型目录>"`（字符串字面量）。
    都改成"从项目根目录推导 / 环境变量覆盖"了 —— 顺带修好了它在**别人机器上必然失败**的问题。
    另外：`src\selftest.py` 当前**没有**被打进包（`pyi-archive_viewer -r -l` 里没有它），
    但仍然是按"会进包"的标准清的（免得哪天有人把它加进 hiddenimports）。

---

## 7. 未完成

1. **真机声学端到端（目标机器）**：需要一台**有真麦克风**的机器，锁屏说一句话验证自动解锁。
   作者本机已成功（0.57 → 自动解锁两次），"其他电脑"上还没验过。
2. **VM 声学链路的最后一环**（本轮新缩小）：假麦克风通路、guest 采集（peak 0.223/活跃 0.61）、
   用它登记建档（self 1.000 ≥ 阈值 0.90）、凭据（交互式登录成功）、CP 在锁屏上持续 ping agent
   —— **都已实测通过**。**只剩"锁屏说话 → 自动解锁"没成功**：锁屏那 95 秒里 agent 只进过一次
   电平触发窗口（报 `[listen] 5 秒内没检测到说话声（共 12 轮）`），说明它**根本没开麦**。
   嫌疑收敛到两处（`_resident_loop` 开麦前的两个前置条件，不满足就 `sleep` 空转且不打日志）：
   ① `session.is_locked()` 在 VM 安全桌面下是否误判；② 安全桌面下音频设备枚举是否为空。
   诊断脚本已写好：`staging\mon.cmd`（独立窗口跑，锁屏时照样运行，每 3 秒记
   `agent status` 的 lock 行 + `enroll --list` 设备数，然后回传）—— 本轮被打断，未取到结果。
   ⚠ 做这类测试前先确认 guest 里**只有一个** agent 在跑（本轮同时起过两个：手工 + 自启）。
3. 收尾清理：`packaging\VoiceUnlock.spec` 是 PyInstaller 自动生成的**过时残留**
   （相对路径 + `upx=True`，与"UPX 一律关掉"冲突）；真正打包用 `packaging\vu.spec`。
   建议删掉，免得有人误用。
4. 小瑕疵（都不影响功能）：
   * 装到英文系统后 `dir` 里显示 `????.txt`（`使用说明.txt` 的中文文件名，cp437 显示不出来）；
     Explorer 与 Notepad 里正常，改不改都行。
   * GBK 控制台上 `✗` 显示成 `?`（见坑 10）。
5. **VM 验证：主要部分已完成，最后一步按用户要求跳过**（2026-10-03 晚）
   已完成（证据见 §3 表格）：新安装包装完 → **任务由安装器建出**、`schtasks /Query /XML`
   设置全对 → 旧 HKCU Run 值不再写入 → **锁屏 30 秒 `收到请求 87 次 / 拒绝对端 0 次`**
   （对端 = `C:\Windows\System32\LogonUI.exe`）→ 证明了 §27.1 那个缺陷确实被修掉。
   自查还发现并修掉一个**自己引入的缺陷**：旧包卸载后计划任务仍在，
   已在 `[UninstallRun]` 加 `uninstall-autostart` 并重打包。
   **被跳过的**：用修复后的安装包再跑一遍"装→确认任务在→卸→确认任务没了"。
   代码层面这条链没有疑点（`uninstall-autostart` 已在 VM 与本机各清理过一次），
   但**没有实测过 Inno 卸载器调用它的那一次**。想补的话：
   `staging\i2.cmd`（装）→ `staging\c1.cmd`（卸 + 残留报告，里面已含
   `schtasks /Query /TN VoiceUnlockAgent` 检查）。

---

## 8. 用户本机当前状态（应保持）

- 声纹 CP：**未注册**（本轮又用 `cp-status` 独立核对：两个键都是 missing）
- 账户：**无密码**（空密码登录返回 `1327`，与本会话最初状态一致；本轮没碰账户）
- 登录自启：**已移除**（`paths` 显示 `autostart = (not registered)`）；agent：**未运行**
- 程序文件仍在 `C:\Program Files\VoiceUnlock\`（装着的但 CP 未注册，处于惰性状态）
- 数据目录（本轮核对过）：
  * 生产 HOME `%LOCALAPPDATA%\VoiceUnlock\` 只有 `profiles.json`（2 条档案）、`state.json`、`logs\`；
    **没有 `credential.bin`** —— 所以 `agent status` 显示"未保存任何凭据"。
  * 项目目录 `<项目目录>\` 里另有上一轮调试留下的 `credential.bin`（553 字节，
    本会话解不开是正常的）与 `profiles.json`；开发态 HOME 就是这个目录。
  * **不要执行 `agent clear-password` / `panic`**，除非用户明确要求（会删数据 / 改注册表）。
- 本轮在本机跑过的命令：**只读**命令（`paths` / `parity` / `cp-status` / `agent status` /
  `enroll --list` / `selftest-decision`）、打包，以及一次**冻结版 agent 启动烟测**
  （8 秒后已杀进程，验证 i18n 改动没让常驻进程起不来）。烟测本想把 HOME 指到临时目录，
  但 `$home` 撞上了 PowerShell 只读变量 `$HOME`（见坑 19），于是它按默认 HOME 运行、
  在 `C:\Users\<你>\logs\agent.log` 写了一行启动日志 ——
  **该 `logs` 目录已删除，profile 根目录已核对无残留**；注册表、账户、自启都没被改动。
- **2026-10-03 晚另有一处"改了又还原"**：为验证新的自启方式，在本机以管理员身份跑了
  冻结版 `install-autostart` → 创建计划任务 `VoiceUnlockAgent` → 回读校验全部设置 →
  紧接着 `uninstall-autostart` 删掉并回读确认不存在。**现在本机没有这个任务**
  （想确认：`schtasks /Query /TN VoiceUnlockAgent` 应当报"找不到文件"）。
- VM 现状（最后一次操作被打断）：里面**装着新包**、并且**可能还留着计划任务
  `VoiceUnlockAgent`**（旧包卸载时留下的那个）。想清干净就在 guest 里跑
  `staging\c1.cmd`（先卸载再报残留）。
- 验收基线目录改名：`dist\_baseline_last_accepted\`（[E] 用它做中文零回归对比；
  语义是**上次验收通过的构建**，不要随手覆盖）。旧的 `dist\_baseline_pre_i18n\` 可以删。
