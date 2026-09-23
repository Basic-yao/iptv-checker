import os, sys, csv, time, socket, re
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 配置 ──
TIMEOUT = 8
MAX_WORKERS = 50
SRC_FILE = "live.txt"

TIER_NEW = "new"
TIER_MONTH = "month"
TIER_3MONTH = "3month"
TIER_OLD = "old"
TIER_ORDER = [TIER_NEW, TIER_MONTH, TIER_3MONTH, TIER_OLD]
TIER_TITLE = {
    TIER_NEW: "🆕 一周内（≤7天）",
    TIER_MONTH: "📅 一个月内（≤30天）",
    TIER_3MONTH: "📆 三个月内（≤90天）",
    TIER_OLD: "🧓 超三个月（>90天）",
}

def bjt_now():
    return datetime.utcnow() + timedelta(hours=8)

def gen_time():
    return bjt_now().strftime("%Y-%m-%d %H:%M:%S")

def age_label(days):
    if days is None: return ""
    if days <= 7: return f"🆕{days}天"
    if days <= 30: return f"📅{days}天"
    if days <= 90: return f"📆{days}天"
    return f"🧓{days}天"

def tier_of(days):
    if days is None: return TIER_OLD
    if days <= 7: return TIER_NEW
    if days <= 30: return TIER_MONTH
    if days <= 90: return TIER_3MONTH
    return TIER_OLD

def parse_age(url):
    # 简单根据域名/路径估算天数（保持你原有逻辑）
    s = url.lower()
    if "raw" in s or "master" in s or "main" in s:
        # 默认视为较新（可自定义）
        return 1
    m = re.search(r'(\d{4})[-/_]?(\d{2})[-/_]?(\d{2})', url)
    if m:
        try:
            d = datetime.strptime(m.group(0).replace("_","-"), "%Y-%m-%d")
            return max(1, (datetime.utcnow() - d).days)
        except: pass
    return 180  # 默认超三月

def check_url(url):
    start = time.time()
    try:
        # 简易探测：HEAD/GET 超时控制
        import urllib.request
        req = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            lat = int((time.time() - start) * 1000)
            return url, True, lat, "", resp.status
    except Exception as e:
        lat = int((time.time() - start) * 1000)
        return url, False, lat, str(e)[:50], 0

