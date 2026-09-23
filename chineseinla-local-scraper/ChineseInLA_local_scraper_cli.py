"""
ChineseInLA 招聘采集：收录目标日期刷新或发布的帖子；默认洛杉矶当天。

依据桌面详情页 div.post_time 的“更新于”或“发布于”日期筛选，任意一个是目标日期即收录。
默认在 Notion“招聘信息监控”下创建或复用 YYYY-MM-DD 日期子页面，逐条追加；保留本地 CSV 和旧内容。
保持原有类型筛选、网页顺序和单次扫描行为，不以旧帖提前停止分页。
重复运行可能重复追加。两个日期均非目标日期或无效的帖子跳过。
默认当天模式跨午夜停止写入；--date-page 指定日期模式始终使用参数日期。

依赖：pip install requests lxml tzdata
凭据：同目录 notion_token.txt 或 NOTION_TOKEN 环境变量，勿提交到 Git。
仅本地测试：python ChineseInLA_local_scraper_cli.py --local-only
"""
from __future__ import annotations

import argparse
import csv
import html as html_std
import json
import os
import random
import re
import time
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

try:
    import requests
except ImportError as exc:
    raise SystemExit(
        "缺少 requests。\n"
        "请在 PyCharm Terminal 运行：\n"
        "pip install requests lxml"
    ) from exc

try:
    from lxml import html
except ImportError as exc:
    raise SystemExit(
        "缺少 lxml。\n"
        "请在 PyCharm Terminal 运行：\n"
        "pip install requests lxml"
    ) from exc


# ============================================================
# 用户配置
# ============================================================

BASE_URL = "https://m.chineseinla.com"
DESKTOP_BASE_URL = "https://www.chineseinla.com"
FORUM_ID = 29

# 手机端负责论坛列表、正文和联系方式。
# 桌面端详情页只用于读取 div.post_time 中的“发布于 / 更新于”。
DESKTOP_FORUM_URL = (
    f"{DESKTOP_BASE_URL}/f/page_viewforum/"
    f"f_{FORUM_ID}.html"
)

# f_29 桌面版分页步长。
DESKTOP_PAGE_STEP = int(
    os.environ.get(
        "DESKTOP_PAGE_STEP",
        "25",
    )
)

# 为某个帖子查刷新时间时，最多向后扫描多少个桌面列表页。
DESKTOP_REFRESH_MAX_PAGES = int(
    os.environ.get(
        "DESKTOP_REFRESH_MAX_PAGES",
        "20",
    )
)

DESKTOP_REFRESH_PAGE_DELAY = float(
    os.environ.get(
        "DESKTOP_REFRESH_PAGE_DELAY",
        "0.6",
    )
)

LIST_URL = (
    f"{BASE_URL}/page_forum/task_vforum/"
    f"f_{FORUM_ID}.html"
)

PAGE_URL_TEMPLATE = (
    f"{BASE_URL}/page_forum/task_vforum/"
    f"f_{FORUM_ID}/p_{{page}}.html"
)

# 不设置帖子数量目标，也不限制扫描页数。
# 每次运行都从第一页开始，按网页顺序一直向后扫描到论坛分页末端。

# 默认只抓招聘。
# 可设置 all / 招聘 / 求职
JOB_TYPE_FILTER = os.environ.get(
    "JOBS_TYPE_FILTER",
    "招聘",
).strip()

# 是否跳过置顶
# 默认保留置顶帖；置顶帖和普通帖都按网页出现顺序写入本地。
SKIP_PINNED = False

# 本地 CSV 顺序固定为：网页按什么顺序抓到，就按什么顺序追加写入。
# 不按刷新时间或其他字段做二次排序。

# 请求间隔，避免过快访问网站
REQUEST_DELAY_MIN_SECONDS = float(
    os.environ.get(
        "REQUEST_DELAY_MIN_SECONDS",
        "1.0",
    )
)

REQUEST_DELAY_MAX_SECONDS = float(
    os.environ.get(
        "REQUEST_DELAY_MAX_SECONDS",
        "2.0",
    )
)

REQUEST_TIMEOUT_SECONDS = int(
    os.environ.get(
        "REQUEST_TIMEOUT_SECONDS",
        "25",
    )
)

STOP_AFTER_EMPTY_PAGES = int(
    os.environ.get(
        "JOBS_STOP_AFTER_EMPTY_PAGES",
        "2",
    )
)


# ============================================================
# 本地输出设置
# ============================================================

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/142.0.0.0 "
    "Safari/537.36"
)

LA_TZ = ZoneInfo(
    "America/Los_Angeles"
)

BASE_DIR = Path(__file__).resolve().parent
NOTION_PAGE_ID = "3e0c18c6-fba4-8125-861c-fc246bbb0092"


def date_matches(value: str, target: date) -> bool:
    """只接受详情页明确的日历日期，不推断相对时间。"""
    match = re.match(r"^\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?=\D|$)", value or "")
    if not match:
        return False
    try:
        return date(*(int(part) for part in match.groups())) == target
    except ValueError:
        return False


def matches_day(row: dict[str, str], target: date) -> bool:
    return any(date_matches(row.get(field, ""), target)
               for field in ("刷新时间", "发布时间"))


