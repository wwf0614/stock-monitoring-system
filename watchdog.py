# -*- coding: utf-8 -*-
"""盯盘服务看门狗。

问题背景：server.py 是纯 pythonw 后台进程，一旦被误关（或系统清理）就没有任何
东西把它拉回来，看板会一直显示「连接异常」，而用户往往看不出是"服务根本没在跑"。

本脚本由计划任务「盯盘系统-服务看门狗」每 5 分钟调用一次，逻辑：
    探测 /api/health -> 连续两次失败才判定服务已死 -> 以脱离进程方式拉起 server.py
幂等：服务健在时它只发一个请求就退出，不做任何事。

也可手动执行：D:\\python\\python.exe watchdog.py
"""
import os
import subprocess
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYW = r"D:\python\pythonw.exe"
PORT = "8765"
LOG = os.path.join(BASE_DIR, "watchdog.log")
LOG_MAX = 200 * 1024
DETACHED_PROCESS = 0x00000008

for _k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_k, None)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            with open(LOG, "r", encoding="utf-8", errors="replace") as f:
                tail = f.read()[-LOG_MAX // 2:]
            with open(LOG, "w", encoding="utf-8") as f:
                f.write("...(日志截断)...\n" + tail)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def alive(timeout=8):
    """服务在监听且能返回 JSON 即算活着（预热中也会返回 200）。"""
    try:
        with _OPENER.open("http://127.0.0.1:%s/api/health" % PORT, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def spawn():
    return subprocess.Popen(
        [PYW, os.path.join(BASE_DIR, "server.py"), PORT],
        cwd=BASE_DIR, close_fds=True, creationflags=DETACHED_PROCESS,
    )


def main():
    if alive():
        return 0
    time.sleep(5)                      # 可能是瞬时抖动，隔 5 秒复核一次
    if alive():
        return 0
    log("服务无响应，正在拉起 server.py %s" % PORT)
    try:
        p = spawn()
    except Exception as e:
        log("拉起失败: %r" % (e,))
        return 1
    for _ in range(12):                # 最多等 24 秒确认
        time.sleep(2)
        if alive():
            log("已恢复 (pid=%s)" % p.pid)
            return 0
    log("已拉起 pid=%s，但 /api/health 仍无响应" % p.pid)
    return 1


if __name__ == "__main__":
    sys.exit(main())
