#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源检测 + txt/m3u 生成（全能解析版）
支持输入格式:
  1) 裸 URL 列表:        http://...
  2) M3U 播放列表:       #EXTINF:-1 tvg-name="..." group-title="..." 后接 URL
  3) 分类标题:           # ---- 分类名 ----   /   # Category: 分类名
输出:
  live_ok.txt    ← 纯 TXT（分类+频道名+URL，已合并去重+排序）
  live_ok.m3u    ← M3U 播放列表（带 tvg-name / group-title）
  live_fail.txt  ← 失效列表（去重）
  live_report.csv← 检测报告（保留全部记录）
  skipped.txt    ← 跳过未检测的源（加密/代理类）
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

# 分类 → m3u group-title 映射
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

# 这些分类的源不发起检测（反爬/加密/动态token/内容不可信）
SKIP_CATEGORIES = {"代理/中转/加密"}

# ━━━ 解析 #EXTINF 行 ━━━━━━━━━━━━━━━━━━━━━
EXTINF_RE = re.compile(
    r'#EXTINF[^\n]*?'
    r'(?:tvg-id="(?P<id>[^"]*)")?[^\n]*?'
    r'(?:tvg-name="(?P<name>[^"]*)")?[^\n]*?'
    r'(?:group-title="(?P<group>[^"]*)")?[^\n]*?,'
    r'(?P<display>[^\r\n]*)'
)

def parse_extinf(line):
    """解析 #EXTINF 行, 返回 (频道名, 分组) 或 (None, None)"""
    m = EXTINF_RE.search(line)
    if not m:
        return None, None
    name = m.group('name') or m.group('display') or m.group('id') or ''
    group = m.group('group') or ''
    return name.strip(), group.strip()

# ━━━ 全能解析输入文件 ━━━━━━━━━━━━━━━━━━━━
def parse_file(filepath):
    """
    支持裸URL / M3U / 分类标题三种格式
    返回 (entries, skipped)
      entries: [(分类, 频道名, url), ...]   频道名可能为空
      skipped: [(分类, url), ...]           未检测(加密/代理类)
    """
    entries = []
    skipped = []
    current_cat = "未分类"
    current_name = ""

    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            # m3u 头部跳过
            if line.startswith('#EXTM3U'):
                continue

            # 注释行
            if line.startswith('#'):
                if 'http' in line:
                    # 行内注释+URL 极少见，忽略注释部分保留URL逻辑放下面处理
                    pass
                else:
                    # 分类分隔线 ---- xxx ----
                    m = re.match(r'#\s*-+\s*(.+?)\s*-+\s*$', line)
                    if m:
                        current_cat = m.group(1).strip()
                        current_name = ""
                        continue
                    # Category: xxx / 分类: xxx
                    m = re.match(r'#\s*(?:category|分类|cat)\s*[:：]\s*(.+?)\s*$',
                                 line, re.IGNORECASE)
                    if m:
                        current_cat = m.group(1).strip()
                        current_name = ""
                        continue
                    # #EXTINF 提取频道名
                    if line.startswith('#EXTINF'):
                        name, group = parse_extinf(line)
                        current_name = name
                        if group:
                            current_cat = group
                        continue
                    # 其他注释: 清空 name，保留分类
                    current_name = ""
                    continue

            # URL 行（可能带行内注释，提取第一个 http）
            if line.startswith('http'):
                # 剥离行内 # 注释
                url = line.split('#')[0].strip()
                if not url.startswith('http'):
                    continue
                if current_cat in SKIP_CATEGORIES:
                    skipped.append((current_cat, url))
                else:
                    entries.append((current_cat, current_name, url))
                current_name = ""   # 用完即清，防止串台
    return entries, skipped

# ━━━ 检测函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_url(url, timeout=8):
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    parsed = urlparse(url)
    if 'migu' in parsed.netloc.lower():
        headers["Referer"] = "https://www.miguvideo.com/"

    start = time.time()
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

# ━━━ 频道名处理与排序 ━━━━━━━━━━━━━━━━━━━━
def clean_name(name, url, cat):
    """得到可用的频道名, 用于聚合与展示"""
    if name:
        # 去掉 m3u 里常见的 group-title 残留或空白
        return name.strip()
    if '/' in url:
        n = url.split('/')[-1].split('?')[0].split('.')[0]
        n = re.sub(r'[-\d]{5,}$', '', n)
        if n:
            return n[:30]
    return cat

def sort_key(name, cat):
    """央视 -> 卫视 -> 港澳台 -> 地方台 -> 电影剧集 -> 其他"""
    n = name.upper()
    if 'CCTV' in n or '央视' in name:
        m = re.search(r'\d+', name)
        return (0, int(m.group()) if m else 0)
    if '卫视' in name:
        return (1, 0)
    if any(k in name for k in ['民視', '民视', '台視', '台视', '華視', '华视', '三立',
                                'TVBS', '中天', '東森', '东森', '澳視', '澳视',
                                '香港', '翡翠', '明珠']):
        return (2, 0)
    if '电视台' in name or (name.endswith('台') and len(name) <= 8):
        return (3, 0)
    if any(k in name for k in ['电影', '影院', '剧', '美剧', '韩剧', '综艺']):
        return (5, 0)
    return (9, 0)

