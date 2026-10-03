; installer.iss -- 声纹解锁 安装包（Inno Setup 6.3+）
;
; 设计要点（每一条都对应一个踩过的坑）：
;   1) 装完要跑三件【安装期才能做对】的事，且顺序有讲究：
;        a. cp-install        注册锁屏凭据提供者（写 HKLM，需要管理员）
;        b. install-autostart 登记登录自启（必须写【原始用户】的 HKCU）
;        c. setup-password    保存登录凭据（必须【原始用户 + 有可见控制台】，
;                             因为要 getpass 读键盘，且 DPAPI 用户作用域只在
;                             该用户的登录会话里能解开）
;      所以 b/c 都用 runasoriginaluser，a 用提权身份。
;   2) 卸载必须【先注销 CP，再删文件】。如果 DLL 被删了但注册还在，锁屏加载
;      凭据提供者会失败 —— 那等于"装了个软件把登录界面搞坏"。所以走 [UninstallRun]，
;      Inno 会在这批命令跑完之后才删文件。
;   3) 升级/覆盖安装前先停掉常驻 agent，否则 exe 被占用，文件复制会失败。
;   4) 卸载时删掉用户数据目录：里面是 DPAPI 密文口令和声纹（生物特征），
;      卸载还留着是不负责任的。

#define AppName "声纹解锁"
#define AppNameEn "VoiceUnlock"
#define AppVer "0.1.0"
#define AppExe "VoiceUnlock.exe"
#define AppPublisher "VoiceUnlock"

