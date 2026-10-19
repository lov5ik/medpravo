import contextlib
import gzip
import importlib
import json
import re
import shutil
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import test_monitor as baseline
import github_collect as c
import github_state as state

class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.old=c.m.DATA;c.m.DATA=self.root/'data';c.m.initialize()
        self.source=next(s for s in c.m.catalog() if s['id']=='minzdrav')
        self.doc={'url':'https://minzdrav.gov.ru/documents/test','title':'Приказ о помощи детям','published_at':'2026-10-14'}
        c.m.store_publication(self.source,self.doc,{'type':'metadata','text':''},True)
    def tearDown(self):
        c.m.DATA=self.old;self.tmp.cleanup()
    def test_export_contains_only_public_fields_and_relative_assets(self):
        with c.m.connection() as con:con.execute('UPDATE publications SET reviewed=1')
        data=c.export_site(self.root/'site','LevshaMV/medpravo',archive=False)
        self.assertFalse(data['publications'][0]['reviewed'])
        self.assertIsNone(data['last_checked_at'])
        self.assertNotIn('http_cache',data)
        self.assertNotIn('runs',data)
        page=(self.root/'site/index.html').read_text(encoding='utf-8')
        self.assertIn('./github-client.js',page);self.assertNotIn('href="/style.css"',page)
        self.assertIn('/actions/workflows/collect.yml',data['workflow_url'])

    def test_retry_failed_keeps_full_collection_time_and_skips_healthy_sources(self):
        finished='2026-10-15T12:00:00+00:00'
        with c.m.connection() as con:
            con.execute("INSERT INTO runs VALUES ('full',?,?,?)",('2026-10-15T11:00:00+00:00',finished,'[]'))
            con.execute("UPDATE sources SET status='collected'")
            con.execute("UPDATE sources SET status='blocked' WHERE id IN ('rospotreb','pravo-text')")
        with patch.object(c.m,'scan_source',side_effect=lambda s:{'source':s['id'],'status':'collected'}) as scan,patch.object(c.m,'scan',side_effect=AssertionError('No full scan needed')):
            rows=c.retry_failed_sources(c.m.catalog())
        self.assertEqual({r['source'] for r in rows},{'rospotreb','pravo-text'});self.assertEqual(scan.call_count,2)
        self.assertEqual(c.export_site(self.root/'site','lov5ik/medpravo',archive=False)['last_checked_at'],finished)
        with c.m.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM runs').fetchone()[0],1)
            con.execute("UPDATE sources SET status='collected'")
        with patch.object(c.m,'scan_source',side_effect=AssertionError('All sources already reachable')):self.assertEqual(c.retry_failed_sources(c.m.catalog()),[])
    def test_state_restore_keeps_queue_dates_and_history_without_cache_or_reviews(self):
        with c.m.connection() as con:
            con.execute('UPDATE publications SET reviewed=1')
            con.execute('INSERT OR REPLACE INTO source_cursor VALUES (?,?)',('minzdrav','["https://minzdrav.gov.ru/documents?page=2"]'))
            con.execute('INSERT INTO http_cache VALUES (?,?,?,?,?,?,?,?)',('https://minzdrav.gov.ru/a',None,None,b'secret test cache','text/html','utf-8','https://minzdrav.gov.ru/a',9999999999))
        persist=self.root/'persist';c.compact_state(persist)
        fresh=self.root/'fresh';self.assertTrue(c.restore_state(persist,fresh))
        with contextlib.closing(sqlite3.connect(fresh/'monitor.sqlite')) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM publications').fetchone()[0],1)
            self.assertEqual(con.execute('SELECT reviewed FROM publications').fetchone()[0],0)
            self.assertEqual(con.execute('SELECT count(*) FROM http_cache').fetchone()[0],0)
            self.assertIn('page=2',con.execute('SELECT queue FROM source_cursor').fetchone()[0])
        with self.assertRaises(ValueError):c.restore_state(persist,fresh)
    def test_changed_frontend_gets_a_new_cache_key(self):
        assets=self.root/'frontend';shutil.copytree(c.ROOT/'github_static',assets/'github_static')
        with patch.object(c,'ROOT',assets):
            c.export_site(self.root/'first','LevshaMV/medpravo',archive=False)
            first=(self.root/'first/index.html').read_text(encoding='utf-8')
            with (assets/'github_static/app.js').open('a',encoding='utf-8') as out:out.write('\n// new release\n')
            c.export_site(self.root/'second','LevshaMV/medpravo',archive=False)
            second=(self.root/'second/index.html').read_text(encoding='utf-8')
        def asset(page,name):return re.search(r'\./'+re.escape(name)+r'\?v=[a-f0-9]+',page)[0]
        self.assertNotEqual(asset(first,'app.js'),asset(second,'app.js'))
        self.assertEqual(asset(first,'style.css'),asset(second,'style.css'))
    def test_corrupt_state_does_not_silently_reset_history(self):
        persist=self.root/'persist';persist.mkdir();(persist/'state.sqlite.gz').write_bytes(gzip.compress(b'not sqlite'))
        with self.assertRaises(ValueError):c.restore_state(persist,self.root/'new')
    def test_export_rechecks_old_noise_without_rewriting_history(self):
        title='Приказ Министерства здравоохранения об утверждении плана научно-практических конференций'
        with c.m.connection() as con:
            con.execute('UPDATE publications SET title=?,relevance=?,topics=?,reviewed=1',(title,'medical','["Работа центра"]'))
            before=tuple(con.execute('SELECT url,first_seen,published_at,initial,reviewed FROM publications').fetchone())
        pub=c.publications()[0]
        self.assertEqual(pub['relevance'],'other')
        self.assertIn('ведомства',pub['relevance_reason'])
        with c.m.connection() as con:self.assertEqual(tuple(con.execute('SELECT url,first_seen,published_at,initial,reviewed FROM publications').fetchone()),before)
    def test_file_archive_reuses_verified_copy_and_never_emits_html(self):
        source=next(s for s in c.m.catalog() if s['id']=='tfoms')
        doc={**self.doc,'url':'https://oms-kaluga.ru/files/test.pdf','title':'Тарифное соглашение ОМС.pdf'}
        c.m.store_publication(source,doc,{'type':'binary','raw':b'%PDF-qa'},True)
        persist=self.root/'persist';site=self.root/'site';site.mkdir()
        with patch.object(c.m,'fetch',return_value={'type':'binary','raw':b'%PDF-qa'}):
            pubs=c.publications();result=c.archive_files(pubs,persist,site)
        self.assertEqual(result['files'],1)
        target=next(p for p in pubs if p['source_id']=='tfoms')['download_path']
        self.assertEqual((site/target).read_bytes(),b'%PDF-qa')
        with patch.object(c.m,'fetch',side_effect=AssertionError('Must reuse cached file')):self.assertEqual(c.archive_files(c.publications(),persist,site)['files'],1)
        for p in (persist/'files').iterdir():p.write_bytes(b'<html>bad</html>')
        with patch.object(c.m,'fetch',return_value={'type':'text','raw':b'<html>bad</html>'}):
            result=c.archive_files(c.publications(),persist,site)
        self.assertEqual(result['files'],0)
    def test_git_state_survives_two_clean_clones(self):
        original=Path.cwd()
        remote=self.root/'remote.git';repo=self.root/'repo'
        subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
        subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
        def run(*args,cwd=repo):return subprocess.run(['git',*args],cwd=cwd,check=True,capture_output=True)
        run('config','user.name','QA');run('config','user.email','qa@example.invalid')
        (repo/'README.md').write_text('qa');run('add','.');run('commit','-m','initial');run('remote','add','origin',str(remote));run('push','origin','HEAD:main')
        folder=repo/'.state';persist=folder/'persist';c.compact_state(persist)
        digest={'schema':1,'reports':[{'date':'2026-10-15','status':'empty','total':0}]}
        (persist/'digests.json').write_text(json.dumps(digest),encoding='utf-8')
        try:
            import os
            os.chdir(repo);state.save(folder)
            clone=self.root/'clone';subprocess.run(['git','clone','--branch','main',str(remote),str(clone)],check=True,capture_output=True)
            os.chdir(clone);state.restore(clone/'.state')
            self.assertEqual(json.loads((clone/'.state/persist/digests.json').read_text(encoding='utf-8')),digest)
            self.assertTrue(c.restore_state(clone/'.state/persist',clone/'.state/data'))
            c.m.DATA=clone/'.state/data';c.m.initialize()
            # Known URL remains one record after another collection process.
            self.assertFalse(c.m.store_publication(self.source,self.doc,{'type':'metadata','text':''},False))
            c.compact_state(clone/'.state/persist');state.save(clone/'.state')
            names=run('ls-tree','-r','--name-only','medpravo-state',cwd=remote).stdout.decode()
            self.assertEqual(set(names.splitlines()),{'state.sqlite.gz','digests.json'})
        finally:os.chdir(original)

    def test_republish_reuses_files_without_contacting_source(self):
        source=next(s for s in c.m.catalog() if s['id']=='tfoms')
        doc={**self.doc,'url':'https://oms-kaluga.ru/files/reuse.pdf','title':'Тарифное соглашение ОМС.pdf'}
        c.m.store_publication(source,doc,{'type':'binary','raw':b'%PDF-reuse'},True)
        persist=self.root/'persist';site=self.root/'site';site.mkdir()
        with patch.object(c.m,'fetch',return_value={'type':'binary','raw':b'%PDF-reuse'}):c.archive_files(c.publications(),persist,site)
        with patch.object(c.m,'fetch',side_effect=AssertionError('No network on republish')):
            data=c.export_site(self.root/'republished','LevshaMV/medpravo',True,persist,archive_budget=0)
        self.assertEqual(data['archive']['files'],1)
        copied=next(p for p in data['publications'] if p['source_id']=='tfoms')
        self.assertEqual((self.root/'republished'/copied['download_path']).read_bytes(),b'%PDF-reuse')

if __name__=='__main__':unittest.main()
