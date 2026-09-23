#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 检查器（四档分档 + 真实源龄 + 原OK判定不变）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
分档：🆕一周内(≤7天) | 📅一个月内(≤30天) | 📆三个月内(≤90天) | 🧓超三个月(>90天)
源龄：远程URL用Last-Modified/GitHub API；取不到标未知
文件：live_ok.txt(总表无标注) live_ok.m3u live_fail.txt live_report.csv
      live_recent.txt live_month.txt live_3month.txt live_old.txt live_stale.txt
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
TIER_NEW = "new"       # ≤7天
TIER_MONTH = "month"   # ≤30天
TIER_3MONTH = "3month" # ≤90天
TIER_OLD = "old"       # >90天

TIMEOUT = 20
THREADS = 10

_lock = threading.Lock()
_results = []
_by_tier = {TIER_NEW: [], TIER_MONTH: [], TIER_3MONTH: [], TIER_OLD: []}
_age_cache = {}
_fail_raw = []
_stale_urls = set()

# ── 工具 ────────────────────────────────────────
def normalize_url(u):
    try:
        p = urlparse(u.strip())
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, p.query, "", ""))
    except Exception:
        return u.strip().lower()

def tier_of(days):
    if days is None: return TIER_OLD
    if days <= 7: return TIER_NEW
    if days <= 30: return TIER_MONTH
    if days <= 90: return TIER_3MONTH
    return TIER_OLD

def age_label(days):
    if days is None: return "❓未知"
    if days <= 7: return f"🆕{days}天(一周内)"
    if days <= 30: return f"📅{days}天(一月内)"
    if days <= 90: return f"📆{days}天(三月内)"
    return f"🧓{days}天(>三月)"

# ── 源龄获取（Last-Modified / GitHub API） ───────
def fetch_age(url):
    if url in _age_cache: return _age_cache[url]
    days = None
    desc = ""
    try:
        # GitHub 原始文件走 API
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
        # 普通 URL 用 HEAD Last-Modified
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

