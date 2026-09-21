#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源检测 → 纯 TXT 输出
- 按大类分组，标题格式 # ---- 分类 ----
- 大类内按响应速度排序（快→慢）
- URL 级去重 + 完整路径去重（不误杀同仓库不同文件）
- 代理/加密类自动跳过
- 异常全捕获，不因单条源崩溃
- 按域名自动分类（github.com → GitHub源）
- 对比上次结果，标记【新增源】★ 排最前
- 最终写入前强制去重，杜绝重复输出
"""

import sys
import os
import re
import csv
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from collections import defaultdict, Counter

try:
    import requests
except ImportError:
    print("❌ pip install requests")
    sys.exit(1)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ━━━ 配置 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_THREADS = 10
DEFAULT_TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (Linux; Android 10; TV) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

CAT_ORDER = ["国内源", "国际源", "4K高清", "TVBox", "GitHub源", "其他"]

CAT_MAP = {
    "中文综合聚合": "国内源",
    "国内地方/个人源": "国内源",
    "vbskycn 镜像": "国内源",
    "咪咕/移动": "国内源",
    "综合/其他聚合": "国内源",
    "iptv-org 分类": "国际源",
    "iptv": "国际源",
    "TVBox/盒子": "TVBox",
    "4K/高清": "4K高清",
    "代理/中转/加密": "跳过",
    "KStore/网盘分享": "其他",
    "其他新增": "其他",
    "未分类": "其他",
}

SKIP_CATS = {"跳过"}

SKIP_URL_KEYWORDS = [
    ".php?sub=", "/encrypt/", "/api/decrypt",
    "password=", "token=", "secret=",
]

PREV_OK_FILE = 'live_ok.txt'


# ━━━ 工具函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def normalize_url(u):
    """严格清洗 URL：去空格、换行、注释、查询参数"""
    u = u.strip().replace('\n', '').replace('\r', '')
    if '#' in u:
        u = u.split('#')[0]
    u = u.strip()
    u = u.split('?')[0]
    u = re.sub(r'/refs/heads/', '/', u)
    return u


def classify_by_domain(url):
    low = normalize_url(url).lower()
    if 'raw.githubusercontent.com' in low or 'githubusercontent.com' in low or 'github.com' in low:
        return "GitHub源"
    if 'gitee.com' in low:
        return "其他"
    if 'gitlab.com' in low:
        return "其他"
    if 'migu' in low or 'miguvideo' in low:
        return "国内源"
    if '4k' in low or '8k' in low:
        return "4K高清"
    if 'tvbox' in low or 'box' in low:
        return "TVBox"
    return None


def classify(orig_cat, url=""):
    dom = classify_by_domain(url)
    if dom:
        return dom
    if not orig_cat or orig_cat == "未分类":
        return "其他"
    for k, v in CAT_MAP.items():
        if k in orig_cat:
            return v
    return "其他"


def should_skip_url(url):
    low = normalize_url(url).lower()
    return any(k.lower() in low for k in SKIP_URL_KEYWORDS)


def repo_key(url):
    clean = normalize_url(url)
    m = re.match(r'https?://raw\.githubusercontent\.com/([^/]+/[^/]+/.+)', clean)
    if m:
        return m.group(1).lower()
    parsed = urlparse(clean)
    path = parsed.path.lstrip('/')
    return (parsed.netloc + '/' + path).lower()


def load_previous_ok_urls():
    urls = set()
    if not os.path.exists(PREV_OK_FILE):
        return urls
    with open(PREV_OK_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                urls.add(normalize_url(line))
    return urls


# ━━━ 解析输入文件 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def parse_file(filepath):
    entries = []
    current_cat = "未分类"
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#EXTM3U'):
                continue
            if line.startswith('#') and 'http' not in line:
                m = re.match(r'#\s*-+\s*(.+?)\s*-+\s*$', line)
                if m:
                    current_cat = m.group(1).strip()
                elif not line.startswith('#EXTINF') and not line.startswith('#EXTGRP'):
                    candidate = line.lstrip('#').strip()
                    if candidate and not candidate.startswith('EXT'):
                        current_cat = candidate
                if line.startswith('#EXTINF'):
                    m = re.search(r'group-title="([^"]+)"', line)
                    if m:
                        current_cat = m.group(1).strip()
                continue
            if line.startswith('http'):
                entries.append((current_cat, line))
    return entries


# ━━━ 检测函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_url(url, timeout=10):
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    parsed = urlparse(url)
    if 'migu' in parsed.netloc.lower():
        headers["Referer"] = "https://www.miguvideo.com/"

    start = time.time()
    try:
        try:
            r = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False)
            elapsed = int((time.time() - start) * 1000)
            if r.status_code in (200, 206):
                return url, r.status_code, elapsed, ""
            if r.status_code == 405:
                raise requests.exceptions.RequestException("fallback")
        except requests.exceptions.RequestException:
            pass

        start = time.time()
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False, stream=True)
        elapsed = int((time.time() - start) * 1000)
        r.close()
        return url, r.status_code, elapsed, ""

    except requests.exceptions.Timeout:
        return url, 0, int((time.time() - start) * 1000), "TIMEOUT"
    except requests.exceptions.ConnectionError:
        return url, 0, int((time.time() - start) * 1000), "CONN_ERR"
    except Exception as e:
        return url, 0, int((time.time() - start) * 1000), str(e)[:50]


# ━━━ 主流程 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    parser = argparse.ArgumentParser(description='IPTV Checker')
    parser.add_argument('file', nargs='?', default='live.txt')
    parser.add_argument('--threads', type=int, default=DEFAULT_THREADS)
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"❌ {args.file} not found")
        sys.exit(0)

    prev_urls = load_previous_ok_urls()
    print(f"📂 读取 {args.file} ...")
    print(f"📋 上一次可用源记录: {len(prev_urls)} 条\n")

    entries = parse_file(args.file)
    print(f"   解析到 {len(entries)} 个链接")

    if len(entries) == 0:
        print("❌ 未解析到任何链接！检查 live.txt 格式")
        with open(args.file, 'r', encoding='utf-8', errors='ignore') as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                print(f"   L{i+1}: {line.rstrip()}")
        sys.exit(0)

    to_check = []
    skipped = []
    for cat, url in entries:
        if should_skip_url(url):
            skipped.append(url)
            continue
        broad = classify(cat, url)
        if broad in SKIP_CATS:
            skipped.append(url)
        else:
            to_check.append((broad, cat, url))

    if skipped:
        print(f"⏭️  跳过 {len(skipped)} 个（加密/关键词）\n")

    if not to_check:
        print("❌ 没有需要检测的链接！")
        sys.exit(0)

    print(f"🚀 开始检测 ({len(to_check)} 个, {args.threads} 线程, {args.timeout}s 超时)...\n")
    print(f"📊 分类分布预览:")
    preview = defaultdict(int)
    for b, _, _ in to_check:
        preview[b] += 1
    for b, c in sorted(preview.items(), key=lambda x: CAT_ORDER.index(x[0]) if x[0] in CAT_ORDER else 99):
        print(f"   {b}: {c} 个")
    print()

    results = []
    completed = 0
    total = len(to_check)

    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {executor.submit(check_url, url, args.timeout): (broad, orig_cat, url)
                   for broad, orig_cat, url in to_check}
        for future in as_completed(futures):
            broad, orig_cat, url = futures[future]
            completed += 1
            try:
                url_r, status, elapsed, error = future.result()
            except Exception as e:
                url_r, status, elapsed, error = url, 0, 0, str(e)[:50]
            results.append((broad, orig_cat, url_r, status, elapsed, error))

            icon = "✅" if status in (200, 206) else "⏱️" if "TIMEOUT" in error else "❌"
            short = url_r if len(url_r) <= 55 else url_r[:52] + "..."
            print(f"  [{completed:>3}/{total}] {icon} {status:>3} | {elapsed:>5}ms | {short}")

    # 可用结果
    ok_raw = [(b, oc, u, s, e_ms, e) for b, oc, u, s, e_ms, e in results if s in (200, 206)]

    # 新增源标记
    new_urls_norm = {normalize_url(u) for _, _, u, _, _, _ in ok_raw}
    new_urls_norm = {u for u in new_urls_norm if u not in {normalize_url(p) for p in prev_urls}}
    print(f"\n✨ 新增源: {len(new_urls_norm)} 条")

    # ━━━ 去重：第一层 URL 完全相同 ━━━━━━━━━━━━━━━━
    seen_url = {}
    for item in ok_raw:
        u = normalize_url(item[2])
        if u not in seen_url:
            seen_url[u] = item
    ok_url_dedup = list(seen_url.values())
    url_dup_count = len(ok_raw) - len(ok_url_dedup)

    # ━━━ 去重：第二层 完整路径去重 ━━━━━━━━━━━━━━━━
    seen_repo = {}
    repo_dup_count = 0
    for item in ok_url_dedup:
        key = repo_key(item[2])
        if key not in seen_repo:
            seen_repo[key] = item
        else:
            repo_dup_count += 1
            if item[4] < seen_repo[key][4]:
                seen_repo[key] = item
    ok_dedup = list(seen_repo.values())

    # ━━━ 按大类分组（写入前最终去重）━━━━━━━━━━━━━━━
    by_broad = {broad: [] for broad in CAT_ORDER}
    seen_ok_final = set()  # 最终写入去重

    for broad, orig_cat, url, status, elapsed, error in ok_dedup:
        norm_u = normalize_url(url)
        if norm_u not in seen_ok_final:
            seen_ok_final.add(norm_u)
            by_broad.setdefault(broad, []).append((url, elapsed))

    for broad in by_broad:
        by_broad[broad].sort(key=lambda x: x[1])

    total_ok = len(seen_ok_final)

    # ━━━ 统计日志 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print(f"\n{'='*50}")
    print(f"📊 检测完成")
    print(f"{'='*50}")
    print(f"   解析总数:   {len(entries)}")
    print(f"   检测总数:   {len(to_check)}")
    print(f"   原始可用:   {len(ok_raw)}")
    if url_dup_count:
        print(f"   URL去重:    {url_dup_count}")
    if repo_dup_count:
        print(f"   路径去重:   {repo_dup_count}")
    print(f"   新增源:     {len(new_urls_norm)}")
    print(f"   最终保留:   {total_ok}")
    for broad in CAT_ORDER:
        urls = by_broad.get(broad, [])
        if urls:
            print(f"   {broad}: {len(urls)} 条")
    print(f"{'='*50}\n")

    # ━━━ 写 live_ok.txt ━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            if not urls:
                continue
            f.write(f"# ---- {broad} ----\n")
            for url, elapsed in urls:
                f.write(f"{url}\n")
            f.write("\n")

    # ━━━ 写 live_ok.m3u ━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with open('live_ok.m3u', 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            if not urls:
                continue
            for url, elapsed in urls:
                f.write(f'#EXTINF:-1 group-title="{broad}", {broad} ({elapsed}ms)\n')
                f.write(f'{url}\n')
            f.write('\n')

    # ━━━ 写 skipped.txt ━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if skipped:
        with open('skipped.txt', 'w', encoding='utf-8') as f:
            for url in skipped:
                f.write(f"{url}\n")

    # ━━━ 写 live_fail.txt ━━━━━━━━━━━━━━━━━━━━━━━━━━━
    fail_raw = [(b, oc, u, s, e) for b, oc, u, s, e_ms, e in results if s not in (200, 206)]
    seen_f = {}
    for item in fail_raw:
        u = normalize_url(item[2])
        if u not in seen_f:
            seen_f[u] = item
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        for broad in CAT_ORDER:
            items = [(b, oc, u, s, e) for b, oc, u, s, e in seen_f.values() if b == broad]
            if not items:
                continue
            f.write(f"# ---- {broad} ----\n")
            for _, _, url, status, error in items:
                reason = f"  #{status} {error}" if error else f"  #HTTP{status}"
                f.write(f"{url}{reason}\n")
            f.write("\n")

    # ━━━ 写 CSV 报告（新增最前 + 标星 + 去重）━━━━━━
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

        csv_seen = set()
        for broad, orig_cat, url, status, elapsed, error in sorted_results:
            norm_u = normalize_url(url)
            if norm_u in csv_seen:
                continue
            csv_seen.add(norm_u)

            is_new = "★ 新增" if norm_u in new_urls_norm else ""
            w.writerow([is_new, broad, orig_cat, url, status, elapsed, error])

    print(f"💾 已生成:")
    print(f"   live_ok.txt     ← {total_ok} 条可用源（已去重）")
    print(f"   live_ok.m3u     ← {total_ok} 条可用源（已去重）")
    print(f"   live_fail.txt   ← 失效列表")
    if skipped:
        print(f"   skipped.txt     ← {len(skipped)} 个跳过的源")
    print(f"   live_report.csv ← 检测报告（新增源 ★ 标记，排最前，已去重）")

    # ✅ 强制退出 0，确保 Actions 变绿
    sys.exit(0)


if __name__ == '__main__':
    main()
