import unittest
from datetime import date, timedelta
import chineseinla_renovation as app


def row(index, marker="read", stamp="2026-09-22"):
    return (f'<div class="topic_list_detail"><span class="{marker}"></span>'
            f'<a class="title" href="/f/page_viewtopic/t_{index}.html">Post {index}</a>'
            f'<span>{stamp} 0 100</span></div>')


class ListingTests(unittest.TestCase):
    def parse(self, sticky_count=0, normal_count=15):
        content = '<div class="forum_line"><div class="bid_box">' + row(900, "bid") + '</div>'
        content += ''.join(row(1000+i, "sticky") for i in range(sticky_count))
        content += ''.join(row(i) for i in range(normal_count))
        content += '<div class="bid_box">' + row(901, "bid") + '</div>'
        content += '<div class="topic_list_detail"></div></div>'
        today = date(2026, 9, 23)
        return app.parse_all_rows(app.read_all_topic_rows(content, app.FORUM_URL), today,
                                  [today, today-timedelta(days=1), today-timedelta(days=2)])

    def test_first_and_second_page_positions_differ_without_ambiguity(self):
        first = self.parse(sticky_count=18)
        second = self.parse()
        selected, start = app.find_main_window(first, 1, None, False, None)
        selected2, start2 = app.find_main_window(second, 2, None, True, start)
        self.assertNotEqual(start, start2)
        self.assertEqual([r['url'] for r in selected], [r['url'] for r in selected2])
        self.assertEqual(len(selected2), 15)

    def test_unexpected_count_still_stops(self):
        with self.assertRaises(RuntimeError):
            app.find_main_window(self.parse(normal_count=14), 2, None, True, 22)

    def test_invalid_dates_still_stop(self):
        rows = self.parse()
        rows[3]['kind'] = 'unknown'
        with self.assertRaises(RuntimeError):
            app.find_main_window(rows, 2, None, True, 22)

    def test_duplicate_urls_still_stop(self):
        rows = self.parse()
        rows[3]['url'] = rows[2]['url']
        with self.assertRaises(RuntimeError):
            app.find_main_window(rows, 2, None, True, 22)


if __name__ == '__main__':
    unittest.main()
