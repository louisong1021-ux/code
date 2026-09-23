import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


# ============================================================
# 基本配置
# ============================================================

NOTION_PAGE_ID = "3dcc18c6fba480e4b1ced952643da794"

# Notion Token 与招聘脚本保持同一种管理方式：
# 在本脚本同目录创建 notion_token.txt
# 文件中可写：
#   ntn_xxxxxxxxxxxxxxxxx
# 或：
#   NOTION_TOKEN=ntn_xxxxxxxxxxxxxxxxx
TOKEN_FILE = Path(__file__).resolve().parent / "notion_token.txt"


def load_notion_token():
    """
    优先从脚本同目录的 notion_token.txt 读取 Notion Token。

    支持：
    1) ntn_xxxxxxxxxxxxxxxxx
    2) NOTION_TOKEN=ntn_xxxxxxxxxxxxxxxxx
    3) NOTION_ACCESS_TOKEN=ntn_xxxxxxxxxxxxxxxxx

    如果文件不存在，再兼容读取环境变量。
    """
    if TOKEN_FILE.exists():
        raw = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()

        if raw:
            first_line = next(
                (
                    line.strip()
                    for line in raw.splitlines()
                    if line.strip()
                    and not line.strip().startswith("#")
                ),
                "",
            )

            if "=" in first_line:
                key, value = first_line.split("=", 1)

                if key.strip() in {
                    "NOTION_TOKEN",
                    "NOTION_ACCESS_TOKEN",
                }:
                    first_line = value.strip()

            token = first_line.strip().strip('"').strip("'")

            if token:
                return token

        raise RuntimeError(
            f"Token 文件存在但内容为空或格式不正确：{TOKEN_FILE}\n\n"
            "请在文件中只放一行 Notion Token，例如：\n"
            "ntn_xxxxxxxxxxxxxxxxx"
        )

    token = (
        os.environ.get("NOTION_TOKEN")
        or os.environ.get("NOTION_ACCESS_TOKEN")
        or ""
    ).strip()

    if token:
        return token

    raise RuntimeError(
        "找不到 Notion Token。\n\n"
        f"请在脚本同目录创建：{TOKEN_FILE.name}\n"
        "文件内容只放一行 Token，例如：\n"
        "ntn_xxxxxxxxxxxxxxxxx"
    )


def try_load_notion_token():
    try:
        return load_notion_token()
    except (RuntimeError, OSError):
        return ""


NOTION_TOKEN = try_load_notion_token()

FORUM_URL = "https://www.chineseinla.com/f/page_viewforum/f_46.html"
BASE_URL = "https://www.chineseinla.com"

# 详情正文固定从桌面详情页读取。
# ChineseInLA 桌面详情页正文结构：
#   div.post_body > p.real-content

KEEP_DAYS = 3
PAGE_STEP = 15
MAX_PAGES = 40
PAGE_DELAY = 0.6
DETAIL_DELAY = 0.30

LA_TIMEZONE = ZoneInfo("America/Los_Angeles")
NOTION_VERSION = "2026-03-11"

PROTECTED_BLOCK_TYPES = {
    "child_page",
    "child_database",
}

DELETE_VERIFY_PASSES = 4
DELETE_DELAY = 0.45

OUTPUT_DIR = Path(__file__).resolve().parent / "chineseinla_output"


# ============================================================
# 时间格式
# ============================================================

TIME_PATTERN = r"(?:1[0-2]|0?[1-9]):[0-5]\d\s*(?:am|pm)"
DATE_PATTERN = r"20\d{2}-\d{2}-\d{2}"

TIME_RE = re.compile(rf"^{TIME_PATTERN}$", re.I)
DATE_RE = re.compile(rf"^{DATE_PATTERN}$")

ROW_META_RE = re.compile(
    rf"(?P<updated>{TIME_PATTERN}|{DATE_PATTERN})"
    rf"\s+"
    rf"(?P<replies>[\d,]+)"
    rf"\s+"
    rf"(?P<views>[\d,]+)"
    rf"\s*$",
    re.I,
)


# ============================================================
# HTTP Session
# ============================================================

forum_session = requests.Session()
forum_session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/142.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
    }
)

notion_session = requests.Session()
notion_headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


# ============================================================
# 通用工具
# ============================================================


def clean_text(text):
    if not text:
        return ""
    text = str(text).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def clean_multiline_text(text):
    if not text:
        return ""

    lines = []
    previous = None

    for raw_line in str(text).replace("\u00a0", " ").splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line:
            continue
        if line == previous:
            continue
        lines.append(line)
        previous = line

    return "\n".join(lines).strip()


def normalize_topic_url(href):
    if not href:
        return ""

    full_url = urljoin(BASE_URL, str(href))
    parts = urlsplit(full_url)

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            "",
            "",
        )
    )


def get_page_url(page_index):
    if page_index == 0:
        return FORUM_URL

    offset = page_index * PAGE_STEP
    return f"{BASE_URL}/f/page_viewforum/f_46/start_{offset}.html"


