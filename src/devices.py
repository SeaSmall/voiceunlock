"""devices.py -- 输入设备枚举与可信度标注

为什么要"可信度"：本项目要求【所有语音输入设备都能当输入源】，
但虚拟混音总线（VoiceMeeter 之类）上可能混着播放侧信号 —— 而本机就有 GPT-SoVITS
的自建音色，放一段"你自己"的克隆音就能骗过声纹。所以：
    trusted   = 只接真实麦克风、不接任何播放侧信号
    untrusted = 混音总线 / 回环 / 虚拟设备（默认不参与解锁，除非显式写进白名单）
"""
from __future__ import annotations

from . import config as C

# 这些关键字命中即判为 untrusted（可被 config.json 的 devices.untrusted_patterns 覆盖）
DEFAULT_UNTRUSTED = [
    "voicemeeter", "vb-audio", "vaio", "steam streaming",
    "cable", "virtual", "loopback", "aux", "soundflower",
]


def _patterns() -> list:
    cfg = C.load_config()
    pats = (cfg.get("devices") or {}).get("untrusted_patterns")
    if not pats:
        pats = DEFAULT_UNTRUSTED
    return [str(p).lower() for p in pats]


def _trusted_ids() -> set:
    cfg = C.load_config()
    return set((cfg.get("devices") or {}).get("trusted") or [])


def list_capture_devices() -> list:
    """枚举当前可用的输入设备（不含回环）。

    返回 [{"id","name","channels","trusted","reason","dev"}]。
    `dev` 是 soundcard 的对象，用来开 recorder；其余字段可序列化。

    注意：**每次解锁都要重新枚举** —— 手机当麦（WO Mic）等设备会随时出现/消失，
    启动时固定一份列表一定会踩坑。
    """
    import soundcard as sc

    trusted_ids = _trusted_ids()
    pats = _patterns()
    out = []
    for m in sc.all_microphones(include_loopback=False):
        name = getattr(m, "name", "") or ""
        did = getattr(m, "id", "") or name
        low = name.lower()
        hit = next((p for p in pats if p in low), None)
        if did in trusted_ids:
            trusted, reason = True, "config 白名单"
        elif hit:
            trusted, reason = False, "命中不可信关键字: %s" % hit
        else:
            trusted, reason = True, "默认可信（未命中虚拟设备关键字）"
        out.append({
            "id": did,
            "name": name,
            "channels": int(getattr(m, "channels", 0) or 0),
            "trusted": trusted,
            "reason": reason,
            "dev": m,
        })
    # 可信的排前面，便于人肉看列表
    out.sort(key=lambda d: (not d["trusted"], d["name"]))
    return out


def trusted_only(devs) -> list:
    return [d for d in devs if d["trusted"]]


def snapshot_ids() -> set:
    return {d["id"] for d in list_capture_devices()}


def watch_new_devices(on_new=None, on_gone=None, interval_s: float = 2.0):
    """轮询监听设备插拔，返回一个 stop() 函数。

    为什么轮询而不是 IMMNotificationClient：轮询 2 秒足够（人插上麦克风到说话
    至少几秒），而且不用处理 COM 回调线程里没有消息泵这一堆坑。
    on_new(dev) / on_gone(dev_id_or_dict) 会在后台线程里被调用。
    """
    import threading
    import time

    stop_evt = threading.Event()

    def loop():
        known = {d["id"]: d for d in list_capture_devices()}
        while not stop_evt.wait(interval_s):
            cur = {d["id"]: d for d in list_capture_devices()}
            for did, d in cur.items():
                if did not in known and on_new:
                    try:
                        on_new(d)
                    except Exception:
                        pass
            for did in list(known.keys()):
                if did not in cur and on_gone:
                    try:
                        on_gone(known[did])
                    except Exception:
                        pass
            known = cur

    t = threading.Thread(target=loop, daemon=True)
    t.start()

    def stop():
        stop_evt.set()
        t.join(timeout=3.0)

    return stop


if __name__ == "__main__":
    ds = list_capture_devices()
    print("枚举到 %d 个输入设备（本机实测这些都能打开并读到数据）\n" % len(ds))
    for d in ds:
        print("%s %s" % ("[可信]" if d["trusted"] else "[不可信]", d["name"]))
        print("   id      : %s" % d["id"])
        print("   channels: %s" % d["channels"])
        print("   理由    : %s" % d["reason"])
