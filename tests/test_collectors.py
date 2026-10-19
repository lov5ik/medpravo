import io
import json
import ssl
import time
import urllib.error
import unittest
from unittest.mock import patch
import test_monitor as baseline
monitor=baseline.monitor
import adapters
import transport
import network

class CollectorTests(unittest.TestCase):
    setUp=baseline.FeedTests.setUp
    tearDown=baseline.FeedTests.tearDown
    content=baseline.FeedTests.content
    def test_pending_documents_survive_missing_listing(self):
        listing=self.content('<a href="/documents/test">Приказ о медицинской помощи детям</a>')
        with patch.object(monitor,'fetch',side_effect=[listing,OSError('temporary')]):monitor.scan_source(self.source)
        with monitor.connection() as con:
            con.execute('UPDATE pending_documents SET next_retry=0')
        with patch.object(monitor,'fetch',side_effect=[self.content(''),self.content()]):result=monitor.scan_source(self.source)
        self.assertEqual(result['added'],1)
        self.assertEqual(result['pending'],0)
    def test_explicit_unrelated_acts_are_not_downloaded_but_unclear_are_retained(self):
        listing=self.content('<a href="/documents/schools.pdf">Постановление о строительстве школ</a><a href="/documents/unknown.pdf">Приказ № 25 от 14.10.2026</a>')
        with patch.object(monitor,'fetch',side_effect=[listing,{'type':'binary','raw':b'%PDF-qa'}]) as fetch:result=monitor.scan_source(self.source)
        self.assertEqual(fetch.call_count,2)
        self.assertTrue(fetch.call_args_list[-1].args[0].endswith('/unknown.pdf'))
        self.assertEqual(result['added'],1)
        self.assertEqual(monitor.list_publications({})['publications'][0]['relevance'],'review')
    def test_next_page_is_resumed_on_next_run(self):
        source={**self.source,'page_limit':1}
        page1=self.content('<a href="?page=2">Следующая</a>')
        with patch.object(monitor,'fetch',return_value=page1):monitor.scan_source(source)
        source['page_limit']=2
        with patch.object(monitor,'fetch',side_effect=[page1,self.content('')]) as fetch:monitor.scan_source(source)
        self.assertTrue(any('page=2' in call.args[0] for call in fetch.call_args_list))
    def test_saved_frontier_does_not_starve_other_oms_seed_sections(self):
        source={**self.source,'page_limit':2,'seeds':['https://minzdrav.gov.ru/tariffs','https://minzdrav.gov.ru/programme']}
        with monitor.connection() as con:con.execute('INSERT OR REPLACE INTO source_cursor VALUES (?,?)',(source['id'],'["https://minzdrav.gov.ru/tariffs?page=29"]'))
        with patch.object(monitor,'fetch',return_value=self.content('')) as fetch:monitor.scan_source(source)
        self.assertEqual([call.args[0] for call in fetch.call_args_list],source['seeds'])
    def test_undated_candidate_is_not_redownloaded_on_every_run(self):
        source={**self.source,'page_limit':1}
        listing=self.content('<a href="/documents/test">Приказ о медицинской помощи детям</a>')
        with patch.dict(monitor.__dict__,PUBLICATION_SINCE='2026-10-01'),patch.object(monitor,'fetch',side_effect=[listing,self.content('')]):monitor.scan_source(source)
        with patch.dict(monitor.__dict__,PUBLICATION_SINCE='2026-10-01'),patch.object(monitor,'fetch',return_value=listing) as fetch:result=monitor.scan_source(source)
        self.assertEqual(fetch.call_count,1);self.assertEqual(result['pending'],0)
    def test_new_publication_of_an_older_signed_act_is_not_skipped(self):
        source={**self.source,'page_limit':1,'allow_document_date':True}
        listing=self.content('<a href="/documents/test">Приказ от 01.09.2026 № 8 о медицинской помощи детям</a>')
        detail=self.content('Дата опубликования: 14.10.2026 Медицинская помощь детям')
        with patch.dict(monitor.__dict__,PUBLICATION_SINCE='2026-10-01'),patch.object(monitor,'fetch',side_effect=[listing,detail]):
            result=monitor.scan_source(source)
        self.assertEqual(result['added'],1)
        self.assertEqual(monitor.list_publications({})['publications'][0]['published_at'],'2026-10-14')
    def test_explicit_medical_act_is_not_starved_by_later_generic_titles(self):
        source={**self.source,'page_limit':1,'document_limit':1,'allow_document_date':True}
        listing=self.content('<a href="/documents/unclear">Приказ от 14.10.2026 № 88</a><a href="/documents/medical">Приказ от 01.10.2026 № 8 о медицинской помощи детям</a>')
        with patch.dict(monitor.__dict__,PUBLICATION_SINCE='2026-10-01'),patch.object(monitor,'fetch',side_effect=[listing,self.content()]) as fetch:
            result=monitor.scan_source(source)
        self.assertEqual(result['added'],1)
        self.assertTrue(fetch.call_args_list[-1].args[0].endswith('/medical'))
        self.assertEqual(result['pending'],1)
    def test_retry_of_older_act_page_survives_until_its_publication_is_checked(self):
        import date_policy
        source={**self.source,'page_limit':1,'allow_document_date':True}
        listing=self.content('<a href="/documents/test">Приказ от 01.09.2026 № 8 о медицинской помощи детям</a>')
        with patch.dict(monitor.__dict__,PUBLICATION_SINCE='2026-10-01'),patch.object(monitor,'fetch',side_effect=[listing,OSError('temporary')]):
            monitor.scan_source(source)
        date_policy.prune(monitor.connection)
        with monitor.connection() as con:self.assertEqual(con.execute('SELECT count(*) FROM pending_documents').fetchone()[0],1)
    def test_grls_news_date_is_not_the_registration_certificate_date(self):
        page='<div class="info-panel__data"><div class="info-panel__date-time">14 окт 2026<p>15:30</p></div><div class="info-panel__topic">Приказ об анестетике. РУ от 27.04.2024</div><a href="ForumTopic.aspx?idTopic=123">подробнее</a></div>'
        found,_=adapters.grls_rows(page,'https://grls.rosminzdrav.ru/',monitor.parse_page)
        self.assertEqual(found[0]['published_at'],'2026-10-14');self.assertIn('idTopic=123',found[0]['url'])
    def test_duma_registration_is_explicitly_not_a_law_publication(self):
        page='<div data-clickopen="/bill/123-9" class="obj_item"><span>Зарегистрирован 09.10.2026</span><div class="o_txt"><p>О медицинской помощи детям</p></div></div>'
        found,_=adapters.duma_rows(page,'https://sozd.duma.gov.ru/',monitor.parse_page)
        self.assertEqual(found[0]['published_at'],'2026-10-09');self.assertIn('не принятия закона',found[0]['date_basis'])
    def test_api_metadata_requires_no_document_download(self):
        source=next(s for s in monitor.catalog() if s['id']=='clinrec')
        response=self.content()
        response['text']=json.dumps({'Data':[{'CodeVersion':'123_2','Name':'Анестезия детям','PublishDateStr':'2026-10-14','AgeCategoryStr':'Дети'}],'TotalRecords':1})
        with patch.object(monitor,'fetch',return_value=response) as fetch:result=monitor.scan_source(source)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(result['added'],1)
        row=monitor.list_publications({})['publications'][0]
        self.assertEqual(row['published_at'],'2026-10-14')
        self.assertIn('Наркоз',row['topics'])
    def test_api_failure_is_not_success_or_empty_catalog(self):
        source=next(s for s in monitor.catalog() if s['id']=='clinrec')
        response={**self.content(),'text':'<html>Unavailable</html>'}
        with patch.object(monitor,'fetch',return_value=response):result=monitor.scan_source(source)
        self.assertEqual(result['status'],'blocked')

    def test_old_pending_tfoms_files_and_software_manual_do_not_retry_forever(self):
        source=next(s for s in monitor.catalog() if s['id']=='tfoms')
        with monitor.connection() as con:
            for path,title in [('old.pdf','Приказ ТФОМС Калужской области от 24.03.2026 №38'),('manual.rar','Документация VipNet клиент версия 4')]:
                con.execute('INSERT INTO pending_documents(source_id,url,title,created_at) VALUES (?,?,?,?)',(source['id'],'https://oms-kaluga.ru/files/'+path,title,monitor.now()))
        with patch.dict(monitor.__dict__,PUBLICATION_SINCE='2026-10-01'),patch.object(monitor,'fetch',return_value=self.content('')) as fetch:
            result=monitor.scan_source(source)
        self.assertEqual(result['pending'],0)
        self.assertFalse(any('/files/' in call.args[0] for call in fetch.call_args_list))

    def test_municipal_attachment_queue_is_replaced_by_dated_act_cards(self):
        source=next(s for s in monitor.catalog() if s['id']=='kaluga-city')
        with monitor.connection() as con:
            for i in range(100):
                con.execute('INSERT INTO pending_documents(source_id,url,title,created_at) VALUES (?,?,?,?)',(source['id'],'https://www.kaluga-gov.ru/upload/'+str(i)+'.doc','Скачать',monitor.now()))
        listing=self.content('<div class="news-item"><div class="news-item-date">16 октября 2026</div><h3><a href="/administratsiya/zakonotvorchestvo/munitsipalnye-pravovye-akty/66734/">Постановление от 15.10.2026 № 1</a></h3><p>Об утверждении порядка медицинской помощи детям</p><a href="/upload/1.doc">Скачать</a></div>')
        listing['url']=source['seeds'][0]
        with patch.dict(monitor.__dict__,PUBLICATION_SINCE='2026-10-01'),patch.object(monitor,'fetch',side_effect=[listing,self.content('')]) as fetch:
            result=monitor.scan_source(source)
        self.assertEqual(result['pending'],0);self.assertEqual(result['added'],1);self.assertEqual(fetch.call_count,2)
        row=monitor.list_publications({})['publications'][0]
        self.assertEqual(row['published_at'],'2026-10-16');self.assertIn('медицинской помощи',row['title'])

    def test_sorted_api_stops_at_confirmed_publication_cutoff(self):
        source={**next(s for s in monitor.catalog() if s['id']=='clinrec'),'api_page_size':1,'page_limit':6}
        response={**self.content(),'text':json.dumps({'Data':[{'CodeVersion':'123_1','Name':'Помощь детям','PublishDateStr':'2026-09-30','AgeCategoryStr':'Дети'}],'TotalRecords':500})}
        with patch.dict(monitor.__dict__,PUBLICATION_SINCE='2026-10-01'),patch.object(monitor,'fetch',return_value=response) as fetch:
            result=monitor.scan_source(source)
        self.assertEqual(result['status'],'collected');self.assertEqual(fetch.call_count,1);self.assertEqual(result['added'],0)
    def test_miac_title_and_date_from_same_row(self):
        source=next(s for s in monitor.catalog() if s['id']=='kaluga-miac')
        found=adapters.miac_rows('<table><tr><td>21/05/2026</td><td><a href="/documents/2192" title="Скачать документ"></a><a href="/documents/2192">Приказ о медицинских услугах</a></td></tr></table>',source['seeds'][0],monitor.parse_page)
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['title'],'Приказ о медицинских услугах')
        self.assertEqual(found[0]['published_at'],'2026-05-21')
    def test_projects_xml_preserves_status_category_and_date(self):
        source=next(s for s in monitor.catalog() if s['id']=='regulation')
        links,total=adapters.parse_projects('<projects total="1"><project id="1"><title>Об утверждении медицинских правил</title><publishDate>2026-10-14T10:00:00Z</publishDate></project></projects>',source)
        self.assertEqual(total,1)
        self.assertTrue(links[0]['title'].startswith('Проект:'))
        self.assertIn('/api/npalist/1',links[0]['url'])
    def test_section_titles_and_illegal_consumption_reports_are_not_acts(self):
        self.assertFalse(monitor.is_publication_link('https://rostrud.gov.ru/control/labor/','Соблюдение законодательства о труде',self.source))
        self.assertFalse(monitor.is_publication_link('https://minzdrav.gov.ru/page/report','Годовой отчет о профилактике незаконного потребления наркотиков за 2026 год',self.source))
    def test_ffoms_card_title_is_not_replaced_by_section_title(self):
        source=next(s for s in monitor.catalog() if s['id']=='ffoms')
        links=adapters.ffoms_rows('<div class="structure-list p-3"><a href="/documents/the-orders-oms/letter-1/"></a><div class="job">Совместное письмо от 03.04.2026 № 31-2/6103</div><div class="name"></div></div>',source['seeds'][0],monitor.parse_page)
        self.assertEqual(len(links),1)
        monitor.store_publication(source,links[0],{**self.content(),'text':'<title>Приказы ФОМС</title>'},False)
        self.assertEqual(monitor.list_publications({})['publications'][0]['title'],'Совместное письмо от 03.04.2026 № 31-2/6103')
    def test_ffoms_prefers_actual_files_and_preserves_day_hospital_spreadsheet(self):
        source=next(s for s in monitor.catalog() if s['id']=='ffoms')
        html='<div class="structure-list p-3"><a href="/upload/act.pdf"></a><a href="/documents/the-orders-oms/letter-1/"></a><div class="job">Совместное письмо от 07.10.2026 № 3</div><div class="name">Об оплате медицинской помощи</div><a href="/upload/day.xlsx">Расшифровка дневного стационара</a></div>'
        links=adapters.ffoms_rows(html,source['seeds'][0],monitor.parse_page)
        self.assertEqual(len(links),2);self.assertTrue(links[0]['url'].endswith('/act.pdf'))
        self.assertTrue(links[1]['url'].endswith('/day.xlsx'));self.assertIn('дневного стационара',links[1]['title'])
    def test_verified_empty_rostrud_catalog_is_distinct_from_unreadable_js(self):
        source=next(s for s in monitor.catalog() if s['id']=='rostrud')
        empty={**self.content(),'text':'<h1>Перечень нормативных актов</h1><div class="b-listing-link"><div class="bs"></div></div>'}
        with patch.object(monitor,'fetch',return_value=empty):result=monitor.scan_source(source)
        self.assertEqual(result['status'],'collected')
    def test_one_document_can_belong_to_two_sources(self):
        monitor.store_publication(self.source,self.candidate,self.content(),False)
        other=next(s for s in monitor.catalog() if s['id']=='roszdrav')
        monitor.store_publication(other,self.candidate,self.content(),False)
        self.assertEqual(monitor.list_publications({'source':[other['id']]})['total'],1)
        self.assertEqual(monitor.list_publications({'relevance':['all']})['total'],1)
    def test_tariff_attachment_from_tfoms_keeps_oms_topic(self):
        source=next(s for s in monitor.catalog() if s['id']=='tfoms')
        relevance,topics,category=monitor.classify('Дополнительное соглашение № 3','',source)
        self.assertEqual(relevance,'medical')
        self.assertIn('ОМС',topics)
        self.assertEqual(category,'ОМС / тарифы')
    def test_conditional_get_uses_etag_and_cached_304_body(self):
        with monitor.connection() as con:
            con.execute('INSERT INTO http_cache VALUES (?,?,?,?,?,?,?,?)',(self.candidate['url'],'"version1"',None,'<main>Приказ</main>'.encode(),'text/html','utf-8',self.candidate['url'],time.time()))
        error=urllib.error.HTTPError(self.candidate['url'],304,'Not modified',{},io.BytesIO())
        opener=unittest.mock.Mock()
        opener.open.side_effect=error
        with patch.object(transport.urllib.request,'build_opener',return_value=opener),patch.object(transport,'pace'):
            result=monitor.fetch(self.candidate['url'],self.source)
        self.assertTrue(result['cached'])
        self.assertIn('Приказ',result['text'])
        self.assertEqual(opener.open.call_args.args[0].get_header('If-none-match'),'"version1"')
    def test_certificate_failure_is_not_retried_without_verification(self):
        opener=unittest.mock.Mock()
        opener.open.side_effect=urllib.error.URLError(ssl.SSLCertVerificationError('synthetic'))
        with patch.object(transport.urllib.request,'build_opener',return_value=opener),patch.object(transport,'pace'):
            with self.assertRaises(urllib.error.URLError):monitor.fetch(self.candidate['url'],self.source)
        self.assertEqual(opener.open.call_count,1)

    def test_verified_direct_fallback_after_relay_tls_failures(self):
        from email.message import Message
        from urllib.response import addinfourl
        headers=Message();headers['Content-Type']='text/html; charset=utf-8'
        primary=unittest.mock.Mock();primary.open.side_effect=urllib.error.URLError(ssl.SSLEOFError('synthetic relay EOF'))
        direct=unittest.mock.Mock();direct.open.return_value=addinfourl(io.BytesIO(b'<main>official document</main>'),headers,self.candidate['url'],200)
        with patch.dict(transport.os.environ,{'MEDPRAVO_HTTPS_PROXY':'http://127.0.0.1:8118'}),patch.object(transport,'proxy_protocols',return_value={'https':'http://127.0.0.1:8118'}),patch.object(transport.urllib.request,'build_opener',side_effect=[primary,direct]),patch.object(transport,'pace'),patch.object(transport.time,'sleep'):
            result=monitor.fetch(self.candidate['url'],self.source)
        self.assertEqual(primary.open.call_count,2);self.assertEqual(direct.open.call_count,1)
        self.assertEqual(result['network_route'],'direct-fallback')
        self.assertEqual(transport.context(self.source,monitor.ROOT).verify_mode,ssl.CERT_REQUIRED)

    def test_municipal_old_publication_boundary_does_not_leave_permanent_page_queue(self):
        source=next(s for s in monitor.catalog() if s['id']=='kaluga-city')
        seed=source['seeds'][0]
        with monitor.connection() as con:con.execute('INSERT OR REPLACE INTO source_cursor VALUES (?,?)',(source['id'],json.dumps([seed+'?PAGEN_1=22'])))
        first={**self.content('<div class="news-item"><div class="news-item-date">30 сентября 2026</div><h3><a href="/administratsiya/zakonotvorchestvo/munitsipalnye-pravovye-akty/123/">Постановление от 29.09.2026 № 1</a></h3><p>О медицинской помощи</p></div><a href="?PAGEN_1=2">Далее</a>'),'url':seed}
        with patch.dict(monitor.__dict__,PUBLICATION_SINCE='2026-10-01'),patch.object(monitor,'fetch',return_value=first) as fetch:result=monitor.scan_source(source)
        self.assertEqual(fetch.call_count,1);self.assertEqual(result['pending'],0);self.assertEqual(result['status'],'collected')

    def test_public_catalog_pagination_strips_admin_return_and_legacy_ui_parameters(self):
        source=next(s for s in monitor.catalog() if s['id']=='rospotreb')
        url='https://rospotrebnadzor.ru/documents/documents.php?back_url_admin=%2Fbitrix%2Fadmin%2Fiblock.php&_49_struts_action=%2Fmy_places%2Fview&arrFilter_ff%5BNAME%5D=&set_filter=Y&PAGEN_1=5'
        self.assertEqual(monitor.canonical(url),source['seeds'][0]+'?PAGEN_1=5')
        self.assertFalse(monitor.permitted('https://rospotrebnadzor.ru/bitrix/admin/index.php?page=2',source))
        self.assertEqual(monitor.canonical('https://minzdrav.gov.ru/documents?page='),'https://minzdrav.gov.ru/documents')
        self.assertIn('arrFilter_ff%5BNAME%5D=%D0%9E%D0%9C%D0%A1',monitor.canonical(source['seeds'][0]+'?arrFilter_ff%5BNAME%5D=%D0%9E%D0%9C%D0%A1&PAGEN_1=5'))

    def test_saved_duplicate_first_page_does_not_consume_collection_limit(self):
        source={**self.source,'page_limit':2}
        with monitor.connection() as con:con.execute('INSERT OR REPLACE INTO source_cursor VALUES (?,?)',(source['id'],json.dumps([source['seeds'][0]+'?page=',source['seeds'][0]+'?page=2'])))
        with patch.object(monitor,'fetch',return_value=self.content('')) as fetch:monitor.scan_source(source)
        self.assertEqual([c.args[0] for c in fetch.call_args_list],[source['seeds'][0],source['seeds'][0]+'?page=2'])
    def test_portal_database_overload_is_retried_and_reported_clearly(self):
        def unavailable(*args,**kwargs):
            raise urllib.error.HTTPError(self.candidate['url'],500,'Internal Server Error',{},io.BytesIO(b'Mysql connect error: Too many connections'))
        opener=unittest.mock.Mock();opener.open.side_effect=unavailable
        with patch.object(transport.urllib.request,'build_opener',return_value=opener),patch.object(transport,'pace'),patch.object(transport.time,'sleep'):
            with self.assertRaises(urllib.error.HTTPError) as err:monitor.fetch(self.candidate['url'],self.source)
        self.assertEqual(opener.open.call_count,2)
        self.assertIn('базой данных',str(err.exception))
    def test_direct_network_uses_only_its_socket_interface(self):
        sock=unittest.mock.Mock()
        with patch.object(network.socket,'getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]),patch.object(network.socket,'socket',return_value=sock):
            self.assertIs(network.connect_direct(12,('example.gov',443),5),sock)
        sock.setsockopt.assert_called_once_with(network.socket.IPPROTO_IP,31,b'\x00\x00\x00\x0c')
        sock.connect.assert_called_once_with(('8.8.8.8',443))
    def test_direct_network_does_not_connect_to_private_targets(self):
        with patch.object(network.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]),patch.object(network.socket,'socket') as create:
            with self.assertRaises(OSError):network.connect_direct(12,('example.gov',443),5)
        create.assert_not_called()

