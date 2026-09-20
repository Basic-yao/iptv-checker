#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPTV 直播源检测脚本。

功能：
- 检测直播源是否可用，按响应速度排序
- 按大类分组输出，标题格式为 # ---- 分类 ----
- URL 级去重 + 同源仓库去重（owner/repo 相同只保留最快）
- 代理/加密类自动跳过
"""

import sys
import os
import re
import csv
import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("请先安装依赖: pip install requests")
    sys.exit(1)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ━━━ 配置 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_THREADS = 20
DEFAULT_TIMEOUT = 10
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; TV) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)

# 大类输出顺序（固定）
CAT_ORDER = ["国内源", "国际源", "4K高清", "TVBox", "其他"]

# 细分类 → 大类
CAT_MAP = {
    "中文综合聚合": "国内源",
    "国内地方/个人源": "国内源",
    "vbskycn 镜像": "国内源",
    "咪咕/移动": "国内源",
    "综合/其他聚合": "国内源",
    "iptv-org 分类": "国际源",
    "iptv": "国际源",
    "TVBox/盒子": "TVBox",
    "4K/高清": "4K高清",
    "代理/中转/加密": "跳过",
    "KStore/网盘分享": "其他",
    "其他新增": "其他",
    "未分类": "其他",
}

SKIP_CATS = {"跳过"}

# 需要跳过的 URL 关键词（含这些的直接跳过不检测）
SKIP_URL_KEYWORDS = [
    ".php?sub=",
    "/encrypt/",
    "/api/decrypt",
    "password=",
    "token=",
    "secret=",
]


# ━━━ 检测函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_url(url, timeout=DEFAULT_TIMEOUT):
    """检测单个 URL 是否可用，返回 (url, status, elapsed_ms, error)"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }
    parsed = urlparse(url)
    if "migu" in parsed.netloc.lower():
        headers["Referer"] = "https://www.miguvideo.com/"

    start = time.time()
    try:
        # 先试 HEAD
        try:
            r = requests.head(
                url, headers=headers, timeout=timeout,
                allow_redirects=True, verify=False,
            )
            elapsed = int((time.time() - start) * 1000)
            if r.status_code in (200, 206):
                return url, r.status_code, elapsed, ""
            if r.status_code == 405:
                raise requests.exceptions.RequestException("fallback to GET")
        except requests.exceptions.RequestException:
            pass

        # HEAD 不行就 GET（stream 模式，不下载正文）
        start = time.time()
        r = requests.get(
            url, headers=headers, timeout=timeout,
            allow_redirects=True, verify=False, stream=True,
        )
        elapsed = int((time.time() - start) * 1000)
        r.close()
        return url, r.status_code, elapsed, ""

    except requests.exceptions.Timeout:
        return url, 0, int((time.time() - start) * 1000), "TIMEOUT"
    except requests.exceptions.ConnectionError:
        return url, 0, int((time.time() - start) * 1000), "CONN_ERR"
    except Exception as e:
        return url, 0, int((time.time() - start) * 1000), str(e)[:50]


# ━━━ 解析输入文件 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def parse_file(filepath):
    """解析 live.txt，返回 [(分类, url), ...]"""
    entries = []
    current_cat = "未分类"
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXTM3U"):
                continue
            # 分类标题行
            if line.startswith("#") and "http" not in line:
                if line.startswith("#EXTINF"):
                    m = re.search(r'group-title="([^"]+)"', line)
                    if m:
                        current_cat = m.group(1).strip()
                    continue
                if line.startswith("#EXTGRP"):
                    continue
                # # ---- 分类名 ---- 格式
                m = re.match(r"#\s*-+\s*(.+?)\s*-+\s*$", line)
                if m:
                    current_cat = m.group(1).strip()
                    continue
                # # 分类名 格式
                candidate = line.lstrip("#").strip()
                if candidate and not candidate.startswith("EXT"):
                    current_cat = candidate
                continue
            # URL 行
            if line.startswith("http"):
                entries.append((current_cat, line))
    return entries


# ━━━ 同源仓库提取 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def repo_key(url):
    """提取 raw.githubusercontent.com/owner/repo 作为同源判定键"""
    m = re.match(r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)", url)
    if m:
        return m.group(1).lower() + "/" + m.group(2).lower()
    return None


# ━━━ 判断是否需要跳过 ━━━━━━━━━━━━━━━━━━━━━━━━━━
def should_skip_url(url):
    url_lower = url.lower()
    for kw in SKIP_URL_KEYWORDS:
        if kw.lower() in url_lower:
            return True
    return False


