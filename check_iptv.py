import os, re, sys, time, csv
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ━━━ 固定 6 大类（方案B） ━━━━━━━━━━━
CAT_ORDER = ["国内源", "国际源", "4K高清", "TVBox", "GitHub源", "其他"]

# 原始标题 → 大类的完整映射
TITLE_MAP = {
    "直播": "国际源", "国际": "国际源", "国外": "国际源",
    "国内": "国内源", "央视": "国内源", "卫视": "国内源",
    "4k": "4K高清", "4K": "4K高清", "8k": "4K高清",
    "tvbox": "TVBox", "TVBox": "TVBox",
    "github": "GitHub源", "GitHub源": "GitHub源",
    "其他": "其他",
}

def normalize_url(u):
    return re.sub(r'^https?://', '', u.strip()).split('?')[0].lower()

def get_domain(url):
    """提取域名用于排序（如 raw.githubusercontent.com）"""
    norm = normalize_url(url)
    return norm.split('/')[0] if norm else url

def is_usable(status, flag):
    # 403/503/重定向均视为可用（防误杀）
    return status in (200, 204, 206, 301, 302, 307, 308, 403, 503)

def classify(url, title="未分类"):
    low = normalize_url(url).lower()
    # 1. URL强制：4K/8K
    if any(k in low for k in ['4k', '8k']):
        return "4K高清"
    # 2. GitHub
    if 'raw.githubusercontent.com' in low or 'github.com' in low:
        return "GitHub源"
    # 3. 国际源典型
    if 'iptv-org' in low:
        return "国际源"
    # 4. 标题映射
    if title and title != "未分类":
        for k, v in TITLE_MAP.items():
            if k in title:
                return v
    # 5. 域名兜底（可选）
    return "其他"

# ━━━ 读取源（保留原始顺序） ━━━━━━━━━━━
def load_sources(path='live.txt'):
    sources = []  # (原始标题, url)
    cur_title = "其他"
    if not os.path.exists(path):
        return sources
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                m = re.search(r'#\s*----?\s*(.*?)\s*----?', line)
                if m:
                    cur_title = m.group(1).strip()
                continue
            if line:
                sources.append((cur_title, line))
    return sources

# ━━━ 检测逻辑（沿用之前：20s超时，GET降级，403保留） ━━━━━━━━━━━
def check(url):
    headers = {'User-Agent': 'Mozilla/5.0'}
    start = time.time()
    try:
        # 先HEAD，失败再GET（兼容不支持HEAD的）
        try:
            r = requests.head(url, timeout=20, allow_redirects=True, headers=headers)
            status = r.status_code
        except Exception:
            r = requests.get(url, timeout=20, stream=True, allow_redirects=True, headers=headers)
            status = r.status_code
            r.close()
        elapsed = int((time.time() - start) * 1000)
        # 403/503算可用，标记limited/overload
        flag = "ok"
        if status == 403: flag = "limited"
        elif status == 503: flag = "overload"
        elif status >= 400: flag = "bad"
        return url, status, elapsed, flag
    except requests.exceptions.Timeout:
        return url, 0, 20000, "timeout"
    except Exception:
        return url, 0, 20000, "conn"

# ━━━ 主流程 ━━━━━━━━━━━
def main():
    sources = load_sources()
    print(f"📥 读取 {len(sources)} 条源")

    results = []
    # 并发检测
    with ThreadPoolExecutor(max_workers=30) as ex:
        futs = {ex.submit(check, u): (t, u) for t, u in sources}
        for fut in as_completed(futs):
            title, url = futs[fut]
            url2, status, elapsed, flag = fut.result()
            broad = classify(url, title)
            results.append((broad, title, url, status, elapsed, flag))
            print(f"  [{'✓' if is_usable(status,flag) else '✗'}] {broad} | {status} {flag} | {url[:60]}")

    # 可用源
    ok = [r for r in results if is_usable(r[3], r[5])]
    print(f"\n✅ 可用 {len(ok)} / {len(results)}")

    # ━━━ 按大类分组 + 同域名首字排序 ━━━━━━━━━━━
    by_broad = {b: [] for b in CAT_ORDER}
    # 记录每个域名的出现顺序（保持同域名内原序）
    domain_order = {}
    idx = 0
    for broad, title, url, status, elapsed, flag in ok:
        dom = get_domain(url)
        if dom not in domain_order:
            domain_order[dom] = idx
            idx += 1
        by_broad[broad].append((dom, domain_order[dom], url, elapsed))

    # 排序：先按域名首字，同域名按首次出现顺序
    for b in CAT_ORDER:
        by_broad[b].sort(key=lambda x: (x[0], x[1]))
        by_broad[b] = [(url, elapsed) for _, _, url, elapsed in by_broad[b]]

    # 写 live_ok.txt
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        for broad in CAT_ORDER:
            urls = by_broad[broad]
            if not urls: continue
            f.write(f"# ---- {broad} ----\n")
            for url, elapsed in urls:
                f.write(f"{url}\n")
            f.write("\n")

    # 写 live_ok.m3u
    with open('live_ok.m3u', 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for broad in CAT_ORDER:
            urls = by_broad[broad]
            if not urls: continue
            for url, elapsed in urls:
                f.write(f'#EXTINF:-1 group-title="{broad}", {broad} ({elapsed}ms)\n')
                f.write(f'{url}\n')
            f.write('\n')

    # 失效源（只收真正失效）
    fail = [r for r in results if not is_usable(r[3], r[5])]
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        for broad in CAT_ORDER:
            items = [r for r in fail if r[0] == broad]
            if not items: continue
            f.write(f"# ---- {broad} ----\n")
            for _, _, url, status, _, flag in items:
                f.write(f"{url}  #{status} {flag}\n")
            f.write("\n")

    # CSV 报告
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['大类', '原始分类', '域名', 'URL', '状态码', '响应时间(ms)', '状态'])
        for broad, title, url, status, elapsed, flag in sorted(results, key=lambda x: (CAT_ORDER.index(x[0]), get_domain(x[2]))):
            state = "可用" if is_usable(status, flag) else flag
            w.writerow([broad, title, get_domain(url), url, status, elapsed, state])

    print(f"💾 已生成: live_ok.txt / live_ok.m3u / live_fail.txt / live_report.csv")

if __name__ == '__main__':
    main()
