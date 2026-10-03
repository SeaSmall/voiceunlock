"""config.py -- 配置与路径解析

一个原则：**所有路径都从 HOME 推导，不写死盘符**。
  这样打包成安装包后，装到别的机器/别的目录都能跑（用户的核心要求之一）。

HOME 的确定顺序：
  1. 环境变量 VOICEUNLOCK_HOME
  2. 【打包形态】%LOCALAPPDATA%\\VoiceUnlock —— 见下面 _resolve_home 的说明
  3. 开发时：包目录的上一级（即项目根目录）
  4. %PROGRAMDATA%\\VoiceUnlock
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(PKG_DIR)


def _resolve_home() -> str:
    env = os.environ.get("VOICEUNLOCK_HOME")
    if env:
        return os.path.abspath(env)
    # ★ 打包形态：绝不能用安装目录当 HOME。
    #   安装目录一般在 Program Files 下，普通用户不可写；而这里要放的东西
    #   （DPAPI 凭据密文、声纹档案）必须由【当前用户】读写 —— DPAPI 用户作用域
    #   的前提就是"同一个用户的登录会话"。所以固定用 %LOCALAPPDATA%\VoiceUnlock。
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "VoiceUnlock")
    # 开发布局：<home>/src/config.py 且 <home>/models/campplus.onnx 存在
    if os.path.isdir(os.path.join(PROJECT_DIR, "models")):
        return PROJECT_DIR
    pd = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return os.path.join(pd, "VoiceUnlock")


HOME = _resolve_home()
CONFIG_PATH = os.path.join(HOME, "config.json")
PROFILES_PATH = os.path.join(HOME, "profiles.json")
MODELS_DIR = os.path.join(HOME, "models")
ONNX_PATH = os.path.join(MODELS_DIR, "campplus.onnx")
LOG_DIR = os.path.join(HOME, "logs")
FLAG_VIGIL_OFF = os.path.join(HOME, "vigil_off.flag")

# 生产采集参数：T=200 帧 = 2.015 秒音频（见 sv.py 的 samples_for_frames）
DEFAULT_CONFIG = {
    "version": 1,
    "audio": {
        "sample_rate": 16000,          # 直接向设备请求 16k，由音频引擎重采样
        "target_frames": 200,          # 必须是"对齐 T"（sv.is_aligned）
        # ★ 说一句就进：只采 1 段（约 2 秒）。模型的最小对齐帧数是 200 帧
        #   （tdnn_len(199..200)=100 才能被 seg_pooling 的 100 整除），
        #   所以 2.0 秒已经是"能说出口的最短一次"。
        "segments": 1,                 # 一次解锁采集几段（1 = 说一句就好）
        "seg_timeout_s": 6.0,
        "wait_voice_s": 25.0,          # （旧路径）常驻监听：最多等多久有人开口
        "unlock_wait_s": 8.0,          # CP 来取凭据时最多等多久票据就绪（务必有界！）
        "listen_poll_s": 5.0,          # 常驻监听每轮的电平窗口长度（秒）
        # 电平触发门限（常驻监听）：
        #   threshold = max(该设备近期电平中位数 * level_rel, level_floor)
        # 踩过的坑：原来本底取【所有设备的最小值】，而 WO Mic 是死的（peak~1e-6），
        # 本底被钉死在 0 -> 门限永远等于绝对下限 3e-5 -> 环境噪声也能触发 ->
        # 拿噪声去比对声纹（分数 0.04~0.09）-> 3 次失败把通道锁 300 秒。
        # 实测本机噪声 peak ≤2.5e-3、说话 ≥1.4e-2，所以 level_floor 取 3e-3。
        "level_floor": 0.003,
        "level_rel": 6.0,
    },
    "decision": {
        "default_threshold": 0.31,     # 官方 yesOrno_thr 锚点
        "vote": "all",                 # all=每段都要过；any=任一段过（仅本设备有档案时）
        # 新插入的设备没有档案时怎么办（用户要求"所有设备含新插入的"）：
        #   deny        最保守，新设备不参与（不满足需求，别用）
        #   provisional 与所有已知档案比取最大，但要【更多证据】才放行（默认）
        #   cross_strict 同上但阈值再加 cross_margin
        "unknown_device_policy": "provisional",
        "provisional_segments": 1,     # 跨设备兜底也需要几段（1 = 同样说一句就好）
        "cross_margin": 0.05,          # 仅 cross_strict 用
        "auto_enroll": True,           # 非声纹手段解锁成功时，自动给活跃设备建档
        # ★ 自动建档的阈值【必须由这一次的实测推出来】，不能拍默认值。
        # 踩过：USB 设备被自动建档成 thr=0.31（default_threshold），而它跨信道
        # 只考 0.244 —— 此后本人每次都被自己的档案拒掉，且当时没有退路。
        # 规则：thr = clamp(实测跨档案分 - margin, floor, default_threshold)
        "auto_enroll_calibrate": True,
        "auto_enroll_margin": 0.05,    # 在"实测跨档案分"下面留多少余量
        "auto_enroll_floor": 0.15,     # 再差也不低于这个（再低就没有区分力了）
        # 建档用的 2 秒窗里至少要有多少比例的"能量块"像语音。
        # ★ 单位坑（我自己踩的）：sv.embed_best_window 返回的 speech_frames
        #   不是 10ms 帧，而是它内部的 samples_for_frames(25) ≈ 0.265 秒能量块，
        #   2 秒窗里最多只有 7~8 块。最初按"帧"理解、默认写了 40，
        #   结果是【任何建档都会被质量闸拒掉】。所以这里用【比例】而不是绝对数。
        "auto_enroll_min_speech_ratio": 0.5,
        "max_failures": 3,
        "lockout_seconds": 300,
        # ★ 仅测试用：无条件让 CP 走"注入回车"这条路，不需要声纹。
        #   用途：在没有麦克风的环境（比如虚拟机）里验证"注入回车能否解锁无密码账户"
        #   这个机制本身。生产必须保持 false。
        "inject_enter_always": False,
        # ★ 仅测试用：跳过声纹判定，直接用库里已保存的凭据备票并交付。
        #   用途：在【没有麦克风】的环境（虚拟机）里验证 CP 的完整链路 ——
        #   "票据就绪 → 凭据出现 → LogonUI 自动提交 → 解锁"。
        #   生产必须保持 false；它等于把声纹这道门整个拆掉。
        "test_bypass_voice": False,
    },
    "devices": {
        "trusted": [],                 # endpoint id 白名单（只接真实麦克风）
        "untrusted_patterns": [
            "VoiceMeeter", "VB-Audio", "VAIO", "Steam Streaming",
            "CABLE", "Virtual", "Loopback", "AUX",
        ],
    },
    "vigil": {
        "enabled": True,
        "rescue_hotkey": "Ctrl+Shift+Alt+Q",
        "hook_watchdog_seconds": 5,
        "allow_injected_input": True,  # 放行远程工具注入的输入
    },
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None) -> dict:
    path = path or CONFIG_PATH
    if not os.path.isfile(path):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
    except Exception:
        # 配置坏了不能让程序起不来：退回默认值（并在调用方日志里体现）
        return json.loads(json.dumps(DEFAULT_CONFIG))
    return _deep_merge(DEFAULT_CONFIG, user)


def save_config(cfg: dict, path: str | None = None) -> str:
    """原子写：先写临时文件再替换，避免掉电写坏配置。"""
    path = path or CONFIG_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".cfg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path


def load_profiles(path: str | None = None) -> dict:
    path = path or PROFILES_PATH
    if not os.path.isfile(path):
        return {"version": 1, "profiles": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("version", 1)
        d.setdefault("profiles", {})
        return d
    except Exception:
        return {"version": 1, "profiles": {}}


def save_profiles(prof: dict, path: str | None = None) -> str:
    path = path or PROFILES_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".prof-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(prof, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path


if __name__ == "__main__":
    print("HOME         =", HOME)
    print("CONFIG_PATH  =", CONFIG_PATH)
    print("PROFILES     =", PROFILES_PATH)
    print("ONNX_PATH    =", ONNX_PATH, "exists =", os.path.isfile(ONNX_PATH))
    print()
    print(json.dumps(load_config(), ensure_ascii=False, indent=2))