[Setup]
AppId={{8F3A2C41-5B7D-4E96-9A1C-2D6E4F8B0A73}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName} {#AppVer}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppNameEn}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 注册 CP 写 HKLM，必须提权
PrivilegesRequired=admin
; 只支持 64 位：CP DLL 要装进 64 位的 LogonUI，Python 侧也是 x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 目标 Win10+（不要写死小版本，否则会把用户挡在门外；运行时再给明确提示）
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename={#AppNameEn}-Setup-{#AppVer}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName}
; 我们自己显式停 agent，不用 Restart Manager 弹窗，免得用户困惑
CloseApplications=no
VersionInfoVersion={#AppVer}
VersionInfoDescription={#AppName} 安装程序

[Languages]
; 第一个是默认语言。
; ★ 这个 .isl 由 build_installer.ps1 复制进 Inno 的 Languages 目录 ——
;   Inno 对内建语言用 compiler: 前缀，自定义语言包放那儿最确定。
; ★ 本文件与 使用说明.txt 都必须带 UTF-8 BOM：Inno 靠 BOM 判定编码，
;   没有 BOM 会按 ANSI(cp936) 读，中文全成乱码（和 .cmd 那个坑同源）。
Name: "cn"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
; ★ 默认是【未勾选】的，推荐项必须显式 checkedonce
Name: "autostart"; Description: "登录时自动启动声纹监听（推荐）"; GroupDescription: "附加选项："; Flags: checkedonce
Name: "setupcred"; Description: "安装完成后立即设置登录凭据（推荐）"; GroupDescription: "附加选项："; Flags: checkedonce
Name: "enroll"; Description: "安装完成后立即登记声纹（推荐，需要麦克风）"; GroupDescription: "附加选项："; Flags: checkedonce
; ★ 这里【曾经】有一个"同时安装 Shittim Logon 主题登录界面"的勾选项，2026-10-03 按用户
;   要求删掉。原因有两条，都成立：
;     1) 它的授权（LICENSE v1.0 §3(a)(d)）禁止再分发与二次打包，随包素材的权利也在
;        原权利人手上 —— 我们本来就不含它的任何文件，只"代跑一下它的安装程序"，
;        这种半吊子集成对用户没有实际好处，却让安装包多背一层解释成本；
;     2) 我们的凭据提供者在没有有效票据时枚举 0 个凭据，**锁屏上不画任何东西**，
;        所以与主题登录界面之间**不需要任何适配**，也就没有必要在安装器里耦合它。
;   要装主题登录界面的话，用户自行运行它的安装程序即可，与本产品互不影响。

[Files]
; PyInstaller one-folder 的全部产物（含 _internal 里的模型与原生 DLL）
Source: "..\dist\{#AppNameEn}\*"; DestDir: "{app}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion
; 锁屏凭据提供者 DLL
Source: "..\provider\build\Release\VoiceCredentialProvider.dll"; DestDir: "{app}\provider"; \
    Flags: ignoreversion
Source: "使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme
; ★ 第三方许可（Apache-2.0 §4(a)(b)(c) 的义务）：
;   随包的 campplus.onnx 是 CAM++ 权重的格式转换件，上游模型卡声明 Apache-2.0。
;   §4(a) 给接收者一份许可正文（上游仓库本身不发 LICENSE，所以由我们补上）；
;   §4(b) 声明修改（PyTorch→ONNX，权重未改）；§4(c) 保留归属（阿里/DAMO + 模型名）。
;   见 packaging\THIRD-PARTY-NOTICES.txt 里的逐条说明与证据 URL。
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE-Apache-2.0.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\查看状态（排障）"; Filename: "{app}\{#AppExe}"; Parameters: "paths"
Name: "{group}\设置登录凭据"; Filename: "{app}\{#AppExe}"; Parameters: "setup-password"
Name: "{group}\登记声纹"; Filename: "{app}\{#AppExe}"; Parameters: "enroll"
; ★ 紧急关闭：放桌面，一键做完注销 CP + 停 agent + 停自启（会自动弹 UAC）。
;   声纹解锁是"接管锁屏"的东西，用户必须有一个不用记命令就能关掉它的出口。
Name: "{group}\★ 紧急关闭声纹解锁"; Filename: "{app}\{#AppExe}"; Parameters: "panic"
Name: "{commondesktop}\★ 紧急关闭声纹解锁"; Filename: "{app}\{#AppExe}"; Parameters: "panic"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"

[Run]
; a) 注册 CP —— 提权身份（写 HKLM）。runhidden：不弹黑窗口
Filename: "{app}\{#AppExe}"; Parameters: "cp-install"; \
    Flags: runhidden waituntilterminated; \
    StatusMsg: "正在注册锁屏凭据提供者..."

; b) 登录自启 —— ★ 现在是【计划任务 + 最高权限】，所以**不能再 runasoriginaluser**：
;    注册 HighestAvailable 的任务必须管理员身份。任务本身仍然以【原始用户】的
;    交互式会话运行（XML 里的 UserId 就是当前用户名），DPAPI 用户作用域不受影响。
;    ★ 为什么非要最高权限：agent 的命名管道有对端校验，要读 SYSTEM 进程
;      （LogonUI.exe）的映像名；普通权限下 OpenProcess 会被拒（err=5）→
;      所有锁屏请求都会被判 peer-not-allowed（另一台机器上实测：日志几百行
;      "拒绝对端（映像='?'）"，收到的请求一条都没有）。
Filename: "{app}\{#AppExe}"; Parameters: "install-autostart"; \
    Flags: runhidden waituntilterminated; Tasks: autostart; \
    StatusMsg: "正在登记登录自启（计划任务，最高权限）..."

; c) 首次设置凭据 —— 原始用户 + 可见控制台（要 getpass），不静默
Filename: "{app}\{#AppExe}"; Parameters: "setup-password"; \
    Flags: postinstall runasoriginaluser; Tasks: setupcred; \
    Description: "设置声纹解锁用的登录凭据（会要求你输入 Windows 登录密码）"

