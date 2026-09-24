"""Daily Notion list: pure merge/render logic plus a guarded batch save.

No website extraction lives here. Notion is the authoritative persisted state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import os
import re
import uuid

LA = ZoneInfo("America/Los_Angeles")
FIELDS = ("帖子ID", "标题", "置顶", "公司名", "地址", "联系人", "电话", "邮箱",
          "发布时间", "刷新时间", "正文", "详情URL")
EMPTY = {"", "未采集", "none", "null"}
BATCH_BYTES = 300_000
BATCH_POSTS = 90
MARKER = re.compile(r"^CHINESEINLASYNC(?:START|END)[0-9a-f]{32}$")

SPECIAL = r"\*~`$[]<>{}|^_"
DETAILS = re.compile(r"^<details(?: [^>\n]*)?>\n<summary>(.*?)</summary>\n(.*?)\n</details>", re.M | re.S)


def valid(value):
    return value is not None and str(value).strip().lower() not in EMPTY


def parse_time(value):
    """Normalize site timestamps for comparison only; retain original field strings."""
    if not valid(value):
        return None
    text = str(value).strip().replace(",", " ")
    text = re.sub(r"\s+", " ", text)
    for fmt in ("%Y/%m/%d %I:%M %p", "%Y/%m/%d %I:%M:%S %p",
                "%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M",
                "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=LA)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(text)
        return dt.replace(tzinfo=LA) if dt.tzinfo is None else dt.astimezone(LA)
    except ValueError:
        return None


def latest(row):
    return parse_time(row.get("刷新时间")) or parse_time(row.get("发布时间"))


def clock_text(row):
    dt = latest(row)
    return dt.strftime("%H:%M") if dt else "--:--"


def escape(text):
    return "".join("\\" + c if c in SPECIAL else c for c in str(text))


def unescape(text):
    # Inline code keeps underscore runs intact through Notion markdown imports.
    text = re.sub(r"(?<!\\)`(_+)`", r"\1", text)
    # Notion auto-links plain email/URL strings when importing markdown.
    # Recover displayed text, leaving escaped literal markdown untouched.
    text = re.sub(r"(?<!\\)\[((?:\\.|[^\]\\])*)\]\((?:\\.|[^)\n])*\)", r"\1", text)
    return re.sub(r"\\([\\*~`$\[\]<>{}|^_])", r"\1", text).replace("\t", " ")


def inline(text):
    return re.sub(r"(?:\\_)+", lambda m: "`" + m.group(0).replace("\\", "") + "`", escape(text)).replace("\r\n", "\n").replace("\n", "<br>")


@dataclass
class Post:
    row: dict
    reposts: int = 0

    def __post_init__(self):
        self.row = {key: str(self.row[key]) if valid(self.row.get(key)) else "未采集" for key in FIELDS}


@dataclass
class DailyState:
    posts: dict = field(default_factory=dict)
    last_success: str = ""
    duplicates: int = 0
    order: list = field(default_factory=list)


def merge_row(old, new):
    result = dict(old)
    for key in FIELDS:
        if valid(new.get(key)):
            result[key] = str(new[key])
    return result


def parse_markdown(markdown):
    """Read old append-only toggles and the new list; fail closed on alien content."""
    text = markdown.replace("\r\n", "\n")
    state = DailyState()
    legacy_times = {}
    explicit_counts = {}
    spans = []
    for match in DETAILS.finditer(text):
        spans.append(match.span())
        title, contents = match.groups()
        # Only strip our generated numbering, never scrape new values from body text.
        title = re.sub(r"^\d{3,}｜(?:\d{2}:\d{2}|--:--)｜", "", unescape(title))
        title = re.sub(r"｜重新发布 \d+ 次$", "", title)
        lines = contents.splitlines()
        if any(line and not line.startswith("\t") for line in lines):
            raise ValueError("Notion toggle 子内容缩进异常，停止更新")
        contents = "\n".join(line[1:] if line.startswith("\t") else line for line in lines)
        # Previous writer split long body strings into adjacent paragraphs without adding text.
        link = re.search(r"\n\[查看原帖\]\((https?://[^\s]+)\)\s*$", contents)
        if not link:
            raise ValueError("已有帖子缺少可恢复的原帖链接，停止更新")
        url = link.group(1)
        content = contents[:link.start()]
        # Metadata is one paragraph; body may span multiple 1800-character paragraphs.
        content = content.replace("\n", "").replace("<br>", "\n")
        content = unescape(content)
        if "\n\n正文：\n" not in content:
            raise ValueError("已有帖子字段或正文分隔不完整，停止更新")
        metadata, body = content.split("\n\n正文：\n", 1)
        row = {"标题": title, "正文": body, "详情URL": url}
        count = None
        for line in metadata.splitlines():
            key, sep, value = line.partition("：")
            if not sep or key not in FIELDS + ("重新发布次数",):
                raise ValueError("已有帖子包含未知字段，停止更新以保留内容")
            if key == "重新发布次数":
                if not value.isdigit():
                    raise ValueError("已有重新发布次数无效，不能重置")
                count = int(value)
            else:
                row[key] = value
        pid = row.get("帖子ID", "").strip()
        if not re.fullmatch(r"\d+", pid):
            raise ValueError("已有帖子缺少有效帖子ID，停止更新")
        row["帖子ID"] = pid
        state.order.append(pid)
        dt = latest(row)
        if dt:
            legacy_times.setdefault(pid, set()).add(dt.isoformat())
        if count is not None:
            explicit_counts[pid] = max(explicit_counts.get(pid, 0), count)
        if pid in state.posts:
            state.duplicates += 1
            old = state.posts[pid].row
            # Legacy rounds are chronological in page order; don't regress the most recent row.
            old_time, new_time = latest(old), latest(row)
            state.posts[pid].row = Post(merge_row(row, old) if old_time and new_time and new_time < old_time
                                    else merge_row(old, row)).row
        else:
            state.posts[pid] = Post(row)
    gaps, cursor = [], 0
    for start, end in spans:
        gaps.append(text[cursor:start])
        cursor = end
    residual = "".join(gaps) + text[cursor:]
    for line in residual.splitlines():
        line = line.strip()
        if not line or line == "<empty-block/>" or MARKER.fullmatch(line):
            continue
        if re.fullmatch(r"## (?:刷新或发布招聘 · \d{4}-\d{2}-\d{2} · 采集于 .+|招聘信息监控 · \d{4}-\d{2}-\d{2})", line):
            continue
        if line.startswith("最后更新："):
            # One paragraph containing the six summary fields, exported with <br>.
            values = line.split("<br>")
            if not all(re.fullmatch(r"(?:最后更新：.+|不同帖子：\d+|今日新发布：\d+|重新发布过：\d+|采集到邮箱：\d+|采集到电话：\d+)", v) for v in values):
                raise ValueError("统计区域存在未知内容，停止更新")
            state.last_success = unescape(values[0].split("：", 1)[1])
            continue
        raise ValueError("日期页含非脚本内容或无法识别的块，停止更新；不会清空页面")
    for pid, post in state.posts.items():
        # Legacy migration: distinct observed timestamps, not number of repeated scan rows.
        post.reposts = explicit_counts.get(pid, max(0, len(legacy_times.get(pid, ())) - 1))
    return state


def merge_posts(state, rows):
    counts = {"new": 0, "updated": 0, "unchanged_time": 0, "refreshed": 0}
    events = []
    # Merge duplicate sightings in the same round without counting them as separate records.
    incoming = {}
    for row in rows:
        pid = str(row.get("帖子ID") or "").strip()
        if not re.fullmatch(r"\d+", pid):
            raise ValueError("本轮记录缺少有效帖子ID")
        incoming[pid] = merge_row(incoming.get(pid, {}), {**row, "帖子ID": pid})
    for pid, row in incoming.items():
        if pid not in state.posts:
            state.posts[pid] = Post(row)
            counts["new"] += 1
            events.append(("NEW", pid, "", clock_text(row), 0))
            continue
        old = state.posts[pid]
        merged = merge_row(old.row, row)
        before, after = latest(old.row), latest(merged)
        if before is not None and after is not None and before != after:
            old.reposts += 1
            counts["refreshed"] += 1
            events.append(("REFRESH", pid, clock_text(old.row), clock_text(merged), old.reposts))
        else:
            counts["unchanged_time"] += 1
        if merged != old.row:
            counts["updated"] += 1
        old.row = Post(merged).row
    return counts, events


def sorted_posts(state):
    return sorted(state.posts.values(), key=lambda p: (
        latest(p.row) or datetime.min.replace(tzinfo=LA), p.row["帖子ID"]), reverse=True)


def statistics(state, target):
    posts = list(state.posts.values())
    return {"total": len(posts),
            "published_today": sum(bool(parse_time(p.row.get("发布时间"))) and
                                   parse_time(p.row.get("发布时间")).date() == target for p in posts),
            "reposted": sum(p.reposts > 0 for p in posts),
            "email": sum(valid(p.row.get("邮箱")) for p in posts),
            "phone": sum(valid(p.row.get("电话")) for p in posts)}


def render_markdown(state, target):
    stats = statistics(state, target)
    parts = [f"## 招聘信息监控 · {target}",
             f"最后更新：{inline(state.last_success or '尚无完整成功轮次')}<br>不同帖子：{stats['total']}"
             f"<br>今日新发布：{stats['published_today']}<br>重新发布过：{stats['reposted']}"
             f"<br>采集到邮箱：{stats['email']}<br>采集到电话：{stats['phone']}"]
    for index, post in enumerate(sorted_posts(state), 1):
        row = post.row
        title = f"{index:03d}｜{clock_text(row)}｜{row.get('标题') or '招聘信息'}"
        if post.reposts:
            title += f"｜重新发布 {post.reposts} 次"
        metadata = [f"{key}：{row.get(key) if valid(row.get(key)) else '未采集'}"
                    for key in FIELDS if key not in {"标题", "正文", "详情URL"}]
        metadata.append(f"重新发布次数：{post.reposts}")
        text = "\n".join(metadata) + "\n\n正文：\n" + (row.get("正文") or "未采集")
        url = row.get("详情URL", "")
        if not re.fullmatch(r"https?://[^\s<>]+", url):
            raise ValueError("帖子原帖链接无效，停止提交")
        # URL parentheses must not terminate the markdown link.
        url = url.replace("(", "%28").replace(")", "%29")
        parts.append(f"<details>\n<summary>{inline(title)}</summary>\n\t{inline(text)}\n\t[查看原帖]({url})\n</details>")
    return "\n".join(parts) + "\n"


def complete_markdown(result):
    if (result.get("object") != "page_markdown" or result.get("truncated") or
            result.get("unknown_block_ids") or not isinstance(result.get("markdown"), str)):
        raise RuntimeError("Notion 页面读取不完整或更新结果未确认，停止处理")
    return result["markdown"]


class DailySync:
    """Read, merge, back up, batch-save and verify the complete daily list.

    The extra preflight GET detects an old concurrently running writer. Notion has
    no conditional-write token here: stop other writers before enabling this code.
    """
    def __init__(self, request, page_id, target, backup_dir, log, guard=lambda: None):
        self.request, self.page_id, self.target = request, page_id, target
        self.backup_dir, self.log, self.guard = Path(backup_dir), log, guard

    def save(self, rows, *, successful_scan=True, now=None, apply=True):
        self.guard()
        now = now or datetime.now(LA)
        path = f"pages/{self.page_id}/markdown"
        old_markdown = complete_markdown(self.request("GET", path))
        state = parse_markdown(old_markdown)
        old_ids = set(state.posts)
        counts, events = merge_posts(state, rows)
        if successful_scan:
            state.last_success = now.astimezone(LA).strftime("%Y-%m-%d %H:%M:%S %Z")
        new_markdown = render_markdown(state, self.target)
        restored = parse_markdown(new_markdown)
        if not old_ids <= set(restored.posts) or state.posts != restored.posts:
            raise RuntimeError("生成内容校验失败，未提交任何修改")
        payload = {"type": "replace_content", "replace_content": {"new_str": new_markdown}}
        chunks = split_batches(new_markdown)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        backup = self.backup_dir / f"{self.page_id}-{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}.json"
        fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"page_id": self.page_id, "before": old_markdown, "after": new_markdown,
                       "before_sha256": hashlib.sha256(old_markdown.encode()).hexdigest()}, stream, ensure_ascii=False)
        report = {**counts, **statistics(state, self.target), "legacy_duplicates": state.duplicates,
                  "last_success": state.last_success, "backup": str(backup),
                  "status": "preview", "page_id": self.page_id}
        if not apply:
            return report
        self.guard()
        if complete_markdown(self.request("GET", path)) != old_markdown:
            raise RuntimeError("Notion 页面在读取后发生变化；取消提交，请停掉旧版/其他写入进程")
        self.guard()
        try:
            if len(chunks) == 1:
                result = self.request("PATCH", path, json=payload)
            else:
                result = self.save_batches(path, old_markdown, chunks, state)
            # A timeout must never be blindly retried. The next round reads the page again.
            saved = parse_markdown(complete_markdown(result))
            expected_order = [p.row["帖子ID"] for p in sorted_posts(state)]
            if (saved.posts != state.posts or saved.duplicates or saved.order != expected_order
                    or saved.last_success != state.last_success):
                raise RuntimeError("Notion 返回内容与提交结果不一致；不能判定成功，请检查备份")
        except Exception as exc:
            self.log("Notion错误", f"本轮提交失败或结果未确认：{type(exc).__name__}: {exc}；备份={backup}")
            raise
        report["status"] = "success" if successful_scan else "partial"
        for kind, pid, old, new, count in events:
            self.log(kind, f"帖子ID={pid} | {state.posts[pid].row.get('标题', '')} | "
                     f"{old + ' → ' if old else ''}{new}" + (f" | 重新发布次数={count}" if kind == "REFRESH" else ""))
        return report

    def save_batches(self, path, old_markdown, chunks, state):
        # Keep every old record visible until ALL new batches have been verified.
        token = uuid.uuid4().hex
        begin, end = "CHINESEINLASYNCSTART" + token, "CHINESEINLASYNCEND" + token
        def insert(content, position):
            return self.request("PATCH", path, json={"type": "insert_content", "insert_content": {
                "content": content, "position": {"type": position}}})
        insert(begin + "\n", "start")
        insert(end + "\n", "end")
        for index, chunk in enumerate(chunks, 1):
            self.guard()
            insert(chunk, "end")
            self.log("Notion", f"暂存批次 {index}/{len(chunks)} 已提交；旧列表保留至完整验证")
        current = complete_markdown(self.request("GET", path))
        if current.count(begin) != 1 or current.count(end) != 1:
            raise RuntimeError("暂存边界不唯一，保留旧内容，停止整理")
        before, staged = current.split(end, 1)
        original = before.split(begin, 1)[1]
        if parse_markdown(original).posts != parse_markdown(old_markdown).posts:
            raise RuntimeError("暂存期间旧记录发生变化，保留全部内容")
        saved = parse_markdown(staged)
        expected_order = [p.row["帖子ID"] for p in sorted_posts(state)]
        if (saved.posts != state.posts or saved.duplicates or saved.order != expected_order
                or saved.last_success != state.last_success):
            raise RuntimeError("分批暂存数据不完整，保留旧列表，不移除任何旧记录")
        self.guard()
        return self.request("PATCH", path, json={"type": "replace_content_range",
                            "replace_content_range": {"content_range": begin + "..." + end, "content": ""}})


def split_batches(markdown):
    """Split only between complete toggles, keeping every post and body intact."""
    matches = list(DETAILS.finditer(markdown))
    pieces = [markdown[:matches[0].start()] if matches else markdown]
    pieces.extend(m.group(0) + "\n" for m in matches)
    chunks, current, posts = [], "", 0
    for piece in pieces:
        if len(json.dumps(piece, ensure_ascii=False).encode("utf-8")) > 480_000:
            raise RuntimeError("单条帖子超过 Notion 安全容量；未提交，不能截断正文")
        size = len(json.dumps(current + piece, ensure_ascii=False).encode("utf-8"))
        if current and (size > BATCH_BYTES or posts >= BATCH_POSTS):
            chunks.append(current)
            current, posts = "", 0
        current += piece
        posts += int(piece.startswith('<details'))
    if current:
        chunks.append(current)
    return chunks
