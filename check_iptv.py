import sys
import os
import re
import csv
import requests
from datetime import datetime
from collections import OrderedDict

# ================== 基础配置 ==================
CAT_ORDER = ['CCTV', '卫视', '地方', '港澳台', '海外', '其他']
TIMEOUT = 20
THREADS = 10

def normalize_url(u):
    return re.sub(r'&?t=\d+', '', u.strip()).split('#')[0].strip()

def is_direct_stream(u):
    return '.m3u8' in u or '.ts' in u or 'stream' in u or 'live' in u

def is_github_url(u):
    return 'raw.githubusercontent.com' in u or 'github.com' in u

def is_web_page(u):
    return not is_direct_stream(u) and not is_github_url(u)

def is_usable(status, flag, url):
    if status in (200, 206): return True
    if is_github_url(url) and status == 403: return True
    if is_web_page(url) and status == 403: return True
    return False

def get_sort_key(name, url):
    base = re.sub(r'^[★ ]+', '', name).strip()
    if base: return base[0].lower(), base
    return (url.split('//')[-1].split('/')[0] or 'z').lower(), url

# ================== 检测逻辑（简化示意，保留核心） ==================
def check_url(url, broad, orig_cat):
    elapsed, status, flag = 0, 0, 'fail'
    try:
        r = requests.head(url, timeout=TIMEOUT, allow_redirects=True)
        status = r.status_code
        elapsed = int(r.elapsed.total_seconds() * 1000)
        if is_usable(status, flag, url): flag = 'ok'
    except Exception as e:
        flag = str(e)[:20]
    return broad, orig_cat, url, status, elapsed, flag

def main():
    import argparse, concurrent.futures
    parser = argparse.ArgumentParser()
    parser.add_argument('file', nargs='?', default='live.txt')
    parser.add_argument('--threads', type=int, default=THREADS)
    parser.add_argument('--timeout', type=int, default=TIMEOUT)
    args = parser.parse_args()

    global TIMEOUT, THREADS
    TIMEOUT, THREADS = args.timeout, args.threads

    lines = open(args.file, encoding='utf-8').read().splitlines()
    tasks = []
    for ln in lines:
        if not ln.strip() or ln.startswith('#'): continue
        # 简单解析：名称,url （实际可按你原逻辑）
        parts = ln.split(',')
        url = parts[-1].strip()
        name = parts[0].strip() if len(parts)>1 else url
        broad = '其他'
        for c in CAT_ORDER:
            if c in name: broad = c; break
        tasks.append((url, broad, name))

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=THREADS) as ex:
        fut = [ex.submit(check_url, u, b, n) for u, b, n in tasks]
        for f in concurrent.futures.as_completed(fut):
            results.append(f.result())

    # 分类与去重
    ok_items = [r for r in results if is_usable(r[3], r[5], r[2])]
    fail_items = [r for r in results if not is_usable(r[3], r[5], r[2])]
    new_urls_norm = set(normalize_url(r[2]) for r in ok_items)

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    header_ok = f"# 生成时间: {ts}\n# 总计: {len(ok_items)}\n"

    # live_ok.txt
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        f.write(header_ok)
        for broad in CAT_ORDER:
            items = sorted([r for r in ok_items if r[0]==broad], key=lambda x: get_sort_key(x[1], x[2]))
            if items:
                f.write(f"# ---- {broad} ----\n")
                for _, orig_cat, url, _, elapsed, _ in items:
                    f.write(f"{orig_cat},{url}  # {elapsed}ms\n")
                f.write("\n")

    # live_ok.m3u
    with open('live_ok.m3u', 'w', encoding='utf-8') as f:
        f.write(f"#EXTM3U\n# {header_ok}")
        for broad in CAT_ORDER:
            items = [r for r in ok_items if r[0]==broad]
            for _, orig_cat, url, _, elapsed, _ in items:
                f.write(f'#EXTINF:-1 group-title="{broad}", {orig_cat} ({elapsed}ms)\n{url}\n')

    # skipped.txt（无跳过则写空/表头）
    with open('skipped.txt', 'w', encoding='utf-8') as f:
        f.write(f"# 生成时间: {ts}\n# 无跳过项\n")

    # live_fail.txt
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        f.write(f"# 生成时间: {ts}\n# 真失效: {len(fail_items)}\n")
        for broad in CAT_ORDER:
            items = [r for r in fail_items if r[0]==broad]
            if items:
                f.write(f"# ---- {broad} ----\n")
                for _, _, url, status, _, flag in items:
                    f.write(f"{url}  #{status} {flag}\n")
                f.write("\n")

    # live_report.csv（始终写表头）
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['生成时间', ts])
        w.writerow(['新增源', '大类', '原始分类', 'URL', '状态码', '响应时间(ms)', '状态', '类型'])
        csv_seen = set()
        for broad, orig_cat, url, status, elapsed, flag in results:
            norm_u = normalize_url(url)
            if norm_u in csv_seen: continue
            csv_seen.add(norm_u)
            is_new = "★ 新增" if norm_u in new_urls_norm else ""
            state = "可用" if is_usable(status, flag, url) else flag
            url_type = "直链" if is_direct_stream(url) else ("GitHub" if is_github_url(url) else ("网页" if is_web_page(url) else "其他"))
            w.writerow([is_new, broad, orig_cat, url, status, elapsed, state, url_type])

    print(f"💾 已生成 (统一时间: {ts})")
    print(f"   live_ok.txt     ← {len(ok_items)} 条")
    print(f"   live_ok.m3u     ← {len(ok_items)} 条")
    print(f"   live_fail.txt   ← {len(fail_items)} 个真失效")
    print(f"   live_report.csv ← 检测报告")

    sys.exit(0)

if __name__ == '__main__':
    main()
