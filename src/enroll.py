"""enroll.py -- 逐设备声纹登记 / 现场试 / 档案管理

最省事的用法（推荐，也是安装包引导走的那条）：
    VoiceUnlock.exe enroll
        -> 自动选第一台可信麦克风，一次连续录 8 秒；屏幕上会打出【要朗读的句子】，
           倒计时结束照着念就行。

其它用法（开发期：在项目根目录下用 venv 里的解释器）：

    # 看有哪些设备、哪些已经有档案
    .\\venv\\Scripts\\python.exe -m src.enroll --list

    # 给所有【可信】设备逐个登记（每台录 3 遍；每遍都会给出要念的句子）
    .\\venv\\Scripts\\python.exe -m src.enroll --all

    # 只登记一台（可用序号或 endpoint id）—— 比 --all 少录很多次
    .\\venv\\Scripts\\python.exe -m src.enroll --device 0

    # 一次连续录音登记（例如 8 秒）
    .\\venv\\Scripts\\python.exe -m src.enroll --single-take 8

    # 顺便采他人的声音，用来自动定阈值（更靠谱）
    .\\venv\\Scripts\\python.exe -m src.enroll --device 0 --others 3

    # 现场试一次（走完整判定流程）
    .\\venv\\Scripts\\python.exe -m src.enroll --test

    # 档案管理
    .\\venv\\Scripts\\python.exe -m src.enroll --show
    .\\venv\\Scripts\\python.exe -m src.enroll --remove <endpoint id>
    .\\venv\\Scripts\\python.exe -m src.enroll --set-threshold <endpoint id> 0.35

设计说明：
  * 一遍 = 一次连续录音（segments 段，默认 4 秒），切成 segments 段各自出向量。
    登记侧与判定侧**用完全相同的切法**，否则两边分布不一致。
  * 阈值不是拍脑袋：先量"本人各段之间的最低相似度"（self_min），
    阈值取 self_min - 0.10；如果有他人样本，取 (self_min + other_max)/2。
  * 官方锚点 yesOrno_thr = 0.31 会一并打印出来做参照。
  * ★ 登记必须告诉用户【说什么、说多久、第几遍】。CAM++ 与文本无关（念什么不影响
    声纹），但含糊的提示会让用户发呆或只说两个字，录到的样本质量很差。
"""
from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from . import audio as A
from . import cli
from . import config as C
from . import devices as D
from . import sv as S
from .profiles import ProfileStore, cosine

BAR = "=" * 74


def _record(dev, samples: int, sr: int = S.SR, timeout_s: float | None = None):
    """注意参数是【采样数】不是帧数 —— 见 audio.capture_multi 的说明。"""
    if timeout_s is None:
        timeout_s = samples / float(sr) + 5.0
    got, errs = A.capture_multi([dev], samples=samples, timeout_s=timeout_s)
    a = got.get(dev["id"])
    if a is None:
        raise RuntimeError("采集失败：%s" % errs.get(dev["id"], "未知原因"))
    return a


# 登记时请用户朗读的句子（正常语速每句约 4 秒）。
# ★ 为什么必须给出具体句子（这是用户提的意见，提得对）：
#   原来只打印一句 "● 说 ..." —— 用户不知道该说什么、要说多久、这一遍和下几遍
#   是什么关系。结果就是对着屏幕发呆，或者只说两个字就停，录到的样本质量很差。
#   给一句现成的话，用户照着念就行，时长也自然落在需要的范围内。
#   注意：CAM++ 与文本无关（内容不影响声纹），给句子纯粹是为了"知道该说什么"、
#   以及让每遍的时长可控。
SENTENCES = [
    "今天天气不错，我打算下午出去走一走。",
    "请帮我打开电脑，我看一下今天有什么安排。",
    "这个东西先放在桌子上面，回头我再收拾。",
]
# 一次连续录音（--single-take 8）用的长句，约 8 秒
LONG_SENTENCE = ("今天天气不错，我打算下午出去走一走，顺便去超市买点水果和牛奶，"
                 "回来的时候把楼下的快递一起取上来。")


def _say_prompt(sentence: str, seconds: float, round_no=None, rounds=None,
                lead: int = 4, who: str = "") -> None:
    """把"该说什么、说多久、第几遍"讲清楚，再倒计时。

    这一段的唯一目的就是让用户【知道现在要做什么】—— 锁屏解锁是零交互的，
    但登记这一步必须靠人配合，含糊就等于让人干等。
    """
    head = ("第 %d/%d 遍" % (round_no, rounds)) if (round_no and rounds) else "准备录音"
    if who:
        head = "%s（%s）" % (head, who)
    print("")
    print("  " + "-" * 68)
    print("  %s —— 这一段要录 %.1f 秒" % (head, seconds))
    print("  请朗读下面这句话（正常语速，不用刻意拖长，也别太急）：")
    print("")
    print("      %s" % sentence)
    print("")
    for i in range(int(lead), 0, -1):
        print("      %d ..." % i, flush=True)
        time.sleep(1)
    print("      ==> 现在开始说！", flush=True)