def time_to_minutes(value):
    match = re.fullmatch(
        r"(1[0-2]|0?[1-9]):([0-5]\d)\s*(am|pm)",
        value,
        re.I,
    )

    if match is None:
        return -1

    hour = int(match.group(1))
    minute = int(match.group(2))
    am_pm = match.group(3).lower()

    if am_pm == "am":
        if hour == 12:
            hour = 0
    else:
        if hour != 12:
            hour += 12

    return hour * 60 + minute


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def split_text_for_notion(text, max_chars=1800):
    """把正文切成多个 Notion paragraph，尽量按换行切。"""
    text = clean_multiline_text(text)
    if not text:
        return []

    chunks = []
    current = ""

    for line in text.splitlines():
        if len(line) > max_chars:
            if current:
                chunks.append(current)
                current = ""

            start = 0
            while start < len(line):
                chunks.append(line[start:start + max_chars])
                start += max_chars
            continue

        candidate = line if not current else current + "\n" + line

        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line

    if current:
        chunks.append(current)

    return chunks




TOPIC_ID_RE = re.compile(
    r"/page_viewtopic/t_(\d+)\.html",
    re.I,
)


# ============================================================
# 网络请求
# ============================================================


def get_forum_page(url):
    response = forum_session.get(
        url,
        timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()

    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding

    return response


def save_debug_html(html, filename):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / filename
    path.write_text(html, encoding="utf-8")
    print("调试 HTML：", path)
    return path


# ============================================================
# 论坛列表页解析
# ============================================================


def read_all_topic_rows(html, current_url):
    soup = BeautifulSoup(html, "html.parser")
    elements = soup.select("div.topic_list_detail")

    raw_rows = []

    for index, row in enumerate(elements):
        topic_link = row.select_one('a.title[href*="page_viewtopic"]')

        if topic_link is None:
            topic_link = row.select_one('a[href*="page_viewtopic"]')

        title = ""
        url = ""

        if topic_link is not None:
            title = clean_text(topic_link.get_text(" ", strip=True))
            href = topic_link.get("href", "")

            if isinstance(href, (list, tuple)):
                href = href[0] if href else ""

            if href:
                url = normalize_topic_url(urljoin(current_url, str(href)))

        row_text = clean_text(row.get_text(" ", strip=True))

        raw_rows.append(
            {
                "dom_index": index,
                "title": title,
                "url": url,
                "text": row_text,
            }
        )

    return raw_rows


def parse_all_rows(raw_rows, run_date, target_dates):
    """
    bucket:
      day0  = 今天
      day1  = 昨天
      day2  = 前天
      older = 第4天或更早
      unknown
    """
    parsed = []

    for item in raw_rows:
        title = clean_text(item.get("title", ""))
        url = normalize_topic_url(item.get("url", ""))
        row_text = clean_text(item.get("text", ""))

        row = {
            "dom_index": item.get("dom_index", -1),
            "title": title,
            "url": url,
            "text": row_text,
            "kind": "unknown",
            "phase": None,
            "updated": "",
            "minutes": -1,
            "date": None,
            "replies": "",
            "views": "",
        }

        if not title or not url.startswith(
            "https://www.chineseinla.com/f/page_viewtopic/"
        ):
            parsed.append(row)
            continue

        meta_match = ROW_META_RE.search(row_text)

        if meta_match is None:
            parsed.append(row)
            continue

        updated = clean_text(meta_match.group("updated"))
        row["updated"] = updated
        row["replies"] = meta_match.group("replies")
        row["views"] = meta_match.group("views")

        if TIME_RE.fullmatch(updated):
            row["kind"] = "day0"
            row["phase"] = 0
            row["date"] = run_date
            row["minutes"] = time_to_minutes(updated)

        elif DATE_RE.fullmatch(updated):
            refresh_date = parse_date(updated)
            row["date"] = refresh_date

            if refresh_date == target_dates[1]:
                row["kind"] = "day1"
                row["phase"] = 1

            elif refresh_date == target_dates[2]:
                row["kind"] = "day2"
                row["phase"] = 2

            elif refresh_date is not None and refresh_date < target_dates[2]:
                row["kind"] = "older"
                row["phase"] = 3

            else:
                row["kind"] = "unknown"

        parsed.append(row)

    return parsed


# ============================================================
# 主列表评分
# ============================================================


def score_window(
    window,
    page_number,
    previous_last_minutes,
    entered_dated_zone,
):
    score = 0

    unknown_count = sum(1 for row in window if row["kind"] == "unknown")
    score -= unknown_count * 5000

    urls = [row["url"] for row in window if row["url"]]
    if len(urls) != len(set(urls)):
        score -= 5000

    previous_phase = -1

    for row in window:
        phase = row.get("phase")

        if phase is None:
            continue

        if previous_phase >= 0 and phase < previous_phase:
            score -= 15000

        previous_phase = max(previous_phase, phase)

    if entered_dated_zone:
        today_after_dated = sum(1 for row in window if row["kind"] == "day0")
        score -= today_after_dated * 20000

    today_rows = [row for row in window if row["kind"] == "day0"]
    previous_minutes = None

    for row in today_rows:
        current_minutes = row["minutes"]

        if previous_minutes is not None:
            if current_minutes <= previous_minutes:
                score += 100
            else:
                score -= 8000

        previous_minutes = current_minutes

    dated_rows = [
        row
        for row in window
        if row["kind"] in {"day1", "day2", "older"}
    ]

    previous_date = None

    for row in dated_rows:
        current_date = row["date"]

        if previous_date is not None and current_date is not None:
            if current_date <= previous_date:
                score += 30
            else:
                score -= 1000

        if current_date is not None:
            previous_date = current_date

    if page_number == 1:
        score += len(today_rows) * 1000
    else:
        score += len(today_rows) * 500

    if previous_last_minutes is not None and today_rows:
        first_minutes = today_rows[0]["minutes"]

        if first_minutes <= previous_last_minutes:
            score += 1500
            gap = previous_last_minutes - first_minutes
            score += max(0, 300 - gap)
        else:
            score -= 12000

    valid_count = sum(
        1
        for row in window
        if row["kind"] in {"day0", "day1", "day2", "older"}
    )
    score += valid_count * 50

    return score


def find_main_window(
    parsed_rows,
    page_number,
    previous_last_minutes,
    entered_dated_zone,
    previous_window_start,
):
    if len(parsed_rows) < PAGE_STEP:
        raise RuntimeError(
            f"页面只有 {len(parsed_rows)} 个帖子容器，"
            f"少于正常分页的 {PAGE_STEP} 条。"
        )

    candidates = []

    for start in range(0, len(parsed_rows) - PAGE_STEP + 1):
        window = parsed_rows[start:start + PAGE_STEP]
        score = score_window(
            window,
            page_number,
            previous_last_minutes,
            entered_dated_zone,
        )

        candidates.append(
            {
                "start": start,
                "end": start + PAGE_STEP - 1,
                "score": score,
                "window": window,
            }
        )

    candidates.sort(key=lambda item: item["score"], reverse=True)
    best = candidates[0]

    print()
    print("自动识别分页主列表：")
    print(f"  DOM位置：{best['start'] + 1}～{best['end'] + 1}")
    print(f"  匹配分数：{best['score']}")

    if len(candidates) >= 2:
        second = candidates[1]
        print(
            f"  第二候选：{second['start'] + 1}～{second['end'] + 1}"
            f" / 分数 {second['score']}"
        )

        score_gap = best["score"] - second["score"]

        if score_gap < 100:
            previous_match = None

            if previous_window_start is not None:
                for candidate in candidates:
                    candidate_gap = best["score"] - candidate["score"]

                    if candidate_gap >= 100:
                        break

                    if candidate["start"] == previous_window_start:
                        previous_match = candidate
                        break

            if previous_match is not None:
                best = previous_match
                print("  ✓ 候选分数接近")
                print(
                    "  ✓ 采用上一页主列表位置："
                    f"{best['start'] + 1}～{best['end'] + 1}"
                )
            else:
                raise RuntimeError(
                    "主列表候选区域分数过于接近，"
                    "且无法通过上一页位置安全判断真正分页列表。"
                )

    window = best["window"]
    unknown_rows = [row for row in window if row["kind"] == "unknown"]

    if unknown_rows:
        print()
        print("无法解析的主列表帖子：")

        for row in unknown_rows:
            print("  -", row["title"])
            print("    ", row["text"])

        raise RuntimeError("真正分页列表中存在无法解析帖子。")

    return window, best["start"]


# ============================================================
# 解析一页
# ============================================================


def extract_posts_from_html(
    html,
    current_url,
    page_number,
    previous_last_minutes,
    entered_dated_zone,
    previous_window_start,
    run_date,
    target_dates,
):
    raw_rows = read_all_topic_rows(html, current_url)
    parsed_rows = parse_all_rows(raw_rows, run_date, target_dates)

    counts = {
        "day0": 0,
        "day1": 0,
        "day2": 0,
        "older": 0,
        "unknown": 0,
    }

    for row in parsed_rows:
        counts[row["kind"]] += 1

    print("全部 topic_list_detail：", len(parsed_rows))
    print(
        "全页内容："
        f"今天 {counts['day0']} / "
        f"昨天 {counts['day1']} / "
        f"前天 {counts['day2']} / "
        f"更早 {counts['older']} / "
        f"无法解析 {counts['unknown']}"
    )

    window, selected_window_start = find_main_window(
        parsed_rows,
        page_number,
        previous_last_minutes,
        entered_dated_zone,
        previous_window_start,
    )

    target_rows = [
        row
        for row in window
        if row["kind"] in {"day0", "day1", "day2"}
    ]

    older_rows = [row for row in window if row["kind"] == "older"]

    print()
    print(
        "真正主列表："
        f"今天 {sum(1 for r in window if r['kind'] == 'day0')} / "
        f"昨天 {sum(1 for r in window if r['kind'] == 'day1')} / "
        f"前天 {sum(1 for r in window if r['kind'] == 'day2')} / "
        f"更早 {len(older_rows)}"
    )

    print()
    print("选中的分页主列表：")

    labels = {
        "day0": "今天",
        "day1": "昨天",
        "day2": "前天",
        "older": "更早",
    }

    for row in window:
        print(
            f"  [{labels.get(row['kind'], '未知')}] "
            f"[{row['updated']}] {row['title']}"
        )

    posts = []

    for row in target_rows:
        posts.append(
            {
                "bucket": row["kind"],
                "title": row["title"],
                "time": row["updated"] if row["kind"] == "day0" else "",
                "date": (
                    str(run_date)
                    if row["kind"] == "day0"
                    else str(row["date"])
                ),
                "url": row["url"],
                "page": page_number,
                "replies": row["replies"],
                "views": row["views"],
            }
        )

    boundary_reached = len(older_rows) > 0
    page_entered_dated_zone = any(
        row["kind"] in {"day1", "day2"}
        for row in window
    )

    last_today_minutes = None
    today_rows = [row for row in window if row["kind"] == "day0"]

    if today_rows:
        last_today_minutes = today_rows[-1]["minutes"]

    return (
        posts,
        boundary_reached,
        page_entered_dated_zone,
        last_today_minutes,
        selected_window_start,
    )


# ============================================================
# 抓最近 3 天列表
# ============================================================


def scrape_three_day_posts():
    run_started_at = datetime.now(LA_TIMEZONE)
    run_date = run_started_at.date()

    target_dates = [
        run_date,
        run_date - timedelta(days=1),
        run_date - timedelta(days=2),
    ]

    print()
    print("=" * 72)
    print("ChineseInLA 装修论坛 → 最近 3 天全部帖子")
    print("requests 后台模式，不会打开浏览器")
    print("今天：", target_dates[0])
    print("昨天：", target_dates[1])
    print("前天：", target_dates[2])
    print("=" * 72)

    posts = []
    seen_urls = set()

    previous_last_minutes = None
    previous_window_start = None
    entered_dated_zone = False
    boundary_found = False

    for page_index in range(MAX_PAGES):
        current_date = datetime.now(LA_TIMEZONE).date()

        if current_date != run_date:
            raise RuntimeError(
                "抓取过程中已经跨过午夜。"
                "为了避免 3 天时间窗口发生变化，本次禁止更新 Notion。"
            )

        page_number = page_index + 1
        url = get_page_url(page_index)

        print()
        print("=" * 72)
        print(f"正在读取第 {page_number} 页...")
        print(url)

        try:
            response = get_forum_page(url)
        except Exception as error:
            raise RuntimeError(
                f"ChineseInLA 第 {page_number} 页请求失败：{error}"
            ) from error

        print("HTTP：", response.status_code)
        html = response.text

        if "topic_list_detail" not in html:
            debug_path = save_debug_html(
                html,
                f"f46_page_{page_number}_no_topics.html",
            )
            raise RuntimeError(
                "网页成功返回，但没有找到 topic_list_detail。\n\n"
                f"调试文件：{debug_path}\n\n"
                "本次禁止更新 Notion。"
            )

        try:
            (
                page_posts,
                boundary_reached,
                page_entered_dated_zone,
                last_today_minutes,
                selected_window_start,
            ) = extract_posts_from_html(
                html,
                response.url,
                page_number,
                previous_last_minutes,
                entered_dated_zone,
                previous_window_start,
                run_date,
                target_dates,
            )
        except Exception:
            save_debug_html(
                html,
                f"f46_page_{page_number}_parse_error.html",
            )
            raise

        previous_window_start = selected_window_start

        new_posts = []

        for post in page_posts:
            if post["url"] in seen_urls:
                continue

            seen_urls.add(post["url"])
            posts.append(post)
            new_posts.append(post)

        print()
        print(f"本页新增最近3天帖子：{len(new_posts)} 条")
        print(f"目前累计最近3天帖子：{len(posts)} 条")

        for post in new_posts:
            prefix = post["time"] if post["bucket"] == "day0" else post["date"]
            print(f"  + [{prefix}] {post['title']}")

        if page_entered_dated_zone:
            entered_dated_zone = True
            previous_last_minutes = None
        elif any(post["bucket"] == "day0" for post in new_posts):
            previous_last_minutes = last_today_minutes

        if boundary_reached:
            boundary_found = True
            print()
            print("✅ 已进入第 4 天或更早。")
            print("✅ 最近 3 天完整边界已经确认。")
            print("停止继续翻页。")
            break

        time.sleep(PAGE_DELAY)

    if not boundary_found:
        raise RuntimeError(
            f"已经读取最多 {MAX_PAGES} 页，仍没有进入第 4 天或更早。\n\n"
            "无法证明最近 3 天数据完整，本次禁止更新 Notion。"
        )

    if datetime.now(LA_TIMEZONE).date() != run_date:
        raise RuntimeError(
            "抓取完成时已经跨过午夜。"
            "本次数据作废，Notion 不会更新。"
        )

    # 今天按具体时间倒序；昨天/前天保留论坛原始顺序
    today_posts = [post for post in posts if post["bucket"] == "day0"]
    other_posts = [post for post in posts if post["bucket"] != "day0"]

    today_posts.sort(
        key=lambda post: time_to_minutes(post["time"]),
        reverse=True,
    )

    posts = today_posts + other_posts

    print()
    print("=" * 72)
    print("最近 3 天列表抓取完成")
    print("最近3天帖子总数：", len(posts))
    print("=" * 72)

    return posts, run_started_at, target_dates


# ============================================================
# 正文抓取
#
# 已通过浏览器 DevTools 确认桌面详情页 DOM：
#
#   <div class="post_body">
#       ...
#       <p class="real-content">正文</p>
#   </div>
#
# 固定规则：
# 1. 直接读取原桌面详情页 URL
# 2. 正文只读取 div.post_body p.real-content
# 3. 不读取其他 DOM
# 4. 不做整页文本兜底
# ============================================================


def extract_topic_body(html):
    """
    只读取：

        div.post_body p.real-content

    找不到时返回空字符串。
    """
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    node = soup.select_one(
        "div.post_body p.real-content"
    )

    if node is None:
        return ""

    # 只清理正文节点内部明显不可见/脚本类元素。
    for child in node.select(
        "script, style, noscript, template, svg"
    ):
        child.decompose()

    body = clean_multiline_text(
        node.get_text(
            "\n",
            strip=True,
        )
    )

    if len(body) > 12000:
        body = (
            body[:12000].rstrip()
            + "\n……（正文过长，已截断）"
        )

    return body


def fetch_post_bodies(posts):
    print()
    print("=" * 72)
    print("开始读取最近 3 天帖子正文")
    print("正文固定来源：桌面详情页 div.post_body p.real-content")
    print("无其他 DOM / 无整页兜底")
    print("=" * 72)

    total = len(posts)

    for index, post in enumerate(
        posts,
        start=1,
    ):
        print(
            f"[{index}/{total}] "
            f"{post['title']}"
        )

        detail_url = post.get(
            "url",
            "",
        )

        if not detail_url:
            print(
                "  ⚠ 原帖 URL 为空"
            )

            post["body"] = (
                "正文未能自动提取，"
                "请点击下方原帖链接查看。"
            )
            post["body_ok"] = False
            continue

        try:
            response = get_forum_page(
                detail_url
            )

            print(
                "  桌面详情："
                f"HTTP {response.status_code} | "
                f"{response.url}"
            )

            body = extract_topic_body(
                response.text
            )

            if not body:
                print(
                    "  ⚠ 未找到 "
                    "div.post_body p.real-content"
                )

                topic_match = TOPIC_ID_RE.search(
                    detail_url
                )
                topic_id = (
                    topic_match.group(1)
                    if topic_match
                    else f"index_{index}"
                )

                save_debug_html(
                    response.text,
                    (
                        f"desktop_topic_{topic_id}"
                        "_missing_real_content.html"
                    ),
                )

                post["body"] = (
                    "正文未能自动提取，"
                    "请点击下方原帖链接查看。"
                )
                post["body_ok"] = False

            else:
                print(
                    "  ✓ real-content："
                    f"{len(body)} 字符"
                )

                post["body"] = body
                post["body_ok"] = True

        except Exception as error:
            print(
                "  ⚠ 正文读取失败：",
                error,
            )

            post["body"] = (
                "正文读取失败，"
                "请点击下方原帖链接查看。"
            )
            post["body_ok"] = False

        time.sleep(
            DETAIL_DELAY
        )

    return posts


# ============================================================
# 最终数据验证
# ============================================================


def validate_posts(posts, run_date, target_dates):
    print()
    print("正在检查最近 3 天全部帖子...")

    urls = [post["url"] for post in posts]

    if len(urls) != len(set(urls)):
        print("❌ 存在重复 URL")
        return False

    valid_buckets = {"day0", "day1", "day2"}

    for post in posts:
        title = post.get("title", "")
        url = post.get("url", "")
        bucket = post.get("bucket")

        if not title:
            print("❌ 空标题")
            return False

        if "�" in title:
            print("❌ 标题乱码：", title)
            return False

        if not url.startswith("https://www.chineseinla.com/f/page_viewtopic/"):
            print("❌ 异常 URL：", url)
            return False

        if bucket not in valid_buckets:
            print("❌ 分类异常：", bucket, title)
            return False

        if bucket == "day0":
            if not TIME_RE.fullmatch(post.get("time", "")):
                print("❌ 今天时间异常：", post.get("time"), title)
                return False
        else:
            post_date = parse_date(post.get("date", ""))
            expected = target_dates[1] if bucket == "day1" else target_dates[2]

            if post_date != expected:
                print("❌ 日期异常：", post.get("date"), title)
                return False

        if not isinstance(post.get("body", ""), str):
            print("❌ 正文类型异常：", title)
            return False

    print("✅ 数据检查通过")
    print("   全部帖子：", len(posts), "条")
    return True


# ============================================================
# Notion 请求
# ============================================================


def notion_request(method, url, **kwargs):
    max_retries = 6

    for attempt in range(max_retries):
        try:
            response = notion_session.request(
                method,
                url,
                headers=notion_headers,
                timeout=30,
                **kwargs,
            )
        except requests.RequestException as error:
            if attempt == max_retries - 1:
                raise

            wait = min(2 ** attempt, 10)
            print("⚠ Notion 网络错误：", error)
            print(f"{wait} 秒后重试...")
            time.sleep(wait)
            continue

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "1")

            try:
                retry_after = float(retry_after)
            except ValueError:
                retry_after = 1

            print(f"⚠ Notion 限流，{retry_after} 秒后重试...")
            time.sleep(retry_after)
            continue

        if response.status_code in {500, 502, 503, 504, 529}:
            if attempt == max_retries - 1:
                response.raise_for_status()

            wait = min(2 ** attempt, 10)
            print(
                f"⚠ Notion 临时错误 {response.status_code}，"
                f"{wait} 秒后重试..."
            )
            time.sleep(wait)
            continue

        response.raise_for_status()
        return response

    raise RuntimeError("Notion API 多次重试失败")


