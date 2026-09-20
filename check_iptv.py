#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源检测 → 纯 TXT 输出
- 按大类分组，标题格式 # ---- 分类 ----
- 大类内按响应速度排序（快→慢）
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
    print("❌ pip install requests")
    sys.exit(1)

# ━━━ 配置 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_THREADS = 20
DEFAULT_TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (Linux; Android 10; TV) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

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

# 需要跳过的 URL 关键词
SKIP_URL_KEYWORDS = [
    ".php?sub=", "/encrypt/", "/api/decrypt",
    "password=", "token=", "secret=",
]


# ━━━ 检测函数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_url(url, timeout=10):
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    parsed = urlparse(url)
    if 'migu' in parsed.netloc.lower():
        headers["Referer"] = "https://www.miguvideo.com/"

    start = time.time()
    try:
        # 先试 HEAD
        try:
            r = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False)
            elapsed = int((time.time() - start) * 1000)
            if r.status_code in (200, 206):
                return url, r.status_code, elapsed, ""
            if r.status_code == 405:
                raise requests.exceptions.RequestException("fallback")
        except requests.exceptions.RequestException:
            pass

        # HEAD 不行就 GET（stream 模式）
        start = time.time()
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, verify=False, stream=True)
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
    entries = []
    current_cat = "未分类"
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#EXTM3U'):
                continue
            # 分类标题行
            if line.startswith('#') and 'http' not in line:
                m = re.match(r'#\s*-+\s*(.+?)\s*-+\s*$', line)
                if m:
                    current_cat = m.group(1).strip()
                elif not line.startswith('#EXTINF') and not line.startswith('#EXTGRP'):
                    candidate = line.lstrip('#').strip()
                    if candidate and not candidate.startswith('EXT'):
                        current_cat = candidate
                if line.startswith('#EXTINF'):
                    m = re.search(r'group-title="([^"]+)"', line)
                    if m:
                        current_cat = m.group(1).strip()
                continue
            # URL 行
            if line.startswith('http'):
                entries.append((current_cat, line))
    return entries


# ━━━ 同源仓库提取 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def repo_key(url):
    """提取 raw.githubusercontent.com/
