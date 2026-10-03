#!/usr/bin/env python
"""vmvnc.py - tiny VNC helper for the VMware Workstation built-in VNC console.

Connection settings come from the environment (the password has NO default):
  VU_VNC_HOST / VNC_HOST    default 127.0.0.1
  VU_VNC_PORT / VNC_PORT    default 5905
  VU_VNC_PASS / VNC_PASS    VNC password -- REQUIRED, no built-in fallback

Usage:
  python vmvnc.py shot out.png
  python vmvnc.py key <keyname>
  python vmvnc.py hammer <keyname> <seconds> [interval]   # send key repeatedly
  python vmvnc.py info
"""
import sys, time, os

HOST = os.environ.get('VU_VNC_HOST') or os.environ.get('VNC_HOST') or '127.0.0.1'
PORT = int(os.environ.get('VU_VNC_PORT') or os.environ.get('VNC_PORT') or '5905')
PASS = os.environ.get('VU_VNC_PASS') or os.environ.get('VNC_PASS')
if not PASS:
    sys.exit('usage error: set VU_VNC_PASS (VNC password); there is no default')

from vncdotool import api


def connect():
    return api.connect('{}::{}'.format(HOST, PORT), password=PASS)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    c = connect()
    try:
        if cmd == 'shot':
            out = sys.argv[2]
            c.captureScreen(out)
            print('saved ' + out)
        elif cmd == 'key':
            k = sys.argv[2]
            c.keyPress(k)
            print('sent ' + k)
        elif cmd == 'hammer':
            k = sys.argv[2]
            secs = float(sys.argv[3])
            iv = float(sys.argv[4]) if len(sys.argv) > 4 else 0.6
            end = time.time() + secs
            n = 0
            while time.time() < end:
                try:
                    c.keyPress(k)
                    n += 1
                except Exception as e:
                    print('  (reconnect after error: %s)' % e)
                    try:
                        c.disconnect()
                    except Exception:
                        pass
                    time.sleep(1)
                    c = connect()
                time.sleep(iv)
            print('sent %d x %s over %.1fs' % (n, k, secs))
        elif cmd == 'info':
            print('connected to %s::%d' % (HOST, PORT))
        else:
            print(__doc__)
            return 2
    finally:
        try:
            c.disconnect()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