def test_notion():
    print()
    print("正在检查 Notion 连接...")

    url = f"https://api.notion.com/v1/pages/{NOTION_PAGE_ID}"

    try:
        notion_request("GET", url)
    except Exception as error:
        print("❌ Notion 连接失败：", error)
        return False

    print("✅ Notion 连接正常")
    return True


def get_all_notion_blocks():
    blocks = []
    url = f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children"
    cursor = None

    while True:
        params = {"page_size": 100}

        if cursor:
            params["start_cursor"] = cursor

        response = notion_request("GET", url, params=params)
        data = response.json()
        blocks.extend(data.get("results", []))

        if not data.get("has_more"):
            break

        cursor = data.get("next_cursor")

        if not cursor:
            break

    return blocks


# ============================================================
# Notion Toggle 内容
# ============================================================


def make_plain_paragraph(text, color=None):
    annotations = {}

    if color:
        annotations["color"] = color

    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": text},
                    "annotations": annotations,
                }
            ]
        },
    }


def make_original_link_paragraph(url):
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": "查看原帖",
                        "link": {"url": url},
                    },
                    "annotations": {
                        "bold": True,
                    },
                }
            ]
        },
    }


def make_toggle_post_block(number, prefix, post):
    children = []

    body_chunks = split_text_for_notion(post.get("body", ""))

    if not body_chunks:
        body_chunks = ["正文未能自动提取，请查看原帖。"]

    for chunk in body_chunks:
        children.append(make_plain_paragraph(chunk))

    children.append(make_original_link_paragraph(post["url"]))

    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": f"{number}. [{prefix}] ",
                    },
                    "annotations": {
                        "color": "gray",
                    },
                },
                {
                    "type": "text",
                    "text": {
                        "content": post["title"],
                    },
                    "annotations": {
                        "bold": True,
                    },
                },
            ],
            "children": children,
        },
    }


