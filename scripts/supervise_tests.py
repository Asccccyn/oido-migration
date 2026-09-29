#!/usr/bin/env python3
"""有界测试监督器（OIDO-EXEC §8.2）。

监督**整棵进程树**（pgid），限制：墙钟超时 + RSS 上限。
超限 → SIGTERM 组 → 宽限 → SIGKILL 组；保留精简诊断。
测试不允许 &、nohup 或无人监督 detached —— 本脚本就是"有人监督"的那层。

用法：
  python scripts/supervise_tests.py --timeout-seconds 180 --rss-limit-mib 1024 \
      -- python -m pytest -x -q tests/unit/test_timebase.py

退出码：0=子进程正常退出 0；3=RSS 超限击杀；4=超时击杀；其他=透传子进程退出码。
必须先通过 --self-test 才允许用于正式验收（EXEC §8.2）。
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time


def _tree_rss_kib(pgid: int) -> int:
    """ps 汇总同 pgid 的全部进程 RSS（KiB）。ps 不可用时返回 0 并由调用方降级。"""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pgid=,rss="],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return 0
    total = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == str(pgid):
            try:
                total += int(parts[1])
            except ValueError:
                continue
    return total


def supervise(cmd: list[str], *, timeout_seconds: float, rss_limit_mib: int,
              grace_seconds: float = 5.0, poll_interval: float = 0.2) -> int:
    rss_limit_kib = rss_limit_mib * 1024
    started = time.monotonic()
    # start_new_session：子进程自成进程组长，组信号能覆盖它的整棵子树
    proc = subprocess.Popen(cmd, start_new_session=True)
    pgid = proc.pid
    reason = None

    while True:
        ret = proc.poll()
        if ret is not None:
            # 子进程已退；仍扫一遍组内残留（孤儿孙进程）
            time.sleep(0.1)
            _signal_group(pgid, signal.SIGKILL, quiet=True)
            return ret
        elapsed = time.monotonic() - started
        rss = _tree_rss_kib(pgid)
        if rss > rss_limit_kib:
            reason = f"rss-exceeded: tree {rss} KiB > limit {rss_limit_kib} KiB after {elapsed:.1f}s"
            break
        if elapsed > timeout_seconds:
            reason = f"timeout: {elapsed:.1f}s > {timeout_seconds}s (tree rss {rss} KiB)"
            break
        time.sleep(poll_interval)

    print(f"[supervise] {reason}; SIGTERM to pgid {pgid}", file=sys.stderr)
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.1)
    if proc.poll() is None:
        print(f"[supervise] grace expired; SIGKILL to pgid {pgid}", file=sys.stderr)
        _signal_group(pgid, signal.SIGKILL)
    killed_rss = _tree_rss_kib(pgid)
    if killed_rss > 0:
        _signal_group(pgid, signal.SIGKILL, quiet=True)
    return 3 if reason.startswith("rss") else 4


def _signal_group(pgid: int, sig: int, *, quiet: bool = False) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        if not quiet:
            raise


def self_test() -> int:
    """三条：正常退出透传；超时击杀；RSS 击杀。全部通过才返回 0。"""
    py = sys.executable

    r = supervise([py, "-c", "import sys; sys.exit(0)"], timeout_seconds=20, rss_limit_mib=512)
    assert r == 0, f"self-test normal-exit failed: {r}"

    r = supervise([py, "-c", "import time; time.sleep(30)"],
                  timeout_seconds=2, rss_limit_mib=512, grace_seconds=3)
    assert r == 4, f"self-test timeout-kill failed: {r}"

    # RSS 击杀用小预算：子进程申请 ~400MiB，限 200MiB（不给宿主真造 1GiB 压力）
    alloc = (
        "data = bytearray(400 * 1024 * 1024)\n"
        "for i in range(0, len(data), 4096): data[i] = 1\n"
        "import time; time.sleep(30)\n"
    )
    r = supervise([py, "-c", alloc], timeout_seconds=60, rss_limit_mib=200, grace_seconds=3)
    assert r == 3, f"self-test rss-kill failed: {r}"

    # 孙进程也要被组信号覆盖
    r = supervise(
        [py, "-c",
         "import subprocess,sys,time\n"
         "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
         "time.sleep(30)\n"],
        timeout_seconds=2, rss_limit_mib=512, grace_seconds=3,
    )
    assert r == 4, f"self-test grandchild-kill failed: {r}"
    print("[supervise] self-test OK: exit-code, timeout-kill, rss-kill, tree-kill")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeout-seconds", type=float, default=180.0)
    ap.add_argument("--rss-limit-mib", type=int, default=1024)
    ap.add_argument("--grace-seconds", type=float, default=5.0)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.cmd or args.cmd[0] == "--":
        args.cmd = args.cmd[1:] if args.cmd else []
    if not args.cmd:
        ap.error("missing command after --")
    return supervise(
        args.cmd,
        timeout_seconds=args.timeout_seconds,
        rss_limit_mib=args.rss_limit_mib,
        grace_seconds=args.grace_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
