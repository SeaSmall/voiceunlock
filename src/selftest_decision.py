"""selftest_decision.py -- 判定逻辑自测（**不需要麦克风**，用合成向量）

要验的是"所有设备含新插入"这条要求对应的三层设计是否真的成立：
  1. 有档案的设备           -> 2 段投票即可放行
  2. 没有档案的新设备       -> 必须 3 段全过；只给 2 段必须判"证据不足"而不是放行
  3. 他人                    -> 必须拒
  4. 非声纹手段解锁确认后     -> 新设备被自动建档，下次就是一等
  5. deny 策略               -> 新设备直接拒
  6. cross_strict 策略       -> 阈值加 margin
  7. 连续失败                -> 触发锁定

★ 仿真模型（第一版这里写错了，值得记下来）：
    一次说话 = 固定信道向量 * chan_k + 每次的小波动 * noise_k
  第一版给"同一台设备"的两次说话各用了一个**独立的随机信道**，
  结果同一设备内的 cos 只有 0.55（现实中同设备约 0.98），
  于是"建档后 2 段即可放行"这条必然失败 —— 是仿真错，不是逻辑错。

用法：
    .\\venv\\Scripts\\python.exe -m src.selftest_decision
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

# 必须在 import src.* 之前改 HOME，否则会写到真实档案库上
_TMP = tempfile.mkdtemp(prefix="vu-decision-")
os.environ["VOICEUNLOCK_HOME"] = _TMP

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np                       # noqa: E402
from src import config as C              # noqa: E402
from src.profiles import ProfileStore    # noqa: E402
from src.verify import State             # noqa: E402

FAILS = []


def check(label, ok, detail=""):
    print("  [%s] %-44s %s" % ("OK" if ok else "FAIL", label, detail))
    if not ok:
        FAILS.append(label)


def section(t):
    print("\n" + "=" * 74)
    print("### %s" % t)
    print("=" * 74)


def unit(v):
    v = np.asarray(v, dtype=np.float64).ravel()
    return v / np.linalg.norm(v)


def speaker(seed):
    return unit(np.random.RandomState(seed).randn(192))


def utterance(base, chan_seed, noise_seed, chan_k=0.15, same_device_cos=0.90):
    """一次说话：固定信道(按 chan_seed) + 每次的波动(按 noise_seed)。

    同一台设备 => 同一个 chan_seed；不同设备 => 不同 chan_seed 且 chan_k 不同。
    波动幅度按信道强度缩放，使得：
        同设备两次 ≈ same_device_cos（默认 0.90，真实设备大致就在这个量级）
        跨设备同人（chan_k 0.15 vs 1.5）≈ 0.45
        他人 ≈ 0.02
    """
    e2 = (1.0 + chan_k ** 2) * (1.0 / same_device_cos - 1.0)
    e = e2 ** 0.5
    return unit(base + chan_k * speaker(chan_seed) + e * speaker(noise_seed))


def main() -> int:
    print("临时 HOME = %s" % _TMP)
    me = speaker(1)
    other = speaker(2)
    CH_A, CH_B = 11, 22                 # 设备 A / B 各自的固定信道
    K_A, K_B = 0.15, 1.50               # 设备 B 的信道特征强得多（比如另一支麦克风）
    a1 = utterance(me, CH_A, 101, K_A)
    a2 = utterance(me, CH_A, 102, K_A)
    b1 = utterance(me, CH_B, 201, K_B)
    b2 = utterance(me, CH_B, 202, K_B)
    b3 = utterance(me, CH_B, 203, K_B)
    imp_a = utterance(other, CH_A, 301, K_A)
    imp_a2 = utterance(other, CH_A, 302, K_A)
    imp_b = utterance(other, CH_B, 303, K_B)

    section("0. 合成向量的分布是否符合预期")
    c_dev_a = float(np.dot(a1, a2))
    c_cross = float(np.dot(a1, b1))
    c_dev_b = float(np.dot(b1, b2))
    c_imp = float(np.dot(a1, imp_a))
    check("同设备两次 ≈0.90", 0.85 < c_dev_a < 0.95, "cos=%.4f" % c_dev_a)
    check("跨设备同人 ≈0.45", 0.35 < c_cross < 0.55, "cos=%.4f" % c_cross)
    check("新设备自身两次 ≈0.90", 0.85 < c_dev_b < 0.95, "cos=%.4f" % c_dev_b)
    check("他人 ≈0.0", abs(c_imp) < 0.15, "cos=%.4f" % c_imp)

    cfg = C.load_config()
    st = ProfileStore(os.path.join(_TMP, "profiles.json"))

    section("1. 有档案的设备：2 段投票即可")
    st.put("dev-a", "设备A", a1, 0.31)
    d = st.decide([a1, a2], "dev-a", cfg)
    check("接受", d["accepted"], d["reason"])
    check("mode=device", d["mode"] == "device", d["mode"])

    section("2. 新设备（无档案）：按 provisional_segments 策略")
    prov = int((cfg.get("decision") or {}).get("provisional_segments", 3))
    print("  当前策略 provisional_segments = %d（1 = 说一句就算）" % prov)
    if prov > 1:
        d2 = st.decide([b1] * (prov - 1), "dev-b", cfg)
        check("不足 %d 段 -> 拒（证据不足）" % prov, not d2["accepted"], d2["reason"])
        check("理由含'证据不足'", "证据不足" in d2["reason"], "")
    else:
        print("  （策略=1：不再要求多段，跳过'证据不足'断言）")
    d3 = st.decide([b1] * prov, "dev-b", cfg)
    check("给足 %d 段且是本人 -> 接受" % prov, d3["accepted"], d3["reason"])
    check("mode=cross", d3["mode"] == "cross", d3["mode"])
    d3b = st.decide([b1] * (prov - 1) + [imp_b], "dev-b", cfg)
    check("含他人 -> 拒", not d3b["accepted"], d3b["reason"])

    section("3. 他人必须拒")
    d4 = st.decide([imp_a, imp_a2], "dev-a", cfg)
    check("他人上已登记设备 -> 拒", not d4["accepted"], d4["reason"])
    d5 = st.decide([imp_a, imp_b, imp_a2], "dev-b", cfg)
    check("他人上跨设备兜底 -> 拒", not d5["accepted"], d5["reason"])

    section("4. 自动登记（密码解锁后给新设备建档）")
    st2 = ProfileStore(os.path.join(_TMP, "profiles2.json"))
    st2.put("dev-a", "设备A", a1, 0.31)
    check("登记前 dev-b 无档案", st2.get("dev-b") is None)
    vec_b = st2.auto_enroll("dev-b", "设备B", b1, cfg,
                            note="测试：模拟一次已确认的密码解锁")
    check("auto_enroll 写入档案", st2.get("dev-b") is not None)
    check("source=auto", vec_b.get("source") == "auto", vec_b.get("source"))
    after = st2.decide([b1, b2], "dev-b", cfg)
    check("建档后 2 段即可放行", after["accepted"] and after["mode"] == "device",
          after["reason"])
    sc = st2.score_one(b1, "dev-b", cfg)
    check("建档后本人分很高", sc["score"] > 0.9, "score=%.4f" % sc["score"])
    st2.auto_enroll("dev-b", "设备B", b3, cfg)
    check("再次 auto_enroll 平滑样本数=2", st2.get("dev-b")["samples"] == 2,
          "samples=%d" % st2.get("dev-b")["samples"])
    check("平滑后仍认得本人", st2.score_one(b1, "dev-b", cfg)["ok"])

    section("4b. 自动建档的阈值必须由【实测】推出，不能拍默认值（踩过的 bug）")
    st4 = ProfileStore(os.path.join(_TMP, "profiles4.json"))
    st4.put("dev-a", "设备A", a1, 0.31)
    # 造一个"跨档案分确实偏低"的向量：把 b1 往一个随机方向拉过去
    b_low = unit(b1 * 0.2 + speaker(77) * 0.98)
    rec = st4.auto_enroll("dev-b", "设备B", b_low, cfg, note="测试：模拟自动建档")
    c = rec.get("calibrated_cross")
    check("记录了实测跨档案分", c is not None and c < 0.31, "c=%s" % c)
    check("阈值 = clamp(c-margin, floor, default)",
          abs(rec["threshold"] - min(0.31, max(0.15, (c or 0.0) - 0.05))) < 1e-9,
          "thr=%.4f" % rec["threshold"])
    check("阈值被压到 default_threshold 以下", rec["threshold"] < 0.31,
          "thr=%.4f < 0.31" % rec["threshold"])
    check("阈值不低于 floor", rec["threshold"] >= 0.15, "thr=%.4f" % rec["threshold"])
    check("写下了阈值依据（可审计）", bool(rec.get("threshold_basis")),
          rec.get("threshold_basis"))
    st4b = ProfileStore(os.path.join(_TMP, "profiles4b.json"))
    rec_b = st4b.auto_enroll("dev-x", "首个设备", a1, cfg)
    check("库里第一份档案无参照可分 -> 用默认阈值",
          abs(rec_b["threshold"] - 0.31) < 1e-9, "thr=%.2f" % rec_b["threshold"])

    section("4c. 设备档案阈值偏高/坏掉时，跨档案兜底必须救回来")
    # 复现真实事故：USB 设备被自动建档成 thr=0.95，而它自己只考 0.90
    st5 = ProfileStore(os.path.join(_TMP, "profiles5.json"))
    st5.put("dev-a", "设备A", a1, 0.31)
    st5.auto_enroll("dev-b", "设备B", b1, cfg, threshold=0.95)
    d_bad = st5.decide([b2], "dev-b", cfg)
    check("设备档案没过 -> 兜底救回", d_bad["accepted"], d_bad["reason"])
    check("mode=device+cross", d_bad["mode"] == "device+cross", d_bad["mode"])
    check("兜底参照里排除了本设备那份坏档案", d_bad.get("matched") == "dev-a",
          "matched=%s（若为 dev-b 说明 exclude 没生效）" % d_bad.get("matched"))
    imp_bad = st5.decide([imp_a], "dev-b", cfg)
    check("但他人依然拒", not imp_bad["accepted"], imp_bad["reason"][:60])
    cfg_deny2 = C.load_config()
    cfg_deny2["decision"]["unknown_device_policy"] = "deny"
    d_deny = st5.decide([b2], "dev-b", cfg_deny2)
    check("deny 策略下不做兜底（要尊重'只认本设备档案'）",
          not d_deny["accepted"], d_deny["reason"][:60])

    section("5. deny 策略：新设备直接拒")
    cfg_deny = C.load_config()
    cfg_deny["decision"]["unknown_device_policy"] = "deny"
    dd = st.decide([b1, b2, b3], "dev-b", cfg_deny)
    check("deny -> 拒", not dd["accepted"], dd["reason"])
    check("mode=denied", dd["mode"] == "denied", dd["mode"])

    section("6. cross_strict 策略：阈值加 margin")
    cfg_cs = C.load_config()
    cfg_cs["decision"]["unknown_device_policy"] = "cross_strict"
    cfg_cs["decision"]["cross_margin"] = 0.30      # 0.31+0.30=0.61 > 跨设备分
    ds = st.decide([b1, b2, b3], "dev-b", cfg_cs)
    check("margin 抬高后 -> 拒", not ds["accepted"], ds["reason"])
    check("阈值 = 0.61", abs(ds["threshold"] - 0.61) < 1e-6, "%.4f" % ds["threshold"])

    section("7. 失败计数与锁定")
    stt = State(os.path.join(_TMP, "state.json"))
    for _ in range(3):
        stt.on_fail(cfg)
    check("3 次失败后进入锁定", stt.locked() > 0, "剩余 %.0f 秒" % stt.locked())
    stt.on_ok()
    check("成功后解锁并清零", stt.locked() == 0 and stt.d["failures"] == 0)

    section("8. 空库 / 无音频的边界")
    st3 = ProfileStore(os.path.join(_TMP, "profiles3.json"))
    e = st3.decide([a1, a2], "dev-a", cfg)
    check("空库 -> 拒且理由明确", not e["accepted"] and e["mode"] == "no_profile",
          e["reason"])
    n = st3.decide([], "dev-a", cfg)
    check("无音频 -> 拒", not n["accepted"], n["reason"])

    print("\n" + "=" * 74)
    if FAILS:
        print("DECISION SELFTEST FAILED (%d 项): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("DECISION SELFTEST OK —— 三层覆盖（本设备/跨设备/自动登记）成立")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
