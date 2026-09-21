#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV Source Checker
===================
把一堆直播源丢进 live.txt（一行一个 URL），脚本会：
  1. 测速（并发 HTTP 探测）
  2. 去重（URL 完全相同 / 同文件 master 与 refs/heads/master 重复）
  3. 分组合并（按大类归并，组内按响应速度排序）
  4. 输出可用列表

输入  : live.txt         纯文本，一行一个 http(s) 链接，可用 # 写注释
输出  : live_ok.m3u      可用源 M3U 格式，可直接导入 TiviMate / VLC / IPTV Smarters
        live_ok.txt      可用源纯文本
        live_fail.txt    失效源
        skipped.txt      跳过的源（加密 / 中转）
        live_report.csv  详细检测报告
"""

import sys
import os
import re
import csv
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from collections import Counter

try:
    import requests
except ImportError:
    print("❌ 请先安装依赖：pip install -r requirements.txt")
    sys.exit(1)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ━━━ 配置 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_THREADS = 10
DEFAULT_TIMEOUT = 10
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; TV) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)

# 大类输出顺序
CAT_ORDER = ["国内源", "国际源", "4K高清", "TVBox", "其他"]

# 域名 / 关键词 → 大类
# 越靠前优先级越高，命中即归入该类
CAT_RULES = [
    ("4K高清",      [r"4k", r"uhd", r"2160p"]),
    ("咪咕/移动",    [r"migu"]),
    ("TVBox",       [r"tvbox", r"box", r"stvm3u", r"stv"]),
    ("国际源",      [r"iptv-?org", r"foreign", r"\b(uk|us|eu)\b", r"global"]),
    ("国内源",      [r"cctv", r"cntv", r"yangshipin", r"tv\.cctv", r"p2p", r"local"]),
]

# 需要跳过的 URL 关键词
SKIP_URL_KEYWORDS = [
    ".php?sub=", "/encrypt/", "/api/decrypt",
    "password=", "token=", "secret=",
]


# ━━━ 工具函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def classify_url(url):
    """根据 URL 的域名和路径自动判断所属大类"""
    low = url.lower()
    for cat, patterns in CAT_RULES:
        for pat in patterns:
            if re.search(pat, low):
                return cat
    return "其他"


def should_skip_url(url):
    """加密 / 中转类源一律跳过"""
    return any(k.lower() in url.lower() for k in SKIP_URL_KEYWORDS)


def normalize_url(url):
    """去掉查询参数，统一 master 与 refs/heads/master 写法"""
    url = url.strip()
    url = url.split('?')[0]
    url = re.sub(r'/refs/heads/', '/', url)
    return url


def repo_key(url):
    """
    同文件去重键：
      raw.githubusercontent.com/owner/repo/branch/path/to/file.m3u
      无论 branch 是 master 还是 refs/heads/master，都算同一个文件
    """
    clean = normalize_url(url)
    m = re.match(r'https?://raw\.githubusercontent\.com/([^/]+/[^/]+/.+)', clean)
    if m:
        return m.group(1).lower()
    parsed = urlparse(clean)
    return (parsed.netloc + parsed.path).lower()


# ━━━ 解析输入文件 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def parse_file(filepath):
    """
    读取 live.txt，支持两种写法：
       http://a.com/1.m3u8                # 纯 URL
       # 国内源                            # 注释行（仅用于阅读，不参与分类）
    返回 [(分类, url), ...]
    """
    entries = []
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):   # 跳过空行和注释
                continue
            if line.startswith('http'):
                entries.append((classify_url(line), line))
    return entries


# ━━━ 检测函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_url(url, timeout=10):
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    is_migu = 'migu' in urlparse(url).netloc.lower()
    if is_migu:
        headers["Referer"] = "https://www.miguvideo.com/"

    start = time.time()

    # 咪咕源不允许 HEAD，直接 GET
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
    parser = argparse.ArgumentParser(description='IPTV Source Checker')
    parser.add_argument('file', nargs='?', default='live.txt')
    parser.add_argument('--threads', type=int, default=DEFAULT_THREADS)
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"❌ 找不到 {args.file}")
        sys.exit(1)

    print(f"📂 读取 {args.file} ...")
    entries = parse_file(args.file)
    print(f"   解析到 {len(entries)} 个链接")

    if len(entries) == 0:
        print("❌ 未解析到任何链接！请确认 live.txt 每行一个 http 链接")
        print("   前 10 行原始内容：")
        with open(args.file, 'r', encoding='utf-8', errors='ignore') as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                print(f"   L{i+1}: {line.rstrip()}")
        sys.exit(1)

    # 分离跳过类
    to_check, skipped = [], []
    for cat, url in entries:
        if should_skip_url(url):
            skipped.append(url)
        else:
            to_check.append((cat, url))

    if skipped:
        print(f"⏭️  跳过 {len(skipped)} 个（加密 / 中转源）\n")

    if not to_check:
        print("❌ 没有需要检测的链接！")
        sys.exit(1)

    print(f"🚀 开始检测（{len(to_check)} 个, {args.threads} 线程, {args.timeout}s 超时）...\n")

    # 并发检测
    results = []
    completed = 0
    total = len(to_check)
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {executor.submit(check_url, url, args.timeout): (cat, url)
                   for cat, url in to_check}
        for future in as_completed(futures):
            cat, url = futures[future]
            completed += 1
            try:
                url_r, status, elapsed, error = future.result()
            except Exception as e:
                url_r, status, elapsed, error = url, 0, 0, str(e)[:50]
            results.append((cat, url_r, status, elapsed, error))

            icon = "✅" if status in (200, 206) else "⏱️" if "TIMEOUT" in error else "❌"
            short = url_r if len(url_r) <= 60 else url_r[:57] + "..."
            print(f"  [{completed:>3}/{total}] {icon} {status:>3} | {elapsed:>5}ms | {short}")

    # 可用结果
    ok_raw = [(c, u, s, e, err) for c, u, s, e, err in results if s in (200, 206)]

    # 去重：第一层 —— URL 完全相同
    seen_url = {}
    for item in ok_raw:
        u = item[1]
        if u not in seen_url:
            seen_url[u] = item
    ok_url_dedup = list(seen_url.values())
    url_dup = len(ok_raw) - len(ok_url_dedup)

    # 去重：第二层 —— 同文件（master vs refs/heads/master）
    seen_repo = {}
    repo_dup = 0
    for item in ok_url_dedup:
        key = repo_key(item[1])
        if key not in seen_repo:
            seen_repo[key] = item
        else:
            repo_dup += 1
            if item[3] < seen_repo[key][3]:   # 保留响应更快的那个
                seen_repo[key] = item
    ok_dedup = list(seen_repo.values())

    # 按大类分组，组内按响应速度排序
    by_broad = {broad: [] for broad in CAT_ORDER}
    for cat, url, status, elapsed, error in ok_dedup:
        by_broad.setdefault(cat, []).append((url, elapsed))
    for broad in by_broad:
        by_broad[broad].sort(key=lambda x: x[1])

    total_ok = sum(len(v) for v in by_broad.values())

    # 诊断日志
    print("\n🔍 同文件去重详情：")
    counter = Counter(repo_key(i[1]) for i in ok_url_dedup)
    merged = {k: c for k, c in counter.items() if c > 1}
    if merged:
        for k, c in list(merged.items())[:8]:
            print(f"   📦 {k}：{c} 条 → 保留 1 条（取最快）")
    else:
        print("   ✅ 无过度合并（同仓库不同文件均保留）")

    # 统计日志
    print(f"\n{'='*50}")
    print("📊 检测完成")
    print(f"{'='*50}")
    print(f"   解析总数: {len(entries)}")
    print(f"   检测总数: {len(to_check)}")
    print(f"   原始可用: {len(ok_raw)}")
    if url_dup:
        print(f"   URL 去重: {url_dup}")
    if repo_dup:
        print(f"   路径去重: {repo_dup}")
    print(f"   最终保留: {total_ok}")
    for broad in CAT_ORDER:
        if by_broad.get(broad):
            print(f"   {broad}: {len(by_broad[broad])} 条")
    print(f"{'='*50}\n")

    # ━━ 输出 1：live_ok.m3u（可直接导入播放器）━━
    with open('live_ok.m3u', 'w', encoding='utf-8') as f:
        f.write("#EXTM3U\n")
        idx = 1
        for broad in CAT_ORDER:
            for url, elapsed in by_broad.get(broad, []):
                # 频道名：分类 + 序号，响应时间作为附加信息
                name = f"{broad} {idx:03d}"
                f.write(f'#EXTINF:-1 group-title="{broad}",{name}\n')
                f.write(f"{url}\n")
                idx += 1

    # ━━ 输出 2：live_ok.txt（可用源纯文本）━━━━━━━
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            if not urls:
                continue
            f.write(f"# ---- {broad} ----\n")
            for url, elapsed in urls:
                f.write(f"{url}\n")
            f.write("\n")

    # ━━ 输出 3：skipped.txt ━━━━━━━━━━━━━━━━━━━━━
    if skipped:
        with open('skipped.txt', 'w', encoding='utf-8') as f:
            for url in skipped:
                f.write(f"{url}\n")

    # ━━ 输出 4：live_fail.txt ━━━━━━━━━━━━━━━━━━━
    fail_raw = [(c, u, s, e, err) for c, u, s, e, err in results if s not in (200, 206)]
    seen_f = {}
    for item in fail_raw:
        u = item[1]
        if u not in seen_f:
            seen_f[u] = item
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        for broad in CAT_ORDER:
            items = [i for i in seen_f.values() if i[0] == broad]
            if not items:
                continue
            f.write(f"# ---- {broad} ----\n")
            for cat, url, status, elapsed, error in items:
                reason = f"  #{status} {error}" if error else f"  #HTTP{status}"
                f.write(f"{url}{reason}\n")
            f.write("\n")

    # ━━ 输出 5：live_report.csv ━━━━━━━━━━━━━━━━━━
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['大类', 'URL', '状态码', '响应时间(ms)', '错误'])
        for cat, url, status, elapsed, error in sorted(
                results, key=lambda x: (CAT_ORDER.index(x[0]) if x[0] in CAT_ORDER else 99, x[3])):
            w.writerow([cat, url, status, elapsed, error])

    # 汇总
    print("💾 已生成：")
    print(f"   live_ok.m3u     ← {total_ok} 条，可直接导入 TiviMate / VLC")
    print(f"   live_ok.txt     ← 可用源纯文本")
    print(f"   live_fail.txt   ← 失效源 {len(seen_f)} 条")
    if skipped:
        print(f"   skipped.txt     ← 跳过 {len(skipped)} 个")
    print(f"   live_report.csv ← 详细检测报告")

    sys.exit(0)


if __name__ == '__main__':
    main()