def _default_device(devs):
    """没指定设备时挑一台：优先【可信】的第一台（虚拟混音设备排在后面）。"""
    t = D.trusted_only(devs)
    if t:
        return t[0]
    return devs[0] if devs else None


# 一段录音里"像说话"的帧至少要占这么多，否则判为"没说话"、拒绝建档。
# 实测：正常说话 0.3~0.7；纯房间噪声 0.00。取值留足余量，避免误拒。
MIN_ACTIVE_RATIO = 0.10


def _check_spoke(st: dict) -> None:
    """录完之后的通用质检：必须真的说了话，否则【绝不能】建档。

    ★ 踩过（真实跑出来的）：纯噪声也能"登记成功"。
      speech_stats 的 silent 只看"设备是不是死的"（包络峰值 < 1e-5），
      房间噪声的峰值比它高好几个数量级，于是没人说话也过了那一关 ——
      接着拿噪声片段之间的相似度（实测 0.77）当"本人一致性"，
      算出一个 0.67 的高阈值写进档案。那种档案只会害人：以后谁也别想通过，
      而用户完全不知道发生了什么（他看到的是"登记成功"）。
      可靠判据是 active_ratio —— 它衡量"像说话的帧占比"，对噪声不敏感。
    """
    if st.get("silent"):
        raise RuntimeError("这段基本是静音（峰值 %.6f）—— 设备没拾到声音？" % st["peak"])
    ar = float(st.get("active_ratio", 0.0))
    if ar < MIN_ACTIVE_RATIO:
        raise RuntimeError(
            "这段里几乎没有人声（活跃比例 %.2f < %.2f，峰值 %.6f）—— "
            "请对着麦克风正常说一句话再试" % (ar, MIN_ACTIVE_RATIO, st["peak"]))


def _runtime_selfcheck(emb, audio, mean, thr):
    """用【判定路径的取窗方式】复核档案，把阈值压到"本人能过"的水平之下。

    ★ 为什么必须有这一步（设计不一致，是实测才发现的）：
      登记时算 self_min 用的是"一次连续录音切成 2 秒段"，相邻段高度相似，
      所以 self_min 偏乐观（常到 0.8+），阈值 self_min-0.10 也就偏高；
      而解锁时走的是"从起音点取 3 秒缓冲、再挑语音最密的 2 秒"，分布并不相同，
      同一个人的分可能只有 0.3~0.5。结果就是：**登记显示成功，解锁时本人过不去。**
      这里取录音开头约 3 秒（正是判定路径的输入形态）算一次本人分，
      如果阈值高到会挡住它，就下调到 s-0.05。

    返回 (阈值, 判定路径下本人分或 None)。
    """
    T = emb.target_frames
    step = S.samples_for_frames(T)
    short = step + int(1.0 * S.SR)          # ≈3.0 秒，与常驻快判的输入长度一致
    seg = np.asarray(audio, dtype=np.float32).reshape(-1)[:short]
    if seg.size < step:
        return thr, None
    try:
        v, _info = emb.embed_best_window(seg)
    except Exception:
        return thr, None
    s = float(cosine(v, mean))
    if thr > s - 0.05:
        return round(max(0.20, s - 0.05), 2), s
    return thr, s


def _take_embeddings(emb: S.SpeakerEmbedder, dev, segments: int,
                     sentence: str, round_no=None, rounds=None,
                     lead: int = 4, who: str = "") -> list:
    T = emb.target_frames
    secs = S.samples_for_frames(T) * segments / float(S.SR)
    _say_prompt(sentence, secs, round_no, rounds, lead, who)
    a = _record(dev, S.samples_for_frames(T) * segments)
    st = A.speech_stats(a)
    print("      录完：峰值 %.5f  活跃比例 %.2f" % (st["peak"], st["active_ratio"]))
    _check_spoke(st)
    embs, _ = emb.embed_segments(a, n_segments=segments)
    return embs


def _pick(devs, spec: str):
    if spec is None:
        return None
    for i, d in enumerate(devs):
        if str(i) == str(spec):
            return d
    for d in devs:
        if d["id"] == spec:
            return d
    for d in devs:
        if spec.lower() in d["name"].lower():
            return d
    return None


