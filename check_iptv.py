#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源检测（方案B：固定6大类输出）
- 输出永远只有 6 个大类：国内源 / 国际源 / 4K高清 / TVBox / GitHub源 / 其他
- live.txt 标题只决定归属，不出现在输出里
- 各大类内部按「域名 + 用户名首字」排序
- 放宽判定：403/301/302/503 保留
- 去重（URL级 + 路径级） + 新增源标星
"""

import sys
import os
import re
import csv
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from collections import defaultdict

try:
    import requests
except ImportError:
    print("❌ pip install requests")
    sys.exit(0)

import urllib3
urllib3.disable_warnings()

# ━━━ 配置 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_THREADS = 10
DEFAULT_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Linux; Android 10; TV) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

# 输出时固定的 6 个大类（顺序不可变）
CAT_ORDER = ["国内源", "国际源", "4K高清", "TVBox", "GitHub源", "其他"]

# 原始标题 → 大类映射
TITLE_MAP = {
    "直播":            "国际源",
    "国际":            "国际源",
    "国外":            "国际源",
    "iptv-org":        "国际源",
    "iptv-org 分类":   "国际源",
    "国内":            "国内源",
    "央视":            "国内源",
    "卫视":            "国内源",
    "国内地方/个人源": "国内源",
    "综合/其他聚合":   "国内源",
    "咪咕/移动":       "国内源",
    "4k":              "4K高清",
    "4K":              "4K高清",
    "8k":              "4K高清",
    "4K/高清":         "4K高清",
    "4K高清":          "4K高清",
    "tvbox":           "TVBox",
    "TVBox":           "TVBox",
    "TVBox/盒子":      "TVBox",
    "github":          "GitHub源",
    "GitHub源":        "GitHub源",
    "代理/中转/加密":  "其他",
    "KStore/网盘分享": "其他",
    "其他新增":        "其他",
    "其他":            "其他",
}

SKIP_URL_KEYWORDS = [
    "proxy.php?sub=", "/encrypt/", "/api/decrypt",
    ".php?sub=", "4key.cn/FP", "zo.gt.tc",
]

PREV_OK_FILE = 'live_ok.txt'


# ━━━ 工具函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def normalize_url(u):
    u = u.strip().replace('\n', '').replace('\r', '')
    if '#' in u:
        u = u.split('#')[0]
    u = u.strip()
    u = u.split('?')[0]
    u = re.sub(r'/refs/heads/', '/', u)
    return u


def get_sort_key(url):
    """
    排序键：(域名, 用户名首段小写)
    - GitHub 源按 用户名 首字排
    - 非 GitHub 源按 域名 首字排
    - 同用户名下按完整路径排
    """
    try:
        p = urlparse(url)
        net = p.netloc.lower()
        if 'github' in net:
            parts = [x for x in p.path.split('/') if x]
            if parts:
                return (net, parts[0].lower(), '/'.join(parts).lower())
        return (net, p.path.lstrip('/').lower())
    except Exception:
        return ('', url.lower())


def classify(url, title="未分类"):
    """三层判定：URL强制 → 标题映射 → 域名兜底 → 其他"""
    low = normalize_url(url).lower()

    # 第1层：URL 强制
    if any(k in low for k in ['4k', '8k']):
        return "4K高清"
    if 'raw.githubusercontent.com' in low or 'githubusercontent.com' in low or 'github.com' in low:
        return "GitHub源"
    if 'iptv-org' in low:
        return "国际源"
    if 'tvbox' in low or 'box' in low or 'tvboxos' in low:
        return "TVBox"

    # 第2层：标题映射
    if title in TITLE_MAP:
        return TITLE_MAP[title]
    for k, v in TITLE_MAP.items():
        if k in title:
            return v

    # 第3层：域名兜底
    if 'gitee.com' in low or 'gitlab.com' in low or 'bitbucket' in low:
        return "国内源"
    if 'migu' in low or 'miguvideo' in low:
        return "国内源"
    if any(d in low for d in ['t.freetv.fun', 'live.zbds', 'live.hacks', 'live.zhoujie', 'freetv', '850930', 'ibert.me']):
        return "国内源"

    # 第4层：兜底
    return "其他"


def should_skip_url(url):
    low = normalize_url(url).lower()
    return any(k.lower() in low for k in SKIP_URL_KEYWORDS)


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
                    continue
                if not line.startswith('#EXTINF') and not line.startswith('#EXTGRP'):
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


# ━━━ 检测函数（宽容版）━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_url(url, timeout=20):
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    parsed = urlparse(url)
    if 'migu' in parsed.netloc.lower():
        headers["Referer"] = "https://www.miguvideo.com/"

    start = time.time()
    status = 0
    elapsed = 0
    flag = ""

    try:
        # 先试 HEAD
        try:
            r = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False)
            elapsed = int((time.time() - start) * 1000)
            status = r.status_code
            if status in (200, 206, 301, 302):
                return url, status, elapsed, "ok"
            if status == 405:
                raise requests.exceptions.RequestException("try_get")
            if status in (403, 503):
                r2 = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False, stream=True)
                elapsed2 = int((time.time() - start) * 1000)
                r2.close()
                return url, r2.status_code if r2.status_code in (200, 206) else status, elapsed2, "limited" if status == 403 else "retry"
        except requests.exceptions.RequestException:
            pass

        # HEAD 不行就 GET
        start = time.time()
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False, stream=True)
        elapsed = int((time.time() - start) * 1000)
        r.close()
        status = r.status_code
        if status in (200, 206, 301, 302):
            return url, status, elapsed, "ok"
        if status in (403, 503):
            return url, status, elapsed, "limited" if status == 403 else "retry"
        return url, status, elapsed, "fail"

    except requests.exceptions.Timeout:
        return url, 0, int((time.time() - start) * 1000), "timeout"
    except requests.exceptions.ConnectionError:
        return url, 0, int((time.time() - start) * 1000), "conn"
    except Exception as e:
        return url, 0, int((time.time() - start) * 1000), "err"


def is_usable(status, flag):
    if status in (200, 206, 301, 302):
        return True
    if status == 403:
        return True
    return False


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
        sys.exit(0)

    to_check = []
    skipped = []
    for cat, url in entries:
        if should_skip_url(url):
            skipped.append(url)
            continue
        broad = classify(url, cat)
        to_check.append((broad, cat, url))

    if skipped:
        print(f"⏭️  跳过 {len(skipped)} 个\n")

    # 分类预览
    print(f"📊 分类预览:")
    preview = defaultdict(int)
    for b, _, _ in to_check:
        preview[b] += 1
    for b in CAT_ORDER:
        if preview.get(b, 0):
            print(f"   {b}: {preview[b]} 个")
    print()

    print(f"🚀 开始检测 ({len(to_check)} 个, {args.threads} 线程, {args.timeout}s 超时)...\n")

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
                url_r, status, elapsed, flag = future.result()
            except Exception as e:
                url_r, status, elapsed, flag = url, 0, 0, "err"
            results.append((broad, orig_cat, url_r, status, elapsed, flag))

            icon = "✅" if is_usable(status, flag) else ("⚠️" if status in (403, 503) else "❌")
            short = url_r if len(url_r) <= 50 else url_r[:47] + "..."
            print(f"  [{completed:>3}/{total}] {icon} {status:>3} | {elapsed:>5}ms | {short}")

    # 可用结果
    ok_raw = [(b, oc, u, s, e_ms, f) for b, oc, u, s, e_ms, f in results if is_usable(s, f)]

    # 新增源
    new_urls_norm = {normalize_url(u) for _, _, u, _, _, _ in ok_raw}
    new_urls_norm = {u for u in new_urls_norm if u not in {normalize_url(p) for p in prev_urls}}
    print(f"\n✨ 新增源: {len(new_urls_norm)} 条")

    # 去重：URL 完全相同
    seen_url = {}
    for item in ok_raw:
        u = normalize_url(item[2])
        if u not in seen_url:
            seen_url[u] = item
    ok_url_dedup = list(seen_url.values())

    # 去重：路径级，保留响应快的
    seen_repo = {}
    for item in ok_url_dedup:
        key = normalize_url(item[2])
        if key not in seen_repo:
            seen_repo[key] = item
        else:
            if item[4] < seen_repo[key][4]:
                seen_repo[key] = item
    ok_dedup = list(seen_repo.values())

    # ━━━ 按大类分组 + 排序 ━━━━━━━━━━━━━━━━━━━━━━━
    # 记录首次出现顺序，保证同排序键下稳定
    first_seen = {}
    idx = 0
    by_broad = {broad: [] for broad in CAT_ORDER}

    for broad, orig_cat, url, status, elapsed, flag in ok_dedup:
        norm_u = normalize_url(url)
        if norm_u not in first_seen:
            first_seen[norm_u] = idx
            idx += 1
        by_broad[broad].append((url, elapsed, first_seen[norm_u]))

    for broad in CAT_ORDER:
        # 排序：先按域名/用户名首字，再按首次出现顺序
        by_broad[broad].sort(key=lambda x: (get_sort_key(x[0]), x[2]))

    total_ok = sum(len(v) for v in by_broad.values())

    print(f"\n{'='*50}")
    print(f"📊 检测完成")
    print(f"{'='*50}")
    for broad in CAT_ORDER:
        urls = by_broad.get(broad, [])
        if urls:
            print(f"   {broad}: {len(urls)} 条")
    print(f"   总计: {total_ok} 条")
    print(f"{'='*50}\n")

    # ━━━ 写文件 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        for broad in CAT_ORDER:
            items = by_broad.get(broad, [])
            if not items:
                continue
            f.write(f"# ---- {broad} ----\n")
            for url, elapsed, _ in items:
                f.write(f"{url}\n")
            f.write("\n")

    with open('live_ok.m3u', 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for broad in CAT_ORDER:
            items = by_broad.get(broad, [])
            if not items:
                continue
            for url, elapsed, _ in items:
                f.write(f'#EXTINF:-1 group-title="{broad}", {broad} ({elapsed}ms)\n')
                f.write(f'{url}\n')
            f.write('\n')

    if skipped:
        with open('skipped.txt', 'w', encoding='utf-8') as f:
            for url in skipped:
                f.write(f"{url}\n")

    # 失效源（只收真正失效的）
    fail_raw = [(b, oc, u, s, e, fl) for b, oc, u, s, e, fl in results if not is_usable(s, fl)]
    seen_f = {}
    for item in fail_raw:
        u = normalize_url(item[2])
        if u not in seen_f:
            seen_f[u] = item
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        for broad in CAT_ORDER:
            items = [(b, oc, u, s, e, fl) for b, oc, u, s, e, fl in seen_f.values() if b == broad]
            if not items:
                continue
            f.write(f"# ---- {broad} ----\n")
            for _, _, url, status, _, flag in items:
                f.write(f"{url}  #{status} {flag}\n")
            f.write("\n")

    # CSV
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['新增源', '大类', '原始分类', 'URL', '状态码', '响应时间(ms)', '状态'])
        sorted_results = sorted(
            results,
            key=lambda x: (
                0 if normalize_url(x[2]) in new_urls_norm else 1,
                CAT_ORDER.index(x[0]) if x[0] in CAT_ORDER else 99,
                get_sort_key(x[2]),
                x[4]
            )
        )
        csv_seen = set()
        for broad, orig_cat, url, status, elapsed, flag in sorted_results:
            norm_u = normalize_url(url)
            if norm_u in csv_seen:
                continue
            csv_seen.add(norm_u)
            is_new = "★ 新增" if norm_u in new_urls_norm else ""
            state = "可用" if is_usable(status, flag) else flag
            w.writerow([is_new, broad, orig_cat, url, status, elapsed, state])

    print(f"💾 已生成:")
    print(f"   live_ok.txt     ← {total_ok} 条（已按域名/用户名首字排序）")
    print(f"   live_ok.m3u     ← {total_ok} 条（已排序）")
    if skipped:
        print(f"   skipped.txt     ← {len(skipped)} 个")
    print(f"   live_fail.txt   ← 真正失效的源")
    print(f"   live_report.csv ← 检测报告")

    sys.exit(0)


if __name__ == '__main__':
    main()
