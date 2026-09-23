#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 检查器（四档分档 + 真实源龄 + 分组裸 URL 输出）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
分档：🆕一周内(≤7天) | 📅一个月内(≤30天) | 📆三个月内(≤90天) | 🧓超三个月(>90天)
输出：每个列表文件开头带【北京时间】生成时间
      CSV 报告：无名称列，URL 在最后一列
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

# ── 常量 ────────────────────────────────────────
TIER_NEW = "new"
TIER_MONTH = "month"
TIER_3MONTH = "3month"
TIER_OLD = "old"

TIMEOUT = 20
THREADS = 10

BJT = timezone(timedelta(hours=8))

_lock = threading.Lock()
_results = []
_by_tier = {TIER_NEW: [], TIER_MONTH: [], TIER_3MONTH: [], TIER_OLD: []}
_age_cache = {}
_fail_raw = []
_stale_urls = set()

TIER_TITLE = {
    TIER_NEW:    "🆕 一周内（≤7天）",
    TIER_MONTH:  "📅 一个月内（≤30天）",
    TIER_3MONTH: "📆 三个月内（≤90天）",
    TIER_OLD:    "🧓 超三个月（>90天）",
}
TIER_ORDER = [TIER_NEW, TIER_MONTH, TIER_3MONTH, TIER_OLD]

# ── 工具 ────────────────────────────────────────
def normalize_url(u):
    try:
        p = urlparse(u.strip())
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, p.query, "", ""))
    except Exception:
        return u.strip().lower()

def tier_of(days):
    if days is None:
        return TIER_OLD
    if days <= 7:
        return TIER_NEW
    if days <= 30:
        return TIER_MONTH
    if days <= 90:
        return TIER_3MONTH
    return TIER_OLD

def age_label(days):
    if days is None:
        return "❓未知"
    if days <= 7:
        return f"🆕{days}天(一周内)"
    if days <= 30:
        return f"📅{days}天(一月内)"
    if days <= 90:
        return f"📆{days}天(三月内)"
    return f"🧓{days}天(>三月)"

def gen_time():
    return datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")

# ── 源龄获取 ────────────────────────────────────
def fetch_age(url):
    if url in _age_cache:
        return _age_cache[url]
    days, desc = None, ""
    try:
        m = re.match(r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", url)
        if m:
            owner, repo, branch, path = m.groups()
            api = f"https://api.github.com/repos/{owner}/{repo}/commits?path={path}&sha={branch}&per_page=1"
            r = requests.get(api, timeout=10)
            if r.ok:
                data = r.json()
                if data:
                    t = data[0]["commit"]["author"]["date"]
                    dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                    days = (datetime.now(timezone.utc) - dt).days
                    desc = "github-api"
        if days is None:
            h = requests.head(url, allow_redirects=True, timeout=10)
            lm = h.headers.get("Last-Modified")
            if lm:
                try:
                    dt = parsedate_to_datetime(lm)
                    days = (datetime.now(timezone.utc) - dt).days
                    desc = "last-modified"
                except Exception:
                    pass
    except Exception:
        pass
    _age_cache[url] = (days, desc)
    return days, desc

# ── 检测核心 ────────────────────────────────────
def check_one(line):
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) >= 2 and parts[-1].startswith("http"):
        name, url = ",".join(parts[:-1]), parts[-1]
    else:
        url = parts[0]
        name = ""
    ok = False
    latency = None
    err = ""
    try:
        r = requests.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True)
        if r.status_code == 200:
            chunk = next(r.iter_content(1024), b"")
            if chunk:
                ok = True
                latency = int(r.elapsed.total_seconds() * 1000)
        else:
            err = f"HTTP{r.status_code}"
        r.close()
    except Timeout:
        err = "超时"
    except ConnectionError:
        err = "连接失败"
    except RequestException as e:
        err = str(e)[:30]

    if not ok:
        try:
            h = requests.head(url, timeout=TIMEOUT, allow_redirects=True)
            if h.status_code == 200:
                ok = True
                latency = int(h.elapsed.total_seconds() * 1000)
                err = ""
        except Exception:
            pass

    days, desc = fetch_age(url)
    tier = tier_of(days)
    rec = (normalize_url(url), name, url, ok, latency, err, days, tier, desc)
    with _lock:
        _results.append(rec)
        if ok:
            _by_tier[tier].append(url)
        else:
            _fail_raw.append(url)
            if days is not None and days > 90:
                _stale_urls.add(url)
    return rec