# ── 检测核心（原OK判定不变） ─────────────────────
def check_one(line):
    raw = line.strip()
    if not raw or raw.startswith("#"): return None
    # 支持 "名称,url" 或 "url"
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) >= 2 and parts[-1].startswith("http"):
        name, url = ",".join(parts[:-1]), parts[-1]
    else:
        url = parts[0]
        name = url.split("/")[-1] or "live"
    ok = False
    latency = None
    err = ""
    try:
        # 1) GET 快速判定（原逻辑）
        r = requests.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True)
        if r.status_code == 200:
            # 读一点内容确认
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

    # 2) 失败再试 HEAD（降级）
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
    THREADS = args.threads
    TIMEOUT = args.timeout

    print(f"🕒 开始检测（北京时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
    print(f"线程: {THREADS} | 超时: {TIMEOUT}s")
    print(f"分档: ≤7天 / ≤30天 / ≤90天 / >90天")

    # 读取
    src = "live.txt"
    lines = []
    with open(src, encoding="utf-8") as f:
        for ln in f:
            if ln.strip(): lines.append(ln)
    print(f"读取 {len(lines)} 行")

    # 去重
    seen = set()
    uniq = []
    for ln in lines:
        u = normalize_url(ln.split(",")[-1].strip() if "," in ln else ln.strip())
        if u in seen: continue
        seen.add(u); uniq.append(ln)
    print(f"去重后 {len(uniq)} 个唯一源")
    print("开始检测...")

    # 并发检测
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        futures = [ex.submit(check_one, ln) for ln in uniq]
        for fu in as_completed(futures):
            fu.result()

    total_ok = sum(1 for r in _results if r[3])
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── live_ok.txt（总表：仅按四档归类，不标时间） ──
    # 写入顺序：一周内 → 一月内 → 三月内 → 大于三月
    order = [TIER_NEW, TIER_MONTH, TIER_3MONTH, TIER_OLD]
    tier_names = {TIER_NEW:"一周内", TIER_MONTH:"一月内", TIER_3MONTH:"三月内", TIER_OLD:"大于三月"}
    with open("live_ok.txt", "w", encoding="utf-8") as f:
        for t in order:
            urls = sorted(set(_by_tier[t]))
            if not urls: continue
            f.write(f"# === {tier_names[t]}（{len(urls)}个） ===\n")
            for u in urls:
                # 找最新名称（按检测结果）
                name = u.split("/")[-1]
                for r in _results:
                    if normalize_url(r[2]) == normalize_url(u) and r[3]:
                        name = r[1]; break
                f.write(f"{name},{u}\n")
            f.write("\n")
        if total_ok == 0:
            f.write("# 无可用源\n")

    # ── live_ok.m3u ──
    with open("live_ok.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for r in _results:
            if r[3]:
                f.write(f"#EXTINF:-1,{r[1]}\n{r[2]}\n")

    # ── 分档单文件（保留标注，方便查看） ──
    for t, fn in [(TIER_NEW,"live_recent.txt"),(TIER_MONTH,"live_month.txt"),
                  (TIER_3MONTH,"live_3month.txt"),(TIER_OLD,"live_old.txt")]:
        with open(fn, "w", encoding="utf-8") as f:
            for u in sorted(set(_by_tier[t])):
                days, desc = _age_cache.get(u, (None, ""))
                f.write(f"{u}  # {age_label(days)}\n")

    # ── 失败 / 僵尸 ──
    with open("live_fail.txt", "w", encoding="utf-8") as f:
        for u in sorted(set(_fail_raw)):
            f.write(u + "\n")
    with open("live_stale.txt", "w", encoding="utf-8") as f:
        for u in sorted(_stale_urls):
            days, _ = _age_cache.get(u, (None, ""))
            f.write(f"{u}  # {age_label(days)}\n")

    # ── CSV 报告（11列） ──
    with open("live_report.csv", "w", encoding="utf-8", newline="") as cf:
        w = csv.writer(cf)
        w.writerow(["名称","URL","状态","延迟ms","错误","分档","源龄","类别","描述","是否僵尸","时间"])
        for r in _results:
            url_n, name, url, ok, lat, err, days, tier, desc = r
            is_stale = "是" if (not ok and days and days>90) else "否"
            w.writerow([name, url, "✅" if ok else "❌", lat or "", err,
                        tier_names[tier], age_label(days), tier, desc, is_stale, ts])
        # 僵尸源汇总行
        if _stale_urls:
            for s in sorted(_stale_urls):
                days, desc = _age_cache.get(s, (None, ""))
                w.writerow(["", "", s, "", "", "僵尸源", "", age_label(days), tier_of(days), desc])
        else:
            w.writerow(["", "", "（无）", "", "", "本次无>90天且不通的源", "", "", "", ""])

    # ── 总结 ──
    print(f"\n{'='*60}")
    print(f"✅ 全部完成 | {ts}")
    print(f"{'='*60}")
    print(f"   live_ok.txt     ← {total_ok} 条（四档分类，无时间标注）")
    print(f"   live_ok.m3u     ← {total_ok} 条")
    print(f"   live_fail.txt   ← {len(_fail_raw)} 个真失效")
    rc = len([u for u in _by_tier[TIER_NEW] if any(normalize_url(r[0])==normalize_url(u) and r[3] for r in _results)])
    print(f"   live_recent.txt ← {rc} 个 🆕一周内(可用)")
    print(f"   live_month.txt  ← {len(_by_tier[TIER_MONTH])} 个 📅一月内")
    print(f"   live_3month.txt ← {len(_by_tier[TIER_3MONTH])} 个 📆三月内")
    print(f"   live_old.txt    ← {len(_by_tier[TIER_OLD])} 个 🧓大于三月")
    print(f"   live_stale.txt  ← {len(_stale_urls)} 个 🧟僵尸源（>90天且不通）")
    print(f"   live_report.csv ← 11列报告")
    print(f"{'='*60}")

    for fn, minn in [("live_ok.txt",1),("live_fail.txt",0),("live_report.csv",1)]:
        if os.path.exists(fn):
            n = sum(1 for _ in open(fn, encoding="utf-8"))
            if n < minn: print(f"⚠️ {fn} 行数{n}<{minn}，疑似清空！")
    sys.exit(0)

if __name__ == "__main__":
    main()
