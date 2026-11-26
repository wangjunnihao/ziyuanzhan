#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tvbox_build.py —— 两源合一，生成 TVBox 可用的 config.json

采集来源：
  A) CMS 采集源   https://github.com/wangjunnihao/ziyuanzhan
                  → docs/data/online.json（62 个苹果CMS接口）
                  → TVBox type: 1（所有壳通用）

  B) py 脚本源    https://github.com/wangjunnihao/fongmi-py
                  → 仓库内 1898 个 .py 文件
                  → TVBox type: 3（需 FongMi 系壳）

流程：
  1. 拉 CMS 节点列表 → 逐个探测 ?ac=videolist&pg=1 → 校验 list / vod_pic / vod_play_url
  2. 拉 py 仓库文件树 → 逐个 HEAD/GET raw 直链 → 校验返回真实脚本内容
  3. 各自去重、去成人站（可选）、按响应速度排序
  4. 合并输出 config.json，CMS 在前 py 在后

纯标准库，Python 3.7+ 直接跑。

────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────

  # 默认：两源全采、全部验证、输出 config.json
  python tvbox_build.py

  # 只要 CMS 源（所有 TVBox 壳都能跑，最稳）
  python tvbox_build.py --only cms

  # 只要 py 源
  python tvbox_build.py --only py

  # 排除成人站
  python tvbox_build.py --exclude-adult

  # CMS 只留资源量 5 万以上的
  python tvbox_build.py --min-total 50000

  # 用本地缓存的 online.json，不联网
  python tvbox_build.py --cms-file online.json

环境变量：
  GITHUB_TOKEN   可选，拉仓库文件树用，有则限额 5000/h
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- 配置

CMS_DATA_URL = ("https://raw.githubusercontent.com/wangjunnihao/"
                "ziyuanzhan/main/docs/data/online.json")
PY_REPO = "wangjunnihao/fongmi-py"
PY_BRANCH = "main"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

ADULT_WORDS = [
    "av", "色", "黄", "裸", "porn", "麻豆", "黑料", "湿", "三级", "伦理",
    "写真", "偷拍", "自慰", "性", "成人", "禁片", "里番", "h漫", "工口",
    "香x儿", "老色", "大x子", "精品x", "x水机", "搜av", "黄av", "屌丝",
]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ================================================================ 网络

def http_get(url, timeout=25, retries=1, want_headers=False):
    """返回 (status, body, content_type)。异常返回 (0, b'', '')。"""
    for i in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                ct = r.headers.get("Content-Type", "")
                return r.status, body, ct
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404, 410):
                return e.code, b"", ""
        except Exception:
            pass
        if i < retries:
            time.sleep(1.2 * (i + 1))
    return 0, b"", ""


