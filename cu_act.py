#!/usr/bin/env python3
"""cu-act —— 带验证的 GUI 动作执行器

和 cu / cu-ax 的区别：那两个只管「做」，这个管「做完到底成没成」。

每一步都是：
    快照 → 动作 → 轮询等结果 → 判定 → 没成就重试（默认 2 次）

判定依据要么是你给的 --expect（出现这段文字 / 这个控件），要么是界面发生了实质变化。
失败会带着证据退出（退出码 1），不会假装自己成功了。

用法见 README.md，或 cu-act --help。
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
CU = os.path.join(ROOT, "cu")
CU_AX = os.path.join(ROOT, "cu-ax")


# ---------------------------------------------------------------- 基础设施

def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def die(msg, code=1):
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    sys.exit(code)


def preflight():
    """锁屏 / 权限检查。任何一步都不该在锁屏时动界面。"""
    rc, out, _ = run([CU, "state"])
    try:
        st = json.loads(out.strip().splitlines()[-1])
    except Exception:
        die("cu state 不可用")
    if st.get("locked"):
        die("screen locked")
    if not st.get("post_events") and not st.get("accessibility"):
        die("no accessibility permission")
    return st


def tree(app=None, pid=None, depth=12, max_nodes=2500):
    cmd = [CU_AX, "tree"]
    if app:
        cmd += ["--app", app]
    if pid:
        cmd += ["--pid", str(pid)]
    cmd += ["--depth", str(depth), "--max", str(max_nodes)]
    rc, out, _ = run(cmd)
    nodes = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{") or '"nodes"' in line:
            continue
        try:
            nodes.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return nodes


def tree_text(nodes):
    """把树压成可比对的文本：只看角色/标题/值，忽略坐标（动画会让坐标抖动）。"""
    rows = sorted(f"{n['role']}\t{n['title']}\t{n['value']}" for n in nodes)
    return "\n".join(rows)


def digest(nodes):
    return hashlib.sha256(tree_text(nodes).encode("utf-8")).hexdigest()[:16]


def find_node(nodes, **kw):
    for n in nodes:
        if all((kw[k] in n.get(k, "") if isinstance(n.get(k), str) else n.get(k) == kw[k])
               for k in kw):
            return n
    return None


# ---------------------------------------------------------------- 动作

def ax_target(target):
    """把 cu-act 的 app=X / pid=123 转成 cu-ax 的参数。"""
    key, val = target.split("=", 1)
    return ["--" + key, val]


def act_press(target, args):
    cmd = [CU_AX, "press"] + ax_target(target)
    if args.title is not None:
        cmd += ["--title", args.title]
    if args.role:
        cmd += ["--role", args.role]
    if args.at:
        cmd += ["--at", str(args.at[0]), str(args.at[1])]
    if args.index != 1:
        cmd += ["--index", str(args.index)]
    rc, out, _ = run(cmd)
    return rc == 0 and '"ok":1' in out, out.strip()


def act_set(target, args):
    cmd = [CU_AX, "set"] + ax_target(target) + ["--value", args.value]
    if args.role:
        cmd += ["--role", args.role]
    if args.index != 1:
        cmd += ["--index", str(args.index)]
    rc, out, _ = run(cmd)
    return rc == 0 and '"ok":1' in out, out.strip()


def act_evclick(target, args):
    pid = resolve_pid(target)
    rc, out, _ = run([CU_AX, "evclick", "--pid", str(pid),
                      str(args.at[0]), str(args.at[1])])
    return rc == 0, f"evclick pid={pid} at={args.at}"


def act_click(target, args):
    """真·坐标点击：会动鼠标。"""
    rc, out, _ = run([CU, "click", str(args.at[0]), str(args.at[1])])
    return rc == 0, out.strip()


def resolve_pid(target):
    rc, out, _ = run([CU_AX, "apps"])
    want = target.split("=", 1)[1]
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and want.lower() in parts[1].lower():
            return int(parts[0])
    die(f"app not found: {want}")


# ---------------------------------------------------------------- 验证

def wait_for(target, args, expect, timeout):
    """轮询到 expect 出现，或界面变化（没有 expect 时）。返回 (ok, 快照)。"""
    deadline = time.time() + timeout
    base = digest(tree(*split_target(target)))
    while True:
        nodes = tree(*split_target(target))
        text = tree_text(nodes)
        if expect:
            if expect in text:
                return True, nodes
        elif digest(nodes) != base:
            return True, nodes
        if time.time() >= deadline:
            return False, nodes
        time.sleep(0.15)


def split_target(target):
    if target.startswith("pid="):
        return None, int(target.split("=", 1)[1])
    return target.split("=", 1)[1], None


def steps_equal(before, after):
    return before == after


# ---------------------------------------------------------------- 主流程

def one_step(target, args, step):
    """执行一步并验证。step 是 dict（来自 flow 文件）合并 CLI 参数。"""
    expect = step["expect"] if "expect" in step else args.expect
    retry = step.get("retry", args.retry)
    timeout = step.get("timeout", args.timeout)
    act = step.get("act") or args.act

    last = {"ok": False}
    for attempt in range(retry + 1):
        before = tree(*split_target(target))
        b_digest = digest(before)

        if act == "press":
            ok_call, raw = act_press(target, argparse.Namespace(
                title=step.get("title"), role=step.get("role"),
                at=step.get("at"), index=step.get("index", 1)))
        elif act == "set":
            ok_call, raw = act_set(target, argparse.Namespace(
                value=step.get("value", ""), role=step.get("role"),
                index=step.get("index", 1)))
        elif act == "evclick":
            ok_call, raw = act_evclick(target, argparse.Namespace(at=step["at"]))
        elif act == "click":
            ok_call, raw = act_click(target, argparse.Namespace(at=step["at"]))
        else:
            die(f"unknown act: {act}")

        # set 的验证最硬：读回值直接比
        if act == "set" and ok_call:
            want = step.get("value", "")
            deadline = time.time() + timeout
            while time.time() < deadline:
                nodes = tree(*split_target(target))
                if any(n.get("value") == want for n in nodes):
                    return {"ok": True, "act": act, "attempt": attempt + 1,
                            "verified_by": "value", "raw": raw}
                time.sleep(0.15)
            last = {"ok": False, "act": act, "attempt": attempt + 1,
                    "reason": "value not applied", "raw": raw}
        else:
            ok, nodes = wait_for(target, args, expect, timeout)
            if ok and ok_call:
                how = "expect" if expect else "changed"
                return {"ok": True, "act": act, "attempt": attempt + 1,
                        "verified_by": how, "raw": raw,
                        "before": b_digest, "after": digest(nodes)}
            last = {"ok": False, "act": act, "attempt": attempt + 1,
                    "reason": "no change" if not expect else f"expect not found: {expect}",
                    "raw": raw, "before": b_digest, "after": digest(nodes)}

        if attempt < retry:
            time.sleep(0.4)

    return last


def main():
    ap = argparse.ArgumentParser(description="带验证的 GUI 动作执行器")
    ap.add_argument("target", help="app=应用名 或 pid=1234")
    ap.add_argument("--act", choices=["press", "set", "evclick", "click"])
    ap.add_argument("--title")
    ap.add_argument("--role")
    ap.add_argument("--value")
    ap.add_argument("--at", nargs=2, type=float, metavar=("X", "Y"))
    ap.add_argument("--index", type=int, default=1)
    ap.add_argument("--expect", help="怎么算成功：界面上要出现这段文字/控件名")
    ap.add_argument("--timeout", type=float, default=5.0, help="每步等结果的秒数")
    ap.add_argument("--retry", type=int, default=2, help="失败重试次数")
    ap.add_argument("--flow", help="跑一个 JSON 步骤文件，而不是单步")
    ap.add_argument("--json", action="store_true", help="只输出机器可读结果")
    args = ap.parse_args()

    preflight()

    if args.flow:
        flow = json.load(open(args.flow, encoding="utf-8"))
        results = []
        for i, step in enumerate(flow):
            r = one_step(args.target, args, step)
            r["step"] = i + 1
            results.append(r)
            if not args.json:
                print(json.dumps(r, ensure_ascii=False))
            if not r["ok"]:
                print(json.dumps({"ok": False, "failed_at": i + 1,
                                  "total": len(flow)}, ensure_ascii=False))
                sys.exit(1)
        print(json.dumps({"ok": True, "steps": len(flow), "results": results},
                         ensure_ascii=False))
        return

    if not args.act:
        die("需要 --act 或 --flow")
    if args.act in ("press", "evclick", "click") and args.title is None and not args.at:
        die("press 需要 --title 或 --at")
    if args.act == "set" and args.value is None:
        die("set 需要 --value")

    r = one_step(args.target, args, {})
    print(json.dumps(r, ensure_ascii=False))
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
