import os
import sys
import csv
import argparse
import concurrent.futures
import urllib.request
import urllib.error
import time
import re

CAT_ORDER = ["国内源", "国际源", "4K高清", "TVBox", "GitHub源", "其他"]
SKIP_CATS = set()

PREV_OK_FILE = 'live_ok.txt'

def normalize_url(u):
    u = u.strip().replace('\n', '').replace('\r', '')
    if '#' in u: u = u.split('#')[0]
    return u.strip()

def classify_by_domain(url):
    low = url.lower().strip()
    if 'raw.githubusercontent.com' in low or 'githubusercontent.com' in low or 'github.com' in low:
        return "GitHub源"
    if 'gitee.com' in low: return "其他"
    if 'gitlab.com' in low: return "其他"
    if 'migu' in low or 'miguvideo' in low: return "国内源"
    if '4k' in low or '8k' in low: return "4K高清"
    if 'tvbox' in low or 'box' in low: return "TVBox"
    return None

def classify(orig_cat, url=""):
    dom = classify_by_domain(url)
    if dom: return dom
    # 原有分类映射兜底
    cat_low = (orig_cat or "").lower()
    if any(k in cat_low for k in ["国内", "中文", "咪咕", "移动", "联通", "电信"]): return "国内源"
    if any(k in cat_low for k in ["国际", "iptv-org", "国外"]): return "国际源"
    if any(k in cat_low for k in ["4k", "高清", "8k"]): return "4K高清"
    if any(k in cat_low for k in ["tvbox", "盒子"]): return "TVBox"
    return "其他"

def should_skip_url(url):
    low = url.lower()
    if 'enc' in low or '加密' in low or 'key' in low: return True
    return False

def load_previous_ok_urls():
    if not os.path.exists(PREV_OK_FILE):
        return set()
    s = set()
    with open(PREV_OK_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            u = normalize_url(line)
            if u: s.add(u)
    return s

def check_one(broad, orig_cat, url, timeout):
    url_r = normalize_url(url)
    if not url_r: return (broad, orig_cat, url, 0, 9999, "empty")
    try:
        req = urllib.request.Request(url_r, method='HEAD')
        req.add_header('User-Agent', 'Mozilla/5.0')
        start = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = int((time.time()-start)*1000)
            status = resp.status
            return (broad, orig_cat, url_r, status, elapsed, "")
    except urllib.error.HTTPError as e:
        elapsed = int((time.time()-start)*1000)
        return (broad, orig_cat, url_r, e.code, elapsed, str(e.reason))
    except Exception as e:
        elapsed = int((time.time()-start)*1000)
        return (broad, orig_cat, url_r, 0, elapsed, str(e))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--threads', type=int, default=10)
    parser.add_argument('--timeout', type=int, default=10)
    args = parser.parse_args()

    print(f"▶ 运行 python check_iptv.py --threads {args.threads} --timeout {args.timeout}")
    print(f"📂 读取 live.txt ...")
    if not os.path.exists('live.txt'):
        print("❌ live.txt 不存在"); sys.exit(0)

    entries = []
    with open('live.txt','r',encoding='utf-8') as f:
        for line in f:
            line=line.strip()
            if not line or line.startswith('#'): continue
            # 支持 "分类,url" 或纯url
            if ',' in line and 'http' in line:
                cat, url = line.split(',',1)
                entries.append((cat.strip(), url.strip()))
            else:
                entries.append(("", line.strip()))

    prev_urls = load_previous_ok_urls()
    print(f"📄 上一次可用源记录：{len(prev_urls)} 条")
    print(f"解析到 {len(entries)} 个链接")

    skipped=[]
    to_check=[]
    for cat, url in entries:
        if should_skip_url(url):
            skipped.append(url); continue
        broad = classify(cat, url)
        if broad in SKIP_CATS:
            skipped.append(url); continue
        to_check.append((broad, cat, url))

    print(f"🚀 开始检测（{len(to_check)} 个，{args.threads} 线程，{args.timeout}s 超时）...")
    results=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs=[ex.submit(check_one, b, oc, u, args.timeout) for b,oc,u in to_check]
        for i,fut in enumerate(concurrent.futures.as_completed(futs),1):
            res=fut.result()
            results.append(res)
            b,oc,u,s,e,err=res
            mark="✅" if s in (200,206) else "❌"
            print(f"[{i}/{len(to_check)}] {mark} {s} {e}ms {u} ({b})")

    # 分类分布
    dist={}
    for b,oc,u,s,e,err in results: dist[b]=dist.get(b,0)+1
    print("📊 分类分布预览：")
    for k in CAT_ORDER: 
        if k in dist: print(f"  {k}: {dist[k]} 个")
    if "其他" in dist: print(f"  其他: {dist['其他']} 个")

    # 新源判定
    ok_urls={normalize_url(u) for b,oc,u,s,e,err in results if s in (200,206)}
    new_urls=ok_urls - prev_urls
    new_urls_norm={normalize_url(u) for u in new_urls}
    total_ok=len(ok_urls)

    # 写 live_ok.txt
    by_broad={b:[] for b in CAT_ORDER}
    for b,oc,u,s,e,err in results:
        if s in (200,206): by_broad[b].append((u,e))

    with open('live_ok.txt','w',encoding='utf-8') as f:
        for b in CAT_ORDER:
            urls=by_broad.get(b,[])
            if not urls: continue
            f.write(f"# ---- {b} ----\n")
            for u,e in urls: f.write(f"{u}\n")
            f.write("\n")

    with open('live_ok.m3u','w',encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for b in CAT_ORDER:
            urls=by_broad.get(b,[])
            if not urls: continue
            for u,e in urls:
                f.write(f'#EXTINF:-1 group-title="{b}", {b} ({e}ms)\n')
                f.write(f'{u}\n')

    if skipped:
        with open('skipped.txt','w',encoding='utf-8') as f:
            for u in skipped: f.write(f"{u}\n")

    # fail
    fail_raw=[(b,oc,u,s,e,err) for b,oc,u,s,e,err in results if s not in (200,206)]
    seen_f={}
    for item in fail_raw:
        u=normalize_url(item[2])
        if u not in seen_f: seen_f[u]=item
    with open('live_fail.txt','w',encoding='utf-8') as f:
        for b in CAT_ORDER:
            items=[v for v in seen_f.values() if v[0]==b]
            if not items: continue
            f.write(f"# ---- {b} ----\n")
            for _,_,u,s,e,err in items:
                reason=f"  #{s} {err}" if err else f"  #HTTP{s}"
                f.write(f"{u}{reason}\n")
            f.write("\n")

    # CSV（新增最前+标星）
    with open('live_report.csv','w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f)
        w.writerow(['新增源','大类','原始分类','URL','状态码','响应时间(ms)','错误'])
        sorted_results=sorted(results, key=lambda x:(
            0 if normalize_url(x[2]) in new_urls_norm else 1,
            CAT_ORDER.index(x[0]) if x
