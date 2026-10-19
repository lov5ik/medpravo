import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('monitor',ROOT/'monitor.py')
monitor=importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)

class FeedTests(unittest.TestCase):
    def setUp(self):
        self.scratch=ROOT/'.test-temp'
        self.scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=self.scratch)
        self.previous=monitor.DATA
        monitor.DATA=Path(self.tmp.name)
        monitor.initialize()
        # Generic feed tests use one catalog. Reserve routes are tested separately.
        self.source={**next(s for s in monitor.catalog() if s['id']=='minzdrav'),'seeds':['https://minzdrav.gov.ru/documents'],'fallback_seeds':[],'obsolete_seeds':[],'obsolete_prefixes':[]}
        self.candidate={'url':'https://minzdrav.gov.ru/documents/test','title':'Приказ о медицинской помощи детям','published_at':'2026-10-14'}
    def tearDown(self):
        monitor.DATA=self.previous
        self.assertTrue(Path(self.tmp.name).resolve().is_relative_to(self.scratch.resolve()))
        self.tmp.cleanup()
    def content(self,text='Медицинская помощь детям'):
        return {'type':'text','text':'<main>'+text+'</main>','raw':text.encode(),'url':self.candidate['url']}
    def test_initial_documents_are_visible_and_labeled(self):
        self.assertTrue(monitor.store_publication(self.source,self.candidate,self.content(),True))
        result=monitor.list_publications({})
        self.assertEqual(result['total'],1)
        self.assertEqual(result['publications'][0]['initial'],1)
        self.assertEqual(result['publications'][0]['published_at'],'2026-10-14')
    def test_layout_links_are_excluded_but_pagination_and_body_survive(self):
        html='<header><a href="/documents/menu.pdf">Закон о медицине</a></header><div class="footer"><div><a href="/policy.pdf">О персональных данных</a></div></div><main><a href="/documents/real.pdf">Приказ о медицинской помощи</a></main><nav class="pagination"><a href="?page=2">2</a></nav>'
        found,pages=monitor.discover(html,'https://minzdrav.gov.ru/documents',self.source)
        self.assertEqual([p['url'] for p in found],['https://minzdrav.gov.ru/documents/real.pdf'])
        self.assertEqual(pages,['https://minzdrav.gov.ru/documents?page=2'])
        self.assertNotIn('персональных',monitor.parse_page(html).visible)
    def test_known_document_never_becomes_change_event(self):
        monitor.store_publication(self.source,self.candidate,self.content('Прежний текст'),False)
        self.assertFalse(monitor.store_publication(self.source,self.candidate,self.content('Другой текст'),False))
        self.assertEqual(monitor.list_publications({})['total'],1)
        self.assertFalse((monitor.DATA/'snapshots').exists())
    def test_known_url_is_not_downloaded_again(self):
        monitor.store_publication(self.source,self.candidate,self.content(),False)
        listing={**self.content(), 'text':'<a href="/documents/test">Приказ о медицинской помощи детям</a>'}
        with patch.object(monitor,'fetch',return_value=listing) as fetch:
            result=monitor.scan_source(self.source)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(result['added'],0)
    def test_medical_topics_and_unknown_binary_are_retained(self):
        relevance,topics,_=monitor.classify('Приказ об ОМС, МРТ и анестезиологии','',self.source)
        self.assertEqual(relevance,'medical')
        self.assertIn('ОМС',topics)
        self.assertIn('МРТ / КТ',topics)
        self.assertIn('Наркоз',topics)
        unknown={**self.candidate,'title':'Документ.pdf'}
        monitor.store_publication(self.source,unknown,{'type':'binary','raw':b'%PDF-test'},False)
        self.assertEqual(monitor.list_publications({})['publications'][0]['relevance'],'review')
    def test_other_topics_are_separate_but_not_deleted(self):
        source=next(s for s in monitor.catalog() if s['id']=='kaluga-city')
        monitor.store_publication(source,{**self.candidate,'title':'Постановление о межевании улицы'},self.content('Кадастровые участки'),False)
        self.assertEqual(monitor.list_publications({})['total'],0)
        self.assertEqual(monitor.list_publications({'relevance':['all']})['total'],1)
    def test_search_is_case_insensitive_for_russian(self):
        monitor.store_publication(self.source,self.candidate,self.content(),False)
        self.assertEqual(monitor.list_publications({'q':['ПОМОЩИ ДЕТЯМ']})['total'],1)
        self.assertEqual(monitor.list_publications({'q':['%']})['total'],0)
    def test_pagination_does_not_hide_older_documents(self):
        for i in range(55):
            monitor.store_publication(self.source,{**self.candidate,'url':self.candidate['url']+str(i)},self.content(),False)
        first=monitor.list_publications({})
        second=monitor.list_publications({'offset':['50']})
        self.assertEqual(first['total'],55)
        self.assertEqual(len(first['publications']),50)
        self.assertEqual(len(second['publications']),5)
        self.assertFalse({p['url'] for p in first['publications']} & {p['url'] for p in second['publications']})
    def test_failed_download_does_not_initialize_source(self):
        listing={**self.content(),'text':'<a href="/documents/test">Приказ о медицинской помощи детям</a>'}
        with patch.object(monitor,'fetch',side_effect=[listing,OSError('synthetic')]):
            result=monitor.scan_source(self.source)
        self.assertEqual(result['status'],'partial')
        with monitor.connection() as con:
            row=con.execute('SELECT * FROM sources WHERE id=?',(self.source['id'],)).fetchone()
            self.assertEqual(row['initialized'],0)
            self.assertIsNone(row['last_success'])
    def test_dynamic_listing_is_not_success(self):
        with patch.object(monitor,'fetch',return_value=self.content('<div id="app"></div>')):
            result=monitor.scan_source(self.source)
        self.assertEqual(result['status'],'needs_adapter')
    def test_scope_domains_and_portal_numbers(self):
        portal=next(s for s in monitor.catalog() if s['id']=='pravo')
        text=''.join('<a href="/document/'+number+'">Закон об охране здоровья граждан</a>' for number in ['0001202610140001','4000202610140001','7700202610140001'])
        found,_=monitor.discover(text,portal['seeds'][0],portal)
        self.assertEqual(len(found),2)
        self.assertFalse(monitor.permitted('https://minzdrav.gov.ru.attacker.example/doc',self.source))
        with self.assertRaises(ValueError):
            monitor.SafeRedirect(self.source).redirect_request(None,None,302,'',{},'http://127.0.0.1/')
    def test_real_world_news_and_section_links_are_rejected(self):
        self.assertFalse(monitor.is_publication_link('https://minzdrav.gov.ru/news/123','Об изменениях в законодательстве о медицинских изделиях',self.source))
        self.assertFalse(monitor.is_publication_link('https://minzdrav.gov.ru/','Федеральный портал нормативных правовых актов',self.source))
        self.assertFalse(monitor.is_publication_link('https://minzdrav.gov.ru/documents','Нормативные правовые акты',self.source))
        self.assertTrue(monitor.is_publication_link('https://minzdrav.gov.ru/documents/123','Приказ Минздрава России от 14.10.2026 № 123',self.source))
    def test_unicode_file_paths_are_encoded(self):
        self.assertIn('%E2%84%96',monitor.canonical('https://minzdrav.gov.ru/файлы/№_1.pdf'))
        self.assertEqual(monitor.canonical(monitor.canonical('https://minzdrav.gov.ru/файлы/№_1.pdf')),monitor.canonical('https://minzdrav.gov.ru/файлы/№_1.pdf'))
    def test_rss_dates_projects_and_pagination(self):
        rss='<rss><channel><item><title>Проект приказа о медицинской помощи</title><link>https://minzdrav.gov.ru/documents/test</link><pubDate>Wed, 14 Oct 2026 06:00:00 GMT</pubDate></item></channel></rss>'
        found,_=monitor.discover(rss,self.source['seeds'][0],self.source)
        monitor.store_publication(self.source,found[0],self.content(),False)
        row=monitor.list_publications({})['publications'][0]
        self.assertEqual(row['category'],'Проект')
        self.assertEqual(row['published_at'],'2026-10-14')
        found,pages=monitor.discover('<a href="?index=2">Следующая страница</a>',self.source['seeds'][0],self.source)
        self.assertEqual(len(found),0)
        self.assertEqual(len(pages),1)
    def test_http_auth_review_and_removed_email(self):
        monitor.store_publication(self.source,self.candidate,self.content(),False)
        with patch.dict(os.environ,{'ADMIN_USER':'owner','ADMIN_PASSWORD':'qa-password-long-enough'}):
            server=monitor.ThreadingHTTPServer(('127.0.0.1',0),monitor.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base='http://127.0.0.1:'+str(server.server_port)
            auth='Basic '+base64.b64encode(b'owner:qa-password-long-enough').decode()
            try:
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(base+'/api/state')
                self.assertEqual(err.exception.code,401)
                state=json.load(urllib.request.urlopen(urllib.request.Request(base+'/api/state',headers={'Authorization':auth})))
                self.assertFalse(state['email'])
                self.assertNotIn('mail_ready',state)
                payload=json.dumps({'url':self.candidate['url'],'reviewed':True}).encode()
                request=urllib.request.Request(base+'/api/review',data=payload,headers={'Authorization':auth,'Content-Type':'application/json','X-MedPravo-Request':'1'})
                self.assertTrue(json.load(urllib.request.urlopen(request))['saved'])
                self.assertEqual(monitor.list_publications({'unread':['1']})['total'],0)
                request=urllib.request.Request(base+'/api/review',data=payload,headers={'Authorization':auth,'Content-Type':'application/json','X-MedPravo-Request':'1','Origin':'https://attacker.example'})
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(request)
                self.assertEqual(err.exception.code,403)
            finally:
                server.shutdown();server.server_close();thread.join(timeout=3)

if __name__=='__main__':
    unittest.main()
