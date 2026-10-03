"""install.py -- 安装/卸载动作（注册 CP、登录自启、清理）

为什么把这些放进程序里、而不是写成 .ps1 或只靠安装包脚本：
    1. **可核对**：写完注册表立刻读回来，给出明确的 OK/FAIL。regsvr32 返回成功
       只说明 DllRegisterServer 被调用了，不代表注册表真的写对了（踩过）。
    2. **不依赖 PowerShell 执行策略**：目标机器可能禁止脚本运行。
    3. **卸载顺序可控**：卸载必须【先注销 CP 再删 DLL】。如果 DLL 没了但注册还在，
       锁屏加载凭据提供者会失败 —— 那是"装了个软件把登录界面搞坏"级别的后果。

CP 注册就是两处键（与 provider/src/dll.cpp 的 DllRegisterServer 一一对应）：
    HKCR\\CLSID\\{GUID}\\InprocServer32            (默认)=DLL 路径, ThreadingModel=Apartment
    HKLM\\...\\Authentication\\Credential Providers\\{GUID}   (默认)=显示名
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import winreg
from xml.sax.saxutils import escape as sax_escape

CLSID = "{7C4E1A62-9D3B-4F58-A1E7-5B2C8D94F013}"
CP_DISPLAY = "Voice Unlock Credential Provider"
CP_DLL_NAME = "VoiceCredentialProvider.dll"
AGENT_EXE_NAME = "VoiceUnlockAgent.exe"
RUN_VALUE = "VoiceUnlockAgent"          # 仅用于迁移：旧的 HKCU Run 自启值

CLSID_KEY = r"SOFTWARE\Classes\CLSID\%s" % CLSID
INPROC_KEY = CLSID_KEY + r"\InprocServer32"
CP_KEY = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication"
          r"\Credential Providers\%s" % CLSID)
RUN_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"

# 这是"运行本程序的那个 exe 所在目录"，安装后就是 {app}
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable))
           if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------- 路径

def cp_dll_path() -> str:
    """CP DLL 的位置。安装后 = {app}\\provider\\VoiceCredentialProvider.dll"""
    cands = [
        os.path.join(APP_DIR, "provider", CP_DLL_NAME),
        os.path.join(APP_DIR, CP_DLL_NAME),
        os.path.join(APP_DIR, "provider", "build", "Release", CP_DLL_NAME),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[0]


def agent_exe_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.join(APP_DIR, AGENT_EXE_NAME)
    return ""


def _admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------- CP 注册

def _read(path: str, name: str = ""):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            v, _t = winreg.QueryValueEx(k, name)
            return v
    except OSError:
        return None


def cp_status(verbose: bool = True) -> bool:
    dll = _read(INPROC_KEY)
    tmodel = _read(INPROC_KEY, "ThreadingModel")
    disp = _read(CP_KEY)
    ok = bool(dll) and bool(disp)
    if verbose:
        print("--- CP 注册状态 ---")
        print("  1) CLSID\\InprocServer32 : %s" % ("存在" if dll else "缺失"))
        if dll:
            print("       DLL      = %s" % dll)
            print("       线程模型 = %s" % tmodel)
            print("       DLL 在盘上 %s" % ("存在" if os.path.isfile(str(dll)) else "【缺失!】"))
        print("  2) Credential Providers : %s" % ("存在" if disp else "缺失"))
        if disp:
            print("       名称     = %s" % disp)
        print("  => %s" % ("两处齐全，锁屏上应当能出现"
                           if ok else "注册不完整，锁屏上不会出现"))
    return ok


def cp_install(quiet: bool = False) -> int:
    if not _admin():
        print("[FAIL] 注册 CP 要写 HKLM，必须以管理员身份运行")
        return 5
    dll = cp_dll_path()
    if not os.path.isfile(dll):
        print("[FAIL] 找不到 CP DLL：%s" % dll)
        return 2
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, INPROC_KEY, 0,
                                winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, dll)
            winreg.SetValueEx(k, "ThreadingModel", 0, winreg.REG_SZ, "Apartment")
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, CP_KEY, 0,
                                winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, CP_DISPLAY)
    except OSError as e:
        print("[FAIL] 写注册表失败：%r" % (e,))
        return 1
    if not quiet:
        print("DLL = %s" % dll)
    ok = cp_status(verbose=not quiet)
    return 0 if ok else 1


def cp_uninstall(quiet: bool = False) -> int:
    """注销 CP。卸载时【必须】在删除 DLL 之前调用。"""
    if not _admin():
        print("[FAIL] 注销 CP 要写 HKLM，必须以管理员身份运行")
        return 5
    for path in (INPROC_KEY, CLSID_KEY, CP_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, path)
            if not quiet:
                print("  已删除 HKLM\\%s" % path)
        except FileNotFoundError:
            pass
        except OSError:
            # 非空子键等：递归清掉
            _delete_tree(winreg.HKEY_LOCAL_MACHINE, path)
    ok = cp_status(verbose=not quiet)
    return 0 if not ok else 1


def _delete_tree(root, path: str) -> None:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(root, path + "\\" + sub)
    except OSError:
        return
    try:
        winreg.DeleteKey(root, path)
    except OSError:
        pass


# ---------------------------------------------------------------- 登录自启（计划任务）
#
# ★ 这里曾经是"HKCU\...\Run 写一个值"，后来在**另一台机器**上被实测推翻 ——
#   那是本项目最重要的一次产品修复：
#
#     现象：装完、锁屏、说声纹 → 永远不解锁。agent.log 里几百行
#           `[pipe] 拒绝对端（映像='?'，期望 ('logonui.exe',)）`，
#           而 `[agent] 收到请求` 一条都没有。
#     原因：管道服务端的对端校验要读**客户端进程的映像名**；锁屏那边是 LogonUI，
#           它是 SYSTEM 进程。**普通权限**的 agent 调 OpenProcess 会被拒（err=5），
#           取不到映像 → 判 peer-not-allowed → 把锁屏请求全部拒掉。
#           同一台机器上以管理员身份跑同一个 exe，就能读到
#           `C:\Windows\System32\LogonUI.exe`，校验通过。
#     结论：**agent 必须以"最高权限"运行**，而 HKCU Run 给不了这个权限。
#           → 改成计划任务：登录触发 + RunLevel=HighestAvailable（不弹 UAC）。
#
#   计划任务的其它几个设置也不是随手加的，各有原因：
#     * ExecutionTimeLimit=PT0S：默认是 72 小时，常驻 agent 会被系统掐掉。
#     * StopIfGoingOnBatteries=false / DisallowStartIfOnBatteries=false：
#       笔记本拔电源就停服，等于解锁功能消失（笔记本上很现实）。
#     * MultipleInstancesPolicy=IgnoreNew：两个实例会抢同一个管道名，
#       锁屏请求会随机落到其中一个（踩过：手工 start 一个 + 自启再起一个）。
#     * RestartOnFailure 3 次 / 1 分钟：agent 崩了要自己回来，否则用户只看到"不灵"。
TASK_NAME = "VoiceUnlockAgent"

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>VoiceUnlock resident agent: listens for the voiceprint while the workstation is locked. Runs with highest available privileges because its named-pipe peer check must read a SYSTEM process image (LogonUI.exe).</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>%(user)s</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%(user)s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(exe)s</Command>
      <Arguments>run</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _hidden(argv):
    """跑一个短命命令，不弹黑窗口。"""
    return subprocess.run(argv, capture_output=True, text=True, timeout=40,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def task_exists() -> bool:
    try:
        return _hidden(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0
    except Exception:
        return False


def task_delete(quiet: bool = True) -> bool:
    try:
        p = _hidden(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        ok = p.returncode == 0
    except Exception:
        ok = False
    if ok and not quiet:
        print("已删除计划任务 %s" % TASK_NAME)
    return ok


def _run_value():
    """旧的 HKCU Run 自启值（迁移用；取不到返回 None）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _t = winreg.QueryValueEx(k, RUN_VALUE)
            return v
    except OSError:
        return None


