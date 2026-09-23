# 当天刷新或发布招聘采集

按 America/Los_Angeles 日期筛选桌面详情页的“更新于”和“发布于”，任意一个为当天即收录。
当天发布但未刷新、旧帖当天刷新，都纳入；原始发布时间和刷新时间分别保留。保留原有招聘类型与置顶筛选。
先读取日期，符合条件才读取手机详情正文。单次扫描仍扫描完整分页，不因一条旧帖提前结束。

```bash
pip install requests lxml tzdata
python ChineseInLA_local_scraper_cli.py
```

在脚本目录放置 `notion_token.txt`，或设置 `NOTION_TOKEN` 环境变量。
Token 对“招聘信息监控”页面需要读取与添加内容权限。
目标页面：https://www.notion.so/3e0c18c6fba48125861cfc246bbb0092

在“招聘信息监控”下面创建日期子页面，名称为洛杉矶日期，例如 `2026-09-23`；同一天再次运行复用已有子页面。每条符合条件的记录追加到日期子页面，包含联系方式、日期、正文与原帖链接。
父页面和日期子页面的原有内容不删除；每轮首条记录前追加日期标题。重复运行可能重复追加。
成功写入 Notion 后保存到本地 CSV。无匹配记录时不修改页面，也不创建空日期页面。
跨洛杉矶午夜或 Notion 写入失败时停止，保留已写入的数据；重跑前可检查页面避免重复。

只抓取到本地、不访问 Notion：

```bash
python ChineseInLA_local_scraper_cli.py --local-only
```

验证（不访问网络、不写 Notion）：`python -m unittest -v test_today_sync`。