# ── 主流程 ───────────────────────────────────────
def main():
    global TIMEOUT, THREADS
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=THREADS)
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    args = ap.parse_args()
    TIMEOUT = args.timeout
    THREADS = args.threads

    ts = gen_time()
    print(f"🕒 开始检测（北京时间 {ts}）")
    print(f"线程: {THREADS} | 超时: {TIMEOUT}s")
    print(f"分档: ≤7天 / ≤30天 / ≤90天 / >90天")

    src = "live.txt"
    lines = []
    with open(src, encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                lines.append(ln)
    print(f"读取 {len(lines)} 行")

    seen = set()
    uniq = []
    for ln in lines:
        u = normalize_url(ln.split(",")[-1].strip() if "," in ln else ln.strip())
        if u in seen:
            continue
        seen.add(u)
        uniq.append(ln)
    print(f"去重后 {len(uniq)} 个唯一源")
    print("开始检测...")

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        futures = [ex.submit(check_one, ln) for ln in uniq]
        done = 0
        for fu in as_completed(futures):
            fu.result()
            done += 1
            if done % 10 == 0:
                print(f"  进度: {done}/{len(uniq)}")

    total_ok = sum(1 for r in _results if r[3])

    # ── live_ok.txt ──
    with open("live_ok.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间(北京时间): {ts}\n")
        f.write(f"# 可用源合计: {total_ok} 个\n\n")
        any_written = False
        for t in TIER_ORDER:
            urls = sorted(set(_by_tier[t]))
            if not urls:
                continue
            f.write(f"# ---- {TIER_TITLE[t]}（{len(urls)}个） ----\n")
            for u in urls:
                f.write(f"{u}\n")
            f.write("\n")
            any_written = True
        if not any_written:
            f.write("# 无可用源\n")

    # ── live_ok.m3u ──
    with open("live_ok.m3u", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间(北京时间): {ts}\n")
        f.write("#EXTM3U\n")
        for r in _results:
            if r[3]:
                name = r[1] if r[1] else r[2].split("/")[-1]
                f.write(f"#EXTINF:-1,{name}\n{r[2]}\n")

    # ── 分档单文件 ──
    for t, fn in [(TIER_NEW, "live_recent.txt"), (TIER_MONTH, "live_month.txt"),
                  (TIER_3MONTH, "live_3month.txt"), (TIER_OLD, "live_old.txt")]:
        with open(fn, "w", encoding="utf-8") as f:
            f.write(f"# 生成时间(北京时间): {ts}\n")
            f.write(f"# {TIER_TITLE[t]}（{len(set(_by_tier[t]))}个）\n\n")
            for u in sorted(set(_by_tier[t])):
                days, desc = _age_cache.get(u, (None, ""))
                f.write(f"{u}  # {age_label(days)}\n")

    # ── 失败 / 僵尸 ──
    with open("live_fail.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间(北京时间): {ts}\n")
        for u in sorted(set(_fail_raw)):
            f.write(u + "\n")

    with open("live_stale.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间(北京时间): {ts}\n")
        f.write(f"# 僵尸源（>90天且不通）{len(_stale_urls)}个\n\n")
        for u in sorted(_stale_urls):
            days, _ = _age_cache.get(u, (None, ""))
            f.write(f"{u}  # {age_label(days)}\n")

    # ── CSV 报告（无名称列，URL 在最后一列） ──
    with open("live_report.csv", "w", encoding="utf-8-sig", newline="") as cf:
        w = csv.writer(cf)
        w.writerow(["状态", "延迟ms", "错误", "分档", "源龄", "类别", "描述", "是否僵尸", "时间", "URL"])
        for r in _results:
            url_n, name, url, ok, lat, err, days, tier, desc = r
            is_stale = "是" if (not ok and days and days > 90) else "否"
            w.writerow([
                "✅" if ok else "❌", lat or "", err,
                TIER_TITLE[tier], age_label(days), tier, desc, is_stale, ts,
                url,
            ])
        if _stale_urls:
            for s in sorted(_stale_urls):
                days, desc = _age_cache.get(s, (None, ""))
                w.writerow(["", "", "", "僵尸源", "", age_label(days), tier_of(days), desc, ts, s])
        else:
            w.writerow(["", "", "", "本次无>90天且不通的源", "", "", "", "", ts, "（无）"])

    # ── 总结 ──
    print(f"\n{'='*60}")
    print(f"✅ 全部完成 | 北京时间 {ts}")
    print(f"{'='*60}")
    print(f"   live_ok.txt     ← {total_ok} 条（四档分组，裸 URL）")
    print(f"   live_ok.m3u     ← {total_ok} 条")
    print(f"   live_fail.txt   ← {len(set(_fail_raw))} 个失效")
    print(f"   live_recent.txt ← {len(set(_by_tier[TIER_NEW]))} 个 🆕一周内")
    print(f"   live_month.txt  ← {len(set(_by_tier[TIER_MONTH]))} 个 📅一月内")
    print(f"   live_3month.txt ← {len(set(_by_tier[TIER_3MONTH]))} 个 📆三月内")
    print(f"   live_old.txt    ← {len(set(_by_tier[TIER_OLD]))} 个 🧓超三月")
    print(f"   live_stale.txt  ← {len(_stale_urls)} 个 🧟僵尸源")
    print(f"   live_report.csv ← 报告（URL 在末列）")
    print(f"{'='*60}")

    for fn, minn in [("live_ok.txt", 1), ("live_fail.txt", 0), ("live_report.csv", 1)]:
        if os.path.exists(fn):
            n = sum(1 for _ in open(fn, encoding="utf-8"))
            if n < minn:
                print(f"⚠️ {fn} 行数{n}<{minn}，疑似异常！")
    sys.exit(0)

if __name__ == "__main__":
    main()
