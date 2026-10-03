"""verify.py -- 判定引擎：采集 -> 去重 -> 多段投票 -> 结论

一次尝试的完整流程：

    1. **重新枚举**当前所有输入设备（不缓存 —— 手机当麦会随时出现/消失，新插的也要认）
    2. 多路**并发**采集：一次连续录 `segments * target_frames` 帧（默认 2×200 帧 ≈ 4.03 秒）
    3. 丢弃基本静音的流（本机 WO Mic 电平低到 peak 4e-4，所以用相对判据）
    4. **同源归组**：虚拟声卡会让同一声音出现在多个端点上，按"每路一票"会把
       一次说话计成 2~3 票 —— 判定永远按"信号源"投票
    5. 每个信号源各自多段投票（见 profiles.decide）
    6. 任一信号源通过即通过；顺便把失败计数/锁定写进 state.json
    7. 通过后：给本次采集到、但还没档案的【可信】设备自动建档（第 3 层覆盖）
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from . import audio as A
from . import config as C
from . import devices as D
from . import sv as S
from .profiles import ProfileStore


# ------------------------------------------------------------------ 失败计数/锁定

class State:
    """很小的一份状态文件：失败次数、锁定截止时间。"""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(C.HOME, "state.json")
        self.d = {"failures": 0, "locked_until": 0.0, "last_ok": 0.0, "note": ""}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.d.update(json.load(f))
        except Exception:
            pass

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path),
                                       prefix=".state-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def locked(self) -> float:
        return max(0.0, float(self.d.get("locked_until", 0)) - time.time())

    def on_fail(self, cfg: dict) -> None:
        dec = cfg.get("decision") or {}
        self.d["failures"] = int(self.d.get("failures", 0)) + 1
        if self.d["failures"] >= int(dec.get("max_failures", 3)):
            self.d["locked_until"] = time.time() + float(dec.get("lockout_seconds", 300))
            self.d["failures"] = 0
            self.d["note"] = "达到失败上限，声纹通道锁定"
        self.save()

    def on_ok(self) -> None:
        self.d["failures"] = 0
        self.d["locked_until"] = 0.0
        self.d["last_ok"] = time.time()
        self.save()


# ------------------------------------------------------------------ 判定引擎

class Verifier:
    def __init__(self, cfg: dict | None = None, store: ProfileStore | None = None,
                 embedder: S.SpeakerEmbedder | None = None,
                 state: State | None = None, logger=None):
        self.cfg = cfg or C.load_config()
        self.store = store or ProfileStore()
        self.log = logger or (lambda m: None)
        aud = self.cfg.get("audio") or {}
        self.embedder = embedder or S.SpeakerEmbedder(
            target_frames=int(aud.get("target_frames", 200)))
        self.state = state if state is not None else State()

    # -------------------------------------------------------------- 主流程

    def attempt(self, only_trusted: bool = True, device_ids=None,
                allow_locked: bool = False, wait_voice_s: float = 0.0,
                count_failures: bool = True, pick_best_window: bool = False,
                best_window_multiplier: float = 3.0) -> dict:
        """跑一次判定。

        count_failures=False 用于【常驻监听】路径：后台一直在听，不该因为
        环境噪声的误触发就烧掉失败额度（踩过：噪声触发 3 次 -> 锁 300 秒 ->
        本人说对了也被拒）。"""
        cfg = self.cfg
        aud = cfg.get("audio") or {}
        T = int(aud.get("target_frames", 200))
        n_seg = int(aud.get("segments", 2))
        timeout = float(aud.get("seg_timeout_s", 6.0))

        left = self.state.locked()
        if left > 0 and not allow_locked:
            return {"accepted": False, "mode": "locked",
                    "reason": "声纹通道已锁定，还需 %.0f 秒" % left}

        # 1. 重新枚举（关键：新插入的设备必须被认到）
        devs = D.list_capture_devices()
        if only_trusted:
            devs = D.trusted_only(devs)
        if device_ids:
            want = set(device_ids)
            devs = [d for d in devs if d["id"] in want]
        if not devs:
            return {"accepted": False, "mode": "no_device",
                    "reason": "没有可用（可信）输入设备"}

        # 2. 常驻监听电平：等有人开口才开始录音（用户明确要求的行为）
        #    为什么不立刻录：锁屏可能挂了很久，没人说话时不该占用麦克风，
        #    也不该拿一段静音去比对（只会白跑一次判定）。
        if wait_voice_s and wait_voice_s > 0:
            trig = A.wait_for_speech(devs, timeout_s=float(wait_voice_s),
                                     logger=self.log)
            if trig is None:
                return {"accepted": False, "mode": "no_speech",
                        "reason": "%.0f 秒内没听到说话声" % wait_voice_s,
                        "devices": [d["name"] for d in devs]}
            devs = [trig]          # 只在触发的这一路录，省事也省算力

        # 3. 需要几段：本设备有档案 -> segments；没有档案 -> provisional_segments
        #    （新设备跨信道会掉分，靠"更多证据"而不是"更严的阈值"来兜）
        n_seg = int(aud.get("segments", 2))
        prov = max(int((cfg.get("decision") or {}).get("provisional_segments", 3)), 1)
        need_seg = n_seg
        for d in devs:
            if self.store.get(d["id"]) is None:
                need_seg = max(need_seg, prov)

        # 3. 并发采集（一次连续录，之后切开/挑窗）
        #    ★ 超时必须按【实际要采的采样数】推导，不能用 need_seg*T。
        #    踩过两次同类坑：
        #      a) 6.015 秒的采集配 6.0 秒超时 -> 整段被丢，报"没听到声音"
        #      b) pick_best_window 要采 6.04 秒（3 倍），超时却仍按 2 秒算成 6.0 秒
        #         -> 同样整段被丢、同样报"没听到声音"（症状一样，误导性极强）
        want = self.embedder.samples_for_segments(need_seg)
        if pick_best_window:
            # 多录几倍，之后挑"语音最密"的那一段。
            # ★ 倍数直接决定【用户说完之后还要干等多久】：
            #   3 倍 = 6.045 秒（实测整条链路 14 秒，锁屏上毫无反馈，体验很差）。
            #   所以常驻监听改成两级递进：先用 1 倍（2.015 秒）抢一次快速判定，
            #   不过再退回 3 倍。同设备档案已经建好时，1 倍通常就够了。
            want = int(want * float(best_window_multiplier))
        cap_secs = want / float(S.SR)
        cap_timeout = max(float(aud.get("seg_timeout_s", 6.0)), cap_secs + 4.0)
        samples, errs = A.capture_multi(devs, samples=want, timeout_s=cap_timeout)

        # 4. 丢静音
        active, silent = {}, {}
        for did, a in samples.items():
            st = A.speech_stats(a)
            (silent if st["silent"] else active)[did] = a
        if not active:
            # 把采集错误一并带出来 —— 否则"没听到声音"会掩盖真正的失败原因
            # （踩过：其实是超时导致整段被丢，却报成"没听到声音"）
            extra = ""
            if errs:
                extra = "；采集错误: " + "; ".join(
                    "%s=%s" % (k.rsplit(".", 1)[-1][:8], v) for k, v in errs.items())
            return {"accepted": False, "mode": "silent",
                    "reason": "没听到声音（或设备电平过低）" + extra,
                    "devices": [d["name"] for d in devs], "errors": errs,
                    "silent": list(silent)}

        # 5. 同源归组
        groups = A.group_same_source(active)

        # 6. 每个信号源多段投票
        results = []
        for g in groups:
            # 先剪掉首尾静音：电平触发是在开口瞬间发生的，若人只说了一两秒，
            # 剩下的静音段 embedding 会完全一致，判定必然失败（实测踩过）。
            speech = A.trim_to_speech(g["rep"])
            step = self.embedder.samples_for_segments(1)
            need_len = step * need_seg
            if len(speech) < need_len:
                # 短句场景：裁剪后不够长，但【整段】够长就直接用整段。
                # 理由：电平触发发生在开口那一瞬间，所以整段 = 从开口起算的
                # 完整窗口，里面大部分就是本人语音。因为"裁掉了静音"就把用户
                # 拒掉，是很蠢的行为。
                if len(g["rep"]) >= need_len:
                    speech = g["rep"]
            avail = len(speech) // step if step else 0
            if avail < need_seg:
                results.append({
                    "accepted": False, "mode": "too_short", "scores": [],
                    "threshold": None, "matched": None,
                    "reason": ("语音太短：只够 %d 段（每段 %.1f 秒），需要 %d 段。"
                               "请连续多说 %.1f 秒"
                               % (avail, self.cfg.get("audio", {}).get("target_frames", 200) * 0.01,
                                  need_seg, need_seg * self.cfg.get("audio", {})
                                  .get("target_frames", 200) * 0.01)),
                    "device_id": g["ids"][0], "device_ids": g["ids"],
                    "peak": round(g["peak"], 6),
                })
                continue
            if pick_best_window and need_seg == 1:
                emb, info = self.embedder.embed_best_window(speech)
                embs = [emb]
                self.log("[agent] 挑窗：第 %.2f 秒起（语音帧 %d，rms %.5f）"
                         % (info["offset_samples"] / 16000.0, info["speech_frames"],
                            info["rms"]))
            else:
                embs, info = self.embedder.embed_segments(speech, n_segments=need_seg)
            did = g["ids"][0]
            dec = self.store.decide(embs, did, cfg)
            dec.update({"device_id": did, "device_ids": g["ids"],
                        "peak": round(g["peak"], 6), "info": info})
            results.append(dec)
        results.sort(key=lambda r: (not r["accepted"], -(max(
            [s for s in r["scores"] if s is not None] or [-9]))))

        best = results[0]
        out = {
            "accepted": bool(best["accepted"]),
            "mode": best["mode"],
            "reason": best["reason"],
            "scores": best["scores"],
            "threshold": best["threshold"],
            "matched": best.get("matched"),
            "device_id": best["device_id"],
            "device_ids": best["device_ids"],
            "segments_captured": need_seg,
            "segments_normal": n_seg,
            "new_device": bool(need_seg > n_seg),   # 调用方据此提示"新设备，请多说一句"
            "groups": [{k: v for k, v in r.items() if k not in ("per_segment", "info")}
                       for r in results],
            "errors": errs,
        }

        # 7. 计数
        if out["accepted"]:
            self.state.on_ok()
            # 8. 给没档案的可信设备自动建档（身份已由本次判定确认）
            if bool((cfg.get("decision") or {}).get("auto_enroll", True)):
                out["auto_enrolled"] = self._auto_enroll(active, groups, best)
        else:
            if count_failures:
                self.state.on_fail(cfg)
        return out

    # -------------------------------------------------------------- 自动登记

    def _enroll_vector(self, a):
        """从这次采集里取出与【判定路径同一性质】的那一段来建档，并过质量闸。

        为什么必须挑窗（踩过两次）：
          1) 原来直接拿原始缓冲按"前 2 秒"嵌入。常驻监听采的是 3 倍长缓冲
             （pick_best_window=True），语音在中间 —— 于是建档算的是【开头那段
             静音】，建出来的"声纹"是噪声，之后该设备怎么考都只有 0.24。
          2) 就算位置对了，短句里也会混进大半静音，统计池化会偏（实测同一人
             从 0.24 掉到 0.09）。
        所以这里与判定路径完全一致：先剪首尾静音 -> 再挑"语音最密"的那 2 秒。

        质量闸：语音帧不够就【拒绝建档】。宁可这次不建（下次密码解锁还会再来
        一遍），也不要把一段静音写进档案库 —— 那是会持续害人的。
        """
        T = int((self.cfg.get("audio") or {}).get("target_frames", 200))
        step = self.embedder.samples_for_segments(1, target_frames=T)
        speech = A.trim_to_speech(a)
        if len(speech) < step and len(a) >= step:
            speech = a            # 裁完不够长就退回整段（与 attempt 里的处理一致）
        if len(speech) < step:
            raise ValueError("音频太短：建档要 %d 采样(%.2f 秒)，只有 %d"
                             % (step, step / float(S.SR), len(speech)))
        emb, info = self.embedder.embed_best_window(speech, target_frames=T)
        # ★ 单位坑：embed_best_window 的 speech_frames【不是 10ms 帧】，而是它内部的
        #   samples_for_frames(25) ≈ 0.265 秒能量块 —— 2 秒窗里最多只有 7~8 块。
        #   （实测日志："语音帧 3" / "语音帧 7" 就是这个量级。）
        #   所以门槛用【比例】表达，别用绝对帧数。
        hop = S.samples_for_frames(25)
        total_hops = max(1, int(step // hop))
        ratio = float(info["speech_frames"]) / float(total_hops)
        min_ratio = float((self.cfg.get("decision") or {})
                          .get("auto_enroll_min_speech_ratio", 0.5))
        if ratio < min_ratio:
            raise ValueError(
                "质量不足：2 秒窗里只有 %d/%d 块像语音（%.0f%%，需 ≥%.0f%%），不建档"
                % (info["speech_frames"], total_hops, ratio * 100.0, min_ratio * 100.0))
        return emb, info

    def _auto_enroll(self, active: dict, groups: list, best: dict) -> list:
        """把本次判定通过时、采集到但还没档案的【可信】设备登记下来。

        安全性：只在 accepted 之后调用 —— 身份已被确认，这些设备录到的是同一段本人语音。
        只登记可信设备：不可信设备（混音总线）上可能混着别人的声音或你自己的克隆音，
        拿它建档等于把后门写进档案库。
        另外：**同源组里的其它端点**不需要单独建档（它们是同一物理声源，
        登记了也只是重复），只给不同信号源的设备建档。
        """
        done = []
        # 只跳过【同一信号源的兄弟端点】；获胜设备本身必须建档 ——
        # 这一步把该设备从"跨设备兜底"升级为"本设备专属"：
        # 分数从跨信道的 ~0.24 跳到同信道的 ~0.9，段数也能从 3 降回 2。
        same_group_ids = set(best.get("device_ids") or []) - {best.get("device_id")}
        devs = {d["id"]: d for d in D.trusted_only(D.list_capture_devices())}
        for did, a in active.items():
            if did in same_group_ids:
                continue                      # 同一信号源的兄弟端点，跳过
            if self.store.get(did) is not None:
                continue                      # 已有档案
            d = devs.get(did)
            if d is None:
                continue                      # 不可信 / 已消失
            try:
                vec, info = self._enroll_vector(a)
                self.store.auto_enroll(did, d["name"], vec, self.cfg,
                                       note="auto: 由一次已确认的解锁自动登记")
                done.append({"id": did, "name": d["name"],
                             "speech_frames": info["speech_frames"],
                             "window_at_s": round(info["offset_samples"] / float(S.SR), 2)})
            except Exception as e:
                done.append({"id": did, "name": d.get("name"), "skipped": str(e)[:120]})
        if done:
            self.store.save()
        return done

    def enroll_from_confirmed_unlock(self, audio_by_device: dict) -> list:
        """给"密码/远程解锁/退出守夜模式"这条路径用。

        新设备第一次用必然是走密码的（正常流程），所以在这里顺手建档最自然：
        不需要用户额外操作，也不降低安全性。
        """
        cfg = self.cfg
        if not bool((cfg.get("decision") or {}).get("auto_enroll", True)):
            return []
        devs = {d["id"]: d for d in D.trusted_only(D.list_capture_devices())}
        done = []
        for did, a in (audio_by_device or {}).items():
            d = devs.get(did)
            if d is None or self.store.get(did) is not None:
                continue
            if A.speech_stats(a)["silent"]:
                continue
            try:
                vec, info = self._enroll_vector(a)
                self.store.auto_enroll(did, d["name"], vec, cfg,
                                       note="auto: 由一次已确认的密码解锁登记")
                done.append({"id": did, "name": d["name"],
                             "speech_frames": info["speech_frames"],
                             "window_at_s": round(info["offset_samples"] / float(S.SR), 2)})
            except Exception as e:
                done.append({"id": did, "name": d.get("name"), "skipped": str(e)[:120]})
        if done:
            self.store.save()
        return done