; d) 登记声纹 —— 档案库为空时说什么都会被拒，所以这一步是必须的
;    ★ 用不带参数的 enroll：一次连续录 8 秒，屏幕上会打出【要朗读的句子】。
;      不用 --all（那会对每台可信设备各录 3 遍，本机 5 个设备就是 15 遍，
;      其中还有虚拟/未接入的端点，用户会被折腾得莫名其妙）。
Filename: "{app}\{#AppExe}"; Parameters: "enroll"; \
    Flags: postinstall runasoriginaluser; Tasks: enroll; \
    Description: "登记声纹（一次 8 秒：照屏幕上给出的句子念一遍）"

[UninstallRun]
; ★ 顺序不能变：先注销 CP（必须在 DLL 被删之前 —— Inno 会等这批命令跑完才删文件），
;   再停 agent，最后**删掉计划任务**。
; ★ 注意 runasoriginaluser 在 [UninstallRun] 里【不支持】（只在 [Run] 里有效），
;   所以旧的 HKCU Run 值由下面的 [Registry] 段清理；而**计划任务只能靠这里清**
;   （注册表段管不着它）。漏了这一步的后果本轮实测到了：卸载后任务还挂着
;   `Status=Ready`，每次登录都会去启动一个已经被删掉的 exe。
Filename: "{app}\{#AppExe}"; Parameters: "cp-uninstall"; \
    Flags: runhidden waituntilterminated; RunOnceId: "UnregCP"
Filename: "{app}\{#AppExe}"; Parameters: "agent-stop"; \
    Flags: runhidden waituntilterminated; RunOnceId: "StopAgent"
Filename: "{app}\{#AppExe}"; Parameters: "uninstall-autostart"; \
    Flags: runhidden waituntilterminated; RunOnceId: "UnregAutostart"

[Registry]
; 登录自启项的卸载清理（**迁移用途**）。
; ★ 新版把登录自启改成了【计划任务 + 最高权限】（原因见 [Run] b 的注释），
;   这个 HKCU Run 值不再由我们写入。但这条留着有用：
;     1) 老版本装过的机器上可能残留它 —— install-autostart 会主动删掉它
;        （两个实例会抢同一个管道名）；
;     2) 万一有人手工写回去，卸载时也要清干净。
;   ValueType: none + uninsdeletevalue 是 Inno 的惯用法：安装时不写值，
;   只登记"卸载时把这个值删掉"。
Root: HKCU; Subkey: "SOFTWARE\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: none; ValueName: "VoiceUnlockAgent"; Flags: dontcreatekey uninsdeletevalue

[UninstallDelete]
; 用户数据目录：DPAPI 密文口令 + 声纹（生物特征）。卸载不该留下这两样。
Type: filesandordirs; Name: "{localappdata}\{#AppNameEn}"

[Code]
// 注意：这里**曾经**有一段 Shittim Logon 集成代码（找它的 install.exe 并代跑）。
// 2026-10-03 按用户要求连同 [Tasks] 的勾选项一起删掉了 —— 理由见 [Tasks] 段注释。
// 现在安装包与主题登录界面**零耦合**：不检测、不调用、不分发。

// 覆盖/升级安装前先停掉常驻 agent，否则 exe 被占用会导致复制失败。
// （taskkill 找不到进程会返回非 0，忽略即可，不要当成错误弹窗。）
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  R: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im VoiceUnlockAgent.exe',
       '', SW_HIDE, ewWaitUntilTerminated, R);
  Result := '';
end;

// 装完给一句人话总结，免得用户不知道下一步
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if not WizardSilent then
      MsgBox('安装完成。' + #13#10#13#10 +
             '用法：按 Win+L 锁屏，然后在锁屏界面正常说一句 2~5 秒的话，'
             + '2~10 秒后会自动进入桌面。' + #13#10#13#10 +
             '注意：锁屏界面上不会出现任何提示（我们的设计就是界面上不出现任何东西）。'
             + #13#10#13#10 +
             '排障：开始菜单 →「查看状态（排障）」，或双击安装目录下的 '
             + 'VoiceUnlock.exe 加参数 paths。',
             mbInformation, MB_OK);
  end;
end;
