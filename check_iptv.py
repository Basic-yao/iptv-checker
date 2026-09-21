#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import re
import sys
import csv
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
import urllib.error

# ====== 配置 ======
INPUT_FILE = "live.txt"
PREV_FILE = "live_ok.txt"  # 上一次可用源，用于判断新增
THREADS = 10
TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# 分类顺序（写文件用）
CAT_ORDER = ["国内源", "国际源", "4K高清", "TVBox", "GitHub源", "其他"]

# 域名 -> 大类（优先）
DOMAIN_MAP = {
    "raw.githubusercontent.com": "GitHub源",
    "githubusercontent.com": "GitHub源",
    "github.com": "GitHub源",
    "iptv-org.github.io": "国际源",
    "iptv-org": "国际源",
    "4k": "4K高清",
    "tvbox": "TVBox",
}

# 原始分类关键词 -> 大类（兜底）
CAT_MAP = {
    "国内": "国内源",
    "地方": "国内源",
    "央视": "国内源",
    "卫视": "国内源",
    "香港": "国际源",
    "台湾": "国际源",
    "国外": "国际源",
    "国际": "国际源",
    "4K": "4K高清",
    "高清": "4K高清",
    "TVBox": "TVBox",
    "GitHub": "GitHub源",
}

SKIP_KEYWORDS = ["加密", "key", "token", "auth", "login"]

# ====== 工具函数 ======
def normalize_url(u):
    u = u.strip().replace('\n', '').replace('\r', '')
    if '#' in u:
        u = u.split('#')[0]
    return u.strip()

def is_skipped(url):
    low = url.lower()
    return any(k.lower() in low for k in SKIP_KEYWORDS)

def classify_by_domain(url):
    low = url.lower()
    for d, c in DOMAIN_MAP.items():
        if d in low:
            return c
    return None

def classify(orig_cat, url=""):
    dom = classify_by_domain(url)
    if dom:
        return dom
    if orig_cat:
        for k, v in CAT_MAP.items():
            if k in orig_cat:
                return v
    return "其他"

# ====== 解析 live.txt ======
def parse_live(path):
    entries = []
    cur_cat = "其他"
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            # 分类行
            if s.startswith("#") or s.startswith("//"):
                m = re.search(r'#\s*[-]*\s*(.*)', s)
                if m:
                    cur_cat = m.group(1).strip() or cur_cat
                continue
            # URL行（可能带频道名）
            url = s
            broad = cur_cat
            # 常见格式：频道名,url 或 频道名：url
            if ',' in s:
                parts = s.split(',')
                if len(parts) == 2 and parts[1].startswith("http"):
                    broad = parts[0].strip()
                    url = parts[1].strip()
            elif '：' in s or ':' in s:
                sep = '：' if '：' in s else ':'
                p1, p2 = s.split(sep, 1)
                if p2.startswith("http"):
                    broad = p1.strip()
                    url = p2.strip()
            if url.startswith("http"):
                entries.append((broad, cur_cat, url))
    return entries