def gh_api(path, token=None):
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request("https://api.github.com" + path, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# ================================================================ 工具

def norm(s):
    s = re.sub(r"\.py.*$", "", str(s), flags=re.I)
    s = re.sub(r"[\(\)（）\[\]【】]", " ", s)
    s = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", s)
    s = re.sub(r"[_\-\s·．.]+", "", s)
    return s.strip().lower()


def is_adult(name, tags=None):
    blob = norm(name) + " " + norm(" ".join(tags or []))
    return any(w in blob for w in ADULT_WORDS if w)


def dedupe(sites, key_fn):
    seen, out, n = set(), [], 0
    for s in sites:
        k = key_fn(s)
        if k and k in seen:
            n += 1
            continue
        if k:
            seen.add(k)
        out.append(s)
    if n:
        print(f"    去重合并 {n} 条")
    return out


def dedupe_names(sites):
    """重名加 -2/-3 后缀，保证最终名字全局唯一。"""
    used = set()
    for s in sites:
        base = s["name"]
        if base not in used:
            used.add(base)
            continue
        n = 2
        while f"{base}-{n}" in used:
            n += 1
        s["name"] = f"{base}-{n}"
        used.add(s["name"])
    return sites


# ================================================================ A. CMS 源

def load_cms(src):
    if os.path.isfile(src):
        raw = open(src, "rb").read()
        print(f"[A] CMS 数据：本地 {src}")
    else:
        st, raw, _ = http_get(src, timeout=30, retries=2)
        if st != 200 or not raw:
            print(f"[A] 拉取失败 HTTP {st}")
            return []
        print(f"[A] CMS 数据：{src}")

    try:
        d = json.loads(raw.decode("utf-8", "ignore"))
    except Exception as e:
        print(f"[A] JSON 解析失败：{e}")
        return []

    nodes = d.get("data") if isinstance(d, dict) else d
    if not isinstance(nodes, list):
        print("[A] 数据结构异常")
        return []
    ts = d.get("timestamp", "?") if isinstance(d, dict) else "?"
    print(f"[A] 节点 {len(nodes)} 个（数据时间 {ts}）")
    return nodes


def norm_api(api):
    a = str(api or "").strip()
    if not a.startswith("http"):
        return ""
    return a if a.endswith("/") else a + "/"


def probe_cms(node):
    """返回 (idx, ok, status, ms, info)"""
    idx = node["_i"]
    api = norm_api(node.get("api"))
    if not api:
        return idx, False, "no-api", 0, {}

    last = "fail"
    for suffix in ("?ac=videolist&pg=1", "?ac=videolist&pg=1&at=json"):
        t0 = time.time()
        st, body, _ = http_get(api + suffix, timeout=25, retries=1)
        ms = int((time.time() - t0) * 1000)
        if st != 200 or not body:
            last = f"HTTP{st}"
            continue
        if body[:1] == b"<":                 # XML，换 at=json
            last = "xml"
            continue
        try:
            j = json.loads(body.decode("utf-8", "ignore"))
        except Exception:
            last = "not-json"
            continue
        lst = j.get("list") or []
        if not lst:
            last = f"empty(code={j.get('code')})"
            continue
        f0 = lst[0]
        return idx, True, "ok", ms, {
            "total": j.get("total") or 0,
            "pic": bool(f0.get("vod_pic")),
            "play": bool(f0.get("vod_play_url")),
            "at_json": suffix.endswith("at=json"),
        }
    return idx, False, last, 0, {}


def collect_cms(nodes, workers, min_total, need_play, exclude_adult):
    for i, n in enumerate(nodes):
        n["_i"] = i

    print(f"[A] 验证 {len(nodes)} 个接口（并发 {workers}）…")
    t0, res = time.time(), {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for idx, ok, st, ms, info in ex.map(probe_cms, nodes):
            res[idx] = (ok, st, ms, info)

    # 超时（ms==0 且非 HTTP 错误）重试一轮
    again = [n for n in nodes
             if not res.get(n["_i"], (0,))[0]
             and res.get(n["_i"], (0, "", 0))[2] == 0]
    if again:
        print(f"    重试超时 {len(again)} 个…")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            for idx, ok, st, ms, info in ex.map(probe_cms, again):
                if ok:
                    res[idx] = (ok, st, ms, info)

    alive = sum(1 for v in res.values() if v[0])
    print(f"[A] 验证完成：可用 {alive}/{len(nodes)}，耗时 {time.time() - t0:.0f}s")

    sites, dropped = [], {"dead": 0, "small": 0, "noplay": 0, "adult": 0}
    for n in nodes:
        ok, st, ms, info = res.get(n["_i"], (False, "", 0, {}))
        if not ok:
            dropped["dead"] += 1
            continue
        if min_total and (info.get("total") or 0) < min_total:
            dropped["small"] += 1
            continue
        if need_play and not info.get("play"):
            dropped["noplay"] += 1
            continue
        name = str(n.get("name") or "未命名").strip()
        if exclude_adult and is_adult(name, n.get("tags")):
            dropped["adult"] += 1
            continue
        api = norm_api(n.get("api"))
        if info.get("at_json"):
            api += "at/json/"
        sites.append({
            "key": "", "name": name, "type": 1, "api": api,
            "searchable": 1, "quickSearch": 1, "filterable": 1,
            "group": "CMS采集",
            "_ms": ms,
        })

    sites.sort(key=lambda x: x["_ms"])
    for i, s in enumerate(sites, 1):
        s["key"] = f"cms_{i:04d}"

    print(f"[A] 保留 {len(sites)} 个 CMS 源"
          f"（剔除：失效 {dropped['dead']} / 资源少 {dropped['small']} / "
          f"无播放地址 {dropped['noplay']} / 成人 {dropped['adult']}）")
    return sites


# ================================================================ B. py 源

def list_py(token):
    info = gh_api(f"/repos/{PY_REPO}", token)
    branch = PY_BRANCH or info.get("default_branch") or "main"
    print(f"[B] 仓库 {info.get('full_name')}@{branch}  大小 {info.get('size')} KB")

    tree = gh_api(f"/repos/{PY_REPO}/git/trees/{branch}?recursive=1", token)
    if tree.get("truncated"):
        print("[B] 警告：文件树被截断")

    paths = sorted(it["path"] for it in tree.get("tree", [])
                   if it.get("type") == "blob"
                   and it["path"].lower().endswith(".py"))
    print(f"[B] 发现 .py {len(paths)} 个")
    return paths, branch


def py_url(path, branch, key="x", mirror="raw"):
    q = urllib.parse.quote(path)
    if mirror == "jsdelivr":
        return f"https://cdn.jsdelivr.net/gh/{PY_REPO}@{branch}/{q}?v={key}"
    return f"https://raw.githubusercontent.com/{PY_REPO}/{branch}/{q}?v={key}"


def probe_py(task):
    i, url = task
    t0 = time.time()
    st, body, _ = http_get(url, timeout=30, retries=2)
    ms = int((time.time() - t0) * 1000)
    ok = st == 200 and len(body) > 32
    if ok:
        head = body[:16].lstrip()
        if head.startswith(b"404") or head.startswith(b"<!DOCTYPE") \
                or head.startswith(b"<html"):
            ok = False
    return i, ok, st, ms


def collect_py(paths, branch, workers, mirror, exclude_adult):
    items = [(i, py_url(p, branch)) for i, p in enumerate(paths)]
    print(f"[B] 验证 {len(items)} 条直链（并发 {workers}）…")
    t0, res = time.time(), {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, ok, st, ms in ex.map(probe_py, items):
            res[i] = (ok, st, ms)
            if len(res) % 300 == 0:
                print(f"    {len(res)}/{len(items)}  {time.time() - t0:.0f}s")

    # 超时重试（status==0）
    again = [(i, u) for i, u in items if res.get(i, (0, 0, 0))[1] == 0]
    if again:
        print(f"    重试超时 {len(again)} 条…")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            for i, ok, st, ms in ex.map(probe_py, again):
                if ok:
                    res[i] = (ok, st, ms)

    alive = sum(1 for v in res.values() if v[0])
    print(f"[B] 验证完成：可用 {alive}/{len(paths)}，耗时 {time.time() - t0:.0f}s")

    sites, adult = [], 0
    for i, p in enumerate(paths):
        ok, st, ms = res.get(i, (False, 0, 0))
        if not ok:
            continue
        name = os.path.basename(p)[:-3].strip()
        if exclude_adult and is_adult(name):
            adult += 1
            continue
        d = os.path.dirname(p)
        sites.append({
            "key": "", "name": name, "type": 3,
            "api": py_url(p, branch, f"py_{len(sites) + 1:04d}", mirror),
            "searchable": 1, "quickSearch": 1, "filterable": 1,
            "group": d if d else "py根目录",
            "_ms": ms,
        })

    for i, s in enumerate(sites, 1):
        key = f"py_{i:04d}"
        s["key"] = key
        s["api"] = re.sub(r"\?v=[^&]*$", f"?v={key}", s["api"])

    print(f"[B] 保留 {len(sites)} 个 py 源"
          + (f"（剔除成人 {adult}）" if adult else ""))
    return sites


# ================================================================ 输出

def strip_private(sites):
    for s in sites:
        s.pop("_ms", None)
    return sites


def write_config(path, sites):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"sites": sites}, f, ensure_ascii=False, indent=2)
    kb = os.path.getsize(path) / 1024
    print(f"\n[+] 已写入 {path}  ({kb:.1f} KB, {len(sites)} 条)")


def main():
    ap = argparse.ArgumentParser(
        description="两源合一生成 TVBox config.json（CMS + py）")
    ap.add_argument("--only", choices=["all", "cms", "py"], default="all")
    ap.add_argument("--out", default="config.json")
    ap.add_argument("--cms-url", default=CMS_DATA_URL)
    ap.add_argument("--cms-file", default=None, help="本地 online.json")
    ap.add_argument("--min-total", type=int, default=0)
    ap.add_argument("--exclude-adult", action="store_true")
    ap.add_argument("--mirror", choices=["raw", "jsdelivr"], default="raw")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("[i] 未设置 GITHUB_TOKEN（API 限额 60/h，本脚本用 2 次）")

    cms_sites, py_sites = [], []

    if args.only in ("all", "cms"):
        nodes = load_cms(args.cms_file or args.cms_url)
        if nodes:
            if args.no_verify:
                cms_sites = [{
                    "key": f"cms_{i:04d}", "name": n.get("name", ""),
                    "type": 1, "api": norm_api(n.get("api")),
                    "searchable": 1, "quickSearch": 1, "filterable": 1,
                    "group": "CMS采集",
                } for i, n in enumerate(nodes, 1) if norm_api(n.get("api"))]
            else:
                cms_sites = collect_cms(nodes, args.workers, args.min_total,
                                        True, args.exclude_adult)

    if args.only in ("all", "py"):
        try:
            paths, branch = list_py(token)
            if args.no_verify:
                py_sites = [{
                    "key": f"py_{i:04d}",
                    "name": os.path.basename(p)[:-3],
                    "type": 3,
                    "api": py_url(p, branch, f"py_{i:04d}", args.mirror),
                    "searchable": 1, "quickSearch": 1, "filterable": 1,
                    "group": os.path.dirname(p) or "py根目录",
                } for i, p in enumerate(paths, 1)]
            else:
                py_sites = collect_py(paths, branch, args.workers,
                                      args.mirror, args.exclude_adult)
        except urllib.error.HTTPError as e:
            print(f"[B] 读取仓库失败 HTTP {e.code}")
            if args.only == "py":
                return 1

    merged = cms_sites + py_sites
    if not merged:
        print("[x] 没有任何可用源")
        return 1

    merged = dedupe(merged, lambda s: s.get("api"))
    merged = dedupe_names(merged)
    merged = strip_private(merged)
    write_config(args.out, merged)

    print("\n[*] 源构成：")
    if cms_sites:
        print(f"    CMS 采集源 type:1   {len(cms_sites):>5} 个  "
              f"→ 所有 TVBox 壳通用")
    if py_sites:
        print(f"    py 脚本源 type:3    {len(py_sites):>5} 个  "
              f"→ 需 FongMi / HoneyTV / 影视仓新版")

    if cms_sites:
        print("\n[*] CMS 前 5（按响应速度）：")
        for s in cms_sites[:5]:
            print(f"    {s['name'][:16]:18} {s['api'][:62]}")

    return 0


def _end(rc):
    if rc == 0:
        print("\n[*] 下一步：")
        print("    1. 把 config.json 传到公网可直链处（GitHub / Gist / OSS）")
        print("    2. 壳 → 设置 → 配置地址 → 填该 json 链接")
    sys.exit(rc)


if __name__ == "__main__":
    try:
        rc = main()
    except KeyboardInterrupt:
        print("\n[!] 已中断")
        sys.exit(130)
    _end(rc)
