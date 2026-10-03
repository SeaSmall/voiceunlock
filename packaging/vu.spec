# -*- mode: python ; coding: utf-8 -*-
"""voiceunlock 打包脚本（PyInstaller one-folder，两个 exe 共享一份 _internal）

    VoiceUnlock.exe       有控制台：设密码 / 登记声纹 / 看状态 / 一致性自检
    VoiceUnlockAgent.exe  无控制台：登录后常驻，锁屏时听声纹

为什么必须两个 exe：见 packaging/vu_agent.py 顶部说明（黑窗口 vs getpass）。

★ 三个延迟导入必须显式列进 hiddenimports，否则【打包后运行时】才炸：
    soundcard             audio.py / devices.py 的函数体内才 import
    kaldi_native_fbank    sv.fbank() 体内才 import
    onnxruntime           SpeakerEmbedder.__init__ 体内才 import
  它们写成延迟导入是为了不让 import 变慢（torch/funasr 的教训），代价就是
  静态分析看不见 —— 这正是一开始就写进 hiddenimports 的原因。
"""
import os

ROOT = os.path.dirname(SPECPATH)          # packaging/ 的上一级

HIDDEN = [
    # 延迟导入的第三方（见上）
    "soundcard", "kaldi_native_fbank", "onnxruntime",
    "onnxruntime.capi.onnxruntime_pybind11_state",
    "cffi", "_cffi_backend",
    # 本项目模块（CLI 用字符串派发，部分是延迟导入）
    "src", "src.cli", "src.config", "src.agent", "src.enroll", "src.devices",
    "src.audio", "src.sv", "src.profiles", "src.verify", "src.session",
    "src.dpapi", "src.win32pipe", "src.parity", "src.install",
    "src.setup_password", "src.selftest_decision",
    # 输出语言层：cli.fix_console() 里是 `from . import i18n`（被 try 包着，静态
    # 分析看不见），i18n_catalog 又是在 i18n._build() 里才 import 的 —— 都得显式列。
    "src.i18n", "src.i18n_catalog",
]

# 这个 venv 里装了大量与运行无关的重家伙，明确排除，免得包体膨胀 / 误收。
# ★ 不要排除 distutils / setuptools：PyInstaller 自己的
#   pre_safe_import_module/hook-distutils.py 会把 setuptools._distutils 别名成
#   distutils，若 distutils 已被排除，modulegraph 会直接抛
#   ValueError: Target module "distutils" already imported as ExcludedModule(...)
EXCLUDES = [
    "torch", "torchaudio", "funasr", "tkinter", "matplotlib", "pandas",
    "pyarrow", "PyQt5", "llvmlite", "numba", "scipy", "IPython", "notebook",
    "pytest",
]

DATAS = [(os.path.join(ROOT, "models", "campplus.onnx"), "models")]

a_cli = Analysis(
    [os.path.join(ROOT, "packaging", "vu_cli.py")],
    pathex=[ROOT],
    binaries=[],
    datas=list(DATAS),
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz_cli = PYZ(a_cli.pure)

a_ag = Analysis(
    [os.path.join(ROOT, "packaging", "vu_agent.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz_ag = PYZ(a_ag.pure)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name='VoiceUnlock',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX 会破坏原生 DLL / 让杀软误报，一律关掉
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

exe_ag = EXE(
    pyz_ag,
    a_ag.scripts,
    [],
    exclude_binaries=True,
    name='VoiceUnlockAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                  # 常驻不能弹黑窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe_cli, a_cli.binaries, a_cli.datas,
    exe_ag, a_ag.binaries, a_ag.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='VoiceUnlock',
)