def cmd_list(store: ProfileStore) -> int:
    devs = D.list_capture_devices()
    print("当前输入设备（枚举到 %d 个）：\n" % len(devs))
    for i, d in enumerate(devs):
        p = store.get(d["id"])
        mark = "[可信]" if d["trusted"] else "[不可信]"
        have = ("已登记 %.2f (%d 样本, %s)"
                % (p["threshold"], p.get("samples", 0), p.get("source", "?"))) if p else "未登记"
        print("  [%d] %s %s" % (i, mark, d["name"]))
        print("      %s" % d["id"])
        print("      档案: %s      %s" % (have, d["reason"]))
    print("\n说明：新插入但未登记的设备也能用 —— 会走『跨设备兜底』（需 3 段全过），")
    print("      并且在任何一次密码/远程解锁成功后被【自动登记】成一等设备。")
    return 0


def cmd_enroll(store: ProfileStore, spec, all_trusted: bool, rounds: int,
               others: int, segments: int, lead: int = 4) -> int:
    emb = S.SpeakerEmbedder()
    devs = D.list_capture_devices()
    if all_trusted:
        todo = D.trusted_only(devs)
    else:
        d = _pick(devs, spec)
        if d is None:
            print("找不到设备: %r（先跑 --list 看序号）" % spec)
            return 2
        todo = [d]
    if not todo:
        print("没有符合条件的设备")
        return 2

    secs = S.samples_for_frames(emb.target_frames) * segments / float(S.SR)
    n_rounds = rounds * len(todo)
    print("登记计划")
    print("  设备 %d 台 × 每台 %d 遍 × 每遍 %.1f 秒  =  一共要录 %d 遍"
          % (len(todo), rounds, secs, n_rounds))
    print("  每遍我都会把【要朗读的那句话】打出来，倒计时结束照着念就行。")
    print("  中途可以 Ctrl+C 停（已经登记完的设备会保留）。")
    if len(todo) > 1:
        print("  提示：只想给常用的那一支麦克风登记的话，用 --device <序号> 少录几次。")
    print("")

    for d in todo:
        print(BAR)
        print("设备: %s   %s" % (d["name"], "[可信]" if d["trusted"] else "[不可信]"))
        print(BAR)
        all_e = []
        ok_rounds = 0
        for r in range(rounds):
            sent = SENTENCES[r % len(SENTENCES)]
            try:
                all_e += _take_embeddings(emb, d, segments, sent,
                                          round_no=r + 1, rounds=rounds, lead=lead)
                ok_rounds += 1
            except Exception as e:
                print("      这一遍没成：%s" % e)
                if ok_rounds == 0:
                    # 第一遍就拾不到声音 -> 这设备大概是不能用的（虚拟端点、没插麦）
                    # 没必要陪着它把剩下的遍数耗完
                    print("      该设备第一遍就没拾到声音，判定不可用，跳过"
                          "（虚拟/未接入的麦克风很常见）\n")
                    break
        if len(all_e) < 2:
            print("  有效样本不足，跳过\n")
            continue

        # 本人各段之间的一致性
        sims = [cosine(all_e[i], all_e[j])
                for i in range(len(all_e)) for j in range(i + 1, len(all_e))]
        self_min = float(min(sims))
        self_med = float(np.median(sims))

        other_max = None
        for r in range(others):
            try:
                oe = _take_embeddings(emb, d, segments, SENTENCES[0],
                                      round_no=r + 1, rounds=others, lead=lead,
                                      who="请换另一个人来念")
            except Exception as e:
                print("      没成：%s" % e)
                continue
            for v in oe:
                c = max(cosine(v, x) for x in all_e)
                other_max = c if other_max is None else max(other_max, c)

        thr = max(0.20, self_min - 0.10)
        if other_max is not None and other_max < self_min:
            thr = round((self_min + other_max) / 2.0, 2)
        thr = round(float(thr), 2)

        mean = np.mean(np.stack(all_e), axis=0)
        mean = mean / np.linalg.norm(mean)
        store.put(d["id"], d["name"], mean, thr, samples=len(all_e),
                  self_min=self_min, other_max=other_max, source="manual")
        store.save()

        print("\n  本人段间相似度: 最低 %.3f  中位 %.3f  最高 %.3f  (%d 个样本)"
              % (self_min, self_med, max(sims), len(all_e)))
        if other_max is not None:
            print("  他人最高相似度: %.3f" % other_max)
            if other_max >= self_min:
                print("  ⚠ 本人最低值 <= 他人最高值 —— 这台设备区分度不够，"
                      "换设备或重录，别靠调阈值硬凑")
        print("  官方锚点 yesOrno_thr = 0.31；本次采用阈值 = %.2f" % thr)
        print("  已写入: %s\n" % store.path)
    return 0