def build_notion_blocks(posts, run_date, target_dates):
    now = datetime.now(LA_TIMEZONE)
    updated_at = now.strftime("%Y-%m-%d %H:%M")

    by_bucket = {
        "day0": [post for post in posts if post["bucket"] == "day0"],
        "day1": [post for post in posts if post["bucket"] == "day1"],
        "day2": [post for post in posts if post["bucket"] == "day2"],
    }

    blocks = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"最后更新：{updated_at}"},
                        "annotations": {"color": "gray"},
                    }
                ]
            },
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"最近3天共 {len(posts)} 条帖子"},
                        "annotations": {"bold": True},
                    }
                ]
            },
        },
        {
            "object": "block",
            "type": "divider",
            "divider": {},
        },
    ]

    sections = [
        ("day0", "今天", target_dates[0]),
        ("day1", "昨天", target_dates[1]),
        ("day2", "前天", target_dates[2]),
    ]

    for section_index, (bucket, label, section_date) in enumerate(sections):
        section_posts = by_bucket[bucket]

        blocks.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {
                                "content": (
                                    f"{label} · {section_date} "
                                    f"（{len(section_posts)} 条）"
                                )
                            },
                        }
                    ]
                },
            }
        )

        if section_posts:
            for number, post in enumerate(section_posts, start=1):
                prefix = post["time"] if bucket == "day0" else str(section_date)
                blocks.append(make_toggle_post_block(number, prefix, post))
        else:
            blocks.append(make_plain_paragraph("没有帖子", color="gray"))

        if section_index < len(sections) - 1:
            blocks.append(
                {
                    "object": "block",
                    "type": "divider",
                    "divider": {},
                }
            )

    return blocks


