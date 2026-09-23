import unittest
from datetime import date
from unittest.mock import Mock, patch

import ChineseInLA_local_scraper_cli as app


class TodaySyncTests(unittest.TestCase):
    def test_refresh_dates_are_strict(self):
        today = date(2026, 9, 23)
        for value in ("2026/09/23, 8:12 am", "2026-9-23 11:59 pm"):
            self.assertTrue(app.refreshed_on(value, today))
        for value in ("", "今天", "8:12 am", "2026/09/22", "2026/09/24", "2026/02/30"):
            self.assertFalse(app.refreshed_on(value, today))

    def test_publish_date_does_not_replace_missing_refresh(self):
        publish, refresh = app.extract_desktop_post_times(
            '<div class="post_time"><span>发布于：2026/09/23 8:00 am</span></div>')
        self.assertTrue(publish)
        self.assertEqual(refresh, "")
        self.assertFalse(app.refreshed_on(refresh, date(2026, 9, 23)))

    def test_crawler_only_writes_today_and_continues_after_old_post(self):
        today = date(2026, 9, 23)
        items = [{"帖子ID": str(i), "标题": str(i), "详情URL": "https://example.com/" + str(i)} for i in range(3)]
        rows = [{**item, "刷新时间": refresh} for item, refresh in zip(items, ("2026/09/22", "", "2026/09/23"))]
        writer = Mock()
        app.STOP_EVENT.clear()
        with patch.object(app, "fetch_html", return_value="<html></html>"), \
                patch.object(app, "extract_topic_links", side_effect=[items] + [[]] * 20), \
                patch.object(app, "process_topic", side_effect=[(r, "招聘") for r in rows]), \
                patch.object(app, "type_allowed", return_value=True), \
                patch.object(app, "ensure_csv_header"), \
                patch.object(app, "append_csv_row") as csv, \
                patch.object(app, "_log"), patch.object(app, "_log_captured_row"):
            result, stats = app.crawl_live(today, writer)
        self.assertEqual(len(result), 1)
        self.assertEqual(stats["date_skipped"], 2)
        writer.write.assert_called_once_with(rows[2])
        csv.assert_called_once_with(rows[2])

    def test_notion_append_keeps_existing_content_and_checks_date(self):
        today = app.datetime.now(app.LA_TZ).date()
        with patch.object(app, "load_notion_token", return_value="dummy"):
            writer = app.NotionWriter(today)
        writer.request = Mock(return_value={"results": [{"id": "heading"}, {"id": "post"}]})
        row = {"标题": "测试招聘", "刷新时间": str(today), "正文": "正文" * 2000,
               "详情URL": "https://example.com/job"}
        writer.write(row)
        args, kwargs = writer.request.call_args
        self.assertEqual(args, ("PATCH", "blocks/" + app.NOTION_PAGE_ID + "/children"))
        self.assertEqual(writer.count, 1)
        children = kwargs["json"]["children"][1]["toggle"]["children"]
        self.assertGreater(len(children), 2)
        with self.assertRaises(ValueError):
            writer.write({**row, "刷新时间": "2001/01/01"})
        self.assertEqual(writer.request.call_count, 1)
        writer.close()

    def test_wrong_page_title_aborts(self):
        with patch.object(app, "load_notion_token", return_value="dummy"):
            writer = app.NotionWriter(date(2026, 9, 23))
        writer.request = Mock(return_value={"properties": {"title": {"type": "title", "title": []}}})
        with self.assertRaises(RuntimeError):
            writer.check_page()
        writer.close()


if __name__ == "__main__":
    unittest.main()