# ====== 检测 ======
def check_one(broad, orig_cat, url):
    start = time.time()
    norm = normalize_url(url)
    if is_skipped(norm):
        return (broad, orig_cat, norm, 0, 0, "跳过(加密/关键词)")
    try:
        req = urllib.request.Request(norm, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read(4096)
            elapsed = int((time.time() - start) * 1000)
            status = resp.status
            return (broad, orig_cat, norm, status, elapsed, "")
    except urllib.error.HTTPError as e:
        elapsed = int((time.time() - start) * 1000)
        return (broad, orig_cat, norm, e.code, elapsed, str(e.reason))
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        return (broad, orig_cat, norm, 0, elapsed, str(e)[:50])

# ====== 主流程 ======
def main():
    global THREADS, TIMEOUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=THREADS)
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    args = ap.parse_args()
    THREADS = args.threads
    TIMEOUT = args.timeout

    # 读取
    if not os.path.exists(INPUT_FILE):
        print(f"❌ 找不到 {INPUT_FILE}")
        sys.exit(1)
    entries = parse_live(INPUT_FILE)
    print(f"📥 读取 {INPUT_FILE} ...")
    print(f"解析到 {len(entries)} 个链接")

    # 上一次可用源（用于新增判断）
    prev_urls = set()
    if os.path.exists(PREV_FILE):
        with open(PREV_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                u = normalize_url(line)
                if u:
                    prev_urls.add(u)
        print(f"📌 上一次可用源记录：{len(prev_urls)} 条")

    # 跳过统计
    skipped = []
    todo = []
    for b, oc, u in entries:
        if is_skipped(u):
            skipped.append(u)
            continue
        todo.append((b, oc, u))
    if skipped:
        print(f"⏭️ 跳过 {len(skipped)} 个（加密/关键词）")

    # 检测
    print(f"🚀 开始检测（{len(todo)} 个，{THREADS} 线程，{TIMEOUT}s 超时）...")
    results = []
    ok_urls = []
    new_urls = []
    new_urls_norm = set()

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        futs = {ex.submit(check_one, b, oc, u): (b, oc, u) for b, oc, u in todo}
        done = 0
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            results.append(r)
            b, oc, u, s, e, err = r
            norm = normalize_url(u)
            tag = ""
            if s in (200, 206):
                ok_urls.append(norm)
                if norm not in prev_urls:
                    new_urls.append(norm)
                    new_urls_norm.add(norm)
                    tag = " 🆕"
                print(f"  [{done}/{len(todo)}] ✅ {s} {e}ms {b} {norm}{tag}")
            else:
                print(f"  [{done}/{len(todo)}] ❌ {s} {e}ms {b} {norm} {err}")

    # 分类统计
    by_broad = {b: [] for b in CAT_ORDER}
    total_ok = 0
    for b, oc, u, s, e, err in results:
        if s in (200, 206):
            total_ok += 1
            cat = classify(oc, u)
            by_broad[cat].append((u, e))

    print("📊 分类分布预览：")
    for b in CAT_ORDER:
        if by_broad[b]:
            print(f"  {b}: {len(by_broad[b])} 个")

    # 写 live_ok.txt
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        for b in CAT_ORDER:
            urls = by_broad.get(b, [])
            if not urls:
                continue
            f.write(f"# ---- {b} ----\n")
            for u, e in urls:
                f.write(f"{u}\n")
            f.write("\n")

    # 写 live_ok.m3u
    with open('live_ok.m3u', 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for b in CAT_ORDER:
            urls = by_broad.get(b, [])
            if not urls:
                continue
            for u, e in urls:
                f.write(f'#EXTINF:-1 group-title="{b}", {b} ({e}ms)\n')
                f.write(f'{u}\n')

    # 写 skipped.txt
    if skipped:
        with open('skipped.txt', 'w', encoding='utf-8') as f:
            for u in skipped:
                f.write(f"{u}\n")

    # 写 live_fail.txt
    fail_raw = [(b, oc, u, s, e, err) for b, oc, u, s, e, err in results if s not in (200, 206)]
    seen_f = {}
    for item in fail_raw:
        u = normalize_url(item[2])
        if u not in seen_f:
            seen_f[u] = item
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        for b in CAT_ORDER:
            items = [v for v in seen_f.values() if v[0] == b]
            if not items:
                continue
            f.write(f"# ---- {b} ----\n")
            for _, _, u, s, e, err in items:
                reason = f"  #{s} {err}" if err else f"  #HTTP{s}"
                f.write(f"{u}{reason}\n")
            f.write("\n")

    # 写 CSV 报告（新增最前 + 标星）✅ 修复第184行括号未闭合
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['新增源', '大类', '原始分类', 'URL', '状态码', '响应时间(ms)', '错误'])
        
        sorted_results = sorted(
            results,
            key=lambda x: (
                0 if normalize_url(x[2]) in new_urls_norm else 1,
                CAT_ORDER.index(x[0]) if x[0] in CAT_ORDER else 99,
                x[4]
            )
        )
        
        for b, oc, u, s, e, err in sorted_results:
            star = "★ 新增" if normalize_url(u) in new_urls_norm else ""
            w.writerow([star, b, oc, u, s, e, err])

    print(f"💾 已生成:")
    print(f"   live_ok.txt     ← {total_ok} 条可用源")
    print(f"   live_ok.m3u     ← {total_ok} 条可用源")
    print(f"   live_fail.txt   ← 失效列表")
    if skipped:
        print(f"   skipped.txt     ← {len(skipped)} 个跳过")
    print(f"   live_report.csv ← 检测报告（新增源 ★ 标记，排最前）")
    print(f"🆕 新增/恢复可用源：{len(new_urls)} 条")

    # ✅ 强制退出 0，确保 Actions 变绿
    sys.exit(0)

if __name__ == '__main__':
    main()