# ============================================================
# Notion 安全覆盖
# ============================================================


def write_new_blocks(blocks):
    url = f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children"
    created_ids = []

    for start in range(0, len(blocks), 100):
        batch = blocks[start:start + 100]

        response = notion_request(
            "PATCH",
            url,
            json={"children": batch},
        )

        results = response.json().get("results", [])

        for result in results:
            block_id = result.get("id")
            if block_id:
                created_ids.append(block_id)

        print(f"新写入：{len(created_ids)}/{len(blocks)}")
        time.sleep(0.4)

    return created_ids


def delete_one_block(block_id):
    url = f"https://api.notion.com/v1/blocks/{block_id}"

    try:
        notion_request("DELETE", url)
        return True
    except requests.HTTPError as error:
        response = getattr(error, "response", None)

        if response is not None and response.status_code == 404:
            return True

        print()
        print("⚠ 删除 block 失败：", block_id)
        print(error)
        return False
    except Exception as error:
        print()
        print("⚠ 删除 block 失败：", block_id)
        print(error)
        return False


def delete_block_ids_resilient(block_ids, label):
    block_ids = list(dict.fromkeys(block_ids))
    total = len(block_ids)
    failed = []

    if total == 0:
        return failed

    for index, block_id in enumerate(block_ids, start=1):
        success = delete_one_block(block_id)

        if not success:
            failed.append(block_id)

        print(
            f"\r{label}：{index}/{total} | 失败 {len(failed)}",
            end="",
            flush=True,
        )
        time.sleep(DELETE_DELAY)

    print()
    return failed


