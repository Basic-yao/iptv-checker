#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 检查器（四档分档 + 真实源龄 + 原OK判定不变）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
分档：🆕一周内(≤7天) | 📅一个月内(≤30天) | 📆三个月内(≤90天) | 🧓超三个月(>90天)
源龄：远程URL用Last-Modified/GitHub API；取不到标未知
文件：live_ok.txt live_ok.m3u live_fail.txt live_report.csv
      live_recent.txt live_month.txt live_3month.txt live_old.txt live_stale.txt
"""
import os, re, sys, time, csv, argparse, socket, threading, json
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urlunparse
try:
    import requests
except ImportError:
    sys.exit("❌ 缺少 requests，请 pip install requests")

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

# 固定6大类
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

# 线程安全
_lock = threading.Lock()
_results = []          # 全部检测结果
_ok_raw = []           # 可用的
_fail_raw = []         # 不通的
_seen_u = set()        # 去重
_uniq_urls = []        # 去重后的URL列表

# 源龄缓存  norm_u -> (days, desc)
_age_cache = {}

# 分档集合
_recent_urls = set()    # ≤7天 且可用
_stale_urls = set()     # >90天 且不通
_old_but_alive = {}     # >90天 但可用
_by_tier = {TIER_NEW: [], TIER_MONTH: [], TIER_3MONTH: [], TIER_OLD: [], TIER_UNKNOWN: []}

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
    except:
        return u.strip().lower()

def get_domain(url):
    try:
        return urlparse(url).netloc.lower()
    except:
        return url

def classify(url, title="未分类"):
    low = normalize_url(url).lower()
    if any(k in low for k in ["4k", "8k"]):
        return "4K高清"
    if "github" in low or "githubusercontent" in low:
        return "GitHub源"
    if "iptv-org" in low:
        return "国际源"
    if "tvbox" in low or "box" in low:
        return "TVBox"
    if title in TITLE_MAP:
        return TITLE_MAP[title]
    if "migu" in low or "miguvideo" in low:
        return "国内源"
    return "其他"

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
# 源龄检测（核心修复：远程用Last-Modified/GitHub API）
# ═══════════════════════════════════════════════
def get_source_age(url):
    """
    返回 (days, desc)
    - GitHub源: 用 commits API 取最后提交时间
    - 普通远程: 用 Last-Modified 头
    - 取不到: (None, "源龄未知")
    """
    norm = normalize_url(url)
    if norm in _age_cache:
        return _age_cache[norm]

    now = now_cst()
    days = None
    desc = ""

    try:
        if is_github_url(url):
            # 解析 raw.githubusercontent.com/owner/repo/branch/path
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
                        desc = f"GitHub最后提交:{dt_str[:10]}"
                        _age_cache[norm] = (days, desc)
                        return days, desc
                except Exception:
                    pass

        # 普通远程：Last-Modified
        try:
            r = requests.head(norm, headers=HEADERS, timeout=10, allow_redirects=True, verify=False)
            lm = r.headers.get("Last-Modified")
            if lm:
                dt = parsedate_to_datetime(lm)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = (now - dt.astimezone(CST8)).days
                desc = f"Last-Modified:{lm[:16]}"
                _age_cache[norm] = (days, desc)
                return days, desc
        except Exception:
            pass

    except Exception:
        pass

    # 全部失败 → 未知
    desc = "源龄未知"
    _age_cache[norm] = (None, desc)
    return None, desc

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
# 检测核心（三层判定，原逻辑保留）
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
        # HEAD 优先
        try:
            r = requests.head(url, headers=HEADERS, timeout=TIMEOUT,
                              allow_redirects=True, verify=False)
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

        # HEAD 失败或不确定的，降级 GET
        if flag in ("", "fail", None):
            start = time.time()
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT,
                             allow_redirects=True, verify=False, stream=True)
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
    except Exception as e:
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

    # 读取
    raw_lines = []
    with open("live.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                raw_lines.append(line)

    print(f"📥 读取 {len(raw_lines)} 行")

    # 去重
    dedup_map = {}
    for line in raw_lines:
        u = normalize_url(line)
        if u and u.startswith("http"):
            dedup_map[u] = line

    print(f"🔗 去重后 {len(dedup_map)} 个唯一源")

    # ── 并发检测 ──
    print(f"🚀 开始检测...")
    completed = 0
    total = len(dedup_map)

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        futures = {ex.submit(check_url, url): url for url in dedup_map.values()}
        for fu in as_completed(futures):
            completed += 1
            url = futures[fu]
            # 找结果
            norm = normalize_url(url)
            ok = any(normalize_url(r[0]) == norm and r[4] for r in _results)
            icon = "✅" if ok else "❌"
            short = url if len(url) <= 50 else url[:47] + "..."
            if completed % 50 == 0 or completed == total:
                print(f"  [{completed}/{total}] {icon} {short}")

    total_ok = len(_ok_raw)
    print(f"\n📊 检测完成: ✅{total_ok} 可用 | ❌{len(_fail_raw)} 不通")

    # ── 源龄检测 + 分档 ──
    print(f"\n🕵️ 开始源龄检测（远程用Last-Modified/GitHub API）...")
    age_completed = 0
    for url in _uniq_urls:
        norm = normalize_url(url)
        days, desc = get_source_age(url)
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

    # ── 防清空 ──
    if total_ok == 0:
        print("⚠️ 可用源为 0！保留旧 live_ok.txt，不覆盖。")
        sys.exit(0)

    # ── 写文件 ──
    print(f"\n💾 写入文件（统一北京时间: {ts}）")

    # live_ok.txt（原OK数据，带分档尾注）
    with open("live_ok.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts}\n")
        f.write(f"# 可用: {total_ok} 条（按分档→大类→域名排序）\n\n")
        # 排序：分档优先(新→旧)，再大类，再域名
        tier_order = [TIER_NEW, TIER_MONTH, TIER_3MONTH, TIER_OLD, TIER_UNKNOWN]
        sorted_ok = sorted(_ok_raw, key=lambda x: (
            tier_order.index(tier_of(_age_cache.get(normalize_url(x[0]), (None, ""))[0])),
            classify(x[0]),
            get_domain(x[0])
        ))
        for url, status, elapsed, flag in sorted_ok:
            norm = normalize_url(url)
            days, desc = _age_cache.get(norm, (None, ""))
            tier = tier_of(days)
            note = f"  # {tier} | 源龄{age_label(days)}"
            if desc and desc != "源龄未知":
                note += f" | {desc}"
            f.write(f"{url}{note}\n")

    # live_ok.m3u
    with open("live_ok.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f"# 生成时间: {ts}\n\n")
        for url, status, elapsed, flag in sorted_ok:
            norm = normalize_url(url)
            days, desc = _age_cache.get(norm, (None, ""))
            tier = tier_of(days)
            tag_map = {TIER_NEW: "[NEW]", TIER_MONTH: "[1M]", TIER_3MONTH: "[3M]", TIER_OLD: "[OLD]"}
            tag = tag_map.get(tier, "[?]")
            broad = classify(url)
            f.write(f'#EXTINF:-1 group-title="{broad}{tag}", {broad} {tier}\n')
            f.write(f"{url}\n")

    # live_fail.txt
    with open("live_fail.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts}\n# 真失效: {len(_fail_raw)} 个\n\n")
        for url, status, elapsed, flag in _fail_raw:
            f.write(f"{url}  # {status} {flag}\n")

    # 四档文件
    def write_tier_file(fname, tier, extra=""):
        with open(fname, "w", encoding="utf-8") as f:
            f.write(f"# 生成时间: {ts}\n")
            f.write(f"# {tier}: {len(_by_tier[tier])} 个{extra}\n\n")
            for u in sorted(_by_tier[tier], key=get_domain):
                norm = normalize_url(u)
                days, desc = _age_cache.get(norm, (None, ""))
                is_ok = any(normalize_url(r[0]) == norm and r[4] for r in _results)
                state = "✅可用" if is_ok else "❌不通"
                note = f"源龄{age_label(days)}"
                if desc and desc != "源龄未知":
                    note += f" | {desc}"
                f.write(f"{u}  # {tier} | {note} | {state}\n")

    write_tier_file("live_recent.txt", TIER_NEW, "（仅含可用的）")
    write_tier_file("live_month.txt", TIER_MONTH)
    write_tier_file("live_3month.txt", TIER_3MONTH)
    write_tier_file("live_old.txt", TIER_OLD, "（超三个月全部源）")

    # 僵尸源（原语义：>90天 且 不通）
    with open("live_stale.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts}\n")
        f.write(f"# 僵尸源（>90天 且 本次不通）: {len(_stale_urls)} 个\n")
        f.write(f"# 这些源已从 live_ok.txt 剔除，建议人工复查后可删除\n\n")
        if _stale_urls:
            for nu in sorted(_stale_urls):
                days, desc = _age_cache.get(nu, (None, ""))
                f.write(f"{nu}  # 僵尸源 | 源龄{age_label(days)}")
                if desc and desc != "源龄未知":
                    f.write(f" | {desc}")
                f.write("\n")
        else:
            f.write("# （无）本次无>90天且不通的源\n")

    # live_report.csv（11列）
    with open("live_report.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["新增源", "大类", "URL", "状态码", "响应时间(ms)",
                    "状态", "类型", "源龄(天)", "更新分档", "备注"])
        csv_seen = set()
        for url, status, elapsed, flag, ok in _results:
            norm = normalize_url(url)
            if norm in csv_seen:
                continue
            csv_seen.add(norm)
            broad = classify(url)
            days, desc = _age_cache.get(norm, (None, ""))
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
            w.writerow(["★" if tier == TIER_NEW and ok else "", broad, url, status, elapsed,
                        state, url_type, age_str, tier, note])

        # 僵尸源区块
        w.writerow([])
        w.writerow(["", "", "", "", "", "", "", "", "", ""])
        w.writerow(["僵尸源清单", f"共{len(_stale_urls)}个", "", "", "", "", "", "", "", ""])
        if _stale_urls:
            for s in sorted(_stale_urls):
                days, desc = _age_cache.get(s, (None, ""))
                w.writerow(["", "", s, "", "", "僵尸源", "", age_label(days), tier_of(days), desc])
        else:
            w.writerow(["", "", "（无）", "", "", "本次无>90天且不通的源", "", "", "", ""])

    # ── 总结输出 ──
    print(f"\n{'='*60}")
    print(f"✅ 全部完成 | {ts}")
    print(f"{'='*60}")
    print(f"   live_ok.txt     ← {total_ok} 条（带分档尾注）")
    print(f"   live_ok.m3u     ← {total_ok} 条")
    print(f"   live_fail.txt   ← {len(_fail_raw)} 个真失效")
    print(f"   live_recent.txt ← {len([u for u in _by_tier[TIER_NEW] if any(normalize_url(r[0])==normalize_url(u) and r[4] for r in _results)])} 个 🆕一周内(可用)")
    print(f"   live_month.txt  ← {len(_by_tier[TIER_MONTH])} 个 📅一个月内")
    print(f"   live_3month.txt ← {len(_by_tier[TIER_3MONTH])} 个 📆三个月内")
    print(f"   live_old.txt    ← {len(_by_tier[TIER_OLD])} 个 🧓超三个月（全部）")
    print(f"   live_stale.txt  ← {len(_stale_urls)} 个 🧟僵尸源（>90天且不通）")
    print(f"   live_report.csv ← 11列报告")
    print(f"{'='*60}")

    # 防清空二次检查
    for fn, minn in [("live_ok.txt", 1), ("live_fail.txt", 0), ("live_report.csv", 1)]:
        if os.path.exists(fn):
            n = sum(1 for _ in open(fn, encoding="utf-8"))
            if n < minn:
                print(f"⚠️ {fn} 行数{n}<{minn}，疑似清空！")

    sys.exit(0)


if __name__ == "__main__":
    main()