def cmd_enroll_single(store: ProfileStore, spec, seconds: float,
                      lead: int = 5) -> int:
    """一次连续录音登记（比"分 3 遍"容易配合得多）。

    录 seconds 秒 -> 按 2.015 秒切成若干段 -> 各出向量 -> 取均值当档案。
    为什么切成多段再平均：单段噪声大（这是原版 enroll_voice.py 就做对的地方）。
    spec 为空时自动选第一台【可信】设备。
    """
    emb = S.SpeakerEmbedder()
    devs = D.list_capture_devices()
    if spec:
        d = _pick(devs, spec)
    else:
        d = _default_device(devs)
    if d is None:
        print("没有可用设备（先跑 --list 看有没有麦克风）")
        return 2
    T = emb.target_frames
    step = S.samples_for_frames(T)
    n_seg = max(int(seconds * S.SR) // step, 1)
    need = step * n_seg
    print("设备: %s   %s" % (d["name"], "[可信]" if d["trusted"] else "[不可信]"))
    print("一次连续录 %.1f 秒，切成 %d 段各出一个向量再取平均"
          % (need / S.SR, n_seg))
    print("（只录这一次，不用反复配合）")
    _say_prompt(LONG_SENTENCE, need / float(S.SR), lead=lead)
    a = _record(d, need)
    st = A.speech_stats(a)
    print("  录完：%.2f 秒，峰值 %.6f，活跃比例 %.2f"
          % (len(a) / S.SR, st["peak"], st["active_ratio"]))
    try:
        _check_spoke(st)
    except RuntimeError as e:
        print("  [!] %s" % e)
        print("  【没有建档】—— 免得把一段噪声写成你的声纹，那种档案以后谁也别想通过。")
        print("  请确认：麦克风没被静音、选中的设备是对的、说话时离麦不要太远，然后重跑。")
        return 1
    embs, info = emb.embed_segments(a, n_segments=n_seg)
    sims = [cosine(embs[i], embs[j])
            for i in range(len(embs)) for j in range(i + 1, len(embs))]
    self_min = float(min(sims)) if sims else 1.0
    self_med = float(np.median(sims)) if sims else 1.0
    print("  各段之间一致性: 最低 %.3f  中位 %.3f  (%d 个样本)"
          % (self_min, self_med, len(embs)))
    thr0 = round(max(0.20, self_min - 0.10), 2)
    mean = np.mean(np.stack(embs), axis=0)
    mean = mean / np.linalg.norm(mean)

    # ★ 按【判定路径】复核一次，别让"登记成功但解锁时本人过不去"
    thr, s_fast = _runtime_selfcheck(emb, a, mean, thr0)
    if s_fast is not None:
        if thr != thr0:
            print("  判定路径自检: 本人分 %.3f —— 阈值从 %.2f 下调到 %.2f"
                  "（原阈值按连续语音算，偏乐观，会把你自己挡在外面）"
                  % (s_fast, thr0, thr))
        else:
            print("  判定路径自检: 本人分 %.3f ≥ 阈值 %.2f  OK" % (s_fast, thr))
        if s_fast < 0.25:
            print("  [!] 判定路径下本人分偏低（%.3f）—— 这支配音的音量/质量不太够，"
                  "建议提高麦克风增益、离麦近一点再重录一次" % s_fast)

    store.put(d["id"], d["name"], mean, thr, samples=len(embs),
              self_min=self_min, other_max=None, source="manual")
    store.save()
    print("  阈值 = %.2f（官方锚点 0.31）" % thr)
    print("  已写入: %s" % store.path)
    print("\n  下一步：直接锁屏说一句（约 2 秒）试试。")
    return 0


def cmd_test(store: ProfileStore, spec, lead: int = 5) -> int:
    from .verify import Verifier
    v = Verifier(store=store)
    devs = D.list_capture_devices()
    ids = None
    if spec:
        d = _pick(devs, spec)
        if d is None:
            print("找不到设备: %r" % spec)
            return 2
        ids = [d["id"]]
    print("现场判定（会先告诉你需不需要多说一句）\n")
    # 预先告警：本次是否需要 3 段
    need3 = any(store.get(d["id"]) is None for d in (D.trusted_only(devs) if not ids
                                                     else [x for x in devs if x["id"] in ids]))
    if need3:
        print("[!] 检测到未登记设备 —— 本次需要说满 3 段（约 6 秒）才够证据\n")
    if lead > 0:
        print("倒计时 %d 秒，结束后请【连续说满 6 秒】：" % lead, flush=True)
        for i in range(int(lead), 0, -1):
            print("  %2d ..." % i, flush=True)
            time.sleep(1)
        print("  ==> 现在开始说！", flush=True)
    r = v.attempt(device_ids=ids)
    print("结果      : %s" % ("通过" if r["accepted"] else "拒绝"))
    print("原因      : %s" % r["reason"])
    print("模式      : %s" % r["mode"])
    print("分数      : %s" % ["%.4f" % s if s is not None else "None"
                              for s in r.get("scores", [])])
    print("阈值      : %s" % r.get("threshold"))
    print("设备      : %s" % r.get("device_id"))
    print("采集段数  : %s (常规 %s)" % (r.get("segments_captured"), r.get("segments_normal")))
    if r.get("auto_enrolled"):
        print("自动登记  : %s" % r["auto_enrolled"])
    if r.get("errors"):
        print("设备错误  : %s" % r["errors"])
    return 0 if r["accepted"] else 1


def cmd_show(store: ProfileStore) -> int:
    rows = store.summary()
    print("档案文件: %s" % store.path)
    print("档案数量: %d\n" % len(rows))
    for s in rows:
        print("  %s" % s["name"])
        print("    id=%s" % s["id"])
        print("    阈值=%.2f  样本=%d  source=%s  登记于 %s"
              % (s["threshold"], s["samples"], s["source"], s["enrolled_at"]))
        print("    本人最低=%.3f  他人最高=%s"
              % (s["self_min"] if s["self_min"] is not None else float("nan"),
                 ("%.3f" % s["other_max"]) if s["other_max"] is not None else "未测"))
    return 0


def main(argv=None) -> int:
    cli.fix_console()
    ap = argparse.ArgumentParser(prog="src.enroll", description="声纹登记/试/管理")
    ap.add_argument("--list", action="store_true", help="列出设备与档案状态")
    ap.add_argument("--all", action="store_true", help="给所有可信设备登记")
    ap.add_argument("--device", help="只登记指定设备（序号或 endpoint id）")
    ap.add_argument("--test", action="store_true", help="现场试一次判定")
    ap.add_argument("--lead", type=int, default=5,
                    help="试音前的倒计时秒数（默认 5；由外部代跑时给大一点）")
    ap.add_argument("--show", action="store_true", help="打印档案库")
    ap.add_argument("--remove", help="删除某设备档案")
    ap.add_argument("--set-threshold", nargs=2, metavar=("ID", "V"),
                    help="手工设置某设备阈值")
    ap.add_argument("--rounds", type=int, default=3, help="每个设备录几遍（默认 3）")
    ap.add_argument("--others", type=int, default=0, help="额外录几遍他人（默认 0）")
    ap.add_argument("--segments", type=int, default=2, help="每遍切几段（默认 2）")
    ap.add_argument("--single-take", type=float, metavar="SECONDS",
                    help="一次连续录音登记（例如 8）：比 --rounds 分几遍容易配合")
    a = ap.parse_args(argv)

    store = ProfileStore()
    print("HOME=%s\n" % C.HOME)

    if a.list:
        return cmd_list(store)
    if a.show:
        return cmd_show(store)
    if a.remove:
        ok = store.remove(a.remove)
        if ok:
            store.save()
        print("已删除" if ok else "没有这条档案")
        return 0 if ok else 2
    if a.set_threshold:
        did, val = a.set_threshold
        p = store.get(did)
        if p is None:
            print("没有这条档案: %s" % did)
            return 2
        p["threshold"] = float(val)
        store.save()
        print("阈值已设为 %.2f" % float(val))
        return 0
    if a.test:
        return cmd_test(store, a.device, a.lead)
    if a.single_take:
        return cmd_enroll_single(store, a.device or "", a.single_take, a.lead)
    if a.all or a.device:
        return cmd_enroll(store, a.device, a.all, a.rounds, a.others, a.segments,
                          a.lead)

    # ★ 什么参数都不给：走最省事的那条路 —— 一次连续录音。
    #   安装包引导用的就是这个（VoiceUnlock.exe enroll），用户只需照着念一句
    #   现成的话，不用来回配合三遍。
    #   10.0 秒在这里会被规整成 4 段 × 2.015 = 8.06 秒，正好和 LONG_SENTENCE
    #   那句 8 秒的话对上（给 8.0 会被截成 3 段 = 6.0 秒，用户还没念完就停了）。
    return cmd_enroll_single(store, "", 10.0, a.lead)


if __name__ == "__main__":
    sys.exit(main())