def verify_new_blocks_exist(created_ids):
    current_blocks = get_all_notion_blocks()
    current_ids = {
        block.get("id")
        for block in current_blocks
        if block.get("id")
    }

    missing_new = set(created_ids) - current_ids

    if missing_new:
        print()
        print("❌ 新写入内容有 block 消失")
        print("缺少：", len(missing_new))
        return False

    return True


def cleanup_old_blocks(old_delete_ids, created_ids):
    target_old_ids = set(old_delete_ids)
    new_ids = set(created_ids)

    if not target_old_ids:
        print("没有需要删除的旧 block。")
        return True

    print()
    print("=" * 72)
    print("开始清理上一版 Notion 内容")
    print("=" * 72)
    print("需要清理：", len(target_old_ids), "个旧 block")

    for pass_number in range(1, DELETE_VERIFY_PASSES + 1):
        print()
        print(f"清理检查第 {pass_number}/{DELETE_VERIFY_PASSES} 轮")

        current_blocks = get_all_notion_blocks()
        current_ids = {
            block.get("id")
            for block in current_blocks
            if block.get("id")
        }

        missing_new = new_ids - current_ids

        if missing_new:
            print("❌ 新数据验证失败")
            print("缺少新 block：", len(missing_new))
            return False

        remaining = target_old_ids & current_ids
        print("旧 block 剩余：", len(remaining))

        if not remaining:
            print("✅ 所有旧 block 已确认删除")
            return True

        failed = delete_block_ids_resilient(
            sorted(remaining),
            f"删除旧内容 第{pass_number}轮",
        )

        if failed:
            print("⚠ 本轮删除失败：", len(failed))

        time.sleep(1.0)

    current_blocks = get_all_notion_blocks()
    current_ids = {
        block.get("id")
        for block in current_blocks
        if block.get("id")
    }

    remaining = target_old_ids & current_ids
    missing_new = new_ids - current_ids

    if missing_new:
        print("❌ 最终验证失败：新内容不完整")
        return False

    if remaining:
        print("❌ 旧内容仍有残留：", len(remaining))
        return False

    print("✅ 最终验证通过：旧 block = 0")
    return True


