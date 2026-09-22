import os
import re
import sys
import csv
import time
import argparse
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ========== 北京时间 ==========
CST8 = timezone(timedelta(hours=8))

# ========== 固定6大类 ==========
CAT_ORDER = ["国内源", "国外源", "其他", "直播", "回放", "测试"]

# ========== 全局配置（修复 global 声明顺序） ==========
DEFAULT_THREADS = 10
DEFAULT_TIMEOUT = 20

# ========== 工具函数 ==========
def now_beijing():
    return datetime.now(CST8).strftime('%Y-%m-%d %H:%M:%S')

def normalize_url(u):
    return re.sub(r'^https?://(www\.)?', '', u).rstrip('/').lower()

def is_github_url(u):
    return any(x in u for x in ['github.com', 'gitee.com', 'gitlab.com'])

def is_direct_stream(u):
    return u.endswith(('.m3u8', '.ts', '.m3u'))

def is_web_page(u):
    return not is_direct_stream(u) and not is_github_url(u)

def get_sort_key(url):
    norm = normalize_url(url)
    return (norm.split('/')[0], norm)

def is_usable(status, flag, url):
    # 直链
    if is_direct_stream(url):
        return status == 200 and flag != 'fail'
    # GitHub
    if is_github_url(url):
        return status in (200, 403) and flag != 'fail'  # 403限流保留
    # 网页
    if is_web_page(url):
        return status == 200 or (status == 403 and flag == 'web_403')  # 网页403保留
    return False

