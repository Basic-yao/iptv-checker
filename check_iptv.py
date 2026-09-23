#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源检测（最终稳定版 + 源龄检测）
- 三层判定：直链严判 / GitHub限流保留 / 网页403保留
- 固定6大类输出
- 同域名首字排序
- 防清空 + 强制写文件
- 生成时间：北京时间（CST8）
- live_report.csv 为标准8列表格，GitHub预览搜索正常
- 源龄检测：>90天且不通 → 分离到 live_stale.txt
"""

import sys
import os
import re
import csv
import time
import json
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
STALE_DAYS = 90  # 僵尸源阈值
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

# 源龄缓存（避免重复请求同一仓库）
_age_cache = {}
_age_lock = None


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
    """通过 commits API 获取文件最后提交时间"""
    m = re.match(r'https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)', url)
    if not m:
        return None
    owner, repo, ref, path = m.groups()
    path = path.split('?')[0].split('#')[0]
    api = f"https://api.github.com/repos/{owner}/{repo}/commits?path={path}&per_page=1"
    try:
        r = requests.get(api, headers={"User-Agent": "iptv-checker"}, timeout=10)
        if r.status_code == 200 and r.json():
            dt = r.json()[0]["commit"]["committer"]["date"]
            return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def get_source_age(url):
    """返回 (源龄天数, 来源描述)。失败返回 (None, 说明)"""
    global _age_cache
    if url in _age_cache:
        return _age_cache[url]

    now = datetime.now(timezone.utc)
    try:
        if is_github_url(url):
            lc = _github_last_commit(url)
            if lc:
                days = (now - lc).days
                res = (days, f"GitHub最后提交:{lc.strftime('%Y-%m-%d')}")
                _age_cache[url] = res
                return res

        r = requests.head(url, timeout=DEFAULT_TIMEOUT,
                          headers={"User-Agent": USER_AGENT}, allow_redirects=True, verify=False)
        lm = r.headers.get("Last-Modified")
        if lm:
            d = parsedate_to_datetime(lm)
            if d is not None:
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                days = (now - d).days
                res = (days, f"Last-Modified:{d.strftime('%Y-%m-%d')}")
                _age_cache[url] = res
                return res
    except Exception as e:
        res = (None, f"获取源龄失败:{str(e)[:30]}")
        _age_cache[url] = res
        return res

    res = (None, "源龄未知")
    _age_cache[url] = res
    return res


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
    print(f"📅 僵尸源阈值: >{STALE_DAYS} 天且不通\n")

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

    # 去重
    seen_url = {}
    for item in ok_raw:
        u = normalize_url(item[2])
        if u not in seen_url:
            seen_url[u] = item
    ok_url_dedup = list(seen_url.values())

    # 按大类分组 + 排序
    by_broad = {broad: [] for broad in CAT_ORDER}
    seen_ok_final = set()
    for broad, orig_cat, url, status, elapsed, flag in ok_url_dedup:
        norm_u = normalize_url(url)
        if norm_u not in seen_ok_final:
            seen_ok_final.add(norm_u)
            by_broad.setdefault(broad, []).append((url, elapsed))

    for broad in CAT_ORDER:
        by_broad[broad].sort(key=lambda x: get_sort_key(x[0]))

    total_ok = len(seen_ok_final)

    # ━━━ 源龄检测（可用源 + 失败源都要看年龄）━━━━
    print(f"\n🕵️  开始源龄检测...")
    age_map = {}        # url -> (days, desc)
    stale_urls = set()  # 僵尸源：>90天 且 不通
    old_but_alive = {}  # 老但存活：>90天 且 通

    # 对去重后的可用源 + 所有失败源做年龄检测
    urls_need_age = set(normalize_url(u) for _, _, u, _, _, _ in results)
    age_completed = 0
    with ThreadPoolExecutor(max_workers=DEFAULT_THREADS) as ex:
        fut_age = {ex.submit(get_source_age, u): u for u in urls_need_age}
        for fu in as_completed(fut_age):
            u = fut_age[fu]
            age_completed += 1
            try:
                days, desc = fu.result()
            except Exception:
                days, desc = None, "源龄检测异常"
            age_map[u] = (days, desc)

            norm_u = normalize_url(u)
            # 判断是否僵尸源
            is_alive = any(normalize_url(item[2]) == norm_u and is_usable(item[3], item[5], item[2])
                           for item in ok_url_dedup)
            if days is not None and days > STALE_DAYS and not is_alive:
                stale_urls.add(norm_u)
            elif days is not None and days > STALE_DAYS and is_alive:
                old_but_alive[norm_u] = days

            if age_completed % 20 == 0:
                print(f"   源龄进度: {age_completed}/{len(urls_need_age)}")

    print(f"   📊 源龄完成: {len(age_map)} 个")
    print(f"   🧟 僵尸源(>{STALE_DAYS}天且不通): {len(stale_urls)} 个")
    print(f"   🧓 老源但存活(>{STALE_DAYS}天): {len(old_but_alive)} 个")

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

    # ━━━ 强制写文件 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # live_ok.txt（剔除僵尸源）
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        f.write(f"# 生成时间: {ts}\n")
        f.write(f"# 总计: {total_ok} 条（已剔除僵尸源 {len(stale_urls)} 个）\n\n")
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            if not urls:
                f.write(f"# ---- {broad} ----\n# (无可用源)\n\n")
                continue
            # 过滤僵尸源
            live_urls = [(u, e) for u, e in urls
                         if normalize_url(u) not in stale_urls]
            if not live_urls:
                f.write(f"# ---- {broad} ----\n# (全部为僵尸源，已剔除)\n\n")
                continue
            f.write(f"# ---- {broad} ----\n")
            for url, elapsed in live_urls:
                norm_u = normalize_url(url)
                tag = ""
                if norm_u in old_but_alive:
                    tag = f"  # 老源{old_but_alive[norm_u]}天"
                f.write(f"{url}{tag}\n")
            f.write("\n")

    # live_ok.m3u
    with open('live_ok.m3u', 'w', encoding='utf-8') as f:
        f.write(f'#EXTM3U\n# 生成时间: {ts}\n\n')
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            live_urls = [(u, e) for u, e in urls
                         if normalize_url(u) not in stale_urls]
            for url, elapsed in live_urls:
                norm_u = normalize_url(url)
                tvg = broad
                if norm_u in old_but_alive:
                    tvg = f"{broad}(老源)"
                f.write(f'#EXTINF:-1 group-title="{tvg}", {broad} ({elapsed}ms)\n')
                f.write(f'{url}\n')
            f.write('\n')

    # skipped.txt
    with open('skipped.txt', 'w', encoding='utf-8') as f:
        f.write(f"# 生成时间: {ts}\n# 跳过: {len(skipped)} 个\n")
        for url in skipped:
            f.write(f"{url}\n")

    # live_fail.txt（真失效，不含僵尸源——僵尸源单独放）
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

    # live_stale.txt（僵尸源：>90天且不通）
    with open('live_stale.txt', 'w', encoding='utf-8') as f:
        f.write(f"# 生成时间: {ts}\n")
        f.write(f"# 僵尸源: {len(stale_urls)} 个（源龄>{STALE_DAYS}天 且 本次不通）\n")
        f.write(f"# 这些源已从 live_ok.txt 中剔除，建议人工复查后可删除\n\n")
        # 按源龄从大到小排序
        stale_sorted = sorted(
            [(u, age_map.get(u, (None, ''))[0], age_map.get(u, (None, ''))[1])
             for u in stale_urls],
            key=lambda x: (x[1] is None, - (x[1] or 0))
        )
        for u, days, desc in stale_sorted:
            f.write(f"{u}  #{days}天 {desc}\n")

    # ━━━ live_report.csv（10列标准表格）━━━━━━━━━━
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow([f'新增源 (生成:{ts})', '大类', '原始分类', 'URL', '状态码',
                    '响应时间(ms)', '状态', '类型', '源龄(天)', '备注'])
        csv_seen = set()
        for broad, orig_cat, url, status, elapsed, flag in results:
            norm_u = normalize_url(url)
            if norm_u in csv_seen:
                continue
            csv_seen.add(norm_u)
            is_new = "★ 新增" if norm_u in new_urls_norm else ""
            state = "可用" if is_usable(status, flag, url) else flag
            url_type = "直链" if is_direct_stream(url) else ("GitHub" if is_github_url(url) else ("网页" if is_web_page(url) else "其他"))
            days, desc = age_map.get(norm_u, (None, ""))
            age_str = str(days) if days is not None else "未知"
            note_parts = []
            if norm_u in stale_urls:
                note_parts.append(f"僵尸源>{STALE_DAYS}天")
            elif norm_u in old_but_alive:
                note_parts.append(f"老源{old_but_alive[norm_u]}天但存活")
            if days is None:
                note_parts.append("源龄未知")
            note = " | ".join(note_parts)
            w.writerow([is_new, broad, orig_cat, url, status, elapsed, state, url_type, age_str, note])

    print(f"💾 已生成（统一北京时间: {ts}）:")
    print(f"   live_ok.txt     ← {total_ok - len(stale_urls)} 条（剔除僵尸源后）")
    print(f"   live_ok.m3u     ← {total_ok - len(stale_urls)} 条")
    if skipped:
        print(f"   skipped.txt     ← {len(skipped)} 个")
    print(f"   live_fail.txt   ← {len(seen_f)} 个真失效")
    print(f"   live_stale.txt  ← {len(stale_urls)} 个僵尸源（>{STALE_DAYS}天且不通）")
    print(f"   live_report.csv ← 检测报告（10列，预览搜索正常）")

    sys.exit(0)


if __name__ == '__main__':
    main()
