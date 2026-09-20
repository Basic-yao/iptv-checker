# IPTV Source Checker

自动检测 IPTV 直播源可用性，每日更新可用列表。

## 使用方法

1. Fork 本仓库
2. 编辑 `live.txt` 添加你的直播源
3. Actions 每天自动运行，或手动触发
4. 查看 `live_ok.m3u` — 直接导入 TiviMate / VLC / IPTV Smarters

## 本地运行

\`\`\`bash
pip install -r requirements.txt
python check_iptv.py
\`\`\`

## 输出文件

| 文件 | 说明 |
|------|------|
| `live_ok.m3u` | ✅ 可用源 M3U 格式，直接导入播放器 |
| `live_ok.txt` | 可用源纯文本 |
| `live_fail.txt` | 失效源 |
| `live_report.csv` | 详细检测报告 |