# ========== 请求检测 ==========
def check_url(url, timeout=DEFAULT_TIMEOUT):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
    }
    flag = ''
    start = time.time()
    try:
        s = requests.Session()
        retries = Retry(total=1, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        s.mount('http://', HTTPAdapter(max_retries=retries))
        s.mount('https://', HTTPAdapter(max_retries=retries))
        
        r = s.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
        elapsed = int((time.time() - start) * 1000)
        status = r.status_code
        
        if is_direct_stream(url):
            flag = 'ok' if status == 200 else 'fail'
        elif is_github_url(url):
            if status == 403:
                flag = 'limited'
            else:
                flag = 'ok' if status == 200 else 'fail'
        else:
            if status == 403:
                flag = 'web_403'
            else:
                flag = 'ok' if status == 200 else 'fail'
        return status, elapsed, flag
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        return 0, elapsed, 'error'

# ========== 分类映射 ==========
def map_category(raw):
    raw_l = raw.lower()
    if any(x in raw_l for x in ['国内', '央视', '卫视', '地方', 'hk', '台湾']):
        return '国内源'
    if any(x in raw_l for x in ['国外', '海外', '国际']):
        return '国外源'
    if '回放' in raw_l:
        return '回放'
    if '测试' in raw_l:
        return '测试'
    return '其他'

# ========== 主流程 ==========
def main():
    global DEFAULT_THREADS, DEFAULT_TIMEOUT
    parser = argparse.ArgumentParser()
    parser.add_argument('--threads', type=int, default=DEFAULT_THREADS)
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    DEFAULT_THREADS = args.threads
    DEFAULT_TIMEOUT = args.timeout

    ts = now_beijing()
    print(f"🕒 生成时间（北京时间）: {ts}")

    # 读输入
    if not os.path.exists('live.txt'):
        print("❌ live.txt 不存在")
        sys.exit(1)
    
    raw_lines = [l.strip() for l in open('live.txt', 'r', encoding='utf-8') if l.strip() and not l.startswith('#')]
    print(f"📥 读取 {len(raw_lines)} 行")

    # 解析
    items = []
    current_cat = '其他'
    for line in raw_lines:
        if line.startswith('#EXTINF') or (line.startswith('#') and 'group-title' in line):
            m = re.search(r'group-title="([^"]+)"', line)
            if m:
                current_cat = m.group(1)
        elif line.startswith('http'):
            broad = map_category(current_cat)
            items.append((broad, current_cat, line))
        else:
            current_cat = line.strip('#').strip() or current_cat

    print(f"🔗 待检测: {len(items)} 个")

    # 检测
    results = []
    skipped = []
    new_urls_norm = set()
    
    # 旧数据对比（如有）
    old_norm = set()
    if os.path.exists('live_ok.txt'):
        for l in open('live_ok.txt', 'r', encoding='utf-8'):
            if l.startswith('http'):
                old_norm.add(normalize_url(l.strip()))

    with ThreadPoolExecutor(max_workers=DEFAULT_THREADS) as ex:
        futures = {ex.submit(check_url, u, DEFAULT_TIMEOUT): (b, oc, u) for b, oc, u in items}
        done = 0
        for fu in as_completed(futures):
            b, oc, u = futures[fu]
            status, elapsed, flag = fu.result()
            done += 1
            if done % 10 == 0:
                print(f"  进度: {done}/{len(items)}")
            if status == 0 and flag == 'error':
                skipped.append(u)
                continue
            results.append((b, oc, u, status, elapsed, flag))
            if normalize_url(u) not in old_norm:
                new_urls_norm.add(normalize_url(u))

    # 分类整理
    by_broad = {b: [] for b in CAT_ORDER}
    for b, oc, u, s, e, fl in results:
        if is_usable(s, fl, u):
            by_broad[b].append((u, e))
    for b in by_broad:
        by_broad[b].sort(key=lambda x: get_sort_key(x[0]))

    total_ok = sum(len(v) for v in by_broad.values())

    # ===== 输出（全部带北京时间头） =====
    
    # live_ok.txt
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        f.write(f"# 生成时间: {ts}\n")
        f.write(f"# 可用源: {total_ok} 个\n\n")
        if total_ok == 0:
            f.write("# (无可用源)\n")
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            if not urls:
                f.write(f"# ---- {broad} ----\n# (无)\n\n")
                continue
            f.write(f"# ---- {broad} ----\n")
            for url, elapsed in urls:
                f.write(f"{url}  #{elapsed}ms\n")
            f.write("\n")

    # live_ok.m3u
    with open('live_ok.m3u', 'w', encoding='utf-8') as f:
        f.write(f'#EXTM3U\n# 生成时间: {ts}\n\n')
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            if not urls:
                continue
            for url, elapsed in urls:
                f.write(f'#EXTINF:-1 group-title="{broad}", {broad} ({elapsed}ms)\n')
                f.write(f'{url}\n')
            f.write('\n')

    # skipped.txt
    with open('skipped.txt', 'w', encoding='utf-8') as f:
        f.write(f"# 生成时间: {ts}\n# 跳过: {len(skipped)} 个\n")
        for url in skipped:
            f.write(f"{url}\n")

    # live_fail.txt（★ 修复：加回时间头）
    fail_raw = [(b, oc, u, s, e, fl) for b, oc, u, s, e, fl in results if not is_usable(s, fl, u)]
    seen_f = {}
    for item in fail_raw:
        u = normalize_url(item[2])
        if u not in seen_f:
            seen_f[u] = item
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        f.write(f"# 生成时间: {ts}\n# 真失效: {len(seen_f)} 个\n\n")
        for broad in CAT_ORDER:
            items_f = [(b, oc, u, s, e, fl) for b, oc, u, s, e, fl in seen_f.values() if b == broad]
            if not items_f:
                f.write(f"# ---- {broad} ----\n# (无)\n\n")
                continue
            f.write(f"# ---- {broad} ----\n")
            for _, _, url, status, _, flag in items_f:
                f.write(f"{url}  #{status} {flag}\n")
            f.write("\n")

    # live_report.csv（★ 保留：时间单行注释，不占数据列）
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow([f'# 生成时间: {ts}'])
        w.writerow(['新增源', '大类', '原始分类', 'URL', '状态码', '响应时间(ms)', '状态', '类型'])
        csv_seen = set()
        for broad, orig_cat, url, status, elapsed, flag in results:
            norm_u = normalize_url(url)
            if norm_u in csv_seen:
                continue
            csv_seen.add(norm_u)
            is_new = "★ 新增" if norm_u in new_urls_norm else ""
            state = "可用" if is_usable(status, flag, url) else flag
            url_type = "直链" if is_direct_stream(url) else ("GitHub" if is_github_url(url) else ("网页" if is_web_page(url) else "其他"))
            w.writerow([is_new, broad, orig_cat, url, status, elapsed, state, url_type])

    print(f"\n💾 已生成（统一北京时间: {ts}）:")
    print(f"   live_ok.txt     ← {total_ok} 条")
    print(f"   live_ok.m3u     ← {total_ok} 条")
    if skipped:
        print(f"   skipped.txt     ← {len(skipped)} 个")
    print(f"   live_fail.txt   ← {len(seen_f)} 个真失效")
    print(f"   live_report.csv ← 检测报告")

    sys.exit(0)

if __name__ == '__main__':
    main()
