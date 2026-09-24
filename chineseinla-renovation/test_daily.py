import unittest
from datetime import date,timedelta
from unittest.mock import Mock,patch
import chineseinla_renovation as app
from test_listing import row

DAY=date(2026,9,23)
DATES=[DAY,DAY-timedelta(days=1),DAY-timedelta(days=2)]
def post(pid,time='10:00 am',**extra):
    return dict({'url':f'https://www.chineseinla.com/f/page_viewtopic/t_{pid}.html','title':f'Post {pid}','bucket':'day0','date':str(DAY),'time':time,'body':'body'},**extra)
def response(data): return Mock(json=Mock(return_value=data))
ROOT={'properties':{'title':{'type':'title','title':[{'plain_text':'装修信息监控'}]}}}
class DailyTests(unittest.TestCase):
    def test_today_boundary(self):
        html='<div class="forum_line">'+''.join(row(i,stamp='10:00 am' if i<3 else '2026-09-22') for i in range(15))+'</div>'
        posts,boundary,*_=app.extract_posts_from_html(html,app.FORUM_URL,1,None,False,None,DAY,DATES)
        self.assertEqual(len(posts),3)
        self.assertTrue(boundary)
        self.assertTrue(all(p['date']==str(DAY) for p in posts))
    def test_newest_duplicate_keeps_site_position(self):
        a=post(1,'09:00 am');new=post(1,'11:00 am',title='new')
        self.assertEqual(app.deduplicate_posts([a,post(2),new,a]),[new,post(2)])
    def test_order_not_resorted_by_timestamp(self):
        rows=[post(1,'09:00 am'),post(2,'11:00 am')]
        blocks=app.build_notion_blocks(rows,DAY,DATES)
        self.assertEqual([b['toggle']['rich_text'][1]['text']['content'] for b in blocks[2:]],['Post 1','Post 2'])
    def test_refresh_then_publish_in_title(self):
        rows=[post(1,publish_time='2026/09/23 08:00 am',refresh_time='2026/09/23 11:00 am'),post(2,publish_time='2026/09/23 07:00 am')]
        blocks=app.build_notion_blocks(rows,DAY,DATES)
        self.assertIn('11:00 am',blocks[2]['toggle']['rich_text'][0]['text']['content'])
        self.assertIn('07:00 am',blocks[3]['toggle']['rich_text'][0]['text']['content'])
    def test_metadata_extraction(self):
        self.assertEqual(app.extract_post_times('<div class="post_time"><span>发布于：2026/09/23 08:00 am</span><span>更新于：2026/09/23 11:00 am</span></div>'),('2026/09/23 08:00 am','2026/09/23 11:00 am'))
        self.assertEqual(app.extract_post_times('<div class="post_time">发布于：2026/09/23 08:00 am</div>'),('2026/09/23 08:00 am',''))
    def test_no_footer_in_timestamp_and_list_bump(self):
        html='<div class="post_time">发布于：2026/09/22, 8:55 pm 引用 禁言</div>'
        self.assertEqual(app.extract_post_times(html),('2026/09/22 8:55 pm',''))
        p=post(1,'11:00 am')
        app.set_post_times(p,html)
        self.assertEqual(p['refresh_time'],'2026-09-23 11:00 am')
    def test_unbumped_post_uses_publication(self):
        p=post(1,'08:00 am')
        app.set_post_times(p,'<div class="post_time">发布于：2026/09/23 08:00 am</div>')
        self.assertEqual(p['refresh_time'],'')

    def test_existing_child_second_page(self):
        with patch.object(app,'notion_request',side_effect=[response(ROOT),response({'results':[],'has_more':True,'next_cursor':'next'}),response({'results':[{'id':'daily','type':'child_page','child_page':{'title':str(DAY)}}]})]) as req:
            self.assertEqual(app.ensure_daily_page(DAY),'daily')
            self.assertTrue(all(c.args[0]=='GET' for c in req.call_args_list))
    def test_create_missing_child(self):
        with patch.object(app,'notion_request',side_effect=[response(ROOT),response({'results':[]}),response({'id':'new'})]) as req:
            self.assertEqual(app.ensure_daily_page(DAY),'new')
            payload=req.call_args.kwargs['json']
            self.assertEqual(payload['parent']['page_id'],app.NOTION_PAGE_ID)
            self.assertEqual(payload['properties']['title']['title'][0]['text']['content'],str(DAY))
    def test_cannot_write_parent(self):
        with patch.object(app,'ACTIVE_DAILY_PAGE_ID',None),patch.object(app,'notion_request') as req:
            with self.assertRaises(RuntimeError):app.write_new_blocks([])
            req.assert_not_called()
    def test_block_operations_target_child(self):
        with patch.object(app,'ACTIVE_DAILY_PAGE_ID','daily'),patch.object(app,'notion_request',return_value=response({'results':[]})) as req:
            app.get_all_notion_blocks()
            self.assertIn('/blocks/daily/children',req.call_args.args[1])
    def test_wrong_root_rejected(self):
        with patch.object(app,'notion_request',return_value=response({'properties':{}})):
            with self.assertRaises(RuntimeError): app.ensure_daily_page(DAY)
    def test_reject_yesterday(self):
        self.assertFalse(app.validate_posts([post(1,date='2026-09-22')],DAY,DATES))
if __name__=='__main__':unittest.main()
