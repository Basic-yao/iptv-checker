#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源检测 + txt 生成
输出: live_ok.txt / live_ok.txt / live_fail.txt / live_report.csv
"""

import sys
import os
import re
import time
import csv
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("❌ pip install requests")
    sys.exit(1)

# ━━━ 配置 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_THREADS = 25
DEFAULT_TIMEOUT = 8
USER_AGENT = "Mozilla/5.0 (Linux; Android 10; TV) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

# 分类 → txt group-title 映射
CAT_MAP = {
    "中文综合聚合": "综合聚合",
    "vbskycn 镜像": "国内源",
    "iptv-org 分类": "国际源",
    "综合/其他聚合": "综合聚合",
    "国内地方/个人源": "国内源",
    "咪咕/移动": "咪咕",
    "TVBox/盒子": "TVBox",
    "4K/高清": "4K",
    "代理/中转/加密": "代理源",
    "KStore/网盘分享": "网盘分享",
    "其他新增": "其他",
    "未分类": "其他",
}

def check_url(url, timeout=8):
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    parsed = urlparse(url)
    if 'migu' in parsed.netloc.lower():
        headers["Referer"] = "https://www.miguvideo.com/"

    start = time.time()
    try:
        # HEAD first
        try:
            r = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False)
            elapsed = int((time.time() - start) * 1000)
            if r.status_code in (200, 206):
                return url, r.status_code, elapsed, ""
            if r.status_code == 405:
                raise requests.exceptions.RequestException("fallback")
        except requests.exceptions.RequestException:
            pass

        # GET fallback
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


def parse_file(filepath):
    entries = []
    current_cat = "未分类"
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
                m = re.match(r'#\s*-+\s*(.+?)\s*-+', line)
                current_cat = m.group(1).strip() if m else line.lstrip('#').strip()
                continue
            if line.startswith('http'):
                entries.append((current_cat, line))
    return entries


def main():
    parser = argparse.ArgumentParser(description='IPTV Checker')
    parser.add_argument('file', nargs='?', default='live.txt')
    parser.add_argument('--threads', type=int, default=DEFAULT_THREADS)
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if not os.path.exists(args.file):
        print(f"❌ {args.file} not found")
        sys.exit(1)

    print(f"📂 读取 {args.file} ...")
    entries = parse_file(args.file)
    print(f"   共 {len(entries)} 个链接\n")

    results = []
    completed = 0
    total = len(entries)

    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {executor.submit(check_url, url, args.timeout): (cat, url) for cat, url in entries}
        for future in as_completed(futures):
            cat, url = futures[future]
            completed += 1
            try:
                url_r, status, elapsed, error = future.result()
            except Exception as e:
                url_r, status, elapsed, error = url, 0, 0, str(e)[:50]
            results.append((cat, url, status, elapsed, error))

            icon = "✅" if status in (200, 206) else "⏱️" if "TIMEOUT" in error else "❌"
            short = url if len(url) <= 55 else url[:52] + "..."
            print(f"  [{completed:>3}/{total}] {icon} {status:>3} | {elapsed:>5}ms | {short}")

    ok_list = [(c, u, s, e_ms, e) for c, u, s, e_ms, e in results if s in (200, 206)]
    fail_list = [(c, u, s, e_ms, e) for c, u, s, e_ms, e in results if s not in (200, 206)]

    print(f"\n✅ 可用: {len(ok_list)}  ❌ 失效: {len(fail_list)}  📊 {len(ok_list)/total*100:.1f}%")

    # ━━━ 写 live_ok.txt ━━━
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        prev_cat = None
        for cat, url, *_ in ok_list:
            if cat != prev_cat:
                f.write(f"\n# ---- {cat} ----\n")
                prev_cat = cat
            f.write(f"{url}\n")

    # ━━━ 写 live_ok.txt（分类分组）━━━
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        prev_cat = None
        for cat, url, *_ in ok_list:
            group = CAT_MAP.get(cat, "其他")
            if cat != prev_cat:
                f.write(f"\n# ===== {cat} =====\n")
                prev_cat = cat
            # 从 URL 提取一个简短名称
            name = url.split('/')[-1].split('.')[0][:30] if '/' in url else cat
            f.write(f'#EXTINF:-1 tvg-name="{name}" group-title="{group}",{name}\n')
            f.write(f'{url}\n')

    # ━━━ 写 live_fail.txt ━━━
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        prev_cat = None
        for cat, url, status, _, error in fail_list:
            if cat != prev_cat:
                f.write(f"\n# ---- {cat} ----\n")
                prev_cat = cat
            reason = f"  # {status} {error}" if error else f"  # HTTP {status}"
            f.write(f"{url}{reason}\n")

    # ━━━ 写 CSV ━━━
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['分类', 'URL', '状态码', '响应时间(ms)', '错误'])
        for cat, url, status, elapsed, error in sorted(results, key=lambda x: (x[0], -x[2])):
            w.writerow([cat, url, status, elapsed, error])

    print(f"\n💾 已生成:")
    print(f"   live_ok.txt    ← 可直接导入播放器")
    print(f"   live_ok.txt")
    print(f"   live_fail.txt")
    print(f"   live_report.csv")


if __name__ == '__main__':
    main()
