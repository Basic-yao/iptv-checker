import os
import re
import sys
import csv
import argparse
import concurrent.futures
import requests
from urllib.parse import urlparse, urlunparse

# ================== 配置 ==================
TIMEOUT = 10
THREADS = 10
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 大类归类（按需调整）
CAT_ORDER = ["央视", "卫视", "地方", "电影", "剧集", "体育", "少儿", "纪实", "音乐", "综艺", "直播", "其他", "GitHub源"]

def normalize_url(u):
    """标准化URL用于去重（去掉查询参数和末尾斜杠）"""
    try:
        p = urlparse(u.strip())
        # 仅保留 scheme + netloc + path，忽略参数/片段
        clean = urlunparse((p.scheme, p.netloc, p.path.rstrip('/'), '', '', ''))
        return clean.lower()
    except:
        return u.strip().lower()

def classify_by_domain(url):
    """按域名/路径归类"""
    try:
        host = urlparse(url).netloc.lower()
        if 'githubusercontent' in host or 'github.com' in host:
            return "GitHub源"
        if 'cctv' in host: return "央视"
        if 'weishi' in host or 'hntv' in host: return "卫视"
        if 'iptv' in host: return "直播"
        return "其他"
    except:
        return "其他"

def get_path_sort_key(url):
    """提取排序键：(域名, 路径首字符)"""
    try:
        p = urlparse(url)
        domain = p.netloc.lower()
        # 路径去掉开头的斜杠，取第一个有效字符，无则空串
        path = p.path.lstrip('/')
        first_char = path[0] if path else ''
        return (domain, first_char)
    except:
        return ('', '')

def check_url(url, timeout=TIMEOUT):
    """检测URL可用性"""
    try:
        headers = {"User-Agent": USER_AGENT}
        # 允许重定向，流式请求
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
        elapsed = int(r.elapsed.total_seconds() * 1000)
        # 读取一点内容确认可用
        next(r.iter_content(chunk_size=1024, decode_unicode=False), None)
        return url, r.status_code, elapsed, ""
    except Exception as e:
        return url, 0, 0, str(e)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--threads', type=int, default=THREADS)
    parser.add_argument('--timeout', type=int, default=TIMEOUT)
    parser.add_argument('--input', default='live.txt')
    args = parser.parse_args()

    # 读取源
    if not os.path.exists(args.input):
        print(f"❌ 找不到 {args.input}")
        sys.exit(0)
    
    raw_urls = []
    with open(args.input, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'): continue
            raw_urls.append(line)

    print(f"📥 读取 {len(raw_urls)} 条源，开始检测...")

    # 新增源判定（基于输入与历史对比，此处简化：全部视为待检）
    new_urls = set(raw_urls)
    new_urls_norm = {normalize_url(u) for u in new_urls}

    results = []  # (大类, 原始分类, url, 状态码, 响应时间, 错误)
    seen_ok = set()
    by_broad = {c: [] for c in CAT_ORDER}

    # 并发检测
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as ex:
        fut = {ex.submit(check_url, u, args.timeout): u for u in raw_urls}
        for f in concurrent.futures.as_completed(fut):
            url, status, elapsed, err = f.result()
            broad = classify_by_domain(url)
            if broad not in by_broad: broad = "其他"
            results.append((broad, "", url, status, elapsed, err))

            if status in (200, 206):
                norm = normalize_url(url)
                if norm not in seen_ok:
                    seen_ok.add(norm)
                    by_broad[broad].append((url, elapsed))

    # ━━━ 写 live_ok.txt（同域名+首字符排序）━━━
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            if not urls: continue
            f.write(f"# ---- {broad} ----\n")
            # 排序：先域名，后路径首字符
            urls.sort(key=lambda x: get_path_sort_key(x[0]))
            for url, _ in urls:
                f.write(f"{url}\n")
            f.write("\n")

    # ━━━ 写 live_ok.m3u（同排序）━━━
    with open('live_ok.m3u', 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            if not urls: continue
            urls.sort(key=lambda x: get_path_sort_key(x[0]))
            for url, elapsed in urls:
                f.write(f'#EXTINF:-1 group-title="{broad}", {broad} ({elapsed}ms)\n')
                f.write(f'{url}\n')

    # ━━━ 写 skipped / fail ━━━
    skipped = []
    if skipped:
        with open('skipped.txt', 'w', encoding='utf-8') as f:
            for url in skipped: f.write(f"{url}\n")

    fail_raw = [r for r in results if r[3] not in (200, 206)]
    seen_f = {}
    for item in fail_raw:
        u = normalize_url(item[2])
        if u not in seen_f: seen_f[u] = item
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        for broad in CAT_ORDER:
            items = [v for v in seen_f.values() if v[0] == broad]
            if not items: continue
            f.write(f"# ---- {broad} ----\n")
            for _, _, url, status, _, err in items:
                reason = f"  #{status} {err}" if err else f"  #HTTP{status}"
                f.write(f"{url}{reason}\n")
            f.write("\n")

    # ━━━ 写 CSV（新增最前 + 标星 + 去重 + 同排序）━━━
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['新增源', '大类', '原始分类', 'URL', '状态码', '响应时间(ms)', '错误'])
        
        # 先按大类，再同域名+首字符
        sorted_results = sorted(
            results,
            key=lambda x: (
                0 if normalize_url(x[2]) in new_urls_norm else 1,
                CAT_ORDER.index(x[0]) if x[0] in CAT_ORDER else 99,
                get_path_sort_key(x[2]),
                x[4]
            )
        )
        
        csv_seen = set()
        for broad, orig_cat, url, status, elapsed, err in sorted_results:
            norm = normalize_url(url)
            if norm in csv_seen: continue
            csv_seen.add(norm)
            star = "★ 新增" if norm in new_urls_norm else ""
            w.writerow([star, broad, orig_cat, url, status, elapsed, err])

    total_ok = len(seen_ok)
    print(f"💾 已生成:")
    print(f"   live_ok.txt     ← {total_ok} 条（已去重，同域名按首字排序）")
    print(f"   live_ok.m3u     ← {total_ok} 条（已去重，同域名按首字排序）")
    print(f"   live_report.csv ← 报告（★新增排最前，同域名按首字排序）")

    sys.exit(0)

if __name__ == '__main__':
    main()