def load_notion_token() -> str:
    path = BASE_DIR / "notion_token.txt"
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if path.exists():
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        token = next((line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")), "")
        if token.startswith(("NOTION_TOKEN=", "NOTION_ACCESS_TOKEN=")):
            token = token.split("=", 1)[1].strip()
    token = token.strip('"').strip("'")
    if not token:
        raise RuntimeError("请设置 NOTION_TOKEN 环境变量，或在脚本目录放置 notion_token.txt")
    return token


def notion_text(text: str) -> list[dict]:
    # Notion 每段 rich_text 的 text.content 最多 2000 字符。
    return [{"type": "text", "text": {"content": text[i:i + 1800]}}
            for i in range(0, len(text), 1800)]


def recruitment_block(row: dict[str, str]) -> dict:
    fields = [f"{key}：{row.get(key) or '未采集'}" for key in OUTPUT_FIELDS
              if key not in {"标题", "正文", "详情URL"}]
    text = "\n".join(fields) + "\n\n正文：\n" + (row.get("正文") or "未采集")
    children = [{"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": notion_text(text[i:i + 1800])}}
                for i in range(0, len(text), 1800)]
    # 单次 block children 最多 100，异常超长内容明确报错，不静默截断。
    if len(children) > 99:
        raise ValueError("帖子正文超出 Notion 单条容量，停止写入以避免截断")
    url = row.get("详情URL", "")
    children.append({"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {"content": "查看原帖", "link": {"url": url}}}]}})
    return {"object": "block", "type": "toggle", "toggle": {
        "rich_text": notion_text(row.get("标题", "招聘信息")[:1800]), "children": children}}


def notion_page_id(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or not (
            parsed.hostname in {"notion.so", "notion.site", "notion.com"}
            or parsed.hostname.endswith((".notion.so", ".notion.site", ".notion.com"))
        ):
            raise ValueError("--date-page 必须是 Notion HTTPS 页面链接或页面 ID")
        value = parsed.path.rstrip("/").split("/")[-1]
    match = re.search(r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$", value)
    if not match:
        raise ValueError("无法从 --date-page 提取 Notion 页面 ID")
    return str(uuid.UUID(match.group(1)))


def page_title(page: dict) -> str:
    return "".join(t.get("plain_text", t.get("text", {}).get("content", ""))
                   for prop in page.get("properties", {}).values()
                   if prop.get("type") == "title" for t in prop.get("title", []))


def date_from_title(title: str) -> date:
    values = re.findall(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?(?!\d)", title)
    try:
        dates = {date(*(int(part) for part in value)) for value in values}
    except ValueError as exc:
        raise ValueError("参数页面标题包含无效日期") from exc
    if len(dates) != 1:
        raise ValueError("参数页面标题必须包含唯一日期，例如 2026-09-22")
    return dates.pop()


def read_date_page(value: str) -> date:
    source_id = notion_page_id(value)
    reader = NotionWriter(datetime.now(LA_TZ).date())
    try:
        page = reader.request("GET", "pages/" + source_id)
        if page.get("archived") or page.get("in_trash"):
            raise ValueError("参数页面已归档或在回收站中")
        return date_from_title(page_title(page))
    finally:
        reader.close()


class NotionWriter:
    """追加本轮结果，不删除页面现有内容。写入不自动重试以免重复追加。"""
    def __init__(self, target: date, guard_midnight=True):
        self.target = target
        self.guard_midnight = guard_midnight
        self.started_date = datetime.now(LA_TZ).date()
        self.count = 0
        self.daily_page_id = None
        self.session = requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + load_notion_token(),
                                     "Notion-Version": "2026-03-11", "Content-Type": "application/json"})

    def request(self, method, path, **kwargs):
        response = self.session.request(method, "https://api.notion.com/v1/" + path,
                                        timeout=45, **kwargs)
        if not response.ok:
            raise RuntimeError(f"Notion 请求失败 HTTP {response.status_code}；请检查页面授权及日志")
        return response.json()

    def check_page(self):
        page = self.request("GET", "pages/" + NOTION_PAGE_ID)
        title = page_title(page)
        if title != "招聘信息监控":
            raise RuntimeError("目标页面标题不匹配，拒绝写入")

    def ensure_daily_page(self):
        if self.daily_page_id:
            return self.daily_page_id
        title = self.target.isoformat()
        matches = []
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            result = self.request("GET", "blocks/" + NOTION_PAGE_ID + "/children", params=params)
            matches.extend(block["id"] for block in result.get("results", [])
                           if block.get("type") == "child_page"
                           and block.get("child_page", {}).get("title") == title
                           and not block.get("archived") and not block.get("in_trash"))
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")
            if not cursor:
                raise RuntimeError("Notion 分页缺少游标，停止以避免重复创建日期页面")
        if len(matches) > 1:
            raise RuntimeError(f"存在多个同名日期页面 {title}，请先确认保留哪一个")
        if matches:
            self.daily_page_id = matches[0]
        else:
            page = self.request("POST", "pages", json={
                "parent": {"type": "page_id", "page_id": NOTION_PAGE_ID},
                "properties": {"title": {"type": "title", "title": notion_text(title)}}})
            self.daily_page_id = page["id"]
        _log("Notion", f"日期页面：{title} | {self.daily_page_id}")
        return self.daily_page_id

    def write(self, row):
        if self.guard_midnight and datetime.now(LA_TZ).date() != self.started_date:
            raise RuntimeError("已跨过洛杉矶午夜，停止写入；请重新运行以抓取新的一天")
        if not matches_day(row, self.target):
            raise ValueError("拒绝写入非目标日期刷新或发布的帖子")
        blocks = [recruitment_block(row)]
        if self.count == 0:
            blocks.insert(0, {"object": "block", "type": "heading_2", "heading_2": {
                "rich_text": notion_text(f"刷新或发布招聘 · {self.target} · 采集于 {datetime.now(LA_TZ):%Y-%m-%d %H:%M:%S %Z}")}})
        page_id = self.ensure_daily_page()
        result = self.request("PATCH", "blocks/" + page_id + "/children", json={"children": blocks})
        if len(result.get("results", [])) != len(blocks):
            raise RuntimeError("Notion 返回的写入数量不符，请检查页面后再重试")
        self.count += 1

    def close(self):
        self.session.close()

OUTPUT_CSV = (
    BASE_DIR
    / "ChineseInLA_local_posts.csv"
)
RESULT_JSON = (
    BASE_DIR
    / "results.json"
)

# 采集停止信号。
# 纯命令行版主要通过 Ctrl+C 终止；保留该事件以兼容现有抓取循环中的停止检查。
STOP_EVENT = threading.Event()




# ============================================================
# 字段与正则
# ============================================================

OUTPUT_FIELDS = [
    "帖子ID",
    "标题",
    "置顶",
    "公司名",
    "地址",
    "联系人",
    "电话",
    "邮箱",
    "发布时间",
    "刷新时间",
    "正文",
    "详情URL",
]

TOPIC_RE = re.compile(
    r"/page_forum/task_vtopic/t_(\d+)\.html",
    re.I,
)

EMAIL_RE = re.compile(
    r"(?<![\w.+-])"
    r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})"
    r"(?![\w.-])",
    re.I,
)

PHONE_RE = re.compile(
    r"(?<!\d)"
    r"(?:\+?1[\s.()-]*)?"
    r"(?:\(?\d{3}\)?[\s.()-]*)"
    r"\d{3}[\s.-]*\d{4}"
    r"(?!\d)"
)

# 时间格式不固定，直接保存网站原文
PUBLISH_RAW_RE = re.compile(
    r"发布于\s*[:：]?\s*(.+?)"
    r"(?=\s*更新于\s*[:：]?"
    r"|\s*(?:引用|禁言|基本信息)\b|$)",
    re.I,
)

UPDATE_RAW_RE = re.compile(
    r"更新于\s*[:：]?\s*(.+?)"
    r"(?=\s*(?:引用|禁言|基本信息|类型|性质|行业|地点|"
    r"名称|公司名|地址|联系人|电话|邮箱|微信)"
    r"\s*[:：]?|$)",
    re.I,
)

# ============================================================
# 桌面端论坛列表刷新时间
#
# 参考用户提供的代码：
# topic_list_detail 整行结尾格式为：
#   更新时间  回复数  浏览数
#
# 今天通常显示：12:34 pm
# 较早帖子通常显示：2026-09-18
# ============================================================

DESKTOP_TIME_PATTERN = (
    r"(?:1[0-2]|0?[1-9]):"
    r"[0-5]\d\s*"
    r"(?:am|pm)"
)

DESKTOP_DATE_PATTERN = (
    r"20\d{2}-\d{2}-\d{2}"
)

DESKTOP_ROW_META_RE = re.compile(
    rf"(?P<updated>"
    rf"{DESKTOP_TIME_PATTERN}|"
    rf"{DESKTOP_DATE_PATTERN}"
    rf")"
    rf"\s+"
    rf"(?P<replies>[\d,]+)"
    rf"\s+"
    rf"(?P<views>[\d,]+)"
    rf"\s*$",
    re.I,
)

DESKTOP_TOPIC_RE = re.compile(
    r"/f/page_viewtopic/t_(\d+)\.html",
    re.I,
)


REFRESH_TIME_RE = re.compile(
    r"(?:刚刚"
    r"|\d+\s*(?:秒|分钟|小时|天|周|个月|月|年)前"
    r"|今天(?:\s+\d{1,2}:\d{2})?"
    r"|昨天(?:\s+\d{1,2}:\d{2})?"
    r"|前天(?:\s+\d{1,2}:\d{2})?"
    r"|20\d{2}[/-]\d{1,2}[/-]\d{1,2}"
    r"(?:\s+\d{1,2}:\d{2}(?:\s*(?:am|pm))?)?"
    r"|\d{1,2}[/-]\d{1,2}"
    r"(?:\s+\d{1,2}:\d{2}(?:\s*(?:am|pm))?)?"
    r"|\d{1,2}:\d{2}\s*(?:am|pm)?)",
    re.I,
)

LABELS = [
    "类型",
    "性质",
    "行业",
    "地点",
    "名称",
    "公司名",
    "地址",
    "联系人",
    "电话",
    "邮箱",
    "微信",
]



# ============================================================
# 通用工具
# ============================================================

def clean(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_space(text: str) -> str:
    return re.sub(
        r"[ \t\xa0]+",
        " ",
        html_std.unescape(
            text or ""
        ),
    ).strip()


def clean_lines(text: str) -> list[str]:
    out: list[str] = []

    for raw in (
        text or ""
    ).replace(
        "\r",
        "\n",
    ).split(
        "\n"
    ):
        line = normalize_space(
            raw
        )

        if line:
            out.append(line)

    return out


def dedupe(
    values: list[str],
    lower: bool = False,
) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    for value in values:
        value = normalize_space(
            value
        )

        if not value:
            continue

        key = (
            value.lower()
            if lower
            else value
        )

        if key in seen:
            continue

        seen.add(key)
        out.append(value)

    return out


def join_values(
    values: list[str],
    lower: bool = False,
) -> str:
    return "; ".join(
        dedupe(
            values,
            lower=lower,
        )
    )


def sleep_between_requests() -> None:
    low = min(
        REQUEST_DELAY_MIN_SECONDS,
        REQUEST_DELAY_MAX_SECONDS,
    )

    high = max(
        REQUEST_DELAY_MIN_SECONDS,
        REQUEST_DELAY_MAX_SECONDS,
    )

    time.sleep(
        random.uniform(
            low,
            high,
        )
    )


def page_url(
    page: int,
) -> str:
    if page <= 1:
        return LIST_URL

    return PAGE_URL_TEMPLATE.format(
        page=page
    )


# ============================================================
# 手机端 HTTP
# ============================================================

def fetch_html(
    url: str,
) -> str:
    request = Request(
        url,
        headers={
            "User-Agent":
                MOBILE_USER_AGENT,

            "Accept":
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8",

            "Accept-Language":
                "zh-CN,zh;q=0.9,en;q=0.7",

            "Referer":
                BASE_URL + "/",

            "Connection":
                "close",
        },
    )

    sleep_between_requests()

    with urlopen(
        request,
        timeout=REQUEST_TIMEOUT_SECONDS,
    ) as response:
        raw = response.read()

        charset = (
            response.headers
            .get_content_charset()
            or "utf-8"
        )

    try:
        return raw.decode(
            charset,
            errors="replace",
        )

    except LookupError:
        return raw.decode(
            "utf-8",
            errors="replace",
        )


# ============================================================
# 桌面端详情页 -> 发布时间 / 刷新时间
# ============================================================

def desktop_topic_url(post_id: str) -> str:
    """根据帖子ID生成桌面版详情页 URL。"""
    post_id = clean(post_id)
    if not post_id:
        return ""

    return (
        f"{DESKTOP_BASE_URL}"
        f"/f/page_viewtopic/t_{post_id}.html"
    )


def fetch_desktop_topic_html(url: str) -> str:
    """读取桌面版帖子详情页。"""
    response = desktop_session.get(
        url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        allow_redirects=True,
    )
    response.raise_for_status()

    if (
        not response.encoding
        or response.encoding.lower() == "iso-8859-1"
    ):
        response.encoding = response.apparent_encoding

    return response.text


def extract_desktop_post_times(page_text: str) -> tuple[str, str]:
    """
    从桌面版详情页的 div.post_time 读取：

        <div class="post_time">
            <span>发布于：2025/03/20 11:19 am</span>
            <span>更新于：2026/09/22, 8:12 am</span>
        </div>

    返回 (发布时间, 刷新时间)。

    有些帖子从未重新发布/刷新，因此页面只有“发布于”，
    没有“更新于”。这种情况刷新时间必须保持空字符串，
    不用发布时间补，也不从其他页面兜底。
    """
    doc = html.fromstring(page_text)

    nodes = doc.xpath(
        '//div['
        'contains('
        'concat(" ", normalize-space(@class), " "), '
        '" post_time "'
        ')'
        ']'
    )

    if not nodes:
        return "", ""

    container = nodes[0]
    values = [
        normalize_space(" ".join(node.itertext()))
        for node in container.xpath('.//span')
    ]

    # 兼容站点以后改变 span 结构：如果没有 span，
    # 就退回读取 post_time 容器自身的可见文本行。
    if not values:
        values = clean_lines(container.text_content())

    publish_time = ""
    refresh_time = ""

    for value in values:
        if not value:
            continue

        publish_match = re.match(
            r'^\s*发布于\s*[:：]?\s*(.*?)\s*$',
            value,
            re.I,
        )
        if publish_match and not publish_time:
            publish_time = normalize_space(publish_match.group(1))
            continue

        refresh_match = re.match(
            r'^\s*更新于\s*[:：]?\s*(.*?)\s*$',
            value,
            re.I,
        )
        if refresh_match and not refresh_time:
            refresh_time = normalize_space(refresh_match.group(1))

    return publish_time, refresh_time


def get_desktop_post_times(post_id: str) -> tuple[str, str]:
    """读取某个新帖桌面详情页的发布时间和刷新时间。"""
    url = desktop_topic_url(post_id)
    if not url:
        return "", ""

    try:
        page_text = fetch_desktop_topic_html(url)
        return extract_desktop_post_times(page_text)
    except (
        requests.RequestException,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        print(
            "  -> 桌面详情页时间读取失败："
            f"{type(exc).__name__}: {exc}"
        )
        return "", ""


# ============================================================
# 桌面端列表 -> 刷新时间
# ============================================================

desktop_session = requests.Session()

desktop_session.headers.update(
    {
        "User-Agent":
            DESKTOP_USER_AGENT,

        "Accept":
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8",

        "Accept-Language":
            "zh-CN,zh;q=0.9,en;q=0.8",

        "Cache-Control":
            "no-cache",

        "Pragma":
            "no-cache",

        "Connection":
            "keep-alive",
    }
)

# 已加载的桌面列表刷新时间：
# 帖子ID -> 网站列表原始刷新时间
DESKTOP_REFRESH_CACHE: dict[
    str,
    str
] = {}

# 下一次需要加载的桌面分页索引：
# 0 = 第一页
# 1 = start_25
# 2 = start_50
DESKTOP_REFRESH_NEXT_PAGE = 0

# 一旦桌面请求失败，本次运行不再持续重试，
# 后续帖子直接回退手机详情页刷新时间。
DESKTOP_REFRESH_DISABLED = False


def desktop_forum_page_url(
    page_index: int,
) -> str:
    if page_index <= 0:
        return DESKTOP_FORUM_URL

    offset = (
        page_index
        * DESKTOP_PAGE_STEP
    )

    return (
        f"{DESKTOP_BASE_URL}"
        f"/f/page_viewforum/"
        f"f_{FORUM_ID}"
        f"/start_{offset}.html"
    )


def fetch_desktop_forum_html(
    url: str,
) -> str:
    response = desktop_session.get(
        url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        allow_redirects=True,
    )

    response.raise_for_status()

    if (
        not response.encoding
        or response.encoding.lower()
        == "iso-8859-1"
    ):
        response.encoding = (
            response.apparent_encoding
        )

    return response.text


def extract_desktop_refresh_map(
    page_text: str,
) -> dict[
    str,
    str
]:
    """
    参照用户提供的代码：

    1. 读取 div.topic_list_detail
    2. 找 page_viewtopic 链接取得帖子ID
    3. 取整行文字
    4. 用 DESKTOP_ROW_META_RE 从行尾解析 updated

    不保存回复数、浏览数，只利用它们定位刷新时间。
    """
    doc = html.fromstring(
        page_text
    )

    elements = doc.xpath(
        '//div['
        'contains('
        'concat(" ", normalize-space(@class), " "), '
        '" topic_list_detail "'
        ')'
        ']'
    )

    result: dict[
        str,
        str
    ] = {}

    for row in elements:
        title_links = row.xpath(
            './/a['
            'contains('
            'concat(" ", normalize-space(@class), " "), '
            '" title "'
            ') '
            'and contains(@href, "page_viewtopic")'
            ']'
        )

        if title_links:
            topic_link = title_links[0]
        else:
            fallback_links = row.xpath(
                './/a['
                'contains(@href, "page_viewtopic")'
                ']'
            )

            if not fallback_links:
                continue

            topic_link = fallback_links[0]

        href = clean(
            topic_link.get(
                "href"
            )
        )

        topic_match = (
            DESKTOP_TOPIC_RE.search(
                href
            )
        )

        if not topic_match:
            continue

        post_id = topic_match.group(1)

        row_text = re.sub(
            r"\s+",
            " ",
            html_std.unescape(
                " ".join(
                    row.itertext()
                )
            ),
        ).strip()

        meta_match = (
            DESKTOP_ROW_META_RE.search(
                row_text
            )
        )

        if not meta_match:
            continue

        updated = clean(
            meta_match.group(
                "updated"
            )
        )

        if updated:
            # 同一帖子可能因置顶/推荐重复出现。
            # 刷新时间相同的情况下保留第一次即可。
            result.setdefault(
                post_id,
                updated,
            )

    return result


def get_desktop_refresh_time(
    post_id: str,
) -> str:
    """
    按帖子ID从桌面论坛列表获取刷新时间。

    为减少请求：
    - 已读取的页面结果永久放在本次运行缓存中。
    - 只有目标帖子尚未出现在缓存里时，才继续读下一页。
    - 最多读取 DESKTOP_REFRESH_MAX_PAGES 页。
    - 桌面端失败时不影响主任务，返回空值，由手机详情页兜底。
    """
    global DESKTOP_REFRESH_NEXT_PAGE
    global DESKTOP_REFRESH_DISABLED

    post_id = clean(
        post_id
    )

    if not post_id:
        return ""

    cached = DESKTOP_REFRESH_CACHE.get(
        post_id
    )

    if cached:
        return cached

    if DESKTOP_REFRESH_DISABLED:
        return ""

    while (
        DESKTOP_REFRESH_NEXT_PAGE
        < DESKTOP_REFRESH_MAX_PAGES
        and not STOP_EVENT.is_set()
    ):
        page_index = (
            DESKTOP_REFRESH_NEXT_PAGE
        )

        url = desktop_forum_page_url(
            page_index
        )

        try:
            page_text = (
                fetch_desktop_forum_html(
                    url
                )
            )

            page_map = (
                extract_desktop_refresh_map(
                    page_text
                )
            )

        except (
            requests.RequestException,
            OSError,
            ValueError,
            TypeError,
        ) as exc:
            DESKTOP_REFRESH_DISABLED = True

            print(
                "  -> 桌面刷新时间读取失败，"
                "本次运行改用手机详情页兜底："
                f"{type(exc).__name__}: {exc}"
            )

            return ""

        DESKTOP_REFRESH_CACHE.update(
            page_map
        )

        DESKTOP_REFRESH_NEXT_PAGE += 1

        print(
            "  -> 桌面刷新时间索引："
            f"第 {page_index + 1} 页，"
            f"本页解析 {len(page_map)} 条，"
            f"累计 {len(DESKTOP_REFRESH_CACHE)} 条"
        )

        cached = (
            DESKTOP_REFRESH_CACHE.get(
                post_id
            )
        )

        if cached:
            return cached

        time.sleep(
            max(
                0.0,
                DESKTOP_REFRESH_PAGE_DELAY,
            )
        )

    return ""


# ============================================================
# 列表页解析
# ============================================================

def clean_topic_title(
    anchor,
) -> str:
    lines = clean_lines(
        anchor.text_content()
    )

    if not lines:
        return ""

    title = lines[0]

    # 如果列表时间被塞在 a 标签末尾，则去掉时间部分
    matches = list(
        REFRESH_TIME_RE.finditer(
            title
        )
    )

    if (
        matches
        and
        matches[-1].end()
        == len(title)
    ):
        prefix = normalize_space(
            title[
                :matches[-1].start()
            ]
        )

        if prefix:
            title = prefix

    return normalize_space(
        title
    )


def topic_is_pinned(
    anchor,
    title: str,
) -> bool:
    """
    判断手机端列表中的帖子是否为置顶帖。

    手机端使用可见文字“置顶”作为标记。
    这里不再只检查标题前方，而是：

    1. 先检查当前标题链接自身；
    2. 再逐级向上寻找“只包含当前这一条 topic 链接”的最近容器；
    3. 只在该单帖容器中检查独立的“置顶”文字。

    这样既能识别“标题 -> 置顶 -> 时间”的结构，
    又尽量避免父容器中其他帖子出现“置顶”造成误判。
    """

    def has_pinned_text(node) -> bool:
        lines = clean_lines(
            node.text_content()
        )

        # 最可靠：页面把“置顶”作为独立文字节点/独立文本行。
        if any(
            line.strip() == "置顶"
            for line in lines
        ):
            return True

        # 兼容网站把“置顶”和时间压到同一文本行的情况。
        text = " ".join(lines)
        return bool(
            re.search(
                r"(?:^|\s)置顶(?=\s|$)",
                text,
            )
        )

    # 某些列表结构会把“置顶”直接放进标题 a 标签。
    if has_pinned_text(anchor):
        return True

    current_href = clean(
        anchor.get("href")
    )

    current_match = TOPIC_RE.search(
        current_href
    )

    current_id = (
        current_match.group(1)
        if current_match
        else ""
    )

    node = anchor

    for _ in range(8):
        parent = node.getparent()

        if parent is None:
            break

        topic_ids: set[str] = set()

        for link in parent.xpath(
            './/a[@href]'
        ):
            href = clean(
                link.get("href")
            )

            match = TOPIC_RE.search(
                href
            )

            if match:
                topic_ids.add(
                    match.group(1)
                )

        # 最近的“单帖容器”才允许用来判断置顶。
        # 一旦父级开始同时包含多条帖子，就停止继续向上，
        # 防止其他置顶帖污染当前帖子。
        if len(topic_ids) > 1:
            break

        if (
            not topic_ids
            or not current_id
            or current_id in topic_ids
        ):
            if has_pinned_text(parent):
                return True

        node = parent

    return False


def extract_topic_links(
    doc,
    list_url: str,
) -> list[dict[str, str]]:
    topics: dict[
        str,
        dict[str, str]
    ] = {}

    for anchor in doc.xpath(
        "//a[@href]"
    ):
        href = (
            anchor.get("href")
            or ""
        ).strip()

        match = TOPIC_RE.search(
            href
        )

        if not match:
            continue

        title = clean_topic_title(
            anchor
        )

        if not title:
            continue

        is_pinned = topic_is_pinned(
            anchor,
            title,
        )

        if (
            SKIP_PINNED
            and is_pinned
        ):
            continue

        url = urljoin(
            list_url,
            href,
        )

        topic_id = match.group(1)

        item = {
            "帖子ID":
                topic_id,

            "标题":
                title,

            # 仅用于折叠标题显示和 CSV 留档；
            # 本地 CSV 通过“置顶”字段保存该状态。
            "置顶":
                ("是" if is_pinned else ""),

            "详情URL":
                url,
        }

        prev = topics.get(
            url
        )

        if (
            prev is None
            or len(title)
            > len(
                prev.get(
                    "标题",
                    "",
                )
            )
        ):
            topics[url] = item

    return list(
        topics.values()
    )


# ============================================================
# 详情页解析
# ============================================================

def extract_labeled_value(
    lines: list[str],
    label: str,
) -> str:
    label_re = re.compile(
        rf"^{re.escape(label)}"
        rf"\s*[:：]?\s*(.*)$",
        re.I,
    )

    for i, line in enumerate(
        lines
    ):
        match = label_re.match(
            line
        )

        if not match:
            continue

        value = normalize_space(
            match.group(1)
        )

        if value:
            return value

        if i + 1 < len(lines):
            nxt = lines[
                i + 1
            ]

            is_another_label = any(
                re.match(
                    rf"^{re.escape(other)}"
                    rf"\s*[:：]?",
                    nxt,
                    re.I,
                )
                for other in LABELS
            )

            if not is_another_label:
                return nxt

    return ""


def extract_title(
    doc,
    fallback: str = "",
) -> str:
    candidates = doc.xpath(
        "//h1|//h2|//title"
    )

    for node in candidates:
        text = normalize_space(
            node.text_content()
        )

        if not text:
            continue

        text = re.sub(
            r"\s*[-–—]\s*"
            r"洛杉矶华人资讯网.*$",
            "",
            text,
        ).strip()

        if text and text not in {
            "工作求职",
            "洛杉矶华人资讯网",
        }:
            return text

    return fallback


def decode_cloudflare_emails(
    doc,
) -> None:
    """
    ChineseInLA 某些邮箱可能被 Cloudflare Email Protection
    编码在 data-cfemail 中。

    这里在解析标签文字前先恢复成用户页面上实际看到的邮箱。
    """
    nodes = doc.xpath(
        '//*[@data-cfemail]'
    )

    for node in nodes:
        raw = (
            node.get(
                "data-cfemail"
            )
            or ""
        ).strip()

        if not raw:
            continue

        try:
            data = bytes.fromhex(
                raw
            )

            if len(data) < 2:
                continue

            key = data[0]

            decoded = bytes(
                value ^ key
                for value
                in data[1:]
            ).decode(
                "utf-8",
                "replace",
            )

            # 清掉 Cloudflare 默认占位文字，
            # 改成真正邮箱。
            node.text = decoded

            for child in list(node):
                child.tail = ""

        except (
            ValueError,
            UnicodeDecodeError,
        ):
            continue


def visible_text_lines(
    doc,
) -> list[str]:
    """
    只读取真正可见文本。

    排除：
    - script
    - style
    - noscript
    - template
    - svg

    这样不会把 JSON-LD、JavaScript、
    页面变量等内容混入正文或字段。
    """
    texts = doc.xpath(
        "//text()["
        "not(ancestor::script) and "
        "not(ancestor::style) and "
        "not(ancestor::noscript) and "
        "not(ancestor::template) and "
        "not(ancestor::svg)"
        "]"
    )

    return [
        normalize_space(
            value
        )
        for value
        in texts
        if normalize_space(
            value
        )
    ]


def detail_content_lines(
    doc,
    title: str,
) -> list[str]:
    """
    读取详情页中与帖子有关的可见文本，
    供发布时间、类型、公司名、地址、联系人、
    电话、邮箱等字段解析使用。

    注意：
    这不是“正文”来源。
    正文仍然只读取 div.topic_text。
    """
    texts = doc.xpath(
        "//text()["
        "not(ancestor::script) and "
        "not(ancestor::style) and "
        "not(ancestor::noscript) and "
        "not(ancestor::template) and "
        "not(ancestor::svg)"
        "]"
    )

    lines = [
        normalize_space(value)
        for value in texts
        if normalize_space(value)
    ]

    if not lines:
        return []

    start = 0

    if title:
        for i, line in enumerate(lines):
            if (
                line == title
                or (
                    len(title) >= 8
                    and title in line
                )
            ):
                start = i
                break

    if start == 0:
        for i, line in enumerate(lines):
            if line.startswith("发布于"):
                start = max(0, i - 1)
                break

    stop_prefixes = (
        "联系时请一定说明是在洛杉矶华人资讯网看到的",
        "进入人才库",
        "投递简历",
        "标签:",
        "标签：",
        "相关标签",
        "猜你喜欢",
        "热门评论",
        "最新评论",
        "相关主题",
        "返回页首",
        "举报",
        "打开华人资讯APP",
        "Advertiser Disclosure",
        "Copyright",
        "©",
    )

    out: list[str] = []

    for line in lines[start:]:
        if (
            out
            and any(
                line.startswith(prefix)
                for prefix in stop_prefixes
            )
        ):
            break

        out.append(line)

    return out


def extract_body_text(
    doc,
    title: str,
) -> str:
    """
    正文只读取手机端真正的帖子正文容器：

        div.topic_text

    不使用任何其他 DOM、桌面端正文或整页文本兜底。
    如果页面没有 topic_text，则正文返回空字符串。

    只排除 script/style/noscript/template/svg，
    其余内容保持为 topic_text 容器中的原始可见文字。
    """
    nodes = doc.xpath(
        '//div['
        'contains('
        'concat(" ", normalize-space(@class), " "), '
        '" topic_text "'
        ')'
        ']'
    )

    if not nodes:
        return ""

    node = nodes[0]

    texts = node.xpath(
        ".//text()["
        "not(ancestor::script) and "
        "not(ancestor::style) and "
        "not(ancestor::noscript) and "
        "not(ancestor::template) and "
        "not(ancestor::svg)"
        "]"
    )

    lines: list[str] = []

    for raw in texts:
        value = normalize_space(
            raw
        )

        if value:
            lines.append(
                value
            )

    return "\n".join(
        lines
    )[:50000]


def extract_contacts(
    lines: list[str],
) -> tuple[str, str]:
    """
    联系方式提取规则：

    电话：
        只读取“电话”标签对应的文字。
        不扫描正文，不扫描整页。

    邮箱：
        只读取“邮箱”标签对应的文字。
        不扫描正文，不扫描整页。

    本脚本不抓取、不保存微信字段。
    """

    explicit_phone = extract_labeled_value(
        lines,
        "电话",
    )

    phones: list[str] = []

    if explicit_phone:
        phones.extend(
            PHONE_RE.findall(
                explicit_phone
            )
        )

    explicit_email = extract_labeled_value(
        lines,
        "邮箱",
    )

    emails: list[str] = []

    if explicit_email:
        emails.extend(
            EMAIL_RE.findall(
                explicit_email
            )
        )

    return (
        join_values(phones),
        join_values(
            emails,
            lower=True,
        ),
    )


def _cut_time_tail(
    raw: str,
    stop_tokens: tuple[str, ...],
) -> str:
    """
    清理“发布于/更新于”后面的内容。

    ChineseInLA 有时会把：
        时间 + 回复 + 收藏 + 点赞 + 基本信息...
    压在同一文本行。

    这里只保留时间本身；
    一旦遇到 UI 操作词或下一个字段标签就截断。
    """
    raw = normalize_space(
        raw
    )

    if not raw:
        return ""

    positions: list[int] = []

    for token in stop_tokens:
        match = re.search(
            rf"\s+{re.escape(token)}"
            rf"(?:\s*[:：])?",
            raw,
            re.I,
        )

        if match:
            positions.append(
                match.start()
            )

    if positions:
        raw = raw[
            :min(positions)
        ]

    return (
        normalize_space(
            raw
        )
        .strip(
            " ,，;；|-·"
        )
    )


def _extract_raw_after_label_from_lines(
    lines: list[str],
    label: str,
    stop_tokens: tuple[str, ...],
) -> str:
    """
    逐行读取“发布于/更新于”后的原始值。

    不再把前几十行拼成一个长字符串，
    从源头避免把后面的页面内容吞进时间字段。
    """
    label_re = re.compile(
        rf"{re.escape(label)}"
        rf"\s*[:：]?\s*",
        re.I,
    )

    limit = min(
        len(lines),
        80,
    )

    for i, line in enumerate(
        lines[:limit]
    ):
        match = label_re.search(
            line
        )

        if not match:
            continue

        # 标签和值在同一行
        tail = line[
            match.end():
        ]

        value = _cut_time_tail(
            tail,
            stop_tokens,
        )

        if value:
            return value

        # 极少数 DOM：
        # “发布于”单独一行，真正时间在下一行
        if i + 1 < limit:
            value = _cut_time_tail(
                lines[
                    i + 1
                ],
                stop_tokens,
            )

            if value:
                return value

    return ""


def extract_post_times(
    lines: list[str],
) -> tuple[str, str]:
    """
    发布时间：
        逐行读取“发布于”，并在页面操作词/字段标签处截断，
        防止混入“回复、收藏、点赞、基本信息”等内容。

    手机详情刷新时间：
        同样只读取“更新于”的干净值；
        当前主流程仍然优先使用桌面论坛列表的刷新时间。
    """
    publish_stops = (
        "更新于",
        "引用",
        "禁言",
        "回复",
        "收藏",
        "点赞",
        "基本信息",
        "类型",
        "性质",
        "行业",
        "地点",
        "名称",
        "公司名",
        "地址",
        "联系人",
        "电话",
        "邮箱",
        "微信",
        "详细描述",
        "查找类似信息",
        "申请职位",
        "联系方式",
    )

    refresh_stops = (
        "引用",
        "禁言",
        "回复",
        "收藏",
        "点赞",
        "基本信息",
        "类型",
        "性质",
        "行业",
        "地点",
        "名称",
        "公司名",
        "地址",
        "联系人",
        "电话",
        "邮箱",
        "微信",
        "详细描述",
        "查找类似信息",
        "申请职位",
        "联系方式",
    )

    publish = (
        _extract_raw_after_label_from_lines(
            lines,
            "发布于",
            publish_stops,
        )
    )

    refresh = (
        _extract_raw_after_label_from_lines(
            lines,
            "更新于",
            refresh_stops,
        )
    )

    return (
        publish,
        refresh,
    )


def clean_publish_time_value(
    value: str,
) -> str:
    """
    发布时间最终保险清理。

    正常值可能是：
        2026/09/19, 12:35 pm
        2026/09/19 12:35 pm
        2026-09-19 12:35 pm
        9:35 am
        昨天 ...

    不强制改成固定格式，只负责去掉明显页面污染。
    """
    value = normalize_space(
        value
    )

    if not value:
        return ""

    bad_tokens = (
        "回复",
        "收藏",
        "点赞",
        "基本信息",
        "类型",
        "性质",
        "行业",
        "地点",
        "名称",
        "公司名",
        "地址",
        "联系人",
        "电话",
        "邮箱",
        "微信",
        "详细描述",
        "查找类似信息",
        "申请职位",
        "联系方式",
    )

    positions: list[int] = []

    for token in bad_tokens:
        pos = value.find(
            token
        )

        if pos >= 0:
            positions.append(
                pos
            )

    if positions:
        value = value[
            :min(positions)
        ]

    return (
        normalize_space(
            value
        )
        .strip(
            " ,，;；|-·"
        )
    )


def type_allowed(
    job_type: str,
) -> bool:
    filt = JOB_TYPE_FILTER.strip()

    if filt.lower() in {
        "",
        "all",
        "全部",
    }:
        return True

    actual = (
        job_type
        .strip()
    )

    # 类型缺失时不直接扔掉，防止旧帖结构异常造成误删。
    if not actual:
        return True

    return (
        actual.lower()
        == filt.lower()
    )


def process_topic(
    item: dict[str, str],
    target_date=None,
) -> tuple[
    dict[str, str],
    str,
]:
    url = item["详情URL"]

    publish_time, refresh_time = get_desktop_post_times(item.get("帖子ID", ""))
    # 先验证刷新或发布日期，旧帖不再请求手机详情页或解析正文。
    if target_date is not None and not matches_day({"刷新时间": refresh_time, "发布时间": publish_time}, target_date):
        return {**item, "发布时间": publish_time, "刷新时间": refresh_time}, ""

    page = fetch_html(
        url
    )

    doc = html.fromstring(
        page
    )

    # 先解码网页中被 Cloudflare 保护的邮箱，
    # 再进行“邮箱”标签文字解析。
    decode_cloudflare_emails(
        doc
    )

    title = extract_title(
        doc,
        fallback=item.get(
            "标题",
            "",
        ),
    )

    lines = detail_content_lines(
        doc,
        title,
    )

    body = extract_body_text(
        doc,
        title,
    )

    # 此处只负责采集字段，不直接输出字段日志。
    # 成功写入后由 crawl_live() 统一输出完整字段采集报告；
    # 正文只报告是否采集到，不打印正文内容。

    # 发布时间 / 刷新时间统一直接读取桌面版详情页 div.post_time。
    # 注意：只发布过一次、从未重新发布/刷新过的帖子没有“更新于”，
    # 此时 refresh_time 保持空字符串，不使用发布时间或其他来源补值。

    phone, email = extract_contacts(
        lines,
    )

    job_type = extract_labeled_value(
        lines,
        "类型",
    )

    row = {
        "帖子ID":
            item.get(
                "帖子ID",
                "",
            ),

        "标题":
            title,

        "置顶":
            item.get(
                "置顶",
                "",
            ),

        "公司名":
            (
                extract_labeled_value(
                    lines,
                    "公司名",
                )
                or
                extract_labeled_value(
                    lines,
                    "名称",
                )
            ),

        "地址":
            extract_labeled_value(
                lines,
                "地址",
            ),

        "联系人":
            extract_labeled_value(
                lines,
                "联系人",
            ),

        "电话":
            phone,

        "邮箱":
            email,


        "发布时间":
            publish_time,

        "刷新时间":
            refresh_time,

        "正文":
            body,

        "详情URL":
            url,
    }

    return (
        row,
        job_type,
    )




# ============================================================
# 本地 CSV / 运行结果
# ============================================================

def ensure_csv_header() -> None:
    """确保 CSV 存在并包含表头；不会清空已有数据。"""
    if OUTPUT_CSV.exists() and OUTPUT_CSV.stat().st_size > 0:
        return

    with OUTPUT_CSV.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=OUTPUT_FIELDS,
        )
        writer.writeheader()


def append_csv_row(row: dict[str, str]) -> None:
    """
    立即把一条记录追加到本地 CSV。

    不做任何去重；调用顺序就是 CSV 中的保存顺序。
    """
    ensure_csv_header()

    with OUTPUT_CSV.open(
        "a",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=OUTPUT_FIELDS,
        )
        writer.writerow(
            {
                field: row.get(field, "")
                for field in OUTPUT_FIELDS
            }
        )
        f.flush()


# ============================================================
# 实时抓取：纯本地、无历史去重
# ============================================================

def _log(tag: str, message: str) -> None:
    """统一日志格式：时间 + 类型 + 内容。"""
    now = datetime.now(LA_TZ).strftime("%H:%M:%S")
    print(f"[{now}] [{tag}] {message}")


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}小时 {minutes}分 {secs}秒"
    if minutes:
        return f"{minutes}分 {secs}秒"
    return f"{secs}秒"


def _short_text(value: str, limit: int = 46) -> str:
    value = normalize_space(value)
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"


def _field_log_value(value: object) -> str:
    """日志字段显示：有值显示原值，无值统一显示“未采集”。"""
    value = clean(value)
    return value if value else "未采集"


def _log_captured_row(
    row: dict[str, str],
    *,
    sequence: int,
) -> None:
    """
    输出一条成功写入记录的完整字段采集报告。

    正文内容绝不写入日志，只报告是否成功采集到正文。
    """
    body_status = "已采集" if clean(row.get("正文")) else "未采集"
    pinned_status = "是" if clean(row.get("置顶")) == "是" else "否"

    _log("写入", f"#{sequence} ─────────────────────────────────────")
    print(f"    帖子ID   ：{_field_log_value(row.get('帖子ID'))}")
    print(f"    标题     ：{_field_log_value(row.get('标题'))}")
    print(f"    置顶     ：{pinned_status}")
    print(f"    公司名   ：{_field_log_value(row.get('公司名'))}")
    print(f"    地址     ：{_field_log_value(row.get('地址'))}")
    print(f"    联系人   ：{_field_log_value(row.get('联系人'))}")
    print(f"    电话     ：{_field_log_value(row.get('电话'))}")
    print(f"    邮箱     ：{_field_log_value(row.get('邮箱'))}")
    print(f"    发布时间 ：{_field_log_value(row.get('发布时间'))}")
    print(f"    刷新时间 ：{_field_log_value(row.get('刷新时间'))}")
    print(f"    正文     ：{body_status}")
    print(f"    详情URL  ：{_field_log_value(row.get('详情URL'))}")


def crawl_live(target_date=None, writer=None) -> tuple[list[dict[str, str]], dict[str, object]]:
    """
    每次运行从论坛第一页开始，按网页列表实际顺序抓取。

    日志原则：
    - 页面：输出开始/完成摘要；
    - 成功：每条帖子逐字段报告实际采集内容；
    - 正文：不输出正文内容，只报告“已采集 / 未采集”；
    - 空字段：统一显示“未采集”；
    - 跳过/失败：单独标记，便于搜索；
    - 结束：由 run() 输出统一运行报告。
    """
    target_date = target_date or datetime.now(LA_TZ).date()
    rows: list[dict[str, str]] = []

    stats: dict[str, object] = {
        "written": 0,
        "detail_failed": 0,
        "list_failed": 0,
        "type_skipped": 0,
        "date_skipped": 0,
        "invalid": 0,
        "pinned_found": 0,
        "normal_found": 0,
        "topics_seen": 0,
        "pages_scanned": 0,
        "forum_exhausted": 0,
        "repeated_page_stop": 0,
        "stop_reason": "",
    }

    page = 1
    consecutive_empty = 0
    stop_reason = ""
    seen_page_signatures: set[tuple[str, ...]] = set()

    ensure_csv_header()

    while not STOP_EVENT.is_set():
        url = page_url(page)
        stats["pages_scanned"] = page
        _log("页面", f"第 {page} 页 | 正在读取列表")

        page_written = 0
        page_skipped = 0
        page_failed = 0
        page_invalid = 0

        try:
            doc = html.fromstring(fetch_html(url))
            topics = extract_topic_links(doc, url)
        except (
            HTTPError,
            URLError,
            OSError,
            ValueError,
        ) as exc:
            stats["list_failed"] = int(stats["list_failed"]) + 1
            page_failed += 1
            _log(
                "警告",
                f"第 {page} 页列表读取失败 | {type(exc).__name__}: {exc}",
            )
            page += 1
            consecutive_empty += 1

            if consecutive_empty >= STOP_AFTER_EMPTY_PAGES:
                stop_reason = "consecutive_empty_or_failed"
                stats["forum_exhausted"] = 1
                break
            continue

        stats["topics_seen"] = int(stats["topics_seen"]) + len(topics)

        if not topics:
            consecutive_empty += 1
            _log(
                "页面",
                f"第 {page} 页 | 0 条 | 连续空页 {consecutive_empty}/{STOP_AFTER_EMPTY_PAGES}",
            )
            if consecutive_empty >= STOP_AFTER_EMPTY_PAGES:
                stop_reason = "forum_end"
                stats["forum_exhausted"] = 1
                break
            page += 1
            continue

        # 只用于避免分页地址异常时无限重复同一整页。
        # 不会过滤页内帖子，也不会与历史 CSV 比较。
        page_signature = tuple(clean(item.get("帖子ID")) for item in topics)
        if page_signature in seen_page_signatures:
            stop_reason = "repeated_page"
            stats["forum_exhausted"] = 1
            stats["repeated_page_stop"] = 1
            _log(
                "警告",
                f"第 {page} 页与之前页面完全重复 | 为避免分页死循环，停止继续翻页",
            )
            break

        seen_page_signatures.add(page_signature)
        consecutive_empty = 0

        _log("页面", f"第 {page} 页 | 发现 {len(topics)} 条帖子")

        for item in topics:
            if STOP_EVENT.is_set():
                stop_reason = "user_stop"
                _log("停止", "收到停止请求，当前已写入的数据会保留")
                break

            post_id = clean(item.get("帖子ID"))
            title = clean(item.get("标题"))

            if not post_id:
                stats["invalid"] = int(stats["invalid"]) + 1
                page_invalid += 1
                _log("跳过", f"第 {page} 页 | 缺少帖子 ID | {_short_text(title)}")
                continue

            item_is_pinned = clean(item.get("置顶")) == "是"

            try:
                row, job_type = process_topic(item, target_date=target_date)
            except (
                HTTPError,
                URLError,
                OSError,
                ValueError,
                TypeError,
                KeyError,
                requests.RequestException,
            ) as exc:
                stats["detail_failed"] = int(stats["detail_failed"]) + 1
                page_failed += 1
                _log(
                    "失败",
                    f"ID {post_id} | {_short_text(title)} | {type(exc).__name__}: {exc}",
                )
                continue

            if not matches_day(row, target_date):
                stats["date_skipped"] = int(stats["date_skipped"]) + 1
                page_skipped += 1
                _log("跳过", f"ID {post_id} | 刷新与发布均非目标日期或日期无效 | {row.get('刷新时间') or '空'}")
                continue

            if not type_allowed(job_type):
                stats["type_skipped"] = int(stats["type_skipped"]) + 1
                page_skipped += 1
                _log(
                    "跳过",
                    f"ID {post_id} | 类型={job_type or '未知'} | {_short_text(title)}",
                )
                continue

            is_pinned = clean(row.get("置顶")) == "是" or item_is_pinned

            if is_pinned:
                row["置顶"] = "是"
                stats["pinned_found"] = int(stats["pinned_found"]) + 1
            else:
                row["置顶"] = ""
                stats["normal_found"] = int(stats["normal_found"]) + 1

            # 先确认 Notion 写入成功，再计入本地成功记录；失败时停止，保留已写入结果。
            if writer is not None:
                writer.write(row)
            append_csv_row(row)
            rows.append(row)
            stats["written"] = int(stats["written"]) + 1
            page_written += 1

            _log_captured_row(
                row,
                sequence=int(stats["written"]),
            )

        _log(
            "页面完成",
            f"第 {page} 页 | 列表 {len(topics)} | 写入 {page_written} | "
            f"跳过 {page_skipped + page_invalid} | 失败 {page_failed} | "
            f"累计 {int(stats['written'])}",
        )

        page += 1

    if STOP_EVENT.is_set():
        stop_reason = "user_stop"

    if not stop_reason:
        stop_reason = "forum_end"

    stats["stop_reason"] = stop_reason
    return rows, stats


# ============================================================
# 主程序
# ============================================================

def run(*, clear_stop: bool = True, local_only: bool = False, date_page: str = None) -> dict:
    """抓取参数页面日期或洛杉矶当天的记录，写入日期子页面。"""
    if clear_stop:
        STOP_EVENT.clear()

    run_started_at = datetime.now(LA_TZ)
    target_date = run_started_at.date()
    source_page_id = None
    date_fallback = False
    if date_page and date_page.strip():
        try:
            target_date = read_date_page(date_page)
            source_page_id = notion_page_id(date_page)
        except Exception as exc:
            # 参数读取失败不影响当天采集；不捕获用户中断或系统退出。
            target_date = datetime.now(LA_TZ).date()
            date_fallback = True
            _log("警告", f"日期参数读取失败（{type(exc).__name__}），自动使用洛杉矶当天 {target_date}")
    started_perf = time.perf_counter()

    print("=" * 76)
    _log("开始", "ChineseInLA 招聘采集 | 指定日期刷新或发布 | " + ("仅本地 CSV" if local_only else "Notion + CSV"))
    _log("日期", f"{target_date} | America/Los_Angeles | 来源={'Notion 页面标题' if source_page_id else '当天'}")
    _log("配置", f"类型={JOB_TYPE_FILTER} | 跳过置顶={'是' if SKIP_PINNED else '否'}")
    _log(
        "配置",
        f"请求间隔={REQUEST_DELAY_MIN_SECONDS:g}-{REQUEST_DELAY_MAX_SECONDS:g} 秒 | "
        f"超时={REQUEST_TIMEOUT_SECONDS} 秒",
    )
    _log("保存", f"CSV：{OUTPUT_CSV}")
    _log("规则", "不读取历史、不去重、按网页顺序、每条成功后立即写入")
    print("=" * 76)

    writer = None if local_only else NotionWriter(target_date, guard_midnight=source_page_id is None)
    try:
        if writer is not None:
            writer.check_page()
        rows, stats = crawl_live(target_date, writer)
    finally:
        if writer is not None:
            writer.close()

    run_finished_at = datetime.now(LA_TZ)
    elapsed_seconds = max(0.0, time.perf_counter() - started_perf)
    written = int(stats.get("written", 0))
    pages = int(stats.get("pages_scanned", 0))
    speed_per_minute = (written / elapsed_seconds * 60.0) if elapsed_seconds > 0 else 0.0

    stop_reason = str(stats.get("stop_reason", "forum_end"))
    reason_text = {
        "user_stop": "用户手动停止",
        "repeated_page": "检测到重复分页，安全停止",
        "consecutive_empty_or_failed": "连续空页或列表读取失败，判定已到末端",
        "forum_end": "论坛分页已扫描到底",
    }.get(stop_reason, stop_reason or "正常结束")

    status_text = "已停止" if stop_reason == "user_stop" else "已完成"

    result = {
        "mode": "date_refreshed_or_published_single_run",
        "target_date": str(target_date),
        "date_source_page_id": source_page_id,
        "date_parameter_fallback": date_fallback,
        "notion_page_id": None if local_only else NOTION_PAGE_ID,
        "notion_daily_page_id": None if writer is None else writer.daily_page_id,
        "notion_written": 0 if writer is None else writer.count,
        "run_started_at": run_started_at.isoformat(timespec="seconds"),
        "run_finished_at": run_finished_at.isoformat(timespec="seconds"),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "status": status_text,
        "stop_reason": stop_reason,
        "stop_reason_text": reason_text,
        "site": BASE_URL,
        "forum_id": FORUM_ID,
        "job_type_filter": JOB_TYPE_FILTER,
        "skip_pinned": SKIP_PINNED,
        "storage": "local_csv_append" if local_only else "notion_and_csv_append",
        "display_order": "forum_order",
        "written_rows": written,
        "valid_rows": written,
        "speed_rows_per_minute": round(speed_per_minute, 2),
        "stopped_by_user": stop_reason == "user_stop",
        "stats": stats,
        "csv": str(OUTPUT_CSV),
    }

    RESULT_JSON.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    print("=" * 76)
    _log("报告", "本次运行报告")
    print("-" * 76)
    print(f"状态        ：{status_text}")
    print(f"结束原因    ：{reason_text}")
    print(f"开始时间    ：{run_started_at.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"结束时间    ：{run_finished_at.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"运行耗时    ：{_format_duration(elapsed_seconds)}")
    print(f"扫描页数    ：{pages} 页")
    print(f"列表帖子    ：{int(stats.get('topics_seen', 0))} 条")
    print(f"成功写入    ：{written} 条")
    print(
        f"  普通帖子  ：{int(stats.get('normal_found', 0))} 条\n"
        f"  置顶帖子  ：{int(stats.get('pinned_found', 0))} 条"
    )
    print(f"类型跳过    ：{int(stats.get('type_skipped', 0))} 条")
    print(f"日期跳过    ：{int(stats.get('date_skipped', 0))} 条")
    print(f"Notion 写入 ：{result['notion_written']} 条")
    print(f"无效帖子    ：{int(stats.get('invalid', 0))} 条")
    print(f"详情失败    ：{int(stats.get('detail_failed', 0))} 条")
    print(f"列表失败    ：{int(stats.get('list_failed', 0))} 页")
    print(f"平均速度    ：{speed_per_minute:.1f} 条/分钟")
    print(f"CSV 文件    ：{OUTPUT_CSV}")
    print(f"运行报告    ：{RESULT_JSON}")
    print("=" * 76)

    return result

def run_cli() -> None:
    """执行一次采集后结束；无需桌面界面。"""
    parser = argparse.ArgumentParser(description="抓取洛杉矶当天刷新或发布的招聘信息并追加到 Notion")
    parser.add_argument("--local-only", action="store_true", help="仅保存 CSV，不写 Notion；传 --date-page 时仍读取参数页面")
    parser.add_argument("--date-page", metavar="URL_OR_ID", help="读取指定 Notion 页面标题中的日期；不传则使用洛杉矶当天")
    args = parser.parse_args()
    try:
        run(local_only=args.local_only, date_page=args.date_page)
    except KeyboardInterrupt:
        STOP_EVENT.set()
        print("\n用户中止。")
        raise SystemExit(130)
    except (
        FileNotFoundError,
        RuntimeError,
        ValueError,
        OSError,
        requests.RequestException,
    ) as exc:
        print()
        print("=" * 72)
        print("程序停止")
        print("=" * 72)
        print(exc)
        raise SystemExit(1)




if __name__ == "__main__":
    run_cli()