def rollback_new_blocks(old_ids):
    try:
        current_blocks = get_all_notion_blocks()
        rollback_ids = []

        for block in current_blocks:
            block_id = block.get("id")
            block_type = block.get("type")

            if not block_id:
                continue

            if block_type in PROTECTED_BLOCK_TYPES:
                continue

            if block_id not in old_ids:
                rollback_ids.append(block_id)

        if rollback_ids:
            print()
            print("准备回滚：", len(rollback_ids), "个新 block")
            delete_block_ids_resilient(rollback_ids, "回滚")

    except Exception as error:
        print("⚠ 自动回滚失败：", error)


def update_notion(posts, run_date, target_dates):
    if not test_notion():
        print("Notion 不会被修改。")
        return False

    new_blocks = build_notion_blocks(posts, run_date, target_dates)
    old_blocks = get_all_notion_blocks()

    old_ids = {
        block["id"]
        for block in old_blocks
        if block.get("id")
    }

    old_delete_ids = []
    protected_old_count = 0

    for block in old_blocks:
        block_id = block.get("id")
        block_type = block.get("type")

        if not block_id:
            continue

        if block_type in PROTECTED_BLOCK_TYPES:
            protected_old_count += 1
            print("⚠ 保留受保护内容：", block_type)
            continue

        old_delete_ids.append(block_id)

    print()
    print("当前 Notion 顶层 block：", len(old_blocks))
    print("准备删除的旧 block：", len(old_delete_ids))

    if protected_old_count:
        print("受保护 block：", protected_old_count)

    try:
        print()
        print("正在写入新的最近 3 天全部帖子结果...")
        created_ids = write_new_blocks(new_blocks)

        if len(created_ids) != len(new_blocks):
            raise RuntimeError(
                "Notion 新 block 数量不完整："
                f"{len(created_ids)}/{len(new_blocks)}"
            )

    except Exception as error:
        print()
        print("❌ 新结果写入失败：", error)
        print("正在保护旧数据并回滚本次新增...")
        rollback_new_blocks(old_ids)
        print("旧 Notion 数据不会删除。")
        return False

    print()
    print("✅ 新结果完整写入：", len(created_ids), "个顶层 block")

    if not verify_new_blocks_exist(created_ids):
        print("❌ 新结果验证失败，旧内容不会删除。")
        return False

    print("✅ 新结果存在性验证通过")

    cleanup_success = cleanup_old_blocks(
        old_delete_ids,
        created_ids,
    )

    if not cleanup_success:
        print("⚠ 新结果已经写入，但旧结果仍有残留。")
        return False

    final_blocks = get_all_notion_blocks()
    final_ids = {
        block.get("id")
        for block in final_blocks
        if block.get("id")
    }

    created_set = set(created_ids)
    missing_new = created_set - final_ids
    remaining_old = set(old_delete_ids) & final_ids

    print()
    print("=" * 72)
    print("Notion 最终验证")
    print("=" * 72)
    print("本次新顶层 block：", len(created_set))
    print("新 block 缺失：", len(missing_new))
    print("旧 block 残留：", len(remaining_old))

    if missing_new or remaining_old:
        print()
        print("❌ Notion 最终验证失败")
        return False

    print()
    print("✅ Notion 最终验证通过")
    print("✅ 最近 3 天全部帖子已写入")
    print("✅ 作者未写入")
    print("✅ 每条帖子可展开查看正文")
    print("✅ 旧数据残留 = 0")
    print("✅ Notion 更新完成")

    return True


