#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 检查器（四档分档 + 原OK判定不变 + 僵尸源语义保留）
分档：
  🆕 一周内 (≤7天)
  📅 一个月内 (≤30天)
  📆 三个月内 (≤90天)
  🧓 超三个月 (>90天)
文件：
  live_ok.txt / live_ok.m3u  （通的，带分档尾注）
  live_recent.txt / live_month.txt / live_3month.txt / live_old.txt
  live_stale.txt （>90天 且 不通，原僵尸源语义）
  live_fail.txt / live_report.csv
"""
import os, re, sys, time, json, csv, socket, argparse, threading
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse
try:
    import requests
except ImportError:
    sys.exit("缺少 requests，请 pip install requests")

# ========== 全局配置 ==========
THREADS = 10
TIMEOUT = 20
RECENT_HOURS = 24 * 7          # 一周内
MONTH_DAYS = 30              # 一个月内
THREE_MONTH_DAYS = 90        # 三个月内
STALE_DAYS = 90              # 僵尸源阈值（>90天）
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
HEADERS = {"User-Agent": USER_AGENT}

TIER_NEW = "🆕一周内"
TIER_MONTH = "📅一个月内"
TIER_3MONTH = "📆三个月内"
TIER_OLD = "🧓超三个月"
TIER_UNKNOWN = "❓未知"

# 固定6大类（同原逻辑）
BIG_CATS = ["新闻综合","影视剧","体育","少儿","纪录片","其他"]
NEWS_KW = ["cctv-1","cctv1","综合","新闻","cctv-13","cctv13"]
MOVIE_KW = ["电影","影院","影视","剧集","cctv-6","cctv6"]
SPORT_KW = ["体育","赛事","足球","篮球","cctv-5","cctv5"]
KIDS_KW = ["少儿","动画","儿童","cctv-14","cctv14"]
DOC_KW = ["纪录","纪录片","cctv-9","cctv9"]

# 线程锁
_lock = threading.Lock()
results = []
ok_raw = []
fail_raw = []
seen_u = set()
uniq_urls = []
age_map = {}
recent_urls = set()
stale_urls = set()
old_but_alive = {}
by_tier = {TIER_NEW:[], TIER_MONTH:[], TIER_3MONTH:[], TIER_OLD:[], TIER_UNKNOWN:[]}
skipped = []

# ========== 工具函数 ==========
def now_cst():
    return datetime.utcnow() + timedelta(hours=8)

def normalize_url(u):
    try:
        p = urlparse(u.strip())
        netloc = p.netloc.lower().replace("www.","")
        path = re.sub(r'/index\.m3u8$','/playlist.m3u8',p.path,flags=re.I)
        return urlunparse((p.scheme.lower(), netloc, path, '', '', ''))
    except:
        return u.strip().lower()

def classify_big(url):
    u = url.lower()
    if any(k in u for k in NEWS_KW): return "新闻综合"
    if any(k in u for k in MOVIE_KW): return "影视剧"
    if any(k in u for k in SPORT_KW): return "体育"
    if any(k in u for k in KIDS_KW): return "少儿"
    if any(k in u for k in DOC_KW): return "纪录片"
    return "其他"

def get_age_days(url):
    """源龄：用 git log 首次出现时间（近似）"""
    nu = normalize_url(url)
    if nu in age_map: return age_map[nu]
    try:
        import subprocess
        out = subprocess.check_output(
            f"git log --reverse --pretty=format:%ci -- {os.path.abspath('live.txt')} 2>/dev/null | head -1",
            shell=True, text=True)
        if out.strip():
            first = datetime.strptime(out.strip()[:19], "%Y-%m-%d %H:%M:%S")
            days = (now_cst() - first).days
            age_map[nu] = (days, out.strip()[:10])
            return age_map[nu]
    except:
        pass
    age_map[nu] = (None, "")
    return (None, "")

def tier_of(days):
    if days is None: return TIER_UNKNOWN
    if days <= RECENT_HOURS//24: return TIER_NEW
    if days <= MONTH_DAYS: return TIER_MONTH
    if days <= THREE_MONTH_DAYS: return TIER_3MONTH
    return TIER_OLD

def age_label(days):
    if days is None: return "未知"
    if days <= RECENT_HOURS//24: return f"{days}天(一周内)"
    if days <= MONTH_DAYS: return f"{days}天(一月内)"
    if days <= THREE_MONTH_DAYS: return f"{days}天(三月内)"
    return f"{days}天(超三月)"

# ========== 检测核心（保留原三层判定） ==========
def check_one(url):
    nu = normalize_url(url)
    if nu in seen_u: return
    seen_u.add(nu)
    uniq_urls.append(url)
    start = time.time()
    try:
        # 直链 m3u8/ts
        if re.search(r'\.(m3u8|ts)(\?|$)', url, re.I):
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True, allow_redirects=True)
            elapsed = round(time.time()-start, 2)
            code = r.status_code
            ok = code in (200,206)
            # 限流/403 也算“可能通”
            if code in (429,403) and "github" in url.lower(): ok = True
            with _lock:
                results.append((url, ok, elapsed, "直链", code, ""))
                if ok: ok_raw.append((url, ok, elapsed, code))
                else: fail_raw.append((url, ok, elapsed, code, ""))
            return
        # GitHub raw（常限流）
        if "raw.githubusercontent" in url.lower():
            r = requests.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            code = r.status_code
            ok = code in (200, 429, 403)  # 限流视为可能通
            elapsed = round(time.time()-start, 2)
            with _lock:
                results.append((url, ok, elapsed, "GitHub", code, ""))
                if ok: ok_raw.append((url, ok, elapsed, code))
                else: fail_raw.append((url, ok, elapsed, code, ""))
            return
        # 普通网页
        r = requests.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        code = r.status_code
        ok = code in (200, 301, 302, 307, 308)
        elapsed = round(time.time()-start, 2)
        with _lock:
            results.append((url, ok, elapsed, "网页", code, ""))
            if ok: ok_raw.append((url, ok, elapsed, code))
            else: fail_raw.append((url, ok, elapsed, code, ""))
    except Exception as e:
        elapsed = round(time.time()-start, 2)
        with _lock:
            results.append((url, False, elapsed, "异常", 0, str(e)))
            fail_raw.append((url, False, elapsed, 0, str(e)))

# ========== 主流程 ==========
def main():
    global THREADS, TIMEOUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=THREADS)
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    args = ap.parse_args()
    THREADS = args.threads
    TIMEOUT = args.timeout

    if not os.path.exists("live.txt"):
        sys.exit("❌ 缺 live.txt")
    lines = [l.strip() for l in open("live.txt", encoding="utf-8") if l.strip() and not l.startswith("#")]
    urls = []
    for l in lines:
        if l.startswith("http"):
            urls.append(l)
        elif "," in l:
            parts = [p.strip() for p in l.split(",")]
            for p in parts:
                if p.startswith("http"): urls.append(p)
    print(f"📥 读取 {len(urls)} 条，去重前")

    # 并发检测
    print(f"🚀 检测中（threads={THREADS}, timeout={TIMEOUT}）...")
    ts = now_cst().strftime("%Y-%m-%d %H:%M:%S")
    pool = []
    for u in urls:
        while len(pool) >= THREADS:
            pool = [t for t in pool if t.is_alive()]
            time.sleep(0.1)
        t = threading.Thread(target=check_one, args=(u,))
        t.start()
        pool.append(t)
    for t in pool: t.join()

    # 分档
    for u in uniq_urls:
        nu = normalize_url(u)
        days, _ = get_age_days(u)
        tier = tier_of(days)
        by_tier[tier].append(u)
        if days is not None and days <= RECENT_HOURS//24:
            recent_urls.add(nu)
        if days is not None and days > STALE_DAYS:
            # 超三月全部
            is_ok = any(normalize_url(x[0])==nu and x[1] for x in ok_raw)
            if not is_ok:
                stale_urls.add(nu)  # 僵尸：超三月且不通
            else:
                old_but_alive[nu] = days

    # live_ok.txt（原OK数据，仅加尾注）
    total_ok = len(ok_raw)
    with open("live_ok.txt","w",encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts}\n")
        f.write(f"# 共 {total_ok} 条（按分档+域名排序）\n")
        sorted_ok = sorted(ok_raw, key=lambda x:(tier_of(get_age_days(x[0])[0]), classify_big(x[0]), x[0]))
        for url,ok,elapsed,code in sorted_ok:
            nu = normalize_url(url)
            days,_ = get_age_days(url)
            tier = tier_of(days)
            note = f" # {tier} | 源龄{age_label(days)}"
            f.write(f"{url}{note}\n")

    # live_ok.m3u
    with open("live_ok.m3u","w",encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for url,ok,elapsed,code in sorted_ok:
            nu = normalize_url(url)
            days,_ = get_age_days(url)
            tier = tier_of(days)
            tag = {"🆕一周内":"[NEW]","📅一个月内":"[1M]","📆三个月内":"[3M]","🧓超三个月":"[OLD]"}.get(tier,"")
            cat = classify_big(url)
            f.write(f'#EXTINF:-1,{tag}{cat}\n{url}\n')

    # 四档 + 超三月全量
    def write_tier(fn, tier, extra_note=""):
        with open(fn,"w",encoding="utf-8") as f:
            f.write(f"# 生成时间: {ts}\n")
            f.write(f"# {tier} 共 {len(by_tier[tier])} 条{extra_note}\n")
            for u in sorted(by_tier[tier], key=lambda x: classify_big(x)+x):
                nu = normalize_url(u)
                days,_ = get_age_days(u)
                is_ok = any(normalize_url(x[0])==nu and x[1] for x in ok_raw)
                st = "✅可用" if is_ok else "❌不通"
                f.write(f"{u} # {tier} | 源龄{age_label(days)} | {st}\n")

    write_tier("live_recent.txt", TIER_NEW)
    write_tier("live_month.txt", TIER_MONTH)
    write_tier("live_3month.txt", TIER_3MONTH)
    write_tier("live_old.txt", TIER_OLD, "（超三个月全部源）")
    # 僵尸源（原语义：>90天且不通）
    with open("live_stale.txt","w",encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts}\n")
        f.write(f"# 僵尸源（>90天 且 本次不通）: {len(stale_urls)} 个\n")
        f.write(f"# 这些源已从 live_ok.txt 剔除，建议人工复查后可删除\n")
        if stale_urls:
            for nu in sorted(stale_urls):
                days,_ = age_map.get(nu,(None,""))
                f.write(f"{nu} # 僵尸源 | 源龄{age_label(days)}\n")
        else:
            f.write("# （无）本次无>90天且不通的源\n")

    # fail
    with open("live_fail.txt","w",encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts}\n")
        f.write(f"# 真失效: {len(fail_raw)} 个\n")
        for url,ok,elapsed,code,err in fail_raw:
            f.write(f"{url} # code={code} err={err}\n")

    # skipped
    if skipped:
        with open("skipped.txt","w",encoding="utf-8") as f:
            for s in skipped: f.write(s+"\n")

    # CSV报告（11列）
    with open("live_report.csv","w",encoding="utf-8-sig") as f:
        f.write("新源,大类,原分类,URL,状态,耗时,可用性,类型,源龄(天),更新分档,备注\n")
        for u in uniq_urls:
            nu = normalize_url(u)
            days,_ = get_age_days(u)
            tier = tier_of(days)
            is_ok = any(normalize_url(x[0])==nu and x[1] for x in ok_raw)
            state = "✅可用" if is_ok else "❌不通"
            elapsed = next((x[2] for x in results if normalize_url(x[0])==nu), "")
            code = next((x[4] for x in results if normalize_url(x[0])==nu), "")
            note_parts=[]
            if nu in recent_urls: note_parts.append(f"🆕{RECENT_HOURS}h内更新")
            if nu in stale_urls: note_parts.append(f"🧟僵尸源>{STALE_DAYS}天")
            elif nu in old_but_alive: note_parts.append(f"🧓老源{old_but_alive[nu]}天但存活")
            if days is None: note_parts.append("源龄未知")
            note=" | ".join(note_parts)
            broad=tier[:2]
            f.write(f"{('1' if tier==TIER_NEW else '')},{broad},,{u},{code},{elapsed},{state},,{age_label(days)},{tier},{note}\n")
        f.write("\n,,,,,,,,,,僵尸源清单\n")
        if stale_urls:
            for s in sorted(stale_urls):
                days,_=age_map.get(s,(None,""))
                f.write(f",,,,,,,{s},,{age_label(days)},僵尸源\n")
        else:
            f.write(",,,,,,,,,（无）本次无>90天且不通的源\n")

    # 总结
    print(f"\n💾 已生成（统一北京时间: {ts}）:")
    print(f"   live_ok.txt     ← {total_ok} 条（带分档尾注）")
    print(f"   live_ok.m3u     ← {total_ok} 条")
    print(f"   live_recent.txt ← {len(by_tier[TIER_NEW])} 个 🆕一周内")
    print(f"   live_month.txt  ← {len(by_tier[TIER_MONTH])} 个 📅一个月内")
    print(f"   live_3month.txt ← {len(by_tier[TIER_3MONTH])} 个 📆三个月内")
    print(f"   live_old.txt    ← {len(by_tier[TIER_OLD])} 个 🧓超三个月（全部）")
    print(f"   live_stale.txt  ← {len(stale_urls)} 个 🧟僵尸源（>90天且不通）")
    print(f"   live_fail.txt   ← {len(fail_raw)} 个真失效")
    print(f"   live_report.csv ← 检测报告（11列）")

    # 防清空
    for fn,minn in [('live_ok.txt',1),('live_fail.txt',0),('live_report.csv',1)]:
        if os.path.exists(fn):
            n=sum(1 for _ in open(fn,encoding='utf-8'))
            if n<minn: print(f"⚠️ {fn} 行数{n}<{minn}，疑似清空，请检查")

if __name__ == "__main__":
    main()
