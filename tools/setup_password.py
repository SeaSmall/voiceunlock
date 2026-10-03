"""setup_password.py -- 开发期薄壳；真身已搬进 src/setup_password.py

为什么搬：安装包要用同一套逻辑，而 tools/ 不进包。留这个壳是为了不破坏
已有的使用习惯（仓库根目录的 setup_password.cmd 仍然指向这里）。

注意默认行为已改：**默认只保存凭据，不改系统密码**。
要同时设置本机登录密码，显式加 --set-windows-password。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import cli                          # noqa: E402
from src.setup_password import main          # noqa: E402

if __name__ == "__main__":
    cli.fix_console()
    sys.exit(main())