# ============================================================
# 主程序
# ============================================================


def main():
    global NOTION_TOKEN

    print()
    print("=" * 72)
    print("ChineseInLA 装修 → Notion 最近 3 天全部帖子自动同步")
    print("抓取全部帖子 / 不保存作者 / Toggle 展开正文")
    print("后台 requests 模式，不会弹浏览器")
    print("=" * 72)

    # 每次运行都重新读取 Token，方便直接修改同目录的 notion_token.txt
    try:
        NOTION_TOKEN = load_notion_token()
    except (RuntimeError, OSError) as error:
        print()
        print("❌ Notion Token 读取失败：")
        print(error)
        return False

    # notion_headers 在模块加载时已经创建，所以必须同步刷新 Authorization。
    notion_headers["Authorization"] = f"Bearer {NOTION_TOKEN}"

    print()
    print(f"✅ Notion Token 已读取：{TOKEN_FILE.name}")

    try:
        posts, run_started_at, target_dates = scrape_three_day_posts()
    except Exception as error:
        print()
        print("❌ 网页列表抓取失败：")
        print(error)
        print()
        print("Notion 不会被修改。")
        return False

    run_date = run_started_at.date()

    try:
        posts = fetch_post_bodies(posts)
    except Exception as error:
        print()
        print("❌ 正文读取失败：")
        print(error)
        print()
        print("Notion 不会被修改。")
        return False

    if not validate_posts(posts, run_date, target_dates):
        print()
        print("⚠ 抓取结果没有通过安全检查。")
        print("Notion 不会被修改。")
        return False

    if datetime.now(LA_TIMEZONE).date() != run_date:
        print()
        print("❌ 当前已经跨过午夜。")
        print("3 天时间窗口已经变化，Notion 不会更新。")
        return False

    by_bucket = {
        "day0": sum(1 for p in posts if p["bucket"] == "day0"),
        "day1": sum(1 for p in posts if p["bucket"] == "day1"),
        "day2": sum(1 for p in posts if p["bucket"] == "day2"),
    }

    print()
    print("准备同步到 Notion：")
    print(f"今天 {target_dates[0]}：{by_bucket['day0']} 条")
    print(f"昨天 {target_dates[1]}：{by_bucket['day1']} 条")
    print(f"前天 {target_dates[2]}：{by_bucket['day2']} 条")
    print("3天合计：", len(posts), "条")

    try:
        success = update_notion(
            posts,
            run_date,
            target_dates,
        )
    except Exception as error:
        print()
        print("❌ 更新 Notion 发生异常：")
        print(error)
        return False

    if not success:
        print()
        print("=" * 72)
        print("本次运行没有通过最终完整性验证")
        print("请查看上方具体错误。")
        print("=" * 72)
        return False

    print()
    print("=" * 72)
    print("全部完成")
    print(f"今天：{by_bucket['day0']} 条")
    print(f"昨天：{by_bucket['day1']} 条")
    print(f"前天：{by_bucket['day2']} 条")
    print(f"3天合计：{len(posts)} 条")
    print("作者字段：不保存")
    print("正文：Notion Toggle 点击展开")
    print("浏览器弹窗：0")
    print("退出代码：0")
    print("=" * 72)

    return True


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)

    except KeyboardInterrupt:
        print()
        print("你手动停止了程序。")
        sys.exit(130)

    except Exception as error:
        print()
        print("=" * 72)
        print("程序发生未处理错误")
        print("=" * 72)
        print()
        print(error)
        print()
        print("Notion 更新不能认为成功。")
        sys.exit(1)
