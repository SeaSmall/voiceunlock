"""vu_cli.py -- 命令行入口的打包入口（**有控制台**）

    VoiceUnlock.exe agent status          看凭据/档案/锁屏状态（排障第一条命令）
    VoiceUnlock.exe agent set-password    加密保存登录密码（不回显）
    VoiceUnlock.exe agent run             前台跑一次常驻（排障用）
    VoiceUnlock.exe enroll --all          登记声纹
    VoiceUnlock.exe enroll --list         列出设备
    VoiceUnlock.exe paths                 打印所有关键路径（支持时最有用）

注意：这里**故意不**在模块顶层 import src.selftest_decision ——
那个模块在 import 时会把 VOICEUNLOCK_HOME 抢成临时目录（它是自测用的），
顶层导入会把整个 CLI 的 HOME 带歪。
"""
from __future__ import annotations

import os
import sys

# PyInstaller one-folder：顶层 import 让打包器看得见这些模块（延迟导入它会漏）
from src import cli                        # noqa: E402
from src.agent import main as agent_main   # noqa: E402
from src.enroll import main as enroll_main  # noqa: E402

HELP = """VoiceUnlock 命令行

  paths                     打印全部关键路径、凭据/档案/CP/自启 状态（排障第一条）
  parity [--ref FILE.npy]    打包一致性自检：冻结版必须与源码版算出一模一样的向量
  setup-password            保存声纹解锁要用的登录凭据（默认不动系统密码）
                            加 --set-windows-password 才同时设置本机登录密码
  agent status              凭据 / 声纹档案 / 锁屏状态一览
  agent run [--once]        前台运行常驻 agent（排障用；正常由自启负责）
  agent clear-password      删除已保存凭据
  enroll --list             列出所有语音输入设备
  enroll --all              登记声纹（对全部可用设备）
  enroll --device <id>      只登记指定设备
  enroll --test             不开管道，直接跑一次判定
  cp-install                注册 Credential Provider（需要管理员）
  cp-uninstall              注销 Credential Provider（需要管理员；卸载必须先做）
  cp-status                 查看 CP 注册状态
  panic                     ★ 一键紧急关闭：注销 CP + 停 agent + 移除自启
                            （会自动请求管理员权限；做完锁屏即恢复原样）
  install-autostart         登记登录自启（计划任务，最高权限；需管理员）
  uninstall-autostart       移除登录自启（计划任务 + 旧的 HKCU Run 项）
  agent-stop                停止正在运行的常驻 agent
  selftest-decision         判定逻辑自测（不需要麦克风）
"""


def cmd_parity(argv) -> int:
    """打包一致性判据：同一段确定性音频，冻结版与源码版必须算出同一个向量。"""
    import numpy as np

    from src.parity import cosine, probe_vector

    ref_path = None
    if argv:
        ref_path = argv[1] if (argv[0] == "--ref" and len(argv) > 1) else argv[0]
    print("计算探针向量（确定性输入，与源码侧同一个函数）...")
    try:
        vec = probe_vector()
    except Exception as e:
        print("[FAIL] 推理跑不起来：%r" % (e,))
        print("       最可能：ONNX 模型没随包走，或 kaldi_native_fbank / onnxruntime 被漏掉")
        return 1
    print("  维度=%d  范数=%.6f" % (vec.size, float(np.linalg.norm(vec))))
    print("  前8位=%s" % np.round(np.asarray(vec[:8]), 6).tolist())
    if not ref_path:
        print("  （未给 --ref：只证明推理能跑通；判等请传 --ref logs\\parity_ref.npy）")
        return 0
    if not os.path.isfile(ref_path):
        print("[FAIL] 参考文件不存在：%s" % ref_path)
        return 1
    c = cosine(vec, np.load(ref_path))
    print("  与参考向量 cos = %.8f" % c)
    if c > 0.99999:
        print("[OK] 冻结版与源码版推理完全一致")
        return 0
    print("[FAIL] 不一致 —— 打包改动了推理（模型版本 / 前端参数 / 原生 DLL 之一）")
    return 1


def cmd_paths() -> int:
    from src import config as C
    from src import sv as S
    from src.dpapi import CredentialStore
    from src.profiles import ProfileStore
    from src.session import is_locked, input_desktop_name

    frozen = bool(getattr(sys, "frozen", False))
    print("打包形态      = %s" % ("是（frozen）" if frozen else "否（源码运行）"))
    print("可执行文件    = %s" % sys.executable)
    print("_MEIPASS      = %s" % (getattr(sys, "_MEIPASS", "(无)")))
    print("HOME          = %s" % C.HOME)
    print("配置          = %s" % C.CONFIG_PATH)
    print("声纹档案      = %s" % C.PROFILES_PATH)
    print("ONNX 模型     = %s %s"
          % (S.DEFAULT_ONNX, "(存在)" if os.path.isfile(S.DEFAULT_ONNX) else "(缺失!)"))
    print("日志目录      = %s" % C.LOG_DIR)
    st = CredentialStore()
    print("凭据文件      = %s %s" % (st.path, "" if st.exists() else "(还没保存)"))
    if st.exists():
        try:
            c = st.load(with_password=True)
            print("凭据可读性    = OK（本会话能解开；用户=%s 密码长度=%d）"
                  % (c.get("username"), len(c.get("password") or "")))
        except Exception as e:
            print("凭据可读性    = ✗ 本会话解不开：%s" % (e,))
            print("                请在本登录会话里重跑一次密码设置（见 README）")
    pr = ProfileStore()
    print("声纹档案数    = %d" % len(pr.profiles))
    for s in pr.summary():
        print("   - %-28s 阈值=%.2f 样本=%s 来源=%s"
              % (str(s["name"])[:28], s["threshold"], s["samples"], s["source"]))
    print("锁屏状态      = %s (输入桌面 %r)" % ("是" if is_locked() else "否",
                                              input_desktop_name()))
    from src import install as I
    I.autostart_status(verbose=True)
    I.cp_status(verbose=True)
    print("agent 进程    = %s" % (I.agent_running() or "（没在运行）"))
    return 0


def cmd_install(cmd: str, argv) -> int:
    """安装/卸载动作：CP 注册、登录自启、停 agent。"""
    from src import install as I

    if cmd == "cp-install":
        return I.cp_install()
    if cmd == "cp-uninstall":
        return I.cp_uninstall()
    if cmd == "cp-status":
        return 0 if I.cp_status() else 1
    if cmd == "install-autostart":
        user = ""
        if argv and argv[0] == "--user" and len(argv) > 1:
            user = argv[1]
        return I.autostart_install(user=user)
    if cmd == "uninstall-autostart":
        return I.autostart_uninstall()
    if cmd == "agent-stop":
        return I.agent_stop()
    return 2


def main(argv=None) -> int:
    cli.fix_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "paths":
        return cmd_paths()
    if cmd == "parity":
        return cmd_parity(rest)
    if cmd == "setup-password":
        from src.setup_password import main as m
        return m(rest)
    if cmd == "panic":
        from src import install as I
        return I.panic()
    if cmd in ("cp-install", "cp-uninstall", "cp-status",
               "install-autostart", "uninstall-autostart", "agent-stop"):
        return cmd_install(cmd, rest)
    if cmd == "agent":
        return agent_main(rest)
    if cmd == "enroll":
        return enroll_main(rest)
    if cmd == "selftest-decision":
        from src.selftest_decision import main as m     # 故意延迟导入
        return m()
    print("未知子命令：%s\n" % cmd)
    print(HELP)
    return 2


if __name__ == "__main__":
    sys.exit(main())
