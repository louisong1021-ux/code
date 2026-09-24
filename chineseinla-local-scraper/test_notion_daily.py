import copy
import json
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch
from pathlib import Path

import notion_daily as d
import ChineseInLA_local_scraper_cli as app

DAY = date(2026, 9, 23)
NOW = datetime(2026, 9, 23, 12, 15, tzinfo=d.LA)


def row(pid="1", time="09:10", **values):
    return {"帖子ID": pid, "标题": "测试招聘", "发布时间": "2026/09/23 08:00 am",
            "刷新时间": "2026/09/23 " + time, "详情URL": f"https://example.com/t_{pid}.html",
            "正文": "正文 <details> & [链接] $25 **不是字段提取**\n第二行", **values}


class FakeNotion:
    def __init__(self, markdown=""):
        self.markdown = markdown
        self.calls = []
        self.fail = False
        self.commit_then_timeout = False
        self.concurrent = False
        self.truncated = False

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "PATCH":
            if self.fail:
                raise RuntimeError("HTTP 500")
            payload = kwargs["json"]
            if payload['type'] == 'replace_content':
                self.markdown = payload['replace_content']['new_str']
            elif payload['type'] == 'insert_content':
                op = payload['insert_content']
                self.markdown = (op['content'] + self.markdown if op['position']['type'] == 'start'
                                 else self.markdown + op['content'])
            elif payload['type'] == 'replace_content_range':
                op = payload['replace_content_range']
                begin, end = op['content_range'].split('...')
                a = self.markdown.index(begin)
                b = self.markdown.index(end, a) + len(end)
                self.markdown = self.markdown[:a] + op['content'] + self.markdown[b:]
            if self.commit_then_timeout:
                raise TimeoutError("response lost")
        if self.concurrent and len(self.calls) == 2:
            self.markdown += "\n用户编辑"
        return {"object": "page_markdown", "markdown": self.markdown,
                "truncated": self.truncated, "unknown_block_ids": []}


