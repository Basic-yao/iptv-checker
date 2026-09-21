@@ -1,8 +1,12 @@
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源检测 + txt 生成
输出: live_ok.txt / live_ok.txt / live_fail.txt / live_report.csv
IPTV 直播源检测 + txt/m3u 生成
输出:
  live_ok.txt    ← 纯 TXT（每行一个 URL）
  live_ok.m3u    ← M3U 播放列表
  live_fail.txt  ← 失效列表
  live_report.csv← 检测报告
"""

import sys
@@ -25,7 +29,6 @@
DEFAULT_TIMEOUT = 8
USER_AGENT = "Mozilla/5.0 (Linux; Android 10; TV) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

# 分类 → txt group-title 映射
CAT_MAP = {
    "中文综合聚合": "综合聚合",
    "vbskycn 镜像": "国内源",
@@ -41,6 +44,7 @@
    "未分类": "其他",
}

# ━━━ 检测函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_url(url, timeout=8):
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    parsed = urlparse(url)
@@ -49,7 +53,6 @@ def check_url(url, timeout=8):

    start = time.time()
    try:
        # HEAD first
        try:
            r = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False)
            elapsed = int((time.time() - start) * 1000)
@@ -60,7 +63,6 @@ def check_url(url, timeout=8):
        except requests.exceptions.RequestException:
            pass

        # GET fallback
        start = time.time()
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False, stream=True)
        elapsed = int((time.time() - start) * 1000)
@@ -75,6 +77,7 @@ def check_url(url, timeout=8):
        return url, 0, int((time.time() - start) * 1000), str(e)[:50]


# ━━━ 解析输入文件 ━━━━━━━━━━━━━━━━━━━━━━━━
def parse_file(filepath):
    entries = []
    current_cat = "未分类"
@@ -92,6 +95,7 @@ def parse_file(filepath):
    return entries


# ━━━ 主流程 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main():
    parser = argparse.ArgumentParser(description='IPTV Checker')
    parser.add_argument('file', nargs='?', default='live.txt')
@@ -134,7 +138,7 @@ def main():

    print(f"\n✅ 可用: {len(ok_list)}  ❌ 失效: {len(fail_list)}  📊 {len(ok_list)/total*100:.1f}%")

    # ━━━ 写 live_ok.txt ━━━
    # ━━━ 写 live_ok.txt（纯 TXT）━━━━━━━━━━━━
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
        prev_cat = None
        for cat, url, *_ in ok_list:
@@ -143,21 +147,20 @@ def main():
                prev_cat = cat
            f.write(f"{url}\n")

    # ━━━ 写 live_ok.txt（分类分组）━━━
    with open('live_ok.txt', 'w', encoding='utf-8') as f:
    # ━━━ 写 live_ok.m3u（M3U 格式）━━━━━━━━━━
    with open('live_ok.m3u', 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        prev_cat = None
        for cat, url, *_ in ok_list:
            group = CAT_MAP.get(cat, "其他")
            if cat != prev_cat:
                f.write(f"\n# ===== {cat} =====\n")
                prev_cat = cat
            # 从 URL 提取一个简短名称
            name = url.split('/')[-1].split('.')[0][:30] if '/' in url else cat
            f.write(f'#EXTINF:-1 tvg-name="{name}" group-title="{group}",{name}\n')
            f.write(f'{url}\n')

    # ━━━ 写 live_fail.txt ━━━
    # ━━━ 写 live_fail.txt ━━━━━━━━━━━━━━━━━━━
    with open('live_fail.txt', 'w', encoding='utf-8') as f:
        prev_cat = None
        for cat, url, status, _, error in fail_list:
@@ -167,18 +170,18 @@ def main():
            reason = f"  # {status} {error}" if error else f"  # HTTP {status}"
            f.write(f"{url}{reason}\n")

    # ━━━ 写 CSV ━━━
    # ━━━ 写 CSV 报告 ━━━━━━━━━━━━━━━━━━━━━━━━
    with open('live_report.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['分类', 'URL', '状态码', '响应时间(ms)', '错误'])
        for cat, url, status, elapsed, error in sorted(results, key=lambda x: (x[0], -x[2])):
            w.writerow([cat, url, status, elapsed, error])

    print(f"\n💾 已生成:")
    print(f"   live_ok.txt    ← 可直接导入播放器")
    print(f"   live_ok.txt")
    print(f"   live_fail.txt")
    print(f"   live_report.csv")
    print(f"   live_ok.txt    ← 纯 TXT（每行一个 URL）")
    print(f"   live_ok.m3u    ← M3U 播放列表")
    print(f"   live_fail.txt  ← 失效列表")
    print(f"   live_report.csv← 检测报告")


if __name__ == '__main__':
