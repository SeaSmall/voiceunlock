"""profiles.py -- 声纹档案库与判定规则

核心问题：**新插入的设备没有档案，怎么办？**（用户明确要求"包括新插入的"）

三层设计：

  1. **每设备一份档案 + 每设备一个阈值** —— 精度最高（同设备同信道）。
  2. **跨设备兜底**：档案里没有该设备时，与【所有已知档案】比对取最大。
     但跨信道必然掉分。所以这一路的判据**不是"更严的阈值"**（分本来就低，
     更严只会更拒），而是**要更多证据**：`provisional_segments` 段全过才算过
     （默认 3 段，而本设备有档案时只要 2 段）。
  3. **自动登记**：任何一次"用非声纹手段成功解锁"（密码 / 远程解锁 / 退出守夜模式）
     时，只要当时有活跃输入设备，就把那段语音登记成该设备的档案。
     新设备第一次用必然是走密码的（正常流程），所以这一步**不需要额外操作、
     也不降低安全性** —— 下一次它就是一等了。

这就是"所有语音输入设备（含新插入）都能用"的完整答案：
  覆盖靠第 2/3 层，精度靠第 1 层。
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile

import numpy as np

from . import config as C


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def cosine(a, b) -> float:
    x = np.asarray(a, dtype=np.float64).ravel()
    y = np.asarray(b, dtype=np.float64).ravel()
    nx, ny = float(np.linalg.norm(x)), float(np.linalg.norm(y))
    if nx <= 0 or ny <= 0:
        return 0.0
    return float(np.dot(x, y) / (nx * ny))


class ProfileStore:
    """profiles.json 的读写 + 判定规则。

    文件结构：
        {"version": 2,
         "profiles": {"<endpoint id>": {"name","vector","threshold","samples",
                                        "self_min","other_max","enrolled_at",
                                        "source","provisional"}}}
    """

    VERSION = 2

    def __init__(self, path: str | None = None):
        self.path = path or C.PROFILES_PATH
        self.profiles: dict = {}
        self.load()

    # ---------------------------------------------------------------- 存取

    def load(self) -> None:
        d = C.load_profiles(self.path)
        profs = d.get("profiles") or {}
        out = {}
        for k, v in profs.items():
            try:
                v = dict(v)
                v["vector"] = np.asarray(v["vector"], dtype=np.float64).reshape(-1)
                if v["vector"].size == 0:
                    continue
                out[k] = v
            except Exception:
                continue          # 单条坏数据不该让整库起不来
        self.profiles = out

    def save(self) -> str:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {"version": self.VERSION, "profiles": {}}
        for k, v in self.profiles.items():
            v = dict(v)
            v["vector"] = [float(x) for x in np.asarray(v["vector"]).ravel()]
            payload["profiles"][k] = v
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path),
                                   prefix=".prof-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return self.path

    # ---------------------------------------------------------------- 增删查

    def put(self, device_id: str, name: str, vector, threshold: float,
            samples: int = 0, self_min: float | None = None,
            other_max: float | None = None, source: str = "manual",
            provisional: bool = False, extra: dict | None = None) -> dict:
        v = np.asarray(vector, dtype=np.float64).ravel()
        n = float(np.linalg.norm(v))
        if n <= 0:
            raise ValueError("声纹向量范数为 0")
        rec = {
            "name": name or device_id,
            "vector": v / n,
            "threshold": float(threshold),
            "samples": int(samples),
            "self_min": None if self_min is None else float(self_min),
            "other_max": None if other_max is None else float(other_max),
            "enrolled_at": _now(),
            "source": source,
            "provisional": bool(provisional),
        }
        if extra:
            rec.update(extra)
        self.profiles[device_id] = rec
        return rec

    def get(self, device_id: str):
        return self.profiles.get(device_id)

    def remove(self, device_id: str) -> bool:
        return self.profiles.pop(device_id, None) is not None

    def ids(self) -> list:
        return list(self.profiles.keys())

    def summary(self) -> list:
        out = []
        for k, v in self.profiles.items():
            out.append({
                "id": k, "name": v.get("name"), "threshold": v.get("threshold"),
                "samples": v.get("samples"), "source": v.get("source"),
                "self_min": v.get("self_min"), "other_max": v.get("other_max"),
                "enrolled_at": v.get("enrolled_at"),
            })
        return out

    # ---------------------------------------------------------------- 判定

    def score_one(self, emb, device_id: str | None, cfg: dict | None = None,
                  exclude: str | None = None) -> dict:
        """单段打分。返回 {"mode","score","threshold","matched","ok"}。

        mode:
          device      —— 该设备有自己的档案（最优）
          cross       —— 没有该设备档案，与所有已知档案比取最大（跨信道）
          no_profile  —— 一个可比档案都没有
          denied      —— 策略要求拒绝未知设备

        exclude：跨档案比较时把这个档案排除掉。**decide 的兜底路径必须用它**——
        否则一份"坏掉的自身档案"会同时毒化兜底：它自己既是最高分来源，
        又把它那高得离谱的阈值带进来（实测模拟：b2 对 b1 得 0.90，而 b1 的阈值
        被设成 0.95，于是兜底照样过不去，兜底等于没做）。
        """
        cfg = cfg or C.load_config()
        dec = cfg.get("decision") or {}
        policy = str(dec.get("unknown_device_policy", "provisional")).lower()
        cross_margin = float(dec.get("cross_margin", 0.05))

        p = self.profiles.get(device_id) if device_id else None
        if p is not None:
            s = cosine(emb, p["vector"])
            thr = float(p["threshold"])
            return {"mode": "device", "score": s, "threshold": thr,
                    "matched": device_id, "ok": s >= thr}

        pool = {k: v for k, v in self.profiles.items() if k != exclude}
        if not pool:
            return {"mode": "no_profile", "score": None, "threshold": None,
                    "matched": None, "ok": False}

        if policy == "deny":
            return {"mode": "denied", "score": None, "threshold": None,
                    "matched": None, "ok": False}

        best_id, best = None, -1.0
        for did, q in pool.items():
            s = cosine(emb, q["vector"])
            if s > best:
                best, best_id = s, did
        # 跨档案的阈值取【最像的那份档案自己的阈值】，而不是 max(所有档案阈值)。
        # 踩过两个后果：档案越多门槛越高；且门槛与你"到底像谁"无关 ——
        # 一个刚自动登记、阈值偏高的设备会把整个跨设备兜底一起拖死。
        thr = float(pool[best_id]["threshold"])
        if policy == "cross_strict":
            thr += cross_margin
        return {"mode": "cross", "score": best, "threshold": thr,
                "matched": best_id, "ok": best >= thr}

    def decide(self, embs, device_id: str | None, cfg: dict | None = None) -> dict:
        """多段投票。

        - 该设备有档案：按 cfg.decision.vote（all/any）判定，段数 = cfg.audio.segments
        - 该设备无档案：**要更多证据** —— 全部 provisional_segments 段都要过
        - 该设备有档案但**没过**：不直接拒，换一套对比基准再判一次（跨档案兜底），
          仍不过才拒。理由见下面 ★ 处。
        """
        cfg = cfg or C.load_config()
        dec = cfg.get("decision") or {}
        policy = str(dec.get("unknown_device_policy", "provisional")).lower()
        embs = [e for e in (embs or []) if e is not None]
        if not embs:
            return {"accepted": False, "reason": "没有可用音频", "scores": [], "mode": "empty"}

        rows = [self.score_one(e, device_id, cfg) for e in embs]
        mode = rows[0]["mode"]

        if mode == "no_profile":
            return {"accepted": False, "reason": "还没有任何声纹档案，请先登记",
                    "scores": [r["score"] for r in rows], "mode": mode, "threshold": None}
        if mode == "denied":
            return {"accepted": False, "reason": "该设备未登记且策略为 deny",
                    "scores": [r["score"] for r in rows], "mode": mode, "threshold": None}

        used = rows
        if mode == "device":
            vote = str(dec.get("vote", "all")).lower()
            need = int((cfg.get("audio") or {}).get("segments", 2))
            need = max(1, min(need, len(rows)))
            oks = [bool(r["ok"]) for r in rows]
            accepted = all(oks[:need]) if vote == "all" else any(oks)
            reason = "设备档案通过" if accepted else "设备档案未通过"

            # ★ 本设备档案没过 ≠ 不是本人。
            #   档案本身可能就不靠谱：样本少、信道变过（换了驱动/增益/摆位）、
            #   或者自动登记时用的那段语音质量差。
            #   踩过：USB 设备被自动建档成 thr=0.31，而它跨信道只考 0.244 ——
            #   此后本人每一次都被自己的档案拒掉，且**没有任何退路**。
            #   所以这里换一套对比基准再判一次：与库里的其它档案比。
            #   要求【每一段都过】：兜底走的是跨信道，比本设备专属弱，证据要齐。
            #   deny 策略下不做这个兜底 —— 那是"只认本设备档案"的意思，要尊重。
            if not accepted and policy != "deny":
                # exclude=device_id 是关键：不能拿【本设备自己那份坏档案】当参照，
                # 否则它既是最高分来源、又把高得离谱的阈值带进来，兜底等于没做。
                xrows = [self.score_one(e, None, cfg, exclude=device_id) for e in embs]
                xs = [r["score"] for r in xrows if r["score"] is not None]
                if xrows and all(bool(r["ok"]) for r in xrows):
                    used, mode, accepted = xrows, "device+cross", True
                    reason = ("设备档案未通过，但跨档案兜底全过（%d 段；最像 %s "
                              "%.4f ≥ %.4f）"
                              % (len(xrows), xrows[0]["matched"],
                                 max(xs) if xs else 0.0, xrows[0]["threshold"]))
                elif xs:
                    reason = ("设备档案未通过；跨档案兜底也未通过"
                              "（最像 %s %.4f < %.4f）"
                              % (xrows[0]["matched"], max(xs), xrows[0]["threshold"]))
        else:  # cross：跨信道，靠更多段数换
            oks = [bool(r["ok"]) for r in rows]
            need = max(int(dec.get("provisional_segments", 3)), 1)
            if len(oks) < need:
                # 不能悄悄把要求降成"实际采到的段数"，否则"要更多证据"就是空的
                accepted = False
                reason = ("跨设备兜底证据不足（需 %d 段，只有 %d 段）"
                          % (need, len(oks)))
            else:
                accepted = all(oks[:need])
                reason = ("跨设备兜底通过（%d 段全过）" % need) if accepted else \
                         ("跨设备兜底未通过（需 %d 段全过，实得 %d/%d）"
                          % (need, sum(oks[:need]), need))

        return {"accepted": bool(accepted), "reason": reason,
                "scores": [r["score"] for r in used], "mode": mode,
                "threshold": used[0]["threshold"], "matched": used[0]["matched"],
                "per_segment": used}

    # ---------------------------------------------------------------- 自动登记

    def auto_enroll(self, device_id: str, name: str, emb, cfg: dict | None = None,
                    threshold: float | None = None, note: str = "") -> dict:
        """用一次"已由其它手段确认是本人"的语音，给新设备建档（第 3 层）。

        ★ 阈值为什么不能拍默认值（踩过）：给新设备建档时，唯一可测的数是
        "这一次语音与本库已有档案有多像" —— 那是**跨信道**的分，实测约 0.24。
        若直接给它 default_threshold=0.31，等于要求它以后考得比它**已经考出来的**
        还高，本人必然被自己的档案拒掉。所以：
            thr = clamp(实测跨档案分 - margin, floor, default_threshold)
        并把实测值和依据一起写进档案，事后可审计。
        """
        cfg = cfg or C.load_config()
        dec = cfg.get("decision") or {}
        emb = np.asarray(emb, dtype=np.float64).ravel()
        p = self.profiles.get(device_id)
        if p is not None:
            # 已有档案：把新向量并进去（在线平滑），样本数 +1
            old = np.asarray(p["vector"], dtype=np.float64).ravel()
            k = max(int(p.get("samples", 1)), 1)
            new = (old * k + emb) / (k + 1)
            p["vector"] = new / np.linalg.norm(new)
            p["samples"] = k + 1
            p["updated_at"] = _now()
            if note:
                p["note"] = note
            return p

        default_thr = float(dec.get("default_threshold", 0.31))
        extra = {"note": note}
        if threshold is not None:
            thr = float(threshold)
            extra["threshold_basis"] = "调用方指定"
        elif not bool(dec.get("auto_enroll_calibrate", True)) or not self.profiles:
            thr = default_thr
            extra["threshold_basis"] = "库里第一份档案，无参照可分，用 default_threshold"
        else:
            c = max(cosine(emb, q["vector"]) for q in self.profiles.values())
            margin = float(dec.get("auto_enroll_margin", 0.05))
            floor = float(dec.get("auto_enroll_floor", 0.15))
            thr = min(default_thr, max(floor, c - margin))
            extra["calibrated_cross"] = round(float(c), 4)
            extra["threshold_basis"] = (
                "实测跨档案分 %.4f - %.2f，再夹到 [%.2f, %.2f]"
                % (c, margin, floor, default_thr))
        return self.put(device_id, name, emb, thr, samples=1, source="auto",
                        provisional=False, extra=extra)


if __name__ == "__main__":
    st = ProfileStore()
    print("档案文件 :", st.path)
    print("档案数量 :", len(st.profiles))
    for s in st.summary():
        print("  - %s  %s  thr=%.2f samples=%d source=%s"
              % (s["id"], s["name"], s["threshold"], s["samples"], s["source"]))
    # 用随机向量验证四种 mode 都能走到
    rng = np.random.RandomState(0)
    print("\n空库判定 :", st.score_one(rng.randn(192), "dev-x")["mode"])
    st.put("dev-a", "测试设备 A", rng.randn(192), 0.31)
    print("有档案判定:", st.score_one(st.profiles["dev-a"]["vector"], "dev-a"))
    print("新设备判定:", st.score_one(st.profiles["dev-a"]["vector"], "dev-B")["mode"])
    print("多段投票 :", st.decide([st.profiles["dev-a"]["vector"]] * 3, "dev-B")["reason"])
