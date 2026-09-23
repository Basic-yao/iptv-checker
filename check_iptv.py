import os
import sys
import csv
import time
import socket
import concurrent.futures
from datetime import datetime, timedelta
from urllib.parse import urlparse

# ── 配置 ──
TIMEOUT = 8
MAX_WORKERS = 50
BJT = timedelta(hours=8)

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

_results = []
_by_tier = {t: [] for t in TIER_ORDER}
_fail_raw = []
_age_cache = {}
_stale_urls = set()

def gen_time():
    return (datetime.utcnow() + BJT).strftime("%Y-%m-%d %H:%M:%S")

def age_label(days):
    if days is None: return "未知"
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

def get_latency(url):
    """返回 (耗时ms, 错误)"""
    try:
        start = time.time()
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        sock = socket.create_connection((host, port), timeout=TIMEOUT)
        sock.close()
        return int((time.time() - start) * 1000), None
    except Exception as e:
        return None, str(e)[:50]

def check(url, name=""):
    url = url.strip()
    if not url or url.startswith("#"): return
    lat, err = get_latency(url)
    ok = lat is not None
    days = _age_cache.get(url, (None, ""))[0]
    tier = tier_of(days)
    desc = _age_cache.get(url, (None, ""))[1] or ""
    
    _results.append((name, url, url, ok, lat, err, days, tier, desc))
    if ok:
        _by_tier[tier].append(url)
        if days and days > 90 and not ok:
            _stale_urls.add(url)
    else:
        _fail_raw.append(url)
        if days and days > 90:
            _stale_urls.add(url)

def load_urls():
    base = "live.txt"
    if not os.path.exists(base): return []
    urls = []
    with open(base, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): continue
            # 支持 "名称,url" 或纯 url
            if "," in line and not line.startswith("http"):
                name, url = line.split(",", 1)
                urls.append((name.strip(), url.strip()))
            else:
                urls.append(("", line))
    return urls

def load_age_cache():
    """从 live_recent/old 等历史推断天数（简化：无历史则未知）"""
    # 实际可结合 git 时间，此处保持原逻辑
    pass

