import unittest
import tempfile
from pathlib import Path
from datetime import date
from unittest.mock import Mock, patch

import ChineseInLA_local_scraper_cli as app


class TodaySyncTests(unittest.TestCase):
    def test_parameter_errors_fall_back_to_today(self):
        for error in (ValueError("invalid date"), RuntimeError("page denied"),
                      app.requests.Timeout("timeout")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory, \
                    patch.object(app, "RESULT_JSON", Path(directory) / "result.json"), \
                    patch.object(app, "read_date_page", side_effect=error), \
                    patch.object(app, "NotionWriter") as factory, \
                    patch.object(app, "crawl_live", return_value=([], {})) as crawl, \
                    patch("builtins.print"):
                writer = factory.return_value
                writer.count = 0
                writer.daily_page_id = None
                result = app.run(date_page="invalid-input")
                today = app.datetime.now(app.LA_TZ).date()
                factory.assert_called_once_with(today, guard_midnight=True)
                crawl.assert_called_once_with(today, writer)
                self.assertEqual(result["target_date"], str(today))
                self.assertIsNone(result["date_source_page_id"])
                self.assertTrue(result["date_parameter_fallback"])

    def test_empty_parameter_uses_today_without_reading_page(self):
        for value in (None, "", "  "):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory, \
                    patch.object(app, "RESULT_JSON", Path(directory) / "result.json"), \
                    patch.object(app, "read_date_page") as read, \
                    patch.object(app, "crawl_live", return_value=([], {})) as crawl, \
                    patch("builtins.print"):
                result = app.run(date_page=value, local_only=True)
                read.assert_not_called()
                crawl.assert_called_once_with(app.datetime.now(app.LA_TZ).date(), None)
                self.assertFalse(result["date_parameter_fallback"])

    def test_parameter_page_dates(self):
        for title in ("2026-09-22", "任务 2026/9/22", "2026年9月22日 招聘"):
            self.assertEqual(app.date_from_title(title), date(2026, 9, 22))
        for title in ("招聘监控", "2026-02-30", "2026-09-21 到 2026-09-22"):
            with self.assertRaises(ValueError):
                app.date_from_title(title)

    def test_parameter_page_id_and_link(self):
        expected = "3e0c18c6-fba4-8125-861c-fc246bbb0092"
        for value in (expected, expected.replace("-", ""),
                      "https://www.notion.so/Date-" + expected.replace("-", "") + "?source=copy_link"):
            self.assertEqual(app.notion_page_id(value), expected)
        with self.assertRaises(ValueError):
            app.notion_page_id("https://example.com/" + expected)

    def test_source_page_read_is_get_only(self):
        with patch.object(app, "load_notion_token", return_value="dummy"), \
                patch.object(app.NotionWriter, "request", return_value={"properties": {
                    "title": {"type": "title", "title": [{"plain_text": "2026-09-22"}]}}}) as request:
            self.assertEqual(app.read_date_page(app.NOTION_PAGE_ID), date(2026, 9, 22))
            request.assert_called_once_with("GET", "pages/" + app.NOTION_PAGE_ID)

    def test_historical_date_propagates_through_run(self):
        target = date(2025, 1, 2)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(app, "RESULT_JSON", Path(directory) / "result.json"), \
                patch.object(app, "read_date_page", return_value=target), \
                patch.object(app, "NotionWriter") as factory, \
                patch.object(app, "crawl_live", return_value=([], {})) as crawl, \
                patch("builtins.print"):
            writer = factory.return_value
            writer.count = 0
            writer.daily_page_id = None
            result = app.run(date_page=app.NOTION_PAGE_ID)
            factory.assert_called_once_with(target, guard_midnight=False)
            crawl.assert_called_once_with(target, writer)
            self.assertEqual(result["target_date"], "2025-01-02")

    def test_explicit_historical_date_can_write(self):
        with patch.object(app, "load_notion_token", return_value="dummy"):
            writer = app.NotionWriter(date(2025, 1, 2), guard_midnight=False)
        writer.daily_page_id = "historical-day"
        writer.request = Mock(return_value={"results": [{}, {}]})
        writer.write({"发布时间": "2025/01/02", "标题": "历史招聘", "详情URL": "https://example.com/job"})
        self.assertEqual(writer.count, 1)
        self.assertEqual(writer.request.call_args.args[1], "blocks/historical-day/children")
        writer.close()

    def test_refresh_dates_are_strict(self):
        today = date(2026, 9, 23)
        for value in ("2026/09/23, 8:12 am", "2026-9-23 11:59 pm"):
            self.assertTrue(app.date_matches(value, today))
        for value in ("", "今天", "8:12 am", "2026/09/22", "2026/09/24", "2026/02/30"):
            self.assertFalse(app.date_matches(value, today))

    def test_publish_date_does_not_replace_missing_refresh(self):
        publish, refresh = app.extract_desktop_post_times(
            '<div class="post_time"><span>发布于：2026/09/23 8:00 am</span></div>')
        self.assertTrue(publish)
        self.assertEqual(refresh, "")
        self.assertFalse(app.date_matches(refresh, date(2026, 9, 23)))
        self.assertTrue(app.matches_day({"发布时间": publish, "刷新时间": refresh}, date(2026, 9, 23)))

    def test_either_date_qualifies(self):
        today = date(2026, 9, 23)
        for published, refreshed, expected in [
            ("2026/09/23", "", True),
            ("2026/09/23", "2026/09/22", True),
            ("2026/09/22", "2026/09/23", True),
            ("2026/09/23", "2026/09/23", True),
            ("2026/09/22", "2026/09/22", False),
            ("", "", False),
        ]:
            with self.subTest(published=published, refreshed=refreshed):
                self.assertEqual(app.matches_day({"发布时间": published, "刷新时间": refreshed}, today), expected)

    def test_crawler_only_writes_today_and_continues_after_old_post(self):
        today = date(2026, 9, 23)
        items = [{"帖子ID": str(i), "标题": str(i), "详情URL": "https://example.com/" + str(i)} for i in range(3)]
        rows = [{**item, "刷新时间": refresh} for item, refresh in zip(items, ("2026/09/22", "", "2026/09/23"))]
        rows[1]["发布时间"] = "2026/09/23"
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
        self.assertEqual(result, rows[1:])
        self.assertEqual(stats["date_skipped"], 1)
        self.assertEqual(writer.write.call_count, 2)
        self.assertEqual(csv.call_count, 2)

    def test_notion_append_keeps_existing_content_and_checks_date(self):
        today = app.datetime.now(app.LA_TZ).date()
        with patch.object(app, "load_notion_token", return_value="dummy"):
            writer = app.NotionWriter(today)
        writer.request = Mock(return_value={"results": [{"id": "heading"}, {"id": "post"}]})
        writer.daily_page_id = "daily-page"
        row = {"标题": "测试招聘", "刷新时间": "", "发布时间": str(today), "正文": "正文" * 2000,
               "详情URL": "https://example.com/job"}
        writer.write(row)
        args, kwargs = writer.request.call_args
        self.assertEqual(args, ("PATCH", "blocks/daily-page/children"))
        self.assertEqual(writer.count, 1)
        children = kwargs["json"]["children"][1]["toggle"]["children"]
        self.assertGreater(len(children), 2)
        with self.assertRaises(ValueError):
            writer.write({**row, "刷新时间": "2001/01/01", "发布时间": "2001/01/01"})
        self.assertEqual(writer.request.call_count, 1)
        writer.close()

    def test_create_date_page_under_monitor(self):
        with patch.object(app, "load_notion_token", return_value="dummy"):
            writer = app.NotionWriter(date(2026, 9, 23))
        writer.request = Mock(side_effect=[{"results": [], "has_more": False}, {"id": "new-day"}])
        self.assertEqual(writer.ensure_daily_page(), "new-day")
        args, kwargs = writer.request.call_args
        self.assertEqual(args, ("POST", "pages"))
        self.assertEqual(kwargs["json"]["parent"]["page_id"], app.NOTION_PAGE_ID)
        self.assertEqual(kwargs["json"]["properties"]["title"]["title"][0]["text"]["content"], "2026-09-23")
        self.assertEqual(writer.ensure_daily_page(), "new-day")
        self.assertEqual(writer.request.call_count, 2)
        writer.close()

    def test_reuse_date_page_on_later_pagination(self):
        with patch.object(app, "load_notion_token", return_value="dummy"):
            writer = app.NotionWriter(date(2026, 9, 23))
        writer.request = Mock(side_effect=[
            {"results": [], "has_more": True, "next_cursor": "next"},
            {"results": [{"id": "existing", "type": "child_page", "child_page": {"title": "2026-09-23"}}], "has_more": False}])
        self.assertEqual(writer.ensure_daily_page(), "existing")
        self.assertEqual(writer.request.call_args.kwargs["params"]["start_cursor"], "next")
        self.assertTrue(all(call.args[0] == "GET" for call in writer.request.call_args_list))
        writer.close()

    def test_duplicate_date_pages_abort(self):
        with patch.object(app, "load_notion_token", return_value="dummy"):
            writer = app.NotionWriter(date(2026, 9, 23))
        writer.request = Mock(return_value={"results": [
            {"id": str(i), "type": "child_page", "child_page": {"title": "2026-09-23"}} for i in range(2)]})
        with self.assertRaises(RuntimeError):
            writer.ensure_daily_page()
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