# ━━━ 主流程 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    parser = argparse.ArgumentParser(description='IPTV Checker (全能解析版)')
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
    entries, skipped = parse_file(args.file)
    print(f"   待检测: {len(entries)} 条")
    if skipped:
        print(f"   ⏭️  跳过(加密/代理): {len(skipped)} 条 → skipped.txt")
    print()

    # 展开为 6 元组 (cat, name, url, status, elapsed, error)
    results = []
    if entries:
        completed = 0
        total = len(entries)
        with ThreadPoolExecutor(max_workers=args.threads) as executor:
            futures = {executor.submit(check_url, url, args.timeout): (cat, name, url)
                       for cat, name, url in entries}
            for future in as_completed(futures):
                cat, name, url = futures[future]
                completed += 1
                try:
                    url_r, status, elapsed, error = future.result()
                except Exception as e:
                    url_r, status, elapsed, error = url, 0, 0, str(e)[:50]
                results.append((cat, name, url, status, elapsed, error))

                icon = "✅" if status in (200, 206) else "⏱️" if "TIMEOUT" in error else "❌"
                short = url if len(url) <= 55 else url[:52] + "..."
                print(f"  [{completed:>3}/{total}] {icon} {status:>3} | {elapsed:>5}ms | {short}")

    # 可用 / 失效分离
    ok_raw = [(c, n, u, s, e_ms, e) for c, n, u, s, e_ms, e in results if s in (200, 206)]
    fail_raw = [(c, n, u, s, e_ms, e) for c, n, u, s, e_ms, e in results if s not in (200, 206)]

    # URL 级去重（保留首次出现）
    seen_url = {}
    for item in ok_raw:
        u = item[2]
        if u not in seen_url:
            seen_url[u] = item
    ok_dedup = list(seen_url.values())

    # 频道级合并：同频道多源 → 取响应最快的一条
    best = {}
    for cat, name, url, status, elapsed, error in ok_dedup:
        n = clean_name(name, url, cat)
        key = (cat, n)
        if key not in best or elapsed < best[key][4]:
            best[key] = (cat, n, url, status, elapsed, error)
    merged = list(best.values())
    merged.sort(key=lambda x: (sort_key(x[1], x[0]), x[1]))

    # 失效列表去重 + 按分类/名称排序
    seen_f = {}
    for item in fail_raw:
        u = item[2]
        if u not in seen_f:
            seen_f[u] = item
    fail_list = sorted(seen_f.values(),
                       key=lambda x: (x[0], clean_name(x[1], x[2], x[0])))

    # 统计输出
    print(f"\n✅ 可用: {len(merged)} (合并自 {len(ok_dedup)} 条，多源择优)")
    print(f"❌ 失效: {len(fail_list)} (去重后)")
    dup = len(ok_raw) - len(ok_dedup)
    merged_dup = len(ok_dedup) - len(merged)
    if dup:
        print(f"   🧹 URL去重移除: {dup}")
    if merged_dup:
        print(f"   🧹 同频道合并移除: {merged_dup}")
    if skipped:
        print(f"   ⏭️  跳过未检测: {len(skipped)}")

    # ━━━ 写 live_ok.txt（分类+频道名+URL）━━━━━━
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        prev = None
        for cat, name, url, *_ in merged:
            if cat != prev:
                f.write(f"\n# ---- {cat} ----\n")
                prev = cat
            f.write(f"# {name}\n{url}\n")

    # ━━━ 写 live_ok.m3u（带 tvg-name / group-title）━━
    with open('live_ok.m3u', 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        prev = None
        for cat, name, url, *_ in merged:
            group = CAT_MAP.get(cat, "其他")
            if cat != prev:
                f.write(f"\n# ===== {cat} =====\n")
                prev = cat
            f.write(f'#EXTINF:-1 tvg-name="{name}" group-title="{group}",{name}\n')
            f.write(f'{url}\n')

    # ━━━ 写 live_fail.txt ━━━━━━━━━━━━━━━━━━━
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        prev = None
        for cat, name, url, status, _, error in fail_list:
            if cat != prev:
                f.write(f"\n# ---- {cat} ----\n")
                prev = cat
            reason = f"  # {status} {error}" if error else f"  # HTTP {status}"
            display = clean_name(name, url, cat)
            f.write(f"# {display}\n{url}{reason}\n")

    # ━━━ 写 skipped.txt ━━━━━━━━━━━━━━━━━━━━
    with open('skipped.txt', 'w', encoding='utf-8') as f:
        prev = None
        for cat, url in skipped:
            if cat != prev:
                f.write(f"\n# ---- {cat} ----\n")
                prev = cat
            f.write(f"{url}\n")

    # ━━━ 写 CSV 报告（不去重，保留全部）━━━━━━
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['分类', '频道名', 'URL', '状态码', '响应时间(ms)', '错误'])
        for cat, name, url, status, elapsed, error in \
                sorted(results, key=lambda x: (x[0], clean_name(x[1], x[2], x[0]))):
            w.writerow([cat, clean_name(name, url, cat), url, status, elapsed, error])

    print(f"\n💾 已生成:")
    print(f"   live_ok.txt    ← 纯 TXT（合并去重+排序）")
    print(f"   live_ok.m3u    ← M3U（带 tvg-name/group-title）")
    print(f"   live_fail.txt  ← 失效列表（去重）")
    print(f"   skipped.txt    ← 跳过未检测（加密/代理源）")
    print(f"   live_report.csv← 检测报告（保留全部记录）")


if __name__ == '__main__':
    main()
