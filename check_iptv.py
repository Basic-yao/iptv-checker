#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源检测 → 纯 TXT 输出
- 输入：live.txt（一堆杂乱源，支持 m3u / 纯 URL 列表）
- 流程：测速 → 去重 → 分组合并
- 输出：live_ok.txt / live_fail.txt / skipped.txt / live_report.csv
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

# 大类输出顺序（固定）
CAT_ORDER = ["国内源", "国际源", "4K高清", "TVBox", "其他"]

# 细分类 → 大类
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

# 需要跳过的 URL 关键词
SKIP_URL_KEYWORDS = [
    ".php?sub=", "/encrypt/", "/api/decrypt",
    "password=", "token=", "secret=",
]


# ━━━ 工具函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def classify(orig_cat):
    """原始分类名 → 大类"""
    if not orig_cat:
        return "其他"
    for k, v in CAT_MAP.items():
        if k in orig_cat:
            return v
    return "其他"


def should_skip_url(url):
    """判断是否应跳过"""
    low = url.lower()
    return any(k.lower() in low for k in SKIP_URL_KEYWORDS)


def normalize_url(url):
    """标准化：去查询参数，统一 master 路径"""
    url = url.strip()
    url = url.split('?')[0]
    url = re.sub(r'/refs/heads/', '/', url)
    return url


def repo_key(url):
    """完整路径去重键"""
    clean = normalize_url(url)
    m = re.match(r'https?://raw\.githubusercontent\.com/([^/]+/[^/]+/.+)', clean)
    if m:
        return m.group(1).lower()
    parsed = urlparse(clean)
    path = parsed.path.lstrip('/')
    return (parsed.netloc + '/' + path).lower()


# ━━━ 解析输入文件 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def parse_file(filepath):
    """
    解析 live.txt，支持两种格式：
    1) M3U 格式：#EXTINF ... 后面跟 URL
    2) 纯 URL 列表：每行一个 http 链接
    返回 [(分类, url), ...]
    """
    entries = []
    current_cat = "未分类"
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#EXTM3U'):
                continue

            # 提取 #EXTINF 里的 group-title（如果是 M3U 行）
            if line.startswith('#EXTINF'):
                m = re.search(r'group-title="([^"]+)"', line)
                if m:
                    current_cat = m.group(1).strip()
                continue

            # 分类标题行（形如 # ---- 国内源 ---- 或纯 # 中文综合聚合）
            if line.startswith('#') and 'http' not in line:
                m = re.match(r'#\s*-+\s*(.+?)\s*-+\s*$', line)
                if m:
                    current_cat = m.group(1).strip()
                else:
                    candidate = line.lstrip('#').strip()
                    if candidate and not candidate.startswith('EXT'):
                        current_cat = candidate
                continue

            # URL 行
            if line.startswith('http'):
                entries.append((current_cat, line))

    return entries