def main():
    ts = gen_time()
    if not os.path.exists(SRC_FILE):
        print(f"❌ 缺少 {SRC_FILE}")
        sys.exit(1)

    raw_urls = []
    with open(SRC_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): continue
            raw_urls.append(line)

    print(f"🔍 检测 {len(raw_urls)} 个源... (北京时间 {ts})")
    _results = []
    _ok_urls = []
    _fail_urls = []
    _by_tier = {t: [] for t in TIER_ORDER}
    _age_cache = {}
    _stale_urls = set()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(check_url, u): u for u in raw_urls}
        for fu in as_completed(futures):
            url, ok, lat, err, code = fu.result()
            days = parse_age(url)
            tier = tier_of(days)
            desc = age_label(days)
            _age_cache[url] = (days, desc)
            _results.append((url, ok, lat, err, days, tier, desc))
            if ok:
                _ok_urls.append(url)
                _by_tier[tier].append(url)
            else:
                _fail_urls.append(url)
                if days > 90:
                    _stale_urls.add(url)

    total_ok = len(_ok_urls)

    # ── live_ok.txt（分组固定顺序，组内字母序，附耗时） ──
    with open("live_ok.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间(北京时间): {ts}\n")
        f.write(f"# 可用源合计: {total_ok} 个\n")
        f.write(f"# 排序: 分组固定(一周→一月→三月→超三月)，组内URL字母升序，附耗时\n\n")
        for t in TIER_ORDER:
            urls = sorted(set(_by_tier[t]))  # 组内字母序
            if not urls: continue
            f.write(f"# ---- {TIER_TITLE[t]}（{len(urls)}个） ----\n")
            for u in urls:
                lat = next((r[2] for r in _results if r[0]==u), None)
                f.write(f"{u}  # {lat}ms\n")
            f.write("\n")

    # ── live_ok.m3u（按访问时间快慢排序，快者在前） ──
    with open("live_ok.m3u", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间(北京时间): {ts}\n")
        f.write(f"# 排序: 按访问延迟升序（越快越前），共 {total_ok} 个\n")
        f.write("#EXTM3U\n")
        # 仅可用源，按延迟升序；无延迟排最后
        ok_results = [r for r in _results if r[1]]
        ok_results.sort(key=lambda x: x[2] if x[2] is not None else float('inf'))
        for r in ok_results:
            url, lat = r[0], r[2]
            f.write(f"#EXTINF:-1,{lat}ms\n")
            f.write(f"{url}\n")

    # ── 单档文件（字母序） ──
    for t, fn in [(TIER_NEW,"live_recent.txt"), (TIER_MONTH,"live_month.txt"),
                  (TIER_3MONTH,"live_3month.txt"), (TIER_OLD,"live_old.txt")]:
        with open(fn, "w", encoding="utf-8") as f:
            f.write(f"# 生成时间: {ts}\n# {TIER_TITLE[t]}\n\n")
            for u in sorted(set(_by_tier[t])):
                f.write(f"{u}\n")

    # ── fail / stale ──
    with open("live_fail.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts}\n# 失败源（字母序）\n\n")
        for u in sorted(set(_fail_urls)):
            f.write(f"{u}\n")

    with open("live_stale.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts}\n# 僵尸源（>90天且不通）{len(_stale_urls)}个\n\n")
        for u in sorted(_stale_urls):
            days = _age_cache.get(u, (None, ""))[0]
            f.write(f"{u}  # {age_label(days)}\n")

    # ── CSV 报告（字母序） ──
    with open("live_report.csv", "w", encoding="utf-8-sig", newline="") as cf:
        w = csv.writer(cf)
        w.writerow(["状态","延迟ms","错误","分档","源龄","类别","描述","是否僵尸","时间","URL"])
        for r in sorted(_results, key=lambda x: x[0]):
            url, ok, lat, err, days, tier, desc = r
            is_stale = "是" if (not ok and days and days>90) else "否"
            w.writerow([
                "✅" if ok else "❌", lat or "", err,
                TIER_TITLE[tier], age_label(days), tier, desc, is_stale, ts, url
            ])
        if _stale_urls:
            for s in sorted(_stale_urls):
                days, desc = _age_cache.get(s, (None,""))
                w.writerow(["","","","僵尸源","",age_label(days),tier_of(days),"",ts,s])
        else:
            w.writerow(["","","","本次无>90天且不通的源","","","","",ts,"（无）"])

    # ── 总结 ──
    print(f"\n{'='*60}")
    print(f"✅ 全部完成 | 北京时间 {ts}")
    print(f"{'='*60}")
    print(f"   live_ok.txt     ← {total_ok} 条（分组固定，组内字母序，附耗时）")
    print(f"   live_ok.m3u     ← {total_ok} 条（⚡按访问时间快慢排序，快者在前）")
    print(f"   live_fail.txt   ← {len(set(_fail_urls))} 个（字母序）")
    print(f"   live_recent.txt ← {len(set(_by_tier[TIER_NEW]))} 个")
    print(f"   live_month.txt  ← {len(set(_by_tier[TIER_MONTH]))} 个")
    print(f"   live_3month.txt ← {len(set(_by_tier[TIER_3MONTH]))} 个")
    print(f"   live_old.txt    ← {len(set(_by_tier[TIER_OLD]))} 个")
    print(f"   live_stale.txt  ← {len(_stale_urls)} 个")
    print(f"   live_report.csv ← 报告（字母序）")
    print(f"{'='*60}")

    for fn, minn in [("live_ok.txt",1),("live_fail.txt",0),("live_report.csv",1)]:
        if os.path.exists(fn):
            n = sum(1 for _ in open(fn, encoding="utf-8"))
            if n < minn: print(f"⚠️ {fn} 行数{n}<{minn}，疑似异常！")
    sys.exit(0)

if __name__ == "__main__":
    main()
