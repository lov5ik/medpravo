import json
from pathlib import Path
import tempfile
import unittest
import monitor as m
import source_reader as r


class ReaderTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.old=m.DATA;m.DATA=Path(self.tmp.name)/'data';m.initialize()
  self.source=next(s for s in m.catalog() if s['id']=='tfoms')
 def tearDown(self):m.DATA=self.old;self.tmp.cleanup()
 def test_readers_escape_source_text_and_link_to_internal_document(self):
  url=self.source['seeds'][0];doc='https://oms-kaluga.ru/files/act.pdf'
  found=[{'url':doc,'title':'Приказ <script>bad</script>'}]
  r.remember(m.connection,self.source,url,'<p>Тарифы ОМС</p><script>bad()</script>',found,'2026-10-15T14:00:00Z',m.permitted,m.parse_page)
  with m.connection() as con:
   con.execute('INSERT INTO publication_content VALUES (?,?,?,?,?,?,?)',(doc,'Текст <img src=x onerror=bad()>','embedded','Дата самого акта','',doc,1))
  source={**self.source,'checked_at':'2026-10-15','status':'partial','detail':'очередь'}
  pubs=[{'url':doc,'title':found[0]['title'],'source_id':'tfoms','published_at':'2026-10-02','source_ids':['tfoms']}]
  site=Path(self.tmp.name)/'site';r.export(site,[source],pubs,m.connection)
  source_html=(site/source['reader_path']).read_text(encoding='utf-8')
  document_html=(site/pubs[0]['reader_path']).read_text(encoding='utf-8')
  self.assertNotIn('<script>',source_html);self.assertNotIn('<img ',document_html)
  self.assertIn('&lt;img',document_html);self.assertIn('не заверенная копия',document_html)
  self.assertIn('../documents/'+Path(pubs[0]['reader_path']).name,source_html)
  self.assertNotIn('bad()',source_html)
 def test_failed_source_has_honest_saved_materials_without_fake_snapshot(self):
  s={**self.source,'checked_at':'2026-10-15','status':'blocked','detail':'HTTP 503'}
  site=Path(self.tmp.name)/'site';r.export(site,[s],[],m.connection)
  page=(site/s['reader_path']).read_text(encoding='utf-8')
  self.assertIn('HTTP 503',page);self.assertIn('не подтверждает отсутствие',page)
  self.assertNotIn('Сохранённый раздел ·',page)
 def test_unsafe_urls_and_non_catalog_crawl_are_not_saved(self):
  url=self.source['seeds'][0]
  r.remember(m.connection,self.source,url,'<p>Catalog</p>',[{'url':'javascript:bad()','title':'bad'},{'url':'https://evil.example/','title':'bad'}],'now',m.permitted,m.parse_page)
  r.remember(m.connection,self.source,'https://oms-kaluga.ru/random','random',[],'now',m.permitted,m.parse_page)
  with m.connection() as con:
   rows=con.execute('SELECT * FROM source_readers').fetchall()
  self.assertEqual(len(rows),1);self.assertEqual(json.loads(rows[0]['links']),[])
  for value in ['javascript:bad()','https://user:secret@example.org/','https://example.org:bad/']:
   self.assertFalse(r.safe_url(value))


if __name__=='__main__':unittest.main()
