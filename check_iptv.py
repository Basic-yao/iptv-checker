#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 检查器（四档分档 + 真实源龄 + 原OK判定不变）
分档：🆕一周内(≤7天) | 📅一个月内(≤30天) | 📆三个月内(≤90天) | 🧓超三个月(>90天)
源龄：远程URL用Last-Modified/GitHub API；取不到标未知
live_ok.txt = 纯URL，按四档归类，无尾注
live_report.csv = 生成时间首行首列 / 更新时间只到日期 / 网址置末 / 严格9列
"""
import os
import re
import sys
import csv
import time
import json
import socket
import threading
import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urlunparse

import requests
from requests.exceptions import RequestException, Timeout, ConnectionError
from concurrent.futures import ThreadPoolExecutor, as_completed

# ═══════════════════════════════════════════════
# 全局配置
# ═══════════════════════════════════════════════
THREADS = 10
TIMEOUT = 20
STALE_DAYS = 90
RECENT_DAYS = 7
MONTH_DAYS = 30
THREE_MONTH_DAYS = 90

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": USER_AGENT}

TIER_NEW = "🆕一周内"
TIER_MONTH = "📅一个月内"
TIER_3MONTH = "📆三个月内"
TIER_OLD = "🧓超三个月"
TIER_UNKNOWN = "❓未知"

CST8 = timezone(timedelta(hours=8))

# ═══════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════
def now_cst():
    return datetime.now(CST8)

def ts_cst():
    return now_cst().strftime("%Y-%m-%d %H:%M:%S")

def normalize_url(u):
    u = u.strip().split("#")[0].strip()
    try:
        p = urlparse(u)
        netloc = p.netloc.lower().replace("www.", "")
        path = p.path.rstrip("/")
        return urlunparse((p.scheme.lower(), netloc, path, "", "", ""))
    except Exception:
        return u.strip().lower()

def get_domain(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return url

def is_direct_stream(url):
    low = url.lower()
    return any(k in low for k in [".m3u8", ".ts?", "/live/", "/stream/", "/play/",
                                   "channel=", "/hls/", "/rtmp/", "/flv", "/dash/"])

def is_web_page(url):
    low = url.lower()
    if "github" in low:
        return False
    if is_direct_stream(url):
        return False
    return any(k in low for k in ["/tv/", "/live/", "/m3u/", "/playlist", ".html",
                                   "/index", "/list/", "/channels/", "/api/channel", "/epg"])

def is_github_url(url):
    low = url.lower()
    return any(k in low for k in ["raw.githubusercontent.com", "githubusercontent.com", "github.com"])

# ═══════════════════════════════════════════════
# 源龄检测（返回 days + 更新日期，只到日期）
# ═══════════════════════════════════════════════
def get_source_age(url):
    norm = normalize_url(url)
    if hasattr(get_source_age, "_cache"):
        if norm in get_source_age._cache:
            return get_source_age._cache[norm]
    else:
        get_source_age._cache = {}

    now = now_cst()
    days = None
    update_date = ""   # 只到日期，如 2026-09-20

    try:
        if is_github_url(url):
            m = re.match(r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", norm)
            if m:
                owner, repo, branch, path = m.groups()
                path = path.split("?")[0].split("#")[0]
                api = f"https://api.github.com/repos/{owner}/{repo}/commits?path={path}&per_page=1"
                try:
                    r = requests.get(api, headers=HEADERS, timeout=10)
                    if r.status_code == 200 and r.json():
                        dt_str = r.json()[0]["commit"]["committer"]["date"]
                        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                        days = (now - dt.astimezone(CST8)).days
                        update_date = dt.astimezone(CST8).strftime("%Y-%m-%d")
                        get_source_age._cache[norm] = (days, update_date)
                        return days, update_date
                except Exception:
                    pass

        try:
            r = requests.head(norm, headers=HEADERS, timeout=10, allow_redirects=True, verify=False)
            lm = r.headers.get("Last-Modified")
            if lm:
                dt = parsedate_to_datetime(lm)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = (now - dt.astimezone(CST8)).days
                update_date = dt.astimezone(CST8).strftime("%Y-%m-%d")
                get_source_age._cache[norm] = (days, update_date)
                return days, update_date
        except Exception:
            pass
    except Exception:
        pass

    update_date = "未知"
    get_source_age._cache[norm] = (None, update_date)
    return None, update_date

def tier_of(days):
    if days is None:
        return TIER_OLD
    if days <= RECENT_DAYS:
        return TIER_NEW
    if days <= MONTH_DAYS:
        return TIER_MONTH
    if days <= THREE_MONTH_DAYS:
        return TIER_3MONTH
    return TIER_OLD

def age_label(days):
    if days is None:
        return "未知"
    if days <= RECENT_DAYS:
        return f"{days}天(一周内)"
    if days <= MONTH_DAYS:
        return f"{days}天(一月内)"
    if days <= THREE_MONTH_DAYS:
        return f"{days}天(三月内)"
    return f"{days}天(超三月)"

# ═══════════════════════════════════════════════
# 检测核心（三层判定）
# ═══════════════════════════════════════════════
def check_url(url):
    norm = normalize_url(url)
    if norm in _seen_u:
        return
    _seen_u.add(norm)
    _uniq_urls.append(url)

    start = time.time()
    status = 0
    elapsed = 0
    flag = ""

    try:
        try:
            r = requests.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, verify=False)
            elapsed = int((time.time() - start) * 1000)
            status = r.status_code
            if status in (200, 206, 301, 302):
                flag = "ok"
            elif status == 403:
                if is_github_url(url):
                    flag = "limited"
                elif is_web_page(url):
                    flag = "web_403"
                else:
                    flag = "fail"
            elif status == 405:
                raise RequestException("try_get")
            else:
                flag = "fail"
        except RequestException:
            pass

        if flag in ("", "fail", None):
            start = time.time()
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, verify=False, stream=True)
            elapsed = int((time.time() - start) * 1000)
            r.close()
            status = r.status_code
            if status in (200, 206, 301, 302):
                flag = "ok"
            elif status == 403:
                if is_github_url(url):
                    flag = "limited"
                elif is_web_page(url):
                    flag = "web_403"
                else:
                    flag = "fail"
            else:
                flag = "fail"
    except Timeout:
        elapsed = int((time.time() - start) * 1000)
        flag = "timeout"
    except ConnectionError:
        elapsed = int((time.time() - start) * 1000)
        flag = "conn"
    except Exception:
        elapsed = int((time.time() - start) * 1000)
        flag = "err"

    ok = is_usable(status, flag, url)
    with _lock:
        _results.append((url, status, elapsed, flag, ok))
        if ok:
            _ok_raw.append((url, status, elapsed, flag))
        else:
            _fail_raw.append((url, status, elapsed, flag))

def is_usable(status, flag, url=""):
    if status in (200, 206, 301, 302):
        return True
    if status == 403 and flag in ("limited", "web_403"):
        return True
    return False

# ═══════════════════════════════════════════════
# 写文件通用表头
# ═══════════════════════════════════════════════
def write_header(f, title):
    f.write(f"# 生成时间(北京时间): {ts_cst()}\n")
    f.write(f"# {title}\n\n")

# ═══════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════
def main():
    global THREADS, TIMEOUT

    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=THREADS)
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    args = ap.parse_args()
    THREADS = args.threads
    TIMEOUT = args.timeout

    ts = ts_cst()
    print(f"🕒 开始检测（北京时间 {ts}）")
    print(f"⚙️ 线程: {THREADS} | 超时: {TIMEOUT}s")
    print(f"📅 分档: ≤{RECENT_DAYS}天 / ≤{MONTH_DAYS}天 / ≤{THREE_MONTH_DAYS}天 / >{THREE_MONTH_DAYS}天")

    if not os.path.exists("live.txt"):
        sys.exit("❌ 缺 live.txt")

    raw_lines = []
    with open("live.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                raw_lines.append(line)
    print(f"📥 读取 {len(raw_lines)} 行")

    dedup_map = {}
    for line in raw_lines:
        u = normalize_url(line)
        if u and u.startswith("http"):
            dedup_map[u] = line
    print(f"🔗 去重后 {len(dedup_map)} 个唯一源")

    print("🚀 开始检测...")
    completed = 0
    total = len(dedup_map)
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        futures = {ex.submit(check_url, url): url for url in dedup_map.values()}
        for fu in as_completed(futures):
            completed += 1
            url = futures[fu]
            norm = normalize_url(url)
            ok = any(normalize_url(r[0]) == norm and r[4] for r in _results)
            icon = "✅" if ok else "❌"
            short = url if len(url) <= 50 else url[:47] + "..."
            if completed % 50 == 0 or completed == total:
                print(f"  [{completed}/{total}] {icon} {short}")

    total_ok = len(_ok_raw)
    print(f"\n📊 检测完成: ✅{total_ok} 可用 | ❌{len(_fail_raw)} 不通")

    print("\n🕵️ 开始源龄检测...")
    age_completed = 0
    for url in _uniq_urls:
        norm = normalize_url(url)
        days, update_date = get_source_age(url)
        tier = tier_of(days)
        _by_tier[tier].append(url)
        is_ok = any(normalize_url(r[0]) == norm and r[4] for r in _results)
        if days is not None and days <= RECENT_DAYS and is_ok:
            _recent_urls.add(norm)
        if days is not None and days > STALE_DAYS:
            if not is_ok:
                _stale_urls.add(norm)
            else:
                _old_but_alive[norm] = days
        age_completed += 1
        if age_completed % 30 == 0:
            print(f"   源龄进度: {age_completed}/{len(_uniq_urls)}")

    print(f"\n📊 分档结果:")
    for t in [TIER_NEW, TIER_MONTH, TIER_3MONTH, TIER_OLD, TIER_UNKNOWN]:
        print(f"   {t}: {len(_by_tier[t])} 个")
    print(f"   🆕一周内(且可用): {len(_recent_urls)} 个")
    print(f"   🧟僵尸源(>90天且不通): {len(_stale_urls)} 个")

    if total_ok == 0:
        print("⚠️ 可用源为 0！保留旧 live_ok.txt，不覆盖。")
        sys.exit(0)

    print(f"\n💾 写入文件（统一北京时间: {ts}）")

    # ── live_ok.txt（纯URL，按四档归类，无尾注）──
    with open("live_ok.txt", "w", encoding="utf-8") as f:
        write_header(f, "可用源总表（按更新分档归类，纯URL）")
        order = [TIER_NEW, TIER_MONTH, TIER_3MONTH, TIER_OLD]
        for tier in order:
            urls_in_tier = sorted(set(_by_tier[tier]), key=get_domain)
            ok_urls = [u for u in urls_in_tier
                       if any(normalize_url(r[0]) == normalize_url(u) and r[4] for r in _results)]
            if not ok_urls:
                continue
            f.write(f"# ---- {tier}（{len(ok_urls)}个） ----\n")
            for u in ok_urls:
                f.write(f"{u}\n")
            f.write("\n")

    # ── live_ok.m3u ──
    with open("live_ok.m3u", "w", encoding="utf-8") as f:
        write_header(f, "可用源播放列表")
        f.write("#EXTM3U\n\n")
        for url, status, elapsed, flag in _ok_raw:
            broad = get_domain(url)
            f.write(f'#EXTINF:-1,{broad}\n{url}\n')

    # ── 分档文件 ──
    def write_tier_file(fname, tier, label):
        with open(fname, "w", encoding="utf-8") as f:
            write_header(f, f"{label}（{len(_by_tier[tier])}个）")
            for u in sorted(set(_by_tier[tier]), key=get_domain):
                f.write(f"{u}\n")

    write_tier_file("live_week.txt", TIER_NEW, "一周内（Week）")
    write_tier_file("live_month.txt", TIER_MONTH, "一个月内")
    write_tier_file("live_3month.txt", TIER_3MONTH, "三个月内")
    write_tier_file("live_old.txt", TIER_OLD, "超三个月")

    # ── live_fail.txt ──
    with open("live_fail.txt", "w", encoding="utf-8") as f:
        write_header(f, f"真失效源（{len(_fail_raw)}个）")
        for url, status, elapsed, flag in _fail_raw:
            f.write(f"{url}\n")

    # ── live_stale.txt（僵尸源）──
    with open("live_stale.txt", "w", encoding="utf-8") as f:
        write_header(f, f"僵尸源 >{STALE_DAYS}天且不通（{len(_stale_urls)}个）")
        f.write("# 这些源已从 live_ok.txt 剔除，建议人工复查后可删除\n\n")
        if _stale_urls:
            for nu in sorted(_stale_urls):
                days, update_date = get_source_age._cache.get(nu, (None, "未知"))
                f.write(f"{nu}")
                if days is not None:
                    f.write(f"  # 源龄{age_label(days)}")
                if update_date and update_date != "未知":
                    f.write(f" | 更新日期:{update_date}")
                f.write("\n")
        else:
            f.write("# （无）本次无>90天且不通的源\n")

# ── live_report.csv（按更新时间从新到旧排序 | 仅此表排序）──
    with open("live_report.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        # 第1行：生成时间占第1列，后8列空（严格9列）
        w.writerow([f"生成时间: {ts}", "", "", "", "", "", "", "", ""])
        # 第2行：表头
        w.writerow(["更新时间", "状态码", "响应时间(ms)", "状态",
                    "类型", "源龄(天)", "更新分档", "备注", "网址"])

        # 先收集去重后的数据行
        rows = []
        csv_seen = set()
        for url, status, elapsed, flag, ok in _results:
            norm = normalize_url(url)
            if norm in csv_seen:
                continue
            csv_seen.add(norm)
            days, update_date = get_source_age._cache.get(norm, (None, "未知"))
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
            # 排序键：日期越新越靠前；"未知"排最后
            sort_key = update_date if update_date != "未知" else "9999-99-99"
            rows.append((sort_key, update_date, status, elapsed, state, url_type, age_str, tier, note, url))

        # 按更新时间从新到旧排序
        rows.sort(key=lambda r: r[0])

        for (_, update_date, status, elapsed, state, url_type, age_str, tier, note, url) in rows:
            w.writerow([update_date, status, elapsed, state, url_type, age_str, tier, note, url])

        # 僵尸源汇总块
        w.writerow(["", "", "", "", "", "", "", "", ""])
        w.writerow(["僵尸源清单", "", "", "", "", "", f"共{len(_stale_urls)}个", "", ""])
        if _stale_urls:
            for s in sorted(_stale_urls):
                days, update_date = get_source_age._cache.get(s, (None, "未知"))
                w.writerow([update_date, "", "", "僵尸源", "", age_label(days), tier_of(days), "僵尸源", s])
        else:
            w.writerow(["本次无>90天且不通的源", "", "", "", "", "", "", "", ""])

    # ── 总结 ──
    print(f"\n{'='*60}")
    print(f"✅ 全部完成 | {ts}")
    print(f"{'='*60}")
    print(f"   live_ok.txt     ← {total_ok} 条（纯URL，四档归类）")
    print(f"   live_ok.m3u     ← {total_ok} 条")
    print(f"   live_fail.txt   ← {len(_fail_raw)} 个真失效")
    print(f"   live_week.txt   ← {len(_by_tier[TIER_NEW])} 个 🆕一周内")
    print(f"   live_month.txt  ← {len(_by_tier[TIER_MONTH])} 个 📅一个月内")
    print(f"   live_3month.txt ← {len(_by_tier[TIER_3MONTH])} 个 📆三个月内")
    print(f"   live_old.txt    ← {len(_by_tier[TIER_OLD])} 个 🧓超三个月")
    print(f"   live_stale.txt  ← {len(_stale_urls)} 个 🧟僵尸源（>90天且不通）")
    print(f"   live_report.csv ← 9列报告（生成时间首列/更新日期/网址置末）")
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