# ━━━ 主流程 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    parser = argparse.ArgumentParser(description="IPTV 直播源检测")
    parser.add_argument("file", nargs="?", default="live.txt")
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print("文件不存在: " + args.file)
        sys.exit(1)

    print("读取 " + args.file + " ...")
    entries = parse_file(args.file)
    print("解析到 " + str(len(entries)) + " 个链接")

    if len(entries) == 0:
        print("未解析到任何链接！检查 live.txt 格式")
        print("前 10 行原始内容:")
        with open(args.file, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                print("  L" + str(i + 1) + ": " + line.rstrip())
        sys.exit(1)

    # 分离跳过类
    to_check = []
    skipped = []
    for cat, url in entries:
        if should_skip_url(url):
            skipped.append(url)
            continue
        broad = CAT_MAP.get(cat, "其他")
        if broad in SKIP_CATS:
            skipped.append(url)
        else:
            to_check.append((broad, cat, url))

    if skipped:
        print("跳过 " + str(len(skipped)) + " 个（加密/关键词）\n")

    if not to_check:
        print("没有需要检测的链接！全部被跳过或解析为空")
        sys.exit(1)

    print("开始检测 (" + str(len(to_check)) + " 个, "
          + str(args.threads) + " 线程, "
          + str(args.timeout) + "s 超时)...\n")

    # 并发检测
    results = []
    completed = 0
    total = len(to_check)

    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {
            executor.submit(check_url, url, args.timeout): (broad, orig_cat, url)
            for broad, orig_cat, url in to_check
        }
        for future in as_completed(futures):
            broad, orig_cat, url = futures[future]
            completed += 1
            try:
                url_r, status, elapsed, error = future.result()
            except Exception as e:
                url_r, status, elapsed, error = url, 0, 0, str(e)[:50]
            results.append((broad, orig_cat, url_r, status, elapsed, error))

            icon = "OK" if status in (200, 206) else "TMO" if "TIMEOUT" in error else "FAIL"
            short = url_r if len(url_r) <= 55 else url_r[:52] + "..."
            print("  [" + str(completed) + "/" + str(total) + "] "
                  + icon + " " + str(status) + " | "
                  + str(elapsed) + "ms | " + short)

    # 可用结果
    ok_raw = [
        (b, oc, u, s, e_ms, e)
        for b, oc, u, s, e_ms, e in results
        if s in (200, 206)
    ]

    # 去重：第一层 URL 完全相同
    seen_url = {}
    for item in ok_raw:
        u = item[2]
        if u not in seen_url:
            seen_url[u] = item
    ok_url_dedup = list(seen_url.values())
    url_dup_count = len(ok_raw) - len(ok_url_dedup)

    # 去重：第二层 同源仓库（owner/repo 相同只保留最快）
    seen_repo = {}
    repo_dup_count = 0
    for item in ok_url_dedup:
        key = repo_key(item[2])
        if key is None:
            seen_repo[item[2]] = item
        elif key not in seen_repo:
            seen_repo[key] = item
        else:
            repo_dup_count += 1
            if item[4] < seen_repo[key][4]:
                seen_repo[key] = item
    ok_dedup = list(seen_repo.values())

    # 按大类分组，大类内按响应速度排序（快→慢）
    by_broad = {broad: [] for broad in CAT_ORDER}
    for broad, orig_cat, url, status, elapsed, error in ok_dedup:
        by_broad.setdefault(broad, []).append((url, elapsed))

    for broad in by_broad:
        by_broad[broad].sort(key=lambda x: x[1])

    total_ok = sum(len(v) for v in by_broad.values())

    # 统计日志
    print("")
    print("=" * 50)
    print("检测完成")
    print("=" * 50)
    print("  解析总数: " + str(len(entries)))
    print("  检测总数: " + str(len(to_check)))
    print("  原始可用: " + str(len(ok_raw)))
    if url_dup_count:
        print("  URL去重:  " + str(url_dup_count))
    if repo_dup_count:
        print("  同源合并: " + str(repo_dup_count))
    print("  最终保留: " + str(total_ok))
    for broad in CAT_ORDER:
        urls = by_broad[broad]
        if urls:
            print("  " + broad + ": " + str(len(urls)) + " 条")
    print("=" * 50)
    print("")

    if total_ok == 0:
        print("没有可用源！检查上方日志中的状态码和错误信息")
        print("可尝试加大超时时间: --timeout 15")
        print("")

    # 写 live_ok.txt
    with open("live_ok.txt", "w", encoding="utf-8") as f:
        for broad in CAT_ORDER:
            urls = by_broad.get(broad, [])
            if not urls:
                continue
            f.write("# ---- " + broad + " ----\n")
            for url, elapsed in urls:
                f.write(url + "\n")
            f.write("\n")

    # 写 skipped.txt
    if skipped:
        with open("skipped.txt", "w", encoding="utf-8") as f:
            for url in skipped:
                f.write(url + "\n")

    # 写 live_fail.txt
    fail_raw = [
        (b, oc, u, s, e)
        for b, oc, u, s, e_ms, e in results
        if s not in (200, 206)
    ]
    seen_f = {}
    for item in fail_raw:
        u = item[2]
        if u not in seen_f:
            seen_f[u] = item
    with open("live_fail.txt", "w", encoding="utf-8") as f:
        for broad in CAT_ORDER:
            items = [
                (b, oc, u, s, e)
                for b, oc, u, s, e in seen_f.values()
                if b == broad
            ]
            if not items:
                continue
            f.write("# ---- " + broad + " ----\n")
            for _, _, url, status, error in items:
                if error:
                    reason = "  #" + str(status) + " " + error
                else:
                    reason = "  #HTTP" + str(status)
                f.write(url + reason + "\n")
            f.write("\n")

    # 写 CSV 报告
    with open("live_report.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["大类", "原始分类", "URL", "状态码", "响应时间(ms)", "错误"])
        sorted_results = sorted(
            results,
            key=lambda x: (
                CAT_ORDER.index(x[0]) if x[0] in CAT_ORDER else 99,
                x[4],
            ),
        )
        for broad, orig_cat, url, status, elapsed, error in sorted_results:
            w.writerow([broad, orig_cat, url, status, elapsed, error])

    print("已生成:")
    print("  live_ok.txt     <- " + str(total_ok) + " 条可用源")
    print("  live_fail.txt   <- 失效列表")
    if skipped:
        print("  skipped.txt     <- " + str(len(skipped)) + " 个跳过的源")
    print("  live_report.csv <- 检测报告")


if __name__ == "__main__":
    main()
