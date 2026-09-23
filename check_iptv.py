#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源自动检测脚本
功能：检测 live.txt 中的 URL 可用性，按源龄分档，生成报告
"""

import os
import sys
import csv
import time
import json
import math
import socket
import argparse
import threading
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ═══════════════════════════════════════════════
# 全局配置
# ═══════════════════════════════════════════════
TIMEOUT = 20
THREADS = 10
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
HEADERS = {"User-Agent": USER_AGENT}

RECENT_DAYS = 7
MONTH_DAYS = 30
THREE_MONTH_DAYS = 90
STALE_DAYS = 90

TIER_NEW = "new"
TIER_MONTH = "month"
TIER_3MONTH = "3month"
TIER_OLD = "old"
TIER_UNKNOWN = "unknown"

# ═══════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════
def now_beijing():
    """获取北京时间字符串"""
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

def normalize_url(url):
    """标准化 URL（用于去重）"""
    url = url.strip().split("#")[0].split("?")[0]
    if url.endswith("/"):
        url = url[:-1]
    return url

def get_domain(url):
    """提取域名"""
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc or urlparse(url).path.split("/")[0]
    except Exception:
        return url

def is_direct_stream(url):
    """是否为直链流媒体"""
    return url.endswith((".m3u8", ".ts", ".m3u"))

def is_github_url(url):
    """是否为 GitHub/Gitee/GitLab 等代码托管"""
    return any(x in url for x in ["github.com", "gitee.com", "gitlab.com", "raw.githubusercontent.com"])

def is_web_page(url):
    """是否为网页"""
    return not is_direct_stream(url) and not is_github_url(url)

def tier_of(days):
    """源龄分档"""
    if days is None:
        return "🧓超三个月"
    if days <= RECENT_DAYS:
        return "🆕一周内"
    if days <= MONTH_DAYS:
        return "📅一个月内"
    if days <= THREE_MONTH_DAYS:
        return "📆三个月内"
    return "🧓超三个月"

def age_label(days):
    if days is None:
        return "未知"
    return f"{days}天"

# ═══════════════════════════════════════════════
# 源龄获取（带缓存）
# ═══════════════════════════════════════════════
get_source_age = lambda url: (None, "未知")
get_source_age._cache = {}

def fetch_source_age(url):
    """获取源更新时间（Last-Modified / GitHub 提交时间）"""
    if url in get_source_age._cache:
        return get_source_age._cache[url]
    try:
        sess = requests.Session()
        retries = Retry(total=1, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        sess.mount("http://", HTTPAdapter(max_retries=retries))
        sess.mount("https://", HTTPAdapter(max_retries=retries))
        
        head = sess.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
        lm = head.headers.get("Last-Modified")
        if lm:
            try:
                dt = datetime.datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z")
                days = (datetime.datetime.utcnow() - dt).days
                res = (days, lm)
                get_source_age._cache[url] = res
                return res
            except Exception:
                pass
        
        # GitHub/Gitee 尝试获取提交时间
        if is_github_url(url):
            try:
                api_url = url.replace("raw.githubusercontent.com", "api.github.com/repos").replace("/raw/", "/contents/")
                api_url = api_url.split("/master/")[0] + "/commits?path=" + "/".join(url.split("/master/")[1:])
                r = sess.get(api_url, headers=HEADERS, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        commit_date = data[0]["commit"]["author"]["date"]
                        dt = datetime.datetime.strptime(commit_date, "%Y-%m-%dT%H:%M:%SZ")
                        days = (datetime.datetime.utcnow() - dt).days
                        res = (days, commit_date[:16].replace("T", " "))
                        get_source_age._cache[url] = res
                        return res
            except Exception:
                pass
        
        res = (None, "未知")
        get_source_age._cache[url] = res
        return res
    except Exception:
        res = (None, "未知")
        get_source_age._cache[url] = res
        return res

# ═══════════════════════════════════════════════
# 检测核心
# ═══════════════════════════════════════════════
def check_url(url):
    """检测单个 URL"""
    start = time.time()
    norm = normalize_url(url)
    try:
        sess = requests.Session()
        retries = Retry(total=1, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        sess.mount("http://", HTTPAdapter(max_retries=retries))
        sess.mount("https://", HTTPAdapter(max_retries=retries))
        
        r = sess.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True, allow_redirects=True)
        elapsed = int((time.time() - start) * 1000)
        status = r.status_code
        
        # 判断是否可用：状态码 200 或 403（部分源 403 但可用）
        ok = status in (200, 206) or (status == 403 and is_direct_stream(url))
        flag = "✅可用" if ok else f"❌{status}"
        
        # 获取源龄
        days, update_time = fetch_source_age(norm)
        
        with _lock:
            _results.append((url, status, elapsed, flag, ok))
            if ok:
                _ok_raw.append(url)
                if days is not None:
                    if days <= RECENT_DAYS:
                        _by_tier[TIER_NEW].append(url)
                    elif days <= MONTH_DAYS:
                        _by_tier[TIER_MONTH].append(url)
                    elif days <= THREE_MONTH_DAYS:
                        _by_tier[TIER_3MONTH].append(url)
                    else:
                        _by_tier[TIER_OLD].append(url)
                        _old_but_alive[norm] = days
                else:
                    _by_tier[TIER_UNKNOWN].append(url)
                
                if days is not None and days <= RECENT_DAYS:
                    _recent_urls.add(norm)
                if days is not None and days > STALE_DAYS and not ok:
                    _stale_urls.add(norm)
            else:
                _fail_raw.append(url)
                if days is not None and days > STALE_DAYS:
                    _stale_urls.add(norm)
        
        return url, status, elapsed, flag, ok
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        flag = f"⏱超时" if "timeout" in str(e).lower() else f"❌异常"
        with _lock:
            _results.append((url, 0, elapsed, flag, False))
            _fail_raw.append(url)
        return url, 0, elapsed, flag, False

# ═══════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════
def main():
    global TIMEOUT, THREADS
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=TIMEOUT)
    parser.add_argument("--threads", type=int, default=THREADS)
    args = parser.parse_args()
    TIMEOUT = args.timeout
    THREADS = args.threads

    ts = now_beijing()
    print(f"\n{'='*60}")
    print(f"🚀 IPTV 检测开始 | {ts} (北京时间)")
    print(f"⏱超时: {TIMEOUT}s | 线程: {THREADS}")
    print(f"{'='*60}")

    # 读取 live.txt
    if not os.path.exists("live.txt"):
        print("❌ live.txt 不存在")
        sys.exit(1)
    
    urls = []
    with open("live.txt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("http"):
                norm = normalize_url(line)
                if norm not in _seen_u:
                    _seen_u.add(norm)
                    urls.append(line)
                    _uniq_urls.append(norm)
    
    print(f"📥 读取 {len(urls)} 个唯一 URL")

    # 并发检测
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        futures = [ex.submit(check_url, u) for u in urls]
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                u, s, e, fl, ok = fut.result()
                mark = "✅" if ok else "❌"
                print(f"  {mark} [{done}/{len(urls)}] {get_domain(u)} | {s} | {e}ms | {fl}")
            except Exception as ex:
                print(f"  ⚠️ 异常: {ex}")

    # 写入文件
    total_ok = len(_ok_raw)
    print(f"\n📊 统计: 可用{total_ok} | 失效{len(_fail_raw)} | 僵尸{len(_stale_urls)}")

    # live_ok.txt（纯 URL）
    with open("live_ok.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts} (北京时间) | 可用: {total_ok}\n")
        for u in _ok_raw:
            f.write(u + "\n")

    # live_ok.m3u
    with open("live_ok.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f"# 生成时间: {ts} (北京时间) | 可用: {total_ok}\n")
        for u in _ok_raw:
            name = get_domain(u)
            f.write(f"#EXTINF:-1,{name}\n{u}\n")

    # 分档 txt
    for fn, key, label in [
        ("live_week.txt", TIER_NEW, f"🆕一周内({RECENT_DAYS}天)"),
        ("live_month.txt", TIER_MONTH, "📅一个月内"),
        ("live_3month.txt", TIER_3MONTH, "📆三个月内"),
        ("live_old.txt", TIER_OLD, "🧓超三个月"),
    ]:
        with open(fn, "w", encoding="utf-8") as f:
            f.write(f"# 生成时间: {ts} (北京时间) | {label} | 数量: {len(_by_tier[key])}\n")
            for u in _by_tier[key]:
                f.write(u + "\n")

    # 僵尸源
    with open("live_stale.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts} (北京时间) | 🧟僵尸源(>90天且不通) | 数量: {len(_stale_urls)}\n")
        for s in sorted(_stale_urls):
            f.write(s + "\n")

    # 失效
    with open("live_fail.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts} (北京时间) | 失效: {len(_fail_raw)}\n")
        for u in _fail_raw:
            f.write(u + "\n")

    # ── live_report.csv（生成时间独占首行 + 更新时间列 + 网址置末）──
    with open("live_report.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        # 第1行：生成时间（仅1列，GitHub预览不报列数不一致）
        w.writerow([f"生成时间: {ts}"])
        # 第2行：表头（无"名称/大类"，更新时间在前，网址在末）
        w.writerow(["更新时间", "状态码", "响应时间(ms)", "状态",
                    "类型", "源龄(天)", "更新分档", "备注", "网址"])
        csv_seen = set()
        for url, status, elapsed, flag, ok in _results:
            norm = normalize_url(url)
            if norm in csv_seen:
                continue
            csv_seen.add(norm)
            days, desc = get_source_age._cache.get(norm, (None, "未知"))
            update_time = desc if desc else "未知"
            tier = tier_of(days)
            state = "✅可用" if ok else flag
            url_type = "直链" if is_direct_stream(url) else ("GitHub" if is_github_url(url) else ("网页" if is_web_page(url) else "其他"))
            age_str = str(days) if days is not None else "未知"
            note_parts = []
            if norm in _recent_urls:
                note_parts.append(f"🆕{RECENT_DAYS}天内更新")
            if norm in _stale_urls:
                note_parts.append(f"🧟僵尸源>{STALE_DAYS}天")
            elif norm in _old_but_alive:
                note_parts.append(f"🧓老源{_old_but_alive[norm]}天但存活")
            if days is None:
                note_parts.append("源龄未知")
            note = " | ".join(note_parts)
            w.writerow([update_time, status, elapsed, state, url_type, age_str, tier, note, url])

        # 僵尸源汇总块
        w.writerow([])
        w.writerow(["僵尸源清单", "", "", "", "", "", "", f"共{len(_stale_urls)}个", ""])
        if _stale_urls:
            for s in sorted(_stale_urls):
                days, desc = get_source_age._cache.get(s, (None, "未知"))
                w.writerow([desc if desc else "未知", "", "", "僵尸源", "", age_label(days), tier_of(days), "僵尸源", s])
        else:
            w.writerow(["本次无>90天且不通的源", "", "", "", "", "", "", "", ""])

    # 总结
    print(f"\n{'='*60}")
    print(f"✅ 全部完成 | {now_beijing()}")
    print(f"{'='*60}")
    print(f"   live_ok.txt     ← {total_ok} 条（纯URL，四档归类）")
    print(f"   live_ok.m3u     ← {total_ok} 条")
    print(f"   live_fail.txt   ← {len(_fail_raw)} 个真失效")
    print(f"   live_week.txt   ← {len(_by_tier[TIER_NEW])} 个 🆕一周内")
    print(f"   live_month.txt  ← {len(_by_tier[TIER_MONTH])} 个 📅一个月内")
    print(f"   live_3month.txt ← {len(_by_tier[TIER_3MONTH])} 个 📆三个月内")
    print(f"   live_old.txt    ← {len(_by_tier[TIER_OLD])} 个 🧓超三个月")
    print(f"   live_stale.txt  ← {len(_stale_urls)} 个 🧟僵尸源（>90天且不通）")
    print(f"   live_report.csv ← 9列报告（生成时间独占首行 / 更新时间 / 网址置末）")
    print(f"{'='*60}")

    for fn, minn in [("live_ok.txt", 1), ("live_fail.txt", 0), ("live_report.csv", 1)]:
        if os.path.exists(fn):
            n = sum(1 for _ in open(fn, encoding="utf-8"))
            if n < minn:
                print(f"⚠️ {fn} 行数{n}<{minn}，疑似清空！")

    sys.exit(0)


# ═══════════════════════════════════════════════
# 模块级集合
# ═══════════════════════════════════════════════
_lock = threading.Lock()
_results = []
_ok_raw = []
_fail_raw = []
_seen_u = set()
_uniq_urls = []
_recent_urls = set()
_stale_urls = set()
_old_but_alive = {}
_by_tier = {TIER_NEW: [], TIER_MONTH: [], TIER_3MONTH: [], TIER_OLD: [], TIER_UNKNOWN: []}

if __name__ == "__main__":
    main()
