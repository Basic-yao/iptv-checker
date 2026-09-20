#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 源检测脚本 (方案A：完整路径去重 + 诊断日志)
移除 gh-proxy 逻辑，直连检测。
"""
import os
import re
import sys
import csv
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from collections import defaultdict
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ━━━ 配置 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIMEOUT = 10
THREADS = 10
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

SKIP_URL_KEYWORDS = [
    ".php?sub=", "/encrypt/", "/api/decrypt",
    "password=", "token=", "secret=",
]

CAT_MAP = {
    "CCTV": "央视", "卫视": "国内源", "国内": "国内源",
    "港澳台": "港澳台", "香港": "港澳台", "台湾": "港澳台",
    "体育": "体育", "电影": "电影", "4K": "4K/高清", "高清": "4K/高清",
    "海外": "海外", "国外": "海外",
}
CAT_ORDER = ["央视", "国内源", "港澳台", "体育", "电影", "4K/高清", "海外", "其他"]

# ━━━ 工具函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def classify(orig_cat):
    if not orig_cat:
        return "其他"
    for k, v in CAT_MAP.items():
        if k in orig_cat:
            return v
    return "其他"

def is_skip(url):
    low = url.lower()
    return any(k.lower() in low for k in SKIP_URL_KEYWORDS)

def normalize_url(url):
    """基础标准化：去查询参数，统一 master 路径"""
    url = url.strip()
    url = url.split('?')[0]
    url = re.sub(r'/refs/heads/', '/', url)
    return url

def repo_key(url):
    """
    方案A核心：完整路径去重。
    只合并真正的重复文件（如 master vs refs/heads/master），
    不再把同仓库不同文件合并。
    """
    clean = normalize_url(url)
    # 提取 github raw 路径
    m = re.match(r'https?://raw\.githubusercontent\.com/([^/]+/[^/]+/.+)', clean)
    if m:
        return m.group(1).lower()
    # 其他域名也走完整路径
    parsed = urlparse(clean)
    path = parsed.path.lstrip('/')
    return (parsed.netloc + '/' + path).lower()

# ━━━ 解析 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def parse_file(path):
    entries = []
    cur_cat = "未分类"
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if s.startswith('#'):
                # 兼容 # 分类, # ---- 分类 ----, #EXTINF
                m = re.search(r'#\s*(?:----)?\s*(.*?)(?:\s*----)?$', s)
                if m:
                    cur_cat = m.group(1).strip() or cur_cat
                continue
            if re.match(r'^https?://', s):
                entries.append((cur_cat, s))
    return entries

# ━━━ 检测 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_url(url, timeout=TIMEOUT):
    try:
        session = requests.Session()
        retries = Retry(total=0)
        session.mount('https://', HTTPAdapter(max_retries=retries))
        session.mount('http://', HTTPAdapter(max_retries=retries))
        
        headers = {"User-Agent": USER_AGENT}
        # 支持 HLS (m3u8) 头检测，允许重定向
        r = session.get(
            url, headers=headers, timeout=timeout,
            allow_redirects=True, verify=False, stream=True
        )
        status = r.status_code
        elapsed = int(r.elapsed.total_seconds() * 1000)
        # 读一点内容确认不是空响应
        content = next(r.iter_content(1024), b'')
        r.close()
        
        if status in (200, 206) and len(content) > 10:
            return status, elapsed, None
        return status, elapsed, "empty" if len(content) <= 10 else None
    except requests.exceptions.Timeout:
        return 0, timeout * 1000, "TIMEOUT"
    except requests.exceptions.ConnectionError as e:
        return 0, 0, "CONN_ERR"
    except Exception as e:
        return 0, 0, str(e)[:20]

# ━━━ 主流程 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    global THREADS, TIMEOUT
    parser = argparse.ArgumentParser()
    parser.add_argument('input', nargs='?', default='live.txt')
    parser.add_argument('--threads', type=int, default=THREADS)
    parser.add_argument('--timeout', type=int, default=TIMEOUT)
    args = parser.parse_args()
    THREADS = args.threads
    TIMEOUT = args.timeout

    if not os.path.exists(args.input):
        print(f"❌ 文件不存在: {args.input}")
        sys.exit(1)

    entries = parse_file(args.input)
    print(f"📖 解析到 {len(entries)} 条链接")

    to_check = []
    skipped = []
    for cat, url in entries:
        if is_skip(url):
            skipped.append(url)
            continue
        to_check.append((cat, url))

    print(f"🔍 待检测: {len(to_check)}, 跳过: {len(skipped)}")

    results = []
    print(f"⏳ 开始检测 (threads={THREADS}, timeout={TIMEOUT}s)...")
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        future_map = {ex.submit(check_url, u): (c, u) for c, u in to_check}
        done = 0
        for fut in as_completed(future_map):
            cat, url = future_map[fut]
            status, elapsed, err = fut.result()
            results.append((classify(cat), cat, url, status, elapsed, err))
            done += 1
            mark = "✅" if status in (200, 206) else ("⏱" if err == "TIMEOUT" else "❌")
            print(f"  {mark} [{elapsed}ms] {url[:70]} ({status})")
            if done % 10 == 0:
                print(f"    进度: {done}/{len(to_check)}")

    # ━━━ 去重：第一层 URL 去重 ━━━
    ok_raw = [r for r in results if r[3] in (200, 206)]
    seen_url = {}
    url_dup_count = 0
    for item in ok_raw:
        broad, orig, url, st, el, er = item
        norm = normalize_url(url)
        if norm not in seen_url or el < seen_url[norm][4]:
            seen_url[norm] = item
        else:
            url_dup_count += 1
    ok_url_dedup = list(seen_url.values())

    # ━━━ 去重：第二层 完整路径去重（方案A）━━━
    seen_repo = {}
    repo_dup_count = 0
    for item in ok_url_dedup:
        broad, orig, url, st, el, er = item
        key = repo_key(url)
        if key not in seen_repo or el < seen_repo[key][4]:
            seen_repo[key] = item
        else:
            repo_dup_count += 1
    ok_dedup = list(seen_repo.values())

    # ━━━ 分类聚合 ━━━
    by_broad = defaultdict(list)
    for item in ok_dedup:
        broad, orig, url, st, el, er = item
        by_broad[broad].append((url, el))

    total_ok = sum(len(v) for v in by_broad.values())

    # ━━━ 诊断日志（方案A新增）━━━
    print(f"\n🔍 同源去重详情 (完整路径级):")
    from collections import Counter
    key_counter = Counter(repo_key(i[2]) for i in ok_url_dedup)
    merged = {k: c for k, c in key_counter.items() if c > 1}
    if merged:
        for k, c in list(merged.items())[:5]:
            print(f"   📦 {k}: {c} 条 → 保留 1 条")
    else:
        print("   ✅ 无过度合并（同仓库不同文件均保留）")

    print(f"\n🔍 分类分布:")
    for broad in CAT_ORDER:
        urls = by_broad.get(broad, [])
        if urls:
            print(f"   {broad}: {len(urls)} 条")
    other_count = len(by_broad.get("其他", []))
    if other_count:
        print(f"   ⚠️ '其他'类有 {other_count} 条，可能包含未匹配分类标题的源")

    # ━━━ 统计日志 ━━━
    print(f"\n{'='*50}")
    print(f"📊 检测完成")
    print(f"{'='*50}")
    print(f"   解析总数:   {len(entries)}")
    print(f"   检测总数:   {len(to_check)}")
    print(f"   原始可用:   {len(ok_raw)}")
    if url_dup_count:
        print(f"   URL去重:    {url_dup_count}")
    if repo_dup_count:
        print(f"   完整路径去重:{repo_dup_count}")
    print(f"   最终保留:   {total_ok}")
    for broad in CAT_ORDER:
        urls = by_broad.get(broad, [])
        if urls:
            print(f"   {broad}: {len(urls)} 条")
    print(f"{'='*50}\n")

    if total_ok == 0:
        print("⚠️ 没有可用源！检查上方日志中的状态码和错误信息\n")

    # ━━━ 写 live_ok.txt ━━━
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            if not urls:
                continue
            f.write(f"# ---- {broad} ----\n")
            for url, elapsed in sorted(urls, key=lambda x: x[1]):
                f.write(f"{url}\n")
            f.write("\n")

    # ━━━ 写 skipped.txt ━━━
    if skipped:
        with open('skipped.txt', 'w', encoding='utf-8') as f:
            for url in skipped:
                f.write(f"{url}\n")

    # ━━━ 写 live_fail.txt ━━━
    fail_raw = [r for r in results if r[3] not in (200, 206)]
    seen_f = {}
    for item in fail_raw:
        u = item[2]
        if u not in seen_f:
            seen_f[u] = item
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        for broad in CAT_ORDER:
            items = [v for v in seen_f.values() if v[0] == broad]
            if not items:
                continue
            f.write(f"# ---- {broad} ----\n")
            for _, _, url, status, error in items:
                reason = f"  #{status} {error}" if error else f"  #HTTP{status}"
                f.write(f"{url}{reason}\n")
            f.write("\n")

    # ━━━ 写 CSV 报告 ━━━
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['大类', '原始分类', 'URL', '状态码', '响应时间(ms)', '错误'])
        for broad, orig_cat, url, status, elapsed, error in sorted(
            results, key=lambda x: (CAT_ORDER.index(x[0]) if x[0] in CAT_ORDER else 99, x[4])
        ):
            w.writerow([broad, orig_cat, url, status, elapsed, error])

    print(f"💾 已生成:")
    print(f"   live_ok.txt     ← {total_ok} 条可用源")
    print(f"   live_fail.txt   ← 失效列表")
    if skipped:
        print(f"   skipped.txt     ← {len(skipped)} 个跳过的源")
    print(f"   live_report.csv ← 检测报告")

if __name__ == '__main__':
    main()