def main():
    ts = gen_time()
    urls = load_urls()
    print(f"🚀 开始检测 {len(urls)} 个源... | {ts}")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(check, u, n) for n, u in urls]
        for i, f in enumerate(concurrent.futures.as_completed(futures)):
            if i % 10 == 0: print(f"  进度: {i+1}/{len(urls)}")
            f.result()
    
    total_ok = len([r for r in _results if r[3]])
    print(f"✅ 检测完成 | 可用: {total_ok}, 失效: {len(set(_fail_raw))}")
    
    # ── live_ok.txt（分组，组内字母序，附耗时） ──
    with open("live_ok.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间(北京时间): {ts}\n")
        f.write(f"# 可用源合计: {total_ok} 个\n")
        f.write(f"# 排序: 分组固定(一周→一月→三月→超三月)，组内按 URL 字母升序\n\n")
        for t in TIER_ORDER:
            urls = sorted(set(_by_tier[t]))
            if not urls: continue
            f.write(f"# ---- {TIER_TITLE[t]}（{len(urls)}个） ----\n")
            for u in urls:
                lat = next((r[4] for r in _results if r[2] == u and r[3]), None)
                lat_str = f"  # {lat}ms" if lat else ""
                f.write(f"{u}{lat_str}\n")
            f.write("\n")
    
    # ── live_ok.m3u（字母序，EXTINF 带耗时） ──
    with open("live_ok.m3u", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间(北京时间): {ts}\n")
        f.write(f"# 排序: 按 URL 字母升序 | 格式: #EXTINF:-1,耗时ms\n")
        f.write("#EXTM3U\n")
        for r in sorted(_results, key=lambda x: x[2]):
            if not r[3]: continue
            lat = r[4] or 0
            f.write(f"#EXTINF:-1,{lat}ms\n")
            f.write(f"{r[2]}\n")
    
    # ── 单档文件（字母序，附耗时） ──
    for t, fn in [(TIER_NEW, "live_recent.txt"), (TIER_MONTH, "live_month.txt"),
                  (TIER_3MONTH, "live_3month.txt"), (TIER_OLD, "live_old.txt")]:
        with open(fn, "w", encoding="utf-8") as f:
            f.write(f"# 生成时间: {ts}\n# {TIER_TITLE[t]}\n\n")
            for u in sorted(set(_by_tier[t])):
                lat = next((r[4] for r in _results if r[2] == u and r[3]), None)
                lat_str = f"  # {lat}ms" if lat else ""
                f.write(f"{u}{lat_str}\n")
    
    # ── 失效/僵尸（字母序） ──
    with open("live_fail.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts}\n# 失效源（{len(set(_fail_raw))}个）\n\n")
        for u in sorted(set(_fail_raw)):
            f.write(f"{u}\n")
    
    with open("live_stale.txt", "w", encoding="utf-8") as f:
        f.write(f"# 生成时间: {ts}\n# 僵尸源（>90天且不通）{len(_stale_urls)}个\n\n")
        for u in sorted(_stale_urls):
            days = _age_cache.get(u, (None, ""))[0]
            f.write(f"{u}  # {age_label(days)}\n")
    
    # ── CSV 报告 ──
    with open("live_report.csv", "w", encoding="utf-8-sig", newline="") as cf:
        w = csv.writer(cf)
        w.writerow(["状态", "延迟ms", "错误", "分档", "源龄", "类别", "描述", "是否僵尸", "时间", "URL"])
        for r in sorted(_results, key=lambda x: x[2]):
            ok, lat, err, days, tier, desc = r[3], r[4], r[5], r[6], r[7], r[8]
            is_stale = "是" if (not ok and days and days > 90) else "否"
            w.writerow([
                "✅" if ok else "❌", lat or "", err,
                TIER_TITLE[tier], age_label(days), tier, desc, is_stale, ts, r[2]
            ])
        if _stale_urls:
            for s in sorted(_stale_urls):
                days, desc = _age_cache.get(s, (None, ""))
                w.writerow(["", "", "", "僵尸源", "", age_label(days), tier_of(days), "", ts, s])
        else:
            w.writerow(["", "", "", "本次无>90天且不通的源", "", "", "", "", ts, "（无）"])
    
    # ── 总结 ──
    print(f"\n{'='*60}")
    print(f"✅ 全部完成 | 北京时间 {ts}")
    print(f"{'='*60}")
    print(f"   live_ok.txt     ← {total_ok} 条（分组固定，组内字母序，附耗时）")
    print(f"   live_ok.m3u     ← {total_ok} 条（字母序，EXTINF带耗时）")
    print(f"   live_fail.txt   ← {len(set(_fail_raw))} 个")
    print(f"   live_recent.txt ← {len(set(_by_tier[TIER_NEW]))} 个")
    print(f"   live_month.txt  ← {len(set(_by_tier[TIER_MONTH]))} 个")
    print(f"   live_3month.txt ← {len(set(_by_tier[TIER_3MONTH]))} 个")
    print(f"   live_old.txt    ← {len(set(_by_tier[TIER_OLD]))} 个")
    print(f"   live_stale.txt  ← {len(_stale_urls)} 个")
    print(f"   live_report.csv ← 报告（字母序）")
    print(f"{'='*60}")

    for fn, minn in [("live_ok.txt", 1), ("live_fail.txt", 0), ("live_report.csv", 1)]:
        if os.path.exists(fn):
            n = sum(1 for _ in open(fn, encoding="utf-8"))
            if n < minn: print(f"⚠️ {fn} 行数{n}<{minn}，疑似异常！")
    sys.exit(0)

if __name__ == "__main__":
    main()