def _run_value_delete() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
        return True
    except OSError:
        return False


def task_install(user: str = "") -> tuple:
    """注册计划任务（最高权限）。返回 (ok, 说明)。"""
    exe = agent_exe_path()
    if not exe or not os.path.isfile(exe):
        return False, "找不到 agent exe：%r" % exe
    user = user or os.environ.get("USERNAME") or ""
    if not user:
        return False, "拿不到用户名"
    xml = TASK_XML % {"user": sax_escape(user), "exe": sax_escape(exe)}
    path = os.path.join(tempfile.gettempdir(), "voiceunlock_agent_task.xml")
    try:
        # schtasks 读 XML 认 UTF-16（写成 UTF-8 会报"XML 格式不正确"）
        with open(path, "w", encoding="utf-16") as f:
            f.write(xml)
        p = _hidden(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"])
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        return p.returncode == 0, out
    except Exception as e:
        return False, repr(e)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def autostart_install(quiet: bool = False, user: str = "") -> int:
    exe = agent_exe_path()
    if not exe:
        print("[FAIL] 非打包形态，拒绝写自启（开发时请手工启动 agent）")
        return 2
    if not os.path.isfile(exe):
        print("[FAIL] 找不到 agent：%s" % exe)
        return 2

    # 先清掉旧的 HKCU Run：它会在同样的登录时刻再起一个**普通权限**实例，
    # 两个实例抢同一个管道名（而且那个普通权限实例做不了对端校验）。
    if _run_value():
        _run_value_delete()
        if not quiet:
            print("已删除旧的 HKCU Run 自启项（改用计划任务，见下）")

    ok, why = task_install(user)
    if not ok and not _admin():
        # 注册"最高权限"的任务需要管理员。复用 panic 的自提权：弹一次 UAC，
        # 在提权后的进程里再跑一次自己。
        if relaunch_elevated(["install-autostart"] + (["--user", user] if user else [])):
            if not quiet:
                print("注册计划任务需要管理员权限，已请求提权，"
                      "请在弹出的窗口里点『是』。")
            return 0
        print("[FAIL] 注册计划任务需要管理员权限，且提权被拒绝或被取消")
        return 5
    if not ok:
        print("[FAIL] 注册计划任务失败：%s" % why[:400])
        return 1
    if not task_exists():
        print("[FAIL] 计划任务注册后却查不到，别信这个结果")
        return 1
    if not quiet:
        print("已登记登录自启：计划任务 %s（登录时启动，最高权限）" % TASK_NAME)
        print("★ 为什么必须【计划任务 + 最高权限】：agent 的管道要对端校验，"
              "需要能读 SYSTEM 进程（LogonUI）的映像名；")
        print("  普通权限下 OpenProcess 会被拒（err=5）→ 取不到映像 → "
              "所有锁屏请求都会被判 peer-not-allowed。")
        print("  代价：它跑在当前用户的登录会话里（DPAPI 用户作用域要求如此），"
              "但带管理员令牌。")
    return 0


def autostart_uninstall(quiet: bool = False) -> int:
    rc = 0
    if task_exists():
        if not task_delete(quiet=quiet):
            print("[FAIL] 删除计划任务失败（可能需要管理员权限）")
            rc = 1
    elif not quiet:
        print("（本来就没有计划任务）")
    if _run_value():
        _run_value_delete()
        if not quiet:
            print("已移除旧的 HKCU Run 自启项")
    return rc


def autostart_status(verbose: bool = True) -> str:
    has_task = task_exists()
    legacy = _run_value()
    if verbose:
        print("登录自启    = %s"
              % ("计划任务 %s（最高权限）" % TASK_NAME if has_task else "（未登记）"))
        if legacy:
            print("              ⚠ 还残留旧的 HKCU Run 项：%s" % legacy)
            print("                它会再起一个普通权限实例抢管道 —— "
                  "跑 install-autostart 会把它清掉")
    return TASK_NAME if has_task else (legacy or "")


# ---------------------------------------------------------------- 紧急关闭

def relaunch_elevated(argv) -> bool:
    """以管理员身份重新启动自己（弹 UAC）。发起成功返回 True，调用方应立即退出。

    为什么要这个：紧急关闭必须"点一下就行"。要求用户"以管理员身份打开命令行、
    敲 VoiceUnlock.exe cp-uninstall"在紧急时刻是不现实的。
    """
    if not getattr(sys, "frozen", False):
        return False        # 开发形态下 exe 是 python.exe，重新拉起自己没意义
    try:
        import ctypes
        params = subprocess.list2cmdline([str(x) for x in argv])
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1)
        return int(rc) > 32
    except Exception:
        return False


