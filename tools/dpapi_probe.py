"""dpapi_probe.py -- 判断"两个进程的 DPAPI 上下文是不是同一个"

背景：同一个用户、同一台机器、同一个 credential.bin，
      A 进程 encrypt -> unprotect 成功；B 进程 unprotect 同一个 blob 却
      0x8009000B (NTE_BAD_KEY_STATE)。必须分清是"上下文不同"还是"blob 坏了"。

做法：每个进程做三件事，各自落一个自己的 blob，然后交叉解密对方的 blob。
    * 自己往返成功 + 解不开对方 -> 两把不同的主密钥（上下文不同）
    * 自己往返成功 + 也解不开 credential.bin -> 那份凭据是在另一个上下文里写的
    * 自己往返失败 -> 本上下文根本没有可用的 DPAPI 主密钥

用法：
    python tools/dpapi_probe.py --tag mine
    python tools/dpapi_probe.py --tag console
输出是 ASCII 标记，避免控制台编码干扰。
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.dpapi import CredentialStore, protect, unprotect    # noqa: E402

LOGS = os.path.join(ROOT, "logs")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="本进程的标签（用于命名自己的 blob）")
    a = ap.parse_args()
    os.makedirs(LOGS, exist_ok=True)

    print("=== dpapi probe: tag=%s ===" % a.tag)
    print("  pid=%d  session=%s" % (os.getpid(), _session_id()))
    print("  user=%s  appdata=%s" % (os.environ.get("USERNAME"),
                                     os.environ.get("APPDATA")))

    # 1) 自己往返
    mine = os.path.join(LOGS, "blob_%s.bin" % a.tag)
    try:
        blob = protect(("from-%s" % a.tag).encode("utf-8"))
        with open(mine, "wb") as f:
            f.write(blob)
        back = unprotect(blob).decode("utf-8")
        print("  [1] self round-trip : OK (%d bytes, %s)" % (len(blob), back))
    except Exception as e:
        print("  [1] self round-trip : FAIL %s" % (e,))

    # 2) 解现有凭据文件
    try:
        st = CredentialStore()
        if st.exists():
            pw = st.load(with_password=True)["password"]
            print("  [2] credential.bin  : OK (password length=%d)" % len(pw))
        else:
            print("  [2] credential.bin  : (missing)")
    except Exception as e:
        print("  [2] credential.bin  : FAIL %s" % (e,))

    # 3) 交叉解密别的 tag 写下的 blob
    others = [p for p in glob.glob(os.path.join(LOGS, "blob_*.bin"))
              if os.path.abspath(p) != os.path.abspath(mine)]
    if not others:
        print("  [3] cross-decrypt   : (no other blob yet)")
    for p in sorted(others):
        try:
            v = unprotect(open(p, "rb").read()).decode("utf-8")
            print("  [3] cross-decrypt %-22s : OK (%s)"
                  % (os.path.basename(p), v))
        except Exception as e:
            print("  [3] cross-decrypt %-22s : FAIL %s"
                  % (os.path.basename(p), str(e)[:60]))
    return 0


def _session_id() -> int:
    pid = ctypes.wintypes.DWORD()
    ctypes.windll.kernel32.ProcessIdToSessionId(
        ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(pid))
    return pid.value


if __name__ == "__main__":
    sys.exit(main())
