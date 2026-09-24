"""Bounded manual smoke test. Never writes to the production daily/pinned pages."""
import argparse
import json
from datetime import datetime, date

import ChineseInLA_local_scraper_cli as app
from notion_daily import DailyState, DailySync, merge_posts, render_markdown, statistics


def collect_small(target, limit):
    # Reuse existing list/detail/field extraction functions, only on page one.
    url = app.page_url(1)
    doc = app.html.fromstring(app.fetch_html(url))
    items = [item for item in app.extract_topic_links(doc, url)
             if app.clean(item.get("置顶")) != "是"][:limit]
    rows = []
    for item in items:
        try:
            row, job_type = app.process_topic(item, target_date=target)
        except Exception as exc:
            print(f"详情失败 ID={item.get('帖子ID')} | {type(exc).__name__}")
            continue
        if app.matches_day(row, target) and app.type_allowed(job_type):
            rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=date.fromisoformat, default=datetime.now(app.LA_TZ).date())
    parser.add_argument('--limit', type=int, choices=range(1, 6), default=5)
    parser.add_argument('--test-page', help='必须是标题为 招聘信息监控测试 · YYYY-MM-DD 的独立测试页')
    parser.add_argument('--apply', action='store_true', help='明确写入独立测试页；省略则仅预览')
    args = parser.parse_args()
    if args.apply and not args.test_page:
        parser.error('--apply 必须同时指定 --test-page；不能写入正式日期页')
    rows = collect_small(args.date, args.limit)
    if not rows:
        raise RuntimeError('样本中没有符合日期的普通招聘帖；不会修改 Notion')
    if any(app.clean(row.get('置顶')) == '是' or not app.matches_day(row, args.date) for row in rows):
        raise ValueError('测试样本必须是目标日期的普通帖子')
    if not args.test_page:
        state = DailyState()
        merge_posts(state, rows)
        preview = render_markdown(state, args.date)
        print(json.dumps({'status': 'memory_preview', 'rendered_characters': len(preview), **statistics(state, args.date)}, ensure_ascii=False))
        return
    writer = app.NotionWriter(args.date, guard_midnight=False)
    try:
        page_id = app.notion_page_id(args.test_page)
        page = writer.request('GET', 'pages/' + page_id)
        expected = '招聘信息监控测试 · ' + str(args.date)
        if app.page_title(page) != expected or page.get('archived') or page.get('in_trash'):
            raise ValueError('只允许写入专用测试页：' + expected)
        report = DailySync(writer.request, page_id, args.date, app._log).save(rows, apply=args.apply)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        writer.close()


if __name__ == '__main__':
    main()