def panic() -> int:
    """一键紧急关闭：注销 CP + 停 agent + 移除自启。

    声纹解锁是"接管锁屏"的东西，所以必须给用户一个【不用打命令、不用记参数】
    就能关掉它的方式。跑完它就等于回到装之前 —— 锁屏就是 Windows 原样的密码框。
    """
    if not _admin():
        print("[!] 需要管理员权限（注销 CP 要写 HKLM）。")
        if relaunch_elevated(["panic"]):
            print("    已请求提权，请在弹出的窗口里点『是』。")
            return 0
        print("    请右键安装目录下的 VoiceUnlock.exe → 以管理员身份运行，"
              "参数填 panic")
        return 5

    print("=== 紧急关闭声纹解锁 ===")
    print("[1/3] 注销锁屏凭据提供者（最关键：这一步做完锁屏就恢复原样）")
    rc = cp_uninstall(quiet=False)
    print("\n[2/3] 停止常驻 agent")
    rc |= agent_stop(quiet=False)
    print("\n[3/3] 移除登录自启")
    rc |= autostart_uninstall(quiet=False)

    print("")
    still = cp_status(verbose=True)
    print("")
    if still:
        print("[!] 凭据提供者似乎还在注册状态 —— 请以管理员身份再跑一次 panic")
        return 1
    print("已关闭。锁屏现在就是 Windows 原来的样子（密码 / PIN 解锁）。")
    print("想恢复声纹解锁：重跑安装包，或依次运行")
    print("    VoiceUnlock.exe cp-install")
    print("    VoiceUnlock.exe install-autostart")
    return 0


# ---------------------------------------------------------------- agent 进程

def agent_running() -> list:
    """返回正在运行的 VoiceUnlockAgent 进程 pid 列表（不依赖 psutil）。"""
    pids = []
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % AGENT_EXE_NAME, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if parts and parts[0].lower() == AGENT_EXE_NAME.lower():
                try:
                    pids.append(int(parts[1]))
                except (IndexError, ValueError):
                    pass
    except Exception:
        pass
    return pids


def agent_stop(quiet: bool = False) -> int:
    pids = agent_running()
    if not pids:
        if not quiet:
            print("agent 没在运行")
        return 0
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    if not quiet:
        print("已停止 agent：%s" % pids)
    return 0