class MergeTests(unittest.TestCase):
    def test_first_and_same_time(self):
        state = d.DailyState()
        c, _ = d.merge_posts(state, [row()])
        self.assertEqual(c['new'], 1)
        self.assertEqual(state.posts['1'].reposts, 0)
        self.assertNotIn('重新发布 0 次', d.render_markdown(state, DAY))
        c, events = d.merge_posts(state, [row(), row()])
        self.assertEqual(c['unchanged_time'], 1)
        self.assertEqual(events, [])
        self.assertEqual(len(state.posts), 1)

    def test_refresh_once_many_restart(self):
        state = d.DailyState()
        for t in ('09:10', '09:10', '10:25', '11:38', '11:38'):
            d.merge_posts(state, [row(time=t)])
        self.assertEqual(state.posts['1'].reposts, 2)
        restored = d.parse_markdown(d.render_markdown(state, DAY))
        d.merge_posts(restored, [row(time='11:38')])
        self.assertEqual(restored.posts['1'].reposts, 2)
        d.merge_posts(restored, [row(time='12:00')])
        self.assertEqual(restored.posts['1'].reposts, 3)

    def test_fields_fill_and_do_not_erase(self):
        state = d.DailyState()
        d.merge_posts(state, [row(邮箱='未采集')])
        d.merge_posts(state, [row(邮箱='abc@example.com', 电话='123', 公司名='公司')])
        for empty in ('', '未采集', None, 'None', 'null'):
            d.merge_posts(state, [row(邮箱=empty, 电话=empty, 公司名=empty, 正文=empty)])
        self.assertEqual(state.posts['1'].row['邮箱'], 'abc@example.com')
        self.assertEqual(state.posts['1'].row['电话'], '123')
        self.assertEqual(state.posts['1'].row['公司名'], '公司')
        self.assertEqual(state.posts['1'].reposts, 0)

    def test_normalized_time_and_publish_fallback(self):
        state = d.DailyState()
        d.merge_posts(state, [row(刷新时间='', 发布时间='2026/09/23 12:00 am')])
        self.assertEqual(d.clock_text(state.posts['1'].row), '00:00')
        d.merge_posts(state, [row(刷新时间='2026/09/23, 12:00 am')])
        self.assertEqual(state.posts['1'].reposts, 0)
        d.merge_posts(state, [row(刷新时间='2026-09-23 00:00')])
        self.assertEqual(state.posts['1'].reposts, 0)
        d.merge_posts(state, [row(刷新时间='2026/09/23 12:00 pm')])
        self.assertEqual(d.clock_text(state.posts['1'].row), '12:00')
        self.assertEqual(state.posts['1'].reposts, 1)

    def test_sort_renumber_and_retain_absent(self):
        state = d.DailyState()
        d.merge_posts(state, [row('1', '08:30'), row('2', '11:42')])
        self.assertEqual([p.row['帖子ID'] for p in d.sorted_posts(state)], ['2', '1'])
        d.merge_posts(state, [row('1', '11:55')])
        text = d.render_markdown(state, DAY)
        self.assertIn('001｜11:55｜测试招聘｜重新发布 1 次', text)
        self.assertIn('002｜11:42｜测试招聘', text)
        self.assertEqual(len(d.parse_markdown(text).posts), 2)

    def test_statistics(self):
        state = d.DailyState()
        d.merge_posts(state, [row('1', 邮箱='abc@example.com'), row('2', 发布时间='2025/09/23', 电话='123')])
        d.merge_posts(state, [row('1', '11:00', 邮箱='未采集')])
        self.assertEqual(d.statistics(state, DAY), dict(total=2, published_today=1, reposted=1, email=1, phone=1))

    def test_legacy_duplicates_do_not_count_scans(self):
        chunks = []
        for time in ('09:10', '09:10', '10:25', '10:25', '11:38'):
            state = d.DailyState()
            d.merge_posts(state, [row(time=time)])
            text = d.render_markdown(state, DAY)
            text = text[text.index('<details>'):].replace('<br>重新发布次数：0', '')
            text = text.replace('001｜'+time+'｜', '')
            chunks.append('## 刷新或发布招聘 · 2026-09-23 · 采集于 test\n'+text)
        restored = d.parse_markdown('\n'.join(chunks))
        self.assertEqual(restored.duplicates, 4)
        self.assertEqual(restored.posts['1'].reposts, 2)
        self.assertEqual(d.clock_text(restored.posts['1'].row), '11:38')

    def test_unknown_content_fail_closed(self):
        for text in ('# 我的笔记', '<page url="x">子页</page>', '<details>\n<summary>坏帖子</summary>\n\t正文\n</details>'):
            with self.assertRaises(ValueError):
                d.parse_markdown(text)

    def test_notion_automatic_email_links(self):
        state = d.DailyState()
        d.merge_posts(state, [row(邮箱='a@example.com', 正文='联系 a@example.com')])
        text = d.render_markdown(state, DAY)
        text = text.replace('邮箱：a@example.com', '[邮箱：a@example.com](mailto:邮箱：a@example.com)')
        text = text.replace('联系 a@example.com', '联系 [a@example.com](mailto:a@example.com)')
        self.assertEqual(d.parse_markdown(text).posts, state.posts)

    def test_roundtrip_escape_long_body(self):
        state = d.DailyState()
        d.merge_posts(state, [row(正文=('电话：不能从这里重新提取 <br> \\ $50\n联系电话： ______________________________\nEmail： _________________________________\n'*200))])
        restored = d.parse_markdown(d.render_markdown(state, DAY))
        self.assertEqual(state.posts, restored.posts)


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Mock()

    def sync(self, fake, guard=lambda: None):
        return d.DailySync(fake, 'test-page', DAY, self.log, guard)

    def test_no_local_files_for_save_or_preview(self):
        fake = FakeNotion()
        with patch('builtins.open', side_effect=AssertionError('File writes forbidden')), patch.object(Path, 'mkdir', side_effect=AssertionError('Directory creation forbidden')), patch.object(Path, 'write_text', side_effect=AssertionError('File writes forbidden')):
            self.sync(fake).save([row()], now=NOW, apply=False)
            self.sync(fake).save([row()], now=NOW)
            with patch.object(app, '_log'), patch.object(app, 'crawl_live', return_value=([row()], {'written': 1, 'stop_reason': 'forum_end'})), patch('builtins.print'):
                result = app.run(dry_run=True)
            self.assertEqual(result['storage'], 'memory_only')

    def test_300_posts_constant_api_calls_and_idempotence(self):
        fake = FakeNotion()
        rows = [row(str(i)) for i in range(300)]
        report = self.sync(fake).save(rows, now=NOW)
        self.assertEqual(report['total'], 300)
        self.assertLess(len(fake.calls), 12)
        self.assertNotIn('CHINESEINLASYNC', fake.markdown)
        self.assertNotIn('backup', report)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        fake.calls.clear()
        report = self.sync(fake).save(rows, now=NOW)
        self.assertEqual(report['new'], 0)
        self.assertEqual(report['refreshed'], 0)
        self.assertEqual(len(d.parse_markdown(fake.markdown).posts), 300)

    def test_batch_failure_preserves_old_records_and_recovers(self):
        fake = FakeNotion()
        self.sync(fake).save([row('1')], now=NOW)
        old = d.parse_markdown(fake.markdown)
        calls = 0
        def broken(method, path, **kwargs):
            nonlocal calls
            if method == 'PATCH':
                calls += 1
                if calls == 4:
                    raise TimeoutError('batch failed')
            return fake(method, path, **kwargs)
        incoming = [row('1','10:25'), row('2'), row('3')]
        with patch.object(d, 'BATCH_POSTS', 1):
            with self.assertRaises(TimeoutError):
                self.sync(broken).save(incoming, now=NOW)
            partial = d.parse_markdown(fake.markdown)
            self.assertTrue(set(old.posts) <= set(partial.posts))
            self.sync(fake).save(incoming, now=NOW)
        final = d.parse_markdown(fake.markdown)
        self.assertEqual(len(final.posts), 3)
        self.assertEqual(final.posts['1'].reposts, 1)
        self.assertEqual(final.duplicates, 0)
        self.assertNotIn('CHINESEINLASYNC', fake.markdown)

    def test_failure_does_not_clear_or_report_success(self):
        fake = FakeNotion()
        self.sync(fake).save([row()], now=NOW)
        before = fake.markdown
        fake.fail = True
        with self.assertRaisesRegex(RuntimeError, 'HTTP 500'):
            self.sync(fake).save([row(time='10:25')], now=NOW)
        self.assertEqual(fake.markdown, before)
        self.assertTrue(self.log.called)

    def test_lost_response_then_restart_no_double_increment(self):
        fake = FakeNotion()
        self.sync(fake).save([row()], now=NOW)
        fake.commit_then_timeout = True
        with self.assertRaises(TimeoutError):
            self.sync(fake).save([row(time='10:25')], now=NOW)
        fake.commit_then_timeout = False
        report = self.sync(fake).save([row(time='10:25')], now=NOW)
        self.assertEqual(report['refreshed'], 0)
        self.assertEqual(d.parse_markdown(fake.markdown).posts['1'].reposts, 1)

    def test_partial_scan_preserves_absent_posts_and_last_success(self):
        fake = FakeNotion()
        self.sync(fake).save([row('1'), row('2')], now=NOW)
        report = self.sync(fake).save([row('1', '10:00')], successful_scan=False,
                                     now=NOW.replace(hour=13))
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['total'], 2)
        self.assertIn('12:15:00', report['last_success'])

    def test_preview_never_writes(self):
        fake = FakeNotion()
        self.sync(fake).save([row()], apply=False, now=NOW)
        self.assertEqual([c[0] for c in fake.calls], ['GET'])

    def test_incomplete_and_concurrent_reads_abort(self):
        for flag in ('truncated', 'concurrent'):
            fake = FakeNotion()
            setattr(fake, flag, True)
            with self.assertRaises(RuntimeError):
                self.sync(fake).save([row()], now=NOW)
            self.assertFalse(any(c[0] == 'PATCH' for c in fake.calls))

    def test_midnight_aborts_before_read(self):
        with patch.object(app, 'load_notion_token', return_value='dummy'):
            writer = app.NotionWriter(DAY)
        self.addCleanup(writer.close)
        writer.started_date = DAY
        with patch.object(app, 'datetime') as clock:
            clock.now.return_value = NOW.replace(day=24, hour=0)
            fake = FakeNotion()
            with self.assertRaisesRegex(RuntimeError, '午夜'):
                self.sync(fake, writer.guard_date).save([row()], now=NOW)
            self.assertEqual(fake.calls, [])
        writer.guard_midnight = False
        with patch.object(app, 'datetime') as clock:
            clock.now.return_value = NOW.replace(day=24, hour=0)
            writer.guard_date()  # Explicit historical target is still valid after midnight.

    def test_no_cross_day_state_reuse(self):
        old = d.DailyState()
        d.merge_posts(old, [row()])
        d.merge_posts(old, [row(time='10:25')])
        fresh = d.DailyState()
        d.merge_posts(fresh, [row(刷新时间='2026/09/24 00:05')])
        self.assertEqual(old.posts['1'].reposts, 1)
        self.assertEqual(fresh.posts['1'].reposts, 0)

    def test_oversize_payload_aborts_without_write(self):
        fake = FakeNotion()
        with self.assertRaisesRegex(RuntimeError, '容量'):
            self.sync(fake).save([row(正文='大' * 200000)], now=NOW)
        self.assertFalse(any(c[0] == 'PATCH' for c in fake.calls))

    def test_midnight_between_preflight_and_write(self):
        fake = FakeNotion()
        guard = Mock(side_effect=[None, None, RuntimeError('午夜')])
        with self.assertRaisesRegex(RuntimeError, '午夜'):
            self.sync(fake, guard).save([row()], now=NOW)
        self.assertFalse(any(c[0] == 'PATCH' for c in fake.calls))

    def test_writer_finish_integration(self):
        with patch.object(app, 'load_notion_token', return_value='dummy'):
            writer = app.NotionWriter(DAY, guard_midnight=False)
        self.addCleanup(writer.close)
        writer.daily_page_id = 'test-page'
        fake = FakeNotion()
        writer.request = fake
        writer.write(row())
        self.assertEqual(fake.calls, [])
        with patch.object(app, 'BASE_DIR', Path(self.temp.name)), patch.object(app, '_log'):
            writer.finish({'stop_reason': 'target_date_boundary'})
        self.assertEqual(writer.daily_report['status'], 'success')
        self.assertEqual(writer.count, 1)
        self.assertEqual(writer.daily_report['total'], 1)

    def test_transport_uses_utf8_payload_without_secret_output(self):
        with patch.object(app, 'load_notion_token', return_value='dummy'):
            writer = app.NotionWriter(DAY, guard_midnight=False)
        self.addCleanup(writer.close)
        writer.session.request = Mock(return_value=Mock(ok=True))
        writer.request('PATCH', 'pages/test/markdown', json={'内容': '中文'})
        kwargs = writer.session.request.call_args.kwargs
        self.assertNotIn('json', kwargs)
        self.assertEqual(json.loads(kwargs['data']), {'内容': '中文'})
        self.assertIn('中文'.encode(), kwargs['data'])

    def test_empty_failed_scan_does_not_create_page(self):
        with patch.object(app, 'load_notion_token', return_value='dummy'):
            writer = app.NotionWriter(DAY, guard_midnight=False)
        self.addCleanup(writer.close)
        writer.request = Mock()
        writer.finish({'list_failed': 1, 'stop_reason': 'consecutive_empty_or_failed'})
        writer.request.assert_not_called()
        self.assertEqual(writer.daily_report['status'], 'skipped')


if __name__ == '__main__':
    unittest.main()
