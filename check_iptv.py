#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源自动检测脚本
源列表：sources.txt（每行一个 URL）
源龄获取：GitHub/Gitee API → JsDelivr → HEAD Last-Modified → URL日期 → 本地git log → 未知
          HEAD Date 仅标记为"今日活跃"，不计入更新分档
缓存：source_age_cache.json 持久化
"""
import os
import sys
import csv
import json
import time
import re
import socket
import subprocess
import threading
import argparse
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ═══════════════════════════════════════════════
# 全局配置
# ═══════════════════════════════════════════════
SOURCE_FILE = "sources.txt"   # 源列表文件
TIMEOUT = 20
THREADS = 10
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
HEADERS = {"User-Agent": USER_AGENT}

RECENT_DAYS = 7
MONTH_DAYS = 30
THREE_MONTH_DAYS = 90
STALE_DAYS = 90

TIER_NEW = "🆕一周内"
TIER_MONTH = "📅一个月内"
TIER_3MONTH = "📆三个月内"
TIER_OLD = "🧓超三个月"
TIER_UNKNOWN = "❓未知"

CST8 = datetime.timezone(datetime.timedelta(hours=8))

# ═══════════════════════════════════════════════
# 持久化缓存
# ═══════════════════════════════════════════════
CACHE_FILE = "source_age_cache.json"
if os.path.exists(CACHE_FILE):
    try:
        _age_cache = json.load(open(CACHE_FILE, encoding="utf-8"))
    except Exception:
        _age_cache = {}
else:
    _age_cache = {}

def save_cache():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_age_cache, f, ensure_ascii=False)
    except Exception:
        pass

# ═══════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════
def now_beijing():
    return datetime.datetime.now(CST8).strftime("%Y-%m-%d %H:%M:%S")

def ts_cst():
    return datetime.datetime.now(CST8).strftime("%Y-%m-%d %H:%M:%S")

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
    if "github" in low or "gitee" in low or "gitlab" in low:
        return False
    if is_direct_stream(url):
        return False
    return any(k in low for k in ["/tv/", "/live/", "/m3u/", "/playlist", ".html",
                                   "/index", "/list/", "/channels/", "/api/channel", "/epg"])

def is_github_url(url):
    low = url.lower()
    return any(k in low for k in ["raw.githubusercontent.com", "githubusercontent.com", "github.com", "gitee.com", "gitlab.com"])

def tier_of(days):
    if days is None:
        return TIER_UNKNOWN
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
# 源龄获取（Date头降级为活跃标记）
# 缓存格式：{"url": {"days": int|None, "date": str, "source": str, "active_today": bool}}
# ═══════════════════════════════════════════════
_github_api_cache = {}

def extract_repo_path(url):
    norm = normalize_url(url)
    m = re.match(r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", norm)
    if m:
        return "github", m.groups()
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", norm)
    if m:
        return "github", m.groups()
    m = re.match(r"https?://gitee\.com/([^/]+)/([^/]+)/raw/([^/]+)/(.+)", norm)
    if m:
        return "gitee", m.groups()
    m = re.match(r"https?://gitee\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", norm)
    if m:
        return "gitee", m.groups()
    m = re.match(r"https?://cdn\.jsdelivr\.net/gh/([^/]+)/([^/]+)@([^/]+)/(.+)", norm)
    if m:
        return "jsdelivr", m.groups()
    return None

def get_git_commit_date(platform, owner, repo, branch, path):
    key = f"{platform}:{owner}/{repo}:{path}"
    if key in _github_api_cache:
        return _github_api_cache[key]
    try:
        if platform == "github":
            api = f"https://api.github.com/repos/{owner}/{repo}/commits?path={path}&per_page=1"
            r = requests.get(api, headers=HEADERS, timeout=8)
        elif platform == "gitee":
            api = f"https://gitee.com/api/v5/repos/{owner}/{repo}/commits?path={path}&per_page=1"
            r = requests.get(api, headers=HEADERS, timeout=8)
        else:
            return None
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                dt_str = data[0]["commit"]["committer"]["date"]
                d = dt_str[:10]
                _github_api_cache[key] = d
                return d
    except Exception:
        pass
    return None

def fetch_source_age(url):
    norm = normalize_url(url)

    if norm in _age_cache:
        cached = _age_cache[norm]
        if isinstance(cached, dict):
            return cached.get("days"), cached.get("date", "未知")
        if isinstance(cached, list) and len(cached) == 2:
            return cached[0], cached[1]
        return None, "未知"

    today = datetime.date.today()
    days = None
    update_date = "未知"
    source_tag = ""

    try:
        info = extract_repo_path(url)
        if info:
            plat, (owner, repo, branch, path) = info
            if plat in ("github", "gitee"):
                d = get_git_commit_date(plat, owner, repo, branch, path)
                if d:
                    days = (today - datetime.date.fromisoformat(d)).days
                    update_date = d
                    source_tag = plat
                    _age_cache[norm] = {"days": days, "date": update_date, "source": source_tag, "active_today": False}
                    return days, update_date
            elif plat == "jsdelivr":
                try:
                    owner, repo, ver, path = owner, repo, branch, path
                    api = f"https://data.jsdelivr.com/v1/lookup?name={owner}/{repo}&version={ver}"
                    r = requests.get(api, timeout=6)
                    if r.status_code == 200:
                        d = r.json().get("published_at", "")[:10]
                        if d:
                            days = (today - datetime.date.fromisoformat(d)).days
                            update_date = d
                            source_tag = "jsdelivr"
                            _age_cache[norm] = {"days": days, "date": update_date, "source": source_tag, "active_today": False}
                            return days, update_date
                except Exception:
                    pass

        # HEAD Last-Modified
        try:
            sess = requests.Session()
            retries = Retry(total=1, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
            sess.mount("http://", HTTPAdapter(max_retries=retries))
            sess.mount("https://", HTTPAdapter(max_retries=retries))
            head = sess.head(norm, headers=HEADERS, timeout=4, allow_redirects=True, verify=False)
            lm = head.headers.get("Last-Modified")
            if lm:
                dt = parsedate_to_datetime(lm)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                d = dt.astimezone(CST8).date()
                days = (today - d).days
                update_date = d.isoformat()
                source_tag = "LM"
                _age_cache[norm] = {"days": days, "date": update_date, "source": source_tag, "active_today": False}
                return days, update_date
        except Exception:
            pass

        # URL 日期特征
        m1 = re.search(r"(\d{4})-(\d{2})-(\d{2})", norm)
        if m1:
            try:
                d = datetime.date.fromisoformat(m1.group(1) + "-" + m1.group(2) + "-" + m1.group(3))
                if 2000 <= d.year <= today.year + 1:
                    days = (today - d).days
                    update_date = d.isoformat()
                    source_tag = "URL"
                    _age_cache[norm] = {"days": days, "date": update_date, "source": source_tag, "active_today": False}
                    return days, update_date
            except Exception:
                pass
        m2 = re.search(r"[/_-](\d{8})[/_.]", norm)
        if m2:
            try:
                s = m2.group(1)
                d = datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
                if 2000 <= d.year <= today.year + 1:
                    days = (today - d).days
                    update_date = d.isoformat()
                    source_tag = "URL"
                    _age_cache[norm] = {"days": days, "date": update_date, "source": source_tag, "active_today": False}
                    return days, update_date
            except Exception:
                pass

        # 本地 git log
        try:
            local_path = norm.replace("file://", "")
            if os.path.exists(local_path):
                out = subprocess.check_output(
                    ["git", "log", "-1", "--format=%cd", "--date=short", local_path],
                    stderr=subprocess.DEVNULL).decode().strip()
                if out:
                    days = (today - datetime.date.fromisoformat(out)).days
                    update_date = out
                    source_tag = "git"
                    _age_cache[norm] = {"days": days, "date": update_date, "source": source_tag, "active_today": False}
                    return days, update_date
        except Exception:
            pass

        # HEAD Date → 仅活跃，不计入分档
        try:
            sess = requests.Session()
            retries = Retry(total=1, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
            sess.mount("http://", HTTPAdapter(max_retries=retries))
            sess.mount("https://", HTTPAdapter(max_retries=retries))
            head = sess.head(norm, headers=HEADERS, timeout=4, allow_redirects=True, verify=False)
            dm = head.headers.get("Date")
            if dm:
                dt = parsedate_to_datetime(dm)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                d = dt.astimezone(CST8).date()
                update_date = d.isoformat() + "(活跃)"
                source_tag = "Date"
                _age_cache[norm] = {"days": None, "date": update_date, "source": source_tag, "active_today": True}
                return None, update_date
        except Exception:
            pass

    except Exception:
        pass

    _age_cache[norm] = {"days": None, "date": "未知", "source": "none", "active_today": False}
    return None, "未知"

# ═══════════════════════════════════════════════
# 检测核心
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
            sess = requests.Session()
            retries = Retry(total=1, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
            sess.mount("http://", HTTPAdapter(max_retries=retries))
            sess.mount("https://", HTTPAdapter(max_retries=retries))
            r = sess.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, verify=False)
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
                raise requests.exceptions.RequestException("try_get")
            else:
                flag = "fail"
        except requests.exceptions.RequestException:
            pass

        if flag in ("", "fail", None):
            start = time.time()
            sess = requests.Session()
            retries = Retry(total=1, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
            sess.mount("http://", HTTPAdapter(max_retries=retries))
            sess.mount("https://", HTTPAdapter(max_retries=retries))
            r = sess.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, verify=False, stream=True)
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
    except requests.exceptions.Timeout:
        elapsed = int((time.time() - start) * 1000)
        flag = "timeout"
    except requests.exceptions.ConnectionError:
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

    if not os.path.exists(SOURCE_FILE):
        print(f"❌ {SOURCE_FILE} 不存在")
        sys.exit(1)

    raw_lines = []
    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
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

    print("\n🕵️ 开始源龄检测（Date头降级为活跃标记）...")
    age_completed = 0
    unknown_count = 0
    date_only_count = 0
    for url in _uniq_urls:
        norm = normalize_url(url)
        days, update_date = fetch_source_age(url)
        if days is None:
            unknown_count += 1
        if norm in _age_cache:
            cached = _age_cache[norm]
            if isinstance(cached, dict) and cached.get("active_today"):
                date_only_count += 1
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
        if age_completed % 50 == 0:
            print(f"   源龄进度: {age_completed}/{len(_uniq_urls)} (未知:{unknown_count} 仅活跃:{date_only_count})")

    print(f"\n📊 分档结果:")
    for t in [TIER_NEW, TIER_MONTH, TIER_3MONTH, TIER_OLD, TIER_UNKNOWN]:
        print(f"   {t}: {len(_by_tier[t])} 个")
    print(f"   🆕一周内(且可用): {len(_recent_urls)} 个")
    print(f"   🧟僵尸源(>90天且不通): {len(_stale_urls)} 个")
    print(f"   ❓源龄未知(含仅活跃): {unknown_count} 个 (其中仅Date活跃:{date_only_count}个)")

    if total_ok == 0:
        print("⚠️ 可用源为 0！保留旧文件，不覆盖。")
        save_cache()
        sys.exit(0)

    print(f"\n💾 写入文件（统一北京时间: {ts}）")

    # live_ok.txt
    with open("live_ok.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间(北京时间): {ts_cst()}\n")
        f.write(f"# 可用总数: {total_ok}\n\n")
        order = [
            (TIER_NEW, "🆕一周内"),
            (TIER_MONTH, "📅一个月内"),
            (TIER_3MONTH, "📆三个月内"),
            (TIER_OLD, "🧓超三个月"),
            (TIER_UNKNOWN, "❓未知(含仅活跃)"),
        ]
        for tier_key, tier_label in order:
            ok_urls = sorted(
                set(_by_tier[tier_key]),
                key=lambda u: u.split("://", 1)[-1].lower()
            )
            if not ok_urls:
                f.write(f"# ---- {tier_label}（0个） ----\n\n")
                continue
            f.write(f"# ---- {tier_label}（{len(ok_urls)}个） ----\n")
            for u in ok_urls:
                f.write(f"{u}\n")
            f.write("\n")

    # live_ok.m3u
    with open("live_ok.m3u", "w", encoding="utf-8") as f:
        write_header(f, "可用源播放列表")
        f.write("#EXTM3U\n\n")
        m3u_items = []
        seen_m3u = set()
        for url, status, elapsed, flag in _ok_raw:
            norm = normalize_url(url)
            if norm in seen_m3u:
                continue
            seen_m3u.add(norm)
            try:
                rt = float(elapsed)
            except Exception:
                rt = 99999999.0
            m3u_items.append((rt, url))
        m3u_items.sort(key=lambda x: x[0])
        for rt, url in m3u_items:
            if rt >= 99999999:
                name = "未知"
            else:
                name = f"{int(rt)}ms"
            f.write(f"#EXTINF:-1,{name}\n{url}\n")

    # 分档文件
    def write_tier_file(fname, tier, label):
        with open(fname, "w", encoding="utf-8") as f:
            write_header(f, f"{label}（{len(_by_tier[tier])}个）")
            for u in sorted(set(_by_tier[tier]), key=lambda u: u.split("://", 1)[-1].lower()):
                f.write(f"{u}\n")

    write_tier_file("live_week.txt", TIER_NEW, "一周内（Week）")
    write_tier_file("live_month.txt", TIER_MONTH, "一个月内")
    write_tier_file("live_3month.txt", TIER_3MONTH, "三个月内")
    write_tier_file("live_old.txt", TIER_OLD, "超三个月")

    # live_fail.txt
    with open("live_fail.txt", "w", encoding="utf-8") as f:
        write_header(f, f"真失效源（{len(_fail_raw)}个）")
        for url, status, elapsed, flag in _fail_raw:
            f.write(f"{url}\n")

    # live_stale.txt
    with open("live_stale.txt", "w", encoding="utf-8") as f:
        write_header(f, f"僵尸源 >{STALE_DAYS}天且不通（{len(_stale_urls)}个）")
        f.write("# 这些源建议人工复查后可删除\n\n")
        if _stale_urls:
            for nu in sorted(_stale_urls):
                cached = _age_cache.get(nu, {"days": None, "date": "未知"})
                if isinstance(cached, dict):
                    days = cached.get("days")
                    update_date = cached.get("date", "未知")
                else:
                    days, update_date = None, "未知"
                f.write(f"{nu}")
                if days is not None:
                    f.write(f"  # 源龄{age_label(days)}")
                if update_date and update_date != "未知":
                    f.write(f" | 更新日期:{update_date}")
                f.write("\n")
        else:
            f.write("# （无）本次无>90天且不通的源\n")

    # live_report.csv
    with open("live_report.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([f"生成时间: {ts}", "", "", "", "", "", "", "", ""])
        w.writerow(["更新时间", "状态码", "响应时间(ms)", "状态", "类型", "源龄(天)", "更新分档", "备注", "网址"])
        rows = []
        csv_seen = set()
        for url, status, elapsed, flag, ok in _results:
            norm = normalize_url(url)
            if norm in csv_seen:
                continue
            csv_seen.add(norm)
            cached = _age_cache.get(norm, {"days": None, "date": "未知", "source": "none", "active_today": False})
            if isinstance(cached, dict):
                days = cached.get("days")
                update_date = cached.get("date", "未知")
                active_today = cached.get("active_today", False)
            else:
                days, update_date, active_today = None, "未知", False
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
                if active_today:
                    note_parts.append("仅今日活跃(Date头)")
                else:
                    note_parts.append("源龄未知")
            note = " | ".join(note_parts)
            if update_date != "未知":
                try:
                    clean = re.sub(r'[^0-9]', '', update_date)[:8]
                    sort_key = -int(clean) if clean else 99999999
                except Exception:
                    sort_key = 99999999
            else:
                sort_key = 99999999
            rows.append((sort_key, update_date, status, elapsed, state, url_type, age_str, tier, note, url))
        rows.sort(key=lambda r: r[0])
        for (_, update_date, status, elapsed, state, url_type, age_str, tier, note, url) in rows:
            w.writerow([update_date, status, elapsed, state, url_type, age_str, tier, note, url])
        w.writerow(["", "", "", "", "", "", "", "", ""])
        w.writerow(["僵尸源清单", "", "", "", "", "", f"共{len(_stale_urls)}个", "", ""])
        if _stale_urls:
            for s in sorted(_stale_urls):
                cached = _age_cache.get(s, {"days": None, "date": "未知"})
                if isinstance(cached, dict):
                    days = cached.get("days")
                    update_date = cached.get("date", "未知")
                else:
                    days, update_date = None, "未知"
                w.writerow([update_date, "", "", "僵尸源", "", age_label(days), tier_of(days), "僵尸源", s])
        else:
            w.writerow(["本次无>90天且不通的源", "", "", "", "", "", "", "", ""])

    save_cache()

    print(f"\n{'='*60}")
    print(f"✅ 全部完成 | {ts}")
    print(f"{'='*60}")
    print(f"   live_ok.txt     ← {total_ok} 条（四档+字母序）")
    print(f"   live_ok.m3u     ← {total_ok} 条（响应时间排序）")
    print(f"   live_fail.txt   ← {len(_fail_raw)} 个真失效")
    print(f"   live_week.txt   ← {len(_by_tier[TIER_NEW])} 个 🆕一周内")
    print(f"   live_month.txt  ← {len(_by_tier[TIER_MONTH])} 个 📅一个月内")
    print(f"   live_3month.txt ← {len(_by_tier[TIER_3MONTH])} 个 📆三个月内")
    print(f"   live_old.txt    ← {len(_by_tier[TIER_OLD])} 个 🧓超三个月")
    print(f"   live_stale.txt  ← {len(_stale_urls)} 个 🧟僵尸源")
    print(f"   live_report.csv ← 9列报告")
    print(f"   源龄缓存        ← {len(_age_cache)} 条 (未知:{unknown_count} 仅活跃:{date_only_count})")
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
