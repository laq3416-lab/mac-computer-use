#!/usr/bin/env python3
"""cu-act —— 带验证、失败会自救的 GUI 动作执行器

三步循环：观察 → 动作 → 验证。失败时先诊断，再按策略救一次，而不是直接放弃。

用法：
    ./cu_act.py "app=文本编辑" --act observe
    ./cu_act.py "app=文本编辑" --act press --title 保存 --expect 已保存
    ./cu_act.py "app=文本编辑" --act set --role AXTextArea --value hi --expect hi
    ./cu_act.py "app=文本编辑" --flow examples/demo.steps.json
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
CU = os.path.join(ROOT, "cu")
CU_AX = os.path.join(ROOT, "cu-ax")
STATE = "/tmp/cu-act-last.json"

PRESS_ROLES = {"AXButton", "AXLink", "AXMenuItem", "AXCheckBox", "AXRadioButton",
               "AXPopUpButton", "AXTab", "AXCell", "AXRow", "AXDisclosureTriangle"}
TEXT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
MODAL_ROLES = {"AXSheet", "AXDialog", "AXSystemDialog"}


# ---------------------------------------------------------------- 基础

def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def die(msg, code=1):
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    sys.exit(code)


def check_app(target):
    """app= 解析不到就早点报错，别让空控件树被当成「没变化」。"""
    if target.startswith("app=") and not pid_of(target):
        die(f"app_not_found: {target.split('=', 1)[1]}")


def preflight():
    rc, out, _ = run([CU, "state"])
    try:
        st = json.loads(out.strip().splitlines()[-1])
    except Exception:
        die("cu state unavailable")
    if st.get("locked"):
        die("screen locked")
    if not st.get("post_events") and not st.get("accessibility"):
        die("no accessibility permission")
    return st


def ax_target(target):
    key, val = target.split("=", 1)
    return ["--" + key, val]


def tree(target, depth=12, max_nodes=2500):
    rc, out, _ = run([CU_AX, "tree"] + ax_target(target) +
                     ["--depth", str(depth), "--max", str(max_nodes)])
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


def focused():
    rc, out, _ = run([CU_AX, "focused"])
    out = out.strip()
    if not out.startswith("{"):
        return None
    try:
        v = json.loads(out)
    except json.JSONDecodeError:
        return None
    return v if v.get("role") else None


def name_of(n):
    return n.get("title") or n.get("desc") or ""


def tree_text(nodes):
    rows = sorted(f"{n['role']}\t{n['title']}\t{n['value']}" for n in nodes)
    return "\n".join(rows)


def digest(nodes):
    return hashlib.sha256(tree_text(nodes).encode()).hexdigest()[:16]


def pid_of(target):
    key, val = target.split("=", 1)
    if key == "pid":
        return int(val)
    rc, out, _ = run([CU_AX, "apps"])
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and val.lower() in parts[1].lower():
            return int(parts[0])
    return 0


# ---------------------------------------------------------------- 观察

def observe(target, limit=40):
    nodes = tree(target)
    wins = [n for n in nodes if n["role"] == "AXWindow"]
    modals = [n for n in nodes if n["role"] in MODAL_ROLES]
    foc = focused()

    controls = []
    for n in nodes:
        if n["role"] in MODAL_ROLES or n["role"] in (
                "AXWindow", "AXMenuBar", "AXMenuBarItem", "AXMenu", "AXApplication", "AXMenuItem"):
            continue
        nm = name_of(n)
        if not nm and not (n["role"] in TEXT_ROLES and n.get("value")):
            continue
        x, y = n["pos"]
        w, h = n["size"]
        if w <= 1 or h <= 1 or y > 1080:      # 菜单位置、零尺寸的容器都不要
            continue
        controls.append({
            "n": (nm or n.get("value", ""))[:40],
            "r": n["role"],
            "p": [int(x + w / 2), int(y + h / 2)],
            "set": n["role"] in TEXT_ROLES,
        })
    controls = controls[:limit]

    return {
        "app": target,
        "window": [w["title"] for w in wins][:3],
        "modal": [m.get("title") or m.get("value") or "?" for m in modals],
        "focused": (foc or {}).get("role"),
        "controls": controls,
        "count": len(controls),
    }


def diff_obs(obs):
    prev = None
    if os.path.exists(STATE):
        try:
            prev = json.load(open(STATE))
        except Exception:
            prev = None
    json.dump(obs, open(STATE, "w"))
    if not prev:
        return []
    pn = {c["n"] for c in prev.get("controls", [])}
    cn = {c["n"] for c in obs["controls"]}
    out = [f"+{n}" for n in cn - pn][:5] + [f"-{n}" for n in pn - cn][:5]
    if prev.get("window") != obs["window"]:
        out.insert(0, f"window: {prev.get('window')} -> {obs['window']}")
    return out


# ---------------------------------------------------------------- 诊断

def diagnose(target, step):
    """返回 (kind, payload)。kind 决定怎么救。"""
    nodes = tree(target)
    modals = [n for n in nodes if n["role"] in MODAL_ROLES]
    if modals:
        return "modal", {"titles": [m.get("title") or m.get("value") or "?" for m in modals],
                         "nodes": nodes, "box": modals[0]}

    pid = pid_of(target)
    if pid and run(["kill", "-0", str(pid)])[0] != 0:
        return "dead", {"pid": pid}

    want = step.get("title")
    if want:
        pool = [name_of(n) for n in nodes
                if n["role"] in PRESS_ROLES | TEXT_ROLES and name_of(n)]
        def score(a, b):
            sa, sb = set(a), set(b)                      # 中文字序不敏感
            jac = len(sa & sb) / max(1, len(sa | sb))
            seq = difflib.SequenceMatcher(None, a, b).ratio()
            return max(seq, jac * 0.9)
        ranked = sorted(((score(want, n), n) for n in pool), reverse=True)[:3]
        scored = [n for sc, n in ranked if sc >= 0.45]
        if scored:
            return "not_found", {"candidates": scored, "nodes": nodes}
    return "unknown", {"nodes": nodes}


def press_node_by_center(target, node):
    x, y = node["pos"]
    w, h = node["size"]
    cx, cy = int(x + w / 2), int(y + h / 2)
    rc, out, _ = run([CU_AX, "press"] + ax_target(target) + ["--at", str(cx), str(cy)])
    return rc == 0 and '"ok":1' in out, f"press@{cx},{cy}"


def recover(target, step, kind, payload, dismiss):
    """按诊断结果救一次。返回 (是否动作了, 说明)。"""
    if kind == "modal":
        mb = payload.get("box") or {}
        mx, my = mb.get("pos", [0, 0]); mw, mh = mb.get("size", [1e9, 1e9])
        def inside(n):
            x, y = n["pos"]; w, h = n["size"]
            cx, cy = x + w / 2, y + h / 2
            return mx - 2 <= cx <= mx + mw + 2 and my - 2 <= cy <= my + mh + 2
        buttons = [n for n in payload["nodes"]
                   if n["role"] == "AXButton" and name_of(n) and inside(n)]
        for d in dismiss:
            for b in buttons:
                if d in name_of(b):
                    nm = name_of(b)
                    rc, out, _ = run([CU_AX, "press"] + ax_target(target) +
                                     ["--title", nm, "--role", "AXButton"])
                    if rc == 0 and '"ok":1' in out:
                        return True, f"dismiss:{nm}"
                    ok, _ = press_node_by_center(target, b)
                    return ok, f"dismiss:{nm}"
        return False, "blocked_by_modal"

    if kind == "not_found":
        cand = payload["candidates"][0]
        for n in payload["nodes"]:
            if name_of(n) == cand:
                ok, _ = press_node_by_center(target, n)
                return ok, f"fuzzy:{cand}"
        return False, "no_candidate"

    if kind == "dead":
        return False, "app_dead"

    return False, "unrecoverable"


# ---------------------------------------------------------------- 动作

def do_action(target, step, act):
    if act == "press":
        cmd = [CU_AX, "press"] + ax_target(target)
        if step.get("title") is not None:
            cmd += ["--title", step["title"]]
        if step.get("role"):
            cmd += ["--role", step["role"]]
        if step.get("at"):
            cmd += ["--at", str(step["at"][0]), str(step["at"][1])]
        if step.get("index", 1) != 1:
            cmd += ["--index", str(step["index"])]
        rc, out, _ = run(cmd)
        return rc == 0 and '"ok":1' in out, out.strip()

    if act == "set":
        cmd = [CU_AX, "set"] + ax_target(target) + ["--value", step.get("value", "")]
        if step.get("role"):
            cmd += ["--role", step["role"]]
        rc, out, _ = run(cmd)
        return rc == 0 and '"ok":1' in out, out.strip()

    if act == "evclick":
        rc, out, _ = run([CU_AX, "evclick", "--pid", str(pid_of(target)),
                          str(step["at"][0]), str(step["at"][1])])
        return rc == 0, out.strip()

    if act == "click":
        rc, out, _ = run([CU, "click", str(step["at"][0]), str(step["at"][1])])
        return rc == 0, out.strip()

    die(f"unknown act: {act}")


def wait_ok(target, before_digest, expect, timeout):
    deadline = time.time() + timeout
    while True:
        nodes = tree(target)
        if expect:
            if expect in tree_text(nodes):
                return True, nodes
        elif digest(nodes) != before_digest:
            return True, nodes
        if time.time() >= deadline:
            return False, nodes
        time.sleep(0.15)


# ---------------------------------------------------------------- 单步

def one_step(target, args, step):
    act = step.get("act") or args.act
    expect = step["expect"] if "expect" in step else args.expect
    retry = step.get("retry", args.retry)
    timeout = step.get("timeout", args.timeout)
    dismiss = step.get("dismiss", args.dismiss or [])

    if act == "observe":
        obs = observe(target)
        obs["diff"] = diff_obs(obs)
        return {"ok": True, "obs": obs}

    last = {"ok": False}
    for attempt in range(retry + 1):
        b_digest = digest(tree(target))
        ok_call, raw = do_action(target, step, act)

        if act == "set" and ok_call:
            want = step.get("value", "")
            deadline = time.time() + timeout
            while time.time() < deadline:
                if any(n.get("value") == want for n in tree(target)):
                    return {"ok": True, "act": act, "attempt": attempt + 1,
                            "verified_by": "value"}
                time.sleep(0.15)
            last = {"ok": False, "act": act, "attempt": attempt + 1,
                    "reason": "value_not_applied"}
            continue

        ok, _ = wait_ok(target, b_digest, expect, timeout)
        if ok and ok_call:
            return {"ok": True, "act": act, "attempt": attempt + 1,
                    "verified_by": "expect" if expect else "changed"}

        reason = "no_change" if not expect else f"expect_missing:{expect}"
        kind, payload = diagnose(target, step)
        acted, how = recover(target, step, kind, payload, dismiss)
        if acted:
            ok2, _ = wait_ok(target, b_digest, expect, timeout)
            if ok2:
                return {"ok": True, "act": act, "attempt": attempt + 1,
                        "verified_by": how, "recovered": True}
        last = {"ok": False, "act": act, "attempt": attempt + 1,
                "reason": reason, "diagnosis": kind, "recovery": how}

        if attempt < retry:
            time.sleep(0.35)
    return last


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(description="带验证与自救的 GUI 动作执行器")
    ap.add_argument("target", help="app=应用名 或 pid=1234")
    ap.add_argument("--act", choices=["press", "set", "evclick", "click", "observe"])
    ap.add_argument("--title")
    ap.add_argument("--role")
    ap.add_argument("--value")
    ap.add_argument("--at", nargs=2, type=float, metavar=("X", "Y"))
    ap.add_argument("--index", type=int, default=1)
    ap.add_argument("--expect")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--retry", type=int, default=2)
    ap.add_argument("--dismiss", nargs="*", default=[],
                    help="遇到弹窗时允许点掉的按钮名，如 --dismiss 取消 关闭")
    ap.add_argument("--flow")
    args = ap.parse_args()

    check_app(args.target)
    if args.act != "observe":
        preflight()

    if args.flow:
        flow = json.load(open(args.flow, encoding="utf-8"))
        for i, step in enumerate(flow):
            r = one_step(args.target, args, step)
            r["step"] = i + 1
            print(json.dumps(r, ensure_ascii=False))
            if not r["ok"]:
                print(json.dumps({"ok": False, "failed_at": i + 1, "total": len(flow)},
                                 ensure_ascii=False))
                sys.exit(1)
        print(json.dumps({"ok": True, "steps": len(flow)}, ensure_ascii=False))
        return

    if not args.act:
        die("需要 --act 或 --flow")

    r = one_step(args.target, args, {})
    print(json.dumps(r, ensure_ascii=False))
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
