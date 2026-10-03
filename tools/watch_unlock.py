"""watch_unlock.py -- 盯住一次解锁的全过程，记成时间线

为什么需要这个（踩过）：
    安全日志里的 4801 = "工作站被解锁"，但它**不能**证明"桌面回来了" ——
    实测出现 4801 之后 6.6 秒又有 4800（又被锁回去）。而 agent.log 里那句
    "交付凭据"只说明我们交了凭据，更说明不了最终状态。
    只看这两者中的任何一个都会误判。所以把三件事按同一时间轴钉在一起：
        1) 当前输入桌面是谁（Default = 普通桌面；Winlogon = 安全桌面/锁屏）
        2) 系统认为锁没锁（session.is_locked，桌面名优先）
        3) 状态变化（只在变化时记一行，免得刷屏）
然后用 tools\\ 里的日志和安全日志 4800/4801 对照，就能确定：
    解锁到底发生了几次、每次之后桌面有没有回来、多久后被锁回去。

用法（不需要管理员，但看安全日志要管理员）：
    python tools\\watch_unlock.py --seconds 150
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import session      # noqa: E402

LOG = os.path.join(ROOT, "logs", "watch_unlock.log")


def stamp() -> str:
    return time.strftime("%H:%M:%S") + (".%03d" % int((time.time() % 1) * 1000))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=150.0)
    ap.add_argument("--poll", type=float, default=0.4)
    a = ap.parse_args()

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    f = open(LOG, "a", encoding="utf-8")

    def emit(line: str) -> None:
        f.write(line + "\n")
        f.flush()

    emit("")
    emit("=== watch_unlock 开始 %s  持续 %.0f 秒 ===" % (stamp(), a.seconds))
    prev = None
    last_beat = 0.0
    n_unlocked = 0
    deadline = time.time() + a.seconds
    while time.time() < deadline:
        try:
            locked = session.is_locked()
            desk = session.input_desktop_name()
            sig = session.lock_signals()
        except Exception as e:
            emit("  %s  探测异常: %r" % (stamp(), e))
            time.sleep(1.0)
            continue
        cur = (locked, desk)
        if cur != prev:
            if prev is not None and prev[0] and not locked:
                n_unlocked += 1
                emit("  %s  ★★ 变为【未锁】(第 %d 次) 桌面=%r  %s"
                     % (stamp(), n_unlocked, desk, sig))
            else:
                emit("  %s  锁=%s 桌面=%r  %s"
                     % (stamp(), "是" if locked else "否", desk, sig))
            prev = cur
            last_beat = time.time()
        elif time.time() - last_beat >= 5.0:
            last_beat = time.time()
            emit("  %s  ...持续 锁=%s 桌面=%r" % (stamp(), "是" if locked else "否", desk))
        time.sleep(a.poll)

    emit("=== watch_unlock 结束 %s  期间观察到解锁 %d 次 ===" % (stamp(), n_unlocked))
    f.close()
    print("时间线已写入 %s（期间解锁 %d 次）" % (LOG, n_unlocked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