# ━━━ 检测函数（咪咕源强制用 GET）━━━━━━━━━━━━━━━━
def check_url(url, timeout=10):
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    parsed = urlparse(url)
    is_migu = 'migu' in parsed.netloc.lower()
    if is_migu:
        headers["Referer"] = "https://www.miguvideo.com/"

    start = time.time()

    # 咪咕源不允许 HEAD，直接走 GET，避免大量 403/405 误判
    if is_migu:
        try:
            r = requests.get(url, headers=headers, timeout=timeout,
                             allow_redirects=True, verify=False, stream=True)
            elapsed = int((time.time() - start) * 1000)
            r.close()
            return url, r.status_code, elapsed, ""
        except requests.exceptions.Timeout:
            return url, 0, int((time.time() - start) * 1000), "TIMEOUT"
        except requests.exceptions.ConnectionError:
            return url, 0, int((time.time() - start) * 1000), "CONN_ERR"
        except Exception as e:
            return url, 0, int((time.time() - start) * 1000), str(e)[:50]

    # 普通源：先 HEAD，失败回退 GET
    try:
        try:
            r = requests.head(url, headers=headers, timeout=timeout,
                              allow_redirects=True, verify=False)
            elapsed = int((time.time() - start) * 1000)
            if r.status_code in (200, 206):
                return url, r.status_code, elapsed, ""
            if r.status_code == 405:
                raise requests.exceptions.RequestException("fallback")
        except requests.exceptions.RequestException:
            pass

        start = time.time()
        r = requests.get(url, headers=headers, timeout=timeout,
                         allow_redirects=True, verify=False, stream=True)
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
        sys.exit(1)

    print(f"📂 读取 {args.file} ...")
    entries = parse_file(args.file)
    print(f"   解析到 {len(entries)} 个链接")

    if len(entries) == 0:
        print("❌ 未解析到任何链接！检查 live.txt 格式")
        print("   前10行原始内容:")
        with open(args.file, 'r', encoding='utf-8', errors='ignore') as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                print(f"   L{i+1}: {line.rstrip()}")
        sys.exit(1)

    # 分离跳过类
    to_check = []
    skipped = []
    for cat, url in entries:
        if should_skip_url(url):
            skipped.append(url)
            continue
        broad = classify(cat)   # 这里改用 classify，CAT_MAP 没有的会归到"其他"
        if broad in SKIP_CATS:
            skipped.append(url)
        else:
            to_check.append((broad, cat, url))

    if skipped:
        print(f"⏭️  跳过 {len(skipped)} 个（加密/关键词）\n")

    if not to_check:
        print("❌ 没有需要检测的链接！")
        sys.exit(1)

    print(f"🚀 开始检测 ({len(to_check)} 个, {args.threads} 线程, {args.timeout}s 超时)...\n")

    # 并发检测
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

    # ━━━ 去重：第一层 URL 完全相同 ━━━━━━━━━━━━━━━━
    seen_url = {}
    for item in ok_raw:
        u = item[2]
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
            if item[4] < seen_repo[key][4]:   # 保留响应更快的
                seen_repo[key] = item
    ok_dedup = list(seen_repo.values())

    # 按大类分组，大类内按响应速度排序
    by_broad = {broad: [] for broad in CAT_ORDER}
    for broad, orig_cat, url, status, elapsed, error in ok_dedup:
        by_broad.setdefault(broad, []).append((url, elapsed))

    for broad in by_broad:
        by_broad[broad].sort(key=lambda x: x[1])

    total_ok = sum(len(v) for v in by_broad.values())

    # ━━━ 诊断日志 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print(f"\n🔍 同源去重详情 (完整路径级):")
    key_counter = Counter(repo_key(i[2]) for i in ok_url_dedup)
    merged = {k: c for k, c in key_counter.items() if c > 1}
    if merged:
        for k, c in list(merged.items())[:8]:
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
        print(f"   ⚠️ '其他'类有 {other_count} 条")

    # ━━━ 统计日志 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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

    # ━━━ 写 skipped.txt ━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if skipped:
        with open('skipped.txt', 'w', encoding='utf-8') as f:
            for url in skipped:
                f.write(f"{url}\n")

    # ━━━ 写 live_fail.txt ━━━━━━━━━━━━━━━━━━━━━━━━━━━
    fail_raw = [(b, oc, u, s, e) for b, oc, u, s, e_ms, e in results if s not in (200, 206)]
    seen_f = {}
    for item in fail_raw:
        u = item[2]
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

    # ━━━ 写 CSV 报告 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['大类', '原始分类', 'URL', '状态码', '响应时间(ms)', '错误'])
        for broad, orig_cat, url, status, elapsed, error in sorted(
                results, key=lambda x: (CAT_ORDER.index(x[0]) if x[0] in CAT_ORDER else 99, x[4])):
            w.writerow([broad, orig_cat, url, status, elapsed, error])

    print(f"💾 已生成:")
    print(f"   live_ok.txt     ← {total_ok} 条可用源")
    print(f"   live_fail.txt   ← 失效列表")
    if skipped:
        print(f"   skipped.txt     ← {len(skipped)} 个跳过的源")
    print(f"   live_report.csv ← 检测报告")

    if len(entries) == 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
