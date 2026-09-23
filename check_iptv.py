#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源检测（最终版：四档更新时间）
- 源龄分四档：🆕一周内 / 📅一个月内 / 📆三个月内 / 🧓超三个月
- 每个源都标注更新时间档位
- 三层判定：直链严判 / GitHub限流保留 / 网页403保留
- 固定6大类、同域名排序、防清空、强制写文件
- 生成时间：北京时间（CST8）
"""

import sys
import os
import re
import csv
import time
import argparse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
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

# ━━━ 全局配置 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_THREADS = 10
DEFAULT_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

CAT_ORDER = ["国内源", "国际源", "4K高清", "TVBox", "GitHub源", "其他"]

TITLE_MAP = {
    "直播": "国际源", "其他": "其他", "GitHub源": "GitHub源",
    "国内地方/个人源": "国内源", "综合/其他聚合": "国内源",
    "咪咕/移动": "国内源", "TVBox/盒子": "TVBox",
    "4K/高清": "4K高清", "4K高清": "4K高清",
    "iptv-org": "国际源", "iptv-org 分类": "国际源",
    "代理/中转/加密": "其他", "KStore/网盘分享": "其他",
    "其他新增": "其他",
}

SKIP_URL_KEYWORDS = [
    "proxy.php?sub=", "/encrypt/", "/api/decrypt",
    ".php?sub=", "4key.cn/FP", "zo.gt.tc",
]

PREV_OK_FILE = 'live_ok.txt'

# 北京时间 UTC+8
CST8 = timezone(timedelta(hours=8))

# 四档阈值（天）
TIER1_DAYS = 7    # ≤7    一周内
TIER2_DAYS = 30   # 8-30  一个月内
TIER3_DAYS = 90   # 31-90 三个月内
# >90 超三个月

# 四档标签
TIER_NEW = "🆕一周内"
TIER_MONTH = "📅一个月内"
TIER_3MONTH = "📆三个月内"
TIER_OLD = "🧓超三个月"

# 源龄缓存
_age_cache = {}


# ━━━ URL 类型判定 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def is_direct_stream(url):
    low = url.lower()
    return any(k in low for k in [
        '.m3u8', '.ts?', '/live/', '/stream/', '/play/',
        'channel=', '/hls/', '/rtmp/', '/flv', '/dash/'
    ])


def is_web_page(url):
    low = url.lower()
    if 'github' in low:
        return False
    if is_direct_stream(url):
        return False
    return any(k in low for k in [
        '/tv/', '/live/', '/m3u/', '/playlist', '.html', '/index',
        '/list/', '/channels/', '/api/channel', '/epg'
    ])


def is_github_url(url):
    low = url.lower()
    return any(k in low for k in ['raw.githubusercontent.com', 'githubusercontent.com', 'github.com'])


# ━━━ 工具函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def normalize_url(u):
    u = u.strip().replace('\n', '').replace('\r', '')
    if '#' in u:
        u = u.split('#')[0]
    u = u.strip()
    return u


def get_sort_key(url):
    try:
        p = urlparse(url)
        net = p.netloc.lower()
        if 'github' in net or 'githubusercontent' in net:
            parts = [x for x in p.path.split('/') if x]
            if parts:
                return (net, parts[0].lower(), '/'.join(parts).lower())
        return (net, p.path.lstrip('/').lower())
    except:
        return ('', url.lower())


def classify(url, title="未分类"):
    low = normalize_url(url).lower()

    if any(k in low for k in ['4k', '8k']):
        return "4K高清"
    if is_github_url(url):
        return "GitHub源"
    if 'iptv-org' in low:
        return "国际源"
    if 'tvbox' in low or 'box' in low:
        return "TVBox"
    if title in TITLE_MAP:
        return TITLE_MAP[title]

    if 'migu' in low or 'miguvideo' in low:
        return "国内源"
    if 'freetv' in low or 'zbds' in low or 'hacks' in low or 'catvod' in low:
        return "国内源"

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


# ━━━ 源龄检测 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _github_last_commit(url):
    m = re.match(r'https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)', url)
    if not m:
        return None, None
    owner, repo, ref, path = m.groups()
    path = path.split('?')[0].split('#')[0]
    api = f"https://api.github.com/repos/{owner}/{repo}/commits?path={path}&per_page=1"
    try:
        r = requests.get(api, headers={"User-Agent": "iptv-checker"}, timeout=10)
        if r.status_code == 200 and r.json():
            dt_str = r.json()[0]["commit"]["committer"]["date"]
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            return dt, dt_str
    except Exception:
        pass
    return None, None


def get_source_age(url):
    """
    返回 (days, iso_string_or_None)。
    days: 源龄天数；None 表示无法获取。
    """
    if url in _age_cache:
        return _age_cache[url]

    now = datetime.now(timezone.utc)
    days = None
    try:
        if is_github_url(url):
            dt, _ = _github_last_commit(url)
            if dt:
                days = (now - dt).days
                _age_cache[url] = (days, _)
                return days, _

        r = requests.head(url, timeout=DEFAULT_TIMEOUT,
                          headers={"User-Agent": USER_AGENT}, allow_redirects=True, verify=False)
        lm = r.headers.get("Last-Modified")
        if lm:
            d = parsedate_to_datetime(lm)
            if d is not None:
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                days = (now - d).days
                _age_cache[url] = (days, lm)
                return days, lm
    except Exception:
        pass

    _age_cache[url] = (None, None)
    return None, None


def age_tier(days):
    """把源龄天数映射到四档标签"""
    if days is None:
        return "❓未知"
    if days <= TIER1_DAYS:
        return TIER_NEW
    if days <= TIER2_DAYS:
        return TIER_MONTH
    if days <= TIER3_DAYS:
        return TIER_3MONTH
    return TIER_OLD


def age_tier_short(days):
    """短标签，用于 m3u group-title"""
    if days is None:
        return "未知"
    if days <= TIER1_DAYS:
        return "NEW"
    if days <= TIER2_DAYS:
        return "1M"
    if days <= TIER3_DAYS:
        return "3M"
    return "OLD"


def tier_sort_key(tier):
    """排序：先按档位（新的在前），再按天数"""
    order = {TIER_NEW: 0, TIER_MONTH: 1, TIER_3MONTH: 2, TIER_OLD: 3, "❓未知": 4}
    return order.get(tier, 5)


# ━━━ 检测函数（三层判定）━━━━━━━━━━━━━━━━━━━━━━━━
def check_url(url, timeout):
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    parsed = urlparse(url)

    if 'hacks.tools' in parsed.netloc.lower():
        headers["Referer"] = "https://live.hacks.tools/"
    if 'catvod' in parsed.netloc.lower():
        headers["Referer"] = "https://iptv.catvod.com/"
    if 'migu' in parsed.netloc.lower():
        headers["Referer"] = "https://www.miguvideo.com/"

    start = time.time()
    status = 0
    elapsed = 0

    try:
        try:
            r = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False)
            elapsed = int((time.time() - start) * 1000)
            status = r.status_code

            if status in (200, 206, 301, 302):
                return url, status, elapsed, "ok"
            if status == 405:
                raise requests.exceptions.RequestException("try_get")
            if status == 403:
                if is_github_url(url):
                    return url, status, elapsed, "limited"
                if is_web_page(url):
                    return url, status, elapsed, "web_403"
                return url, status, elapsed, "fail"
            if status in (503,):
                return url, status, elapsed, "fail"
        except requests.exceptions.RequestException:
            pass

        start = time.time()
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False, stream=True)
        elapsed = int((time.time() - start) * 1000)
        r.close()
        status = r.status_code

        if status in (200, 206, 301, 302):
            return url, status, elapsed, "ok"
        if status == 403:
            if is_github_url(url):
                return url, status, elapsed, "limited"
            if is_web_page(url):
                return url, status, elapsed, "web_403"
            return url, status, elapsed, "fail"
        if status in (503,):
            return url, status, elapsed, "fail"

        return url, status, elapsed, "fail"

    except requests.exceptions.Timeout:
        return url, 0, int((time.time() - start) * 1000), "timeout"
    except requests.exceptions.ConnectionError:
        return url, 0, int((time.time() - start) * 1000), "conn"
    except Exception:
        return url, 0, int((time.time() - start) * 1000), "err"


def is_usable(status, flag, url=""):
    if status in (200, 206, 301, 302):
        return True
    if status == 403 and flag == "limited":
        return True
    if status == 403 and flag == "web_403":
        return True
    return False


# ━━━ 主流程 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    global DEFAULT_THREADS, DEFAULT_TIMEOUT

    parser = argparse.ArgumentParser(description='IPTV Checker')
    parser.add_argument('file', nargs='?', default='live.txt')
    parser.add_argument('--threads', type=int, default=DEFAULT_THREADS)
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    DEFAULT_THREADS = args.threads
    DEFAULT_TIMEOUT = args.timeout

    ts = datetime.now(CST8).strftime('%Y-%m-%d %H:%M:%S')

    if not os.path.exists(args.file):
        print(f"❌ {args.file} not found")
        sys.exit(0)

    prev_urls = load_previous_ok_urls()
    print(f"🕒 生成时间（北京时间）: {ts}")
    print(f"📂 读取 {args.file} ...")
    print(f"📋 上一次可用源记录: {len(prev_urls)} 条")
    print(f"⚙️ 线程: {DEFAULT_THREADS} | 超时: {DEFAULT_TIMEOUT}s")
    print(f"📅 更新时间分档: ≤{TIER1_DAYS}天 / ≤{TIER2_DAYS}天 / ≤{TIER3_DAYS}天 / >{TIER3_DAYS}天\n")

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

    print(f"📊 分类预览:")
    preview = defaultdict(int)
    for b, _, _ in to_check:
        preview[b] += 1
    for b in CAT_ORDER:
        if preview.get(b, 0):
            print(f"   {b}: {preview[b]} 个")
    print()

    print(f"🚀 开始检测 ({len(to_check)} 个)...\n")

    results = []
    completed = 0
    total = len(to_check)

    with ThreadPoolExecutor(max_workers=DEFAULT_THREADS) as executor:
        futures = {executor.submit(check_url, url, DEFAULT_TIMEOUT): (broad, orig_cat, url)
                   for broad, orig_cat, url in to_check}
        for future in as_completed(futures):
            broad, orig_cat, url = futures[future]
            completed += 1
            try:
                url_r, status, elapsed, flag = future.result()
            except Exception as e:
                url_r, status, elapsed, flag = url, 0, 0, "err"
            results.append((broad, orig_cat, url_r, status, elapsed, flag))

            icon = "✅" if is_usable(status, flag, url_r) else ("⚠️" if flag in ("limited", "web_403") else "❌")
            short = url_r if len(url_r) <= 55 else url_r[:52] + "..."
            print(f"  [{completed:>3}/{total}] {icon} {status:>3} | {elapsed:>5}ms | {flag:>8} | {short}")

    # ━━━ 判定可用 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ok_raw = [(b, oc, u, s, e_ms, f) for b, oc, u, s, e_ms, f in results if is_usable(s, f, u)]

    new_urls_norm = {normalize_url(u) for _, _, u, _, _, _ in ok_raw}
    new_urls_norm = {u for u in new_urls_norm if u not in {normalize_url(p) for p in prev_urls}}
    print(f"\n✨ 新增源: {len(new_urls_norm)} 条")

    seen_url = {}
    for item in ok_raw:
        u = normalize_url(item[2])
        if u not in seen_url:
            seen_url[u] = item
    ok_url_dedup = list(seen_url.values())

    total_ok = len(ok_url_dedup)

    # ━━━ 源龄分档 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print(f"\n🕵️  开始源龄检测...")
    age_map = {}       # norm_u -> (days, iso)
    tier_map = {}      # norm_u -> 档位标签
    by_tier = defaultdict(list)   # 档位 -> [norm_u, ...]

    urls_need_age = set(normalize_url(u) for _, _, u, _, _, _ in results)
    age_completed = 0
    with ThreadPoolExecutor(max_workers=DEFAULT_THREADS) as ex:
        fut_age = {ex.submit(get_source_age, u): u for u in urls_need_age}
        for fu in as_completed(fut_age):
            u = fut_age[fu]
            age_completed += 1
            try:
                days, iso = fu.result()
            except Exception:
                days, iso = None, None
            age_map[u] = (days, iso)
            tier = age_tier(days)
            tier_map[u] = tier
            by_tier[tier].append(u)
            if age_completed % 20 == 0:
                print(f"   源龄进度: {age_completed}/{len(urls_need_age)}")

    for t in [TIER_NEW, TIER_MONTH, TIER_3MONTH, TIER_OLD, "❓未知"]:
        print(f"   {t}: {len(by_tier.get(t, []))} 个")

    # 超三个月且不通 → 僵尸源
    stale_urls = set()
    for u in by_tier.get(TIER_OLD, []):
        alive = any(normalize_url(item[2]) == u and is_usable(item[3], item[5], item[2])
                    for item in ok_url_dedup)
        if not alive:
            stale_urls.add(u)
    print(f"   🧟 超三个月且不通(僵尸源): {len(stale_urls)} 个")

    # ━━━ 按大类 + 档位分组（档位优先排序）━━━━━━━
    by_broad = {broad: [] for broad in CAT_ORDER}
    seen_ok_final = set()
    for broad, orig_cat, url, status, elapsed, flag in ok_url_dedup:
        norm_u = normalize_url(url)
        if norm_u not in seen_ok_final:
            seen_ok_final.add(norm_u)
            by_broad.setdefault(broad, []).append((url, elapsed, norm_u))

    # 排序：先档位后域名
    for broad in CAT_ORDER:
        by_broad[broad].sort(key=lambda x: (tier_sort_key(tier_map.get(x[2], "❓未知")), get_sort_key(x[0])))

    print(f"\n{'='*60}")
    print(f"📊 检测完成 | {ts}")
    print(f"{'='*60}")
    for broad in CAT_ORDER:
        urls = by_broad.get(broad, [])
        if urls:
            print(f"   {broad}: {len(urls)} 条")
    print(f"   总计: {total_ok} 条")
    print(f"{'='*60}\n")

    # ━━━ 防清空 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if total_ok == 0:
        print("⚠️ 可用源为 0！保留旧 live_ok.txt，不覆盖。")
        sys.exit(0)

    # ━━━ 写文件 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # live_ok.txt
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        f.write(f"# 生成时间: {ts}\n")
        f.write(f"# 总计: {total_ok} 条\n")
        f.write(f"# 更新分档: 🆕一周内 / 📅一个月内 / 📆三个月内 / 🧓超三个月\n\n")
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            if not urls:
                f.write(f"# ---- {broad} ----\n# (无)\n\n")
                continue
            f.write(f"# ---- {broad} ----\n")
            for url, elapsed, norm_u in urls:
                tier = tier_map.get(norm_u, "❓未知")
                days, _ = age_map.get(norm_u, (None, None))
                age_str = f"{days}天" if days is not None else "未知"
                f.write(f"{url}  # {tier} | 源龄{age_str}\n")
            f.write("\n")

    # live_ok.m3u
    with open('live_ok.m3u', 'w', encoding='utf-8') as f:
        f.write(f'#EXTM3U\n# 生成时间: {ts}\n\n')
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            for url, elapsed, norm_u in urls:
                tier = tier_map.get(norm_u, "❓未知")
                short = tier_sort_key(tier)  # NEW/1M/3M/OLD
                f.write(f'#EXTINF:-1 group-title="{broad}[{short}]", {broad} {tier}\n')
                f.write(f'{url}\n')
            f.write('\n')

    # skipped.txt
    with open('skipped.txt', 'w', encoding='utf-8') as f:
        f.write(f"# 生成时间: {ts}\n# 跳过: {len(skipped)} 个\n")
        for url in skipped:
            f.write(f"{url}\n")

    # live_fail.txt
    fail_raw = [(b, oc, u, s, e, fl) for b, oc, u, s, e, fl in results if not is_usable(s, fl, u)]
    seen_f = {}
    for item in fail_raw:
        u = normalize_url(item[2])
        if u not in seen_f:
            seen_f[u] = item
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        f.write(f"# 生成时间: {ts}\n# 真失效: {len(seen_f)} 个\n\n")
        for broad in CAT_ORDER:
            items = [(b, oc, u, s, e, fl) for b, oc, u, s, e, fl in seen_f.values() if b == broad]
            if not items:
                f.write(f"# ---- {broad} ----\n# (无)\n\n")
                continue
            f.write(f"# ---- {broad} ----\n")
            for _, _, url, status, _, flag in items:
                f.write(f"{url}  #{status} {flag}\n")
            f.write("\n")

    # 四档分文件
    for tier, fname in [
        (TIER_NEW, 'live_recent.txt'),
        (TIER_MONTH, 'live_month.txt'),
        (TIER_3MONTH, 'live_3month.txt'),
        (TIER_OLD, 'live_stale.txt'),
    ]:
        urls_in_tier = by_tier.get(tier, [])
        with open(fname, 'w', encoding='utf-8') as f:
            f.write(f"# 生成时间: {ts}\n")
            f.write(f"# {tier}: {len(urls_in_tier)} 个\n\n")
            for u in urls_in_tier:
                days, desc = age_map.get(u, (None, None))
                age_str = f"{days}天" if days is not None else "未知"
                desc_str = f" | {desc}" if desc else ""
                alive = any(normalize_url(item[2]) == u and is_usable(item[3], item[5], item[2])
                            for item in ok_url_dedup)
                state = "✅可用" if alive else "❌不通"
                f.write(f"{u}  # {tier} | 源龄{age_str}{desc_str} | {state}\n")

    # live_report.csv（11列：新增四档列）
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow([f'新增源 (生成:{ts})', '大类', '原始分类', 'URL', '状态码',
                    '响应时间(ms)', '状态', '类型', '源龄(天)', '更新分档', '备注'])
        csv_seen = set()
        for broad, orig_cat, url, status, elapsed, flag in results:
            norm_u = normalize_url(url)
            if norm_u in csv_seen:
                continue
            csv_seen.add(norm_u)
            is_new = "★ 新增" if norm_u in new_urls_norm else ""
            state = "可用" if is_usable(status, flag, url) else flag
            url_type = "直链" if is_direct_stream(url) else ("GitHub" if is_github_url(url) else ("网页" if is_web_page(url) else "其他"))
            days, desc = age_map.get(norm_u, (None, None))
            age_str = str(days) if days is not None else "未知"
            tier = tier_map.get(norm_u, "❓未知")
            note = desc or ""
            w.writerow([is_new, broad, orig_cat, url, status, elapsed, state, url_type, age_str, tier, note])

    print(f"💾 已生成（统一北京时间: {ts}）:")
    print(f"   live_ok.txt     ← {total_ok} 条")
    print(f"   live_ok.m3u     ← {total_ok} 条")
    if skipped:
        print(f"   skipped.txt     ← {len(skipped)} 个")
    print(f"   live_fail.txt   ← {len(seen_f)} 个真失效")
    print(f"   live_recent.txt ← {len(by_tier.get(TIER_NEW, []))} 个 🆕一周内")
    print(f"   live_month.txt  ← {len(by_tier.get(TIER_MONTH, []))} 个 📅一个月内")
    print(f"   live_3month.txt ← {len(by_tier.get(TIER_3MONTH, []))} 个 📆三个月内")
    print(f"   live_stale.txt  ← {len(by_tier.get(TIER_OLD, []))} 个 🧓超三个月（其中{len(stale_urls)}个不通）")
    print(f"   live_report.csv ← 检测报告（11列，预览搜索正常）")

    sys.exit(0)


if __name__ == '__main__':
    main()
