"""One-shot collection and safe static export for GitHub Pages; stdlib only."""
import argparse
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import urllib.parse
import monitor as m
import downloads
import relevance as scope
import date_policy
import daily_digest
import source_reader
import concurrent.futures

ROOT=Path(__file__).resolve().parent
MAX_ARCHIVE=60*1024*1024
FILE_PATH=re.compile(r'files/[0-9a-f]{64}\.(pdf|zip|rar|7z|doc|docx|xls|xlsx|rtf)$')


def retry_failed_sources(bounded):
    """Refresh failed sources without claiming a new full 26-source collection."""
    enabled=set(m.settings()['enabled_sources'])
    with m.connection() as con:
        failed={r[0] for r in con.execute("SELECT id FROM sources WHERE status IN ('blocked','needs_adapter')")}
    selected=[s for s in bounded if s['id'] in enabled and s['id'] in failed]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(m.scan_source,selected))

def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    temp.replace(path)

def seed_history(path):
    """Only public legal metadata; no local credentials, read flags, cache or settings."""
    values=json.loads(path.read_text(encoding='utf-8'))
    sources={s['id']:s for s in m.catalog()}
    with m.connection() as con:
        for p in values:
            source=sources.get(p['source_id'])
            if not source or not m.permitted(p['url'],source):continue
            con.execute('INSERT OR IGNORE INTO publications VALUES (?,?,?,?,?,?,?,?,?,0)',
                (p['url'],p['title'],p['source_id'],p.get('published_at'),p['first_seen'],1,p['relevance'],json.dumps(p['topics'],ensure_ascii=False),p['category']))
            con.execute('INSERT OR IGNORE INTO publication_sources VALUES (?,?)',(p['url'],p['source_id']))
            con.execute('INSERT OR IGNORE INTO publication_labels VALUES (?,?)',(p['url'],p.get('original_title') or p['title']))
            if p.get('source_page') and m.permitted(p['source_page'],source):
                con.execute('INSERT OR IGNORE INTO publication_locations VALUES (?,?,?)',(p['url'],p['source_id'],p['source_page']))
            for sid in p.get('source_ids',[]):
                if sid in sources and m.permitted(p['url'],sources[sid]):
                    con.execute('INSERT OR IGNORE INTO publication_sources VALUES (?,?)',(p['url'],sid))

def publications():
    date_policy.prune(m.connection)
    result=[]
    with m.connection() as con:
        rows=con.execute('SELECT p.*,l.original_title,c.text AS extracted_text,c.text_status,c.date_basis,c.date_evidence,c.text_source,(SELECT source_page FROM publication_locations x WHERE x.url=p.url AND x.source_id=p.source_id) source_page FROM publications p LEFT JOIN publication_labels l USING(url) LEFT JOIN publication_content c USING(url) ORDER BY COALESCE(p.published_at,substr(p.first_seen,1,10)) DESC,p.first_seen DESC,p.url').fetchall()
        assoc={}
        for row in con.execute('SELECT url,source_id FROM publication_sources'):assoc.setdefault(row['url'],[]).append(row['source_id'])
    sources={s['id']:s for s in m.catalog()}
    for row in rows:
        p=dict(row)
        source=sources.get(p['source_id'])
        if not source or not m.permitted(p['url'],source):continue
        # Export an explicit list of fields; future DB columns cannot leak into Pages.
        item={k:p[k] for k in ('url','title','source_id','published_at','first_seen','initial','relevance','category','original_title')}
        item['topics']=json.loads(p['topics']);item['reviewed']=False
        evaluation=scope.evaluate(p['title'],p['extracted_text'] or '',source)
        if p['category']=='Справочная страница':evaluation={'relevance':'other','topics':[],'reason':'Справочная страница, а не отдельный акт'}
        item['relevance']=evaluation['relevance'];item['topics']=evaluation['topics'];item['relevance_reason']=evaluation['reason']
        item['source_ids']=assoc.get(p['url'],[p['source_id']])
        item['source_page']=p['source_page'] if p['source_page'] and m.permitted(p['source_page'],source) else None
        ext=Path(urllib.parse.urlsplit(p['url']).path).suffix.lower()
        item['file_type']=ext[1:] if ext in m.ALLOWED_EXT else None
        item.update(text_excerpt=(p['extracted_text'] or '')[:6000],text_status=p['text_status'] or 'not_extracted',date_basis=p['date_basis'] or 'Сохранённая дата публикации источника',date_evidence=p['date_evidence'] if p['date_evidence'] and m.permitted(p['date_evidence'],source) else item['source_page'],text_source=p['text_source'] if p['text_source'] and m.permitted(p['text_source'],source) else None)
        item['date_kind']='registration' if p['source_id']=='duma' and (p['date_basis'] or '').startswith('Дата регистрации законопроекта') else 'document' if (p['date_basis'] or '').startswith('Дата самого акта') else 'publication'
        result.append(item)
    return result

def archive_files(pubs,persist,site,budget=90):
    """Keep verified TFOMS binaries under content hashes, with bounded public storage."""
    persisted=persist/'files';persisted.mkdir(parents=True,exist_ok=True)
    path=persist/'files.json'
    old=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    manifest={};total=0;errors=[];started=time.monotonic()
    source=next(s for s in m.catalog() if s['id']=='tfoms')
    for p in pubs:
        if p['source_id']!='tfoms' or not p['file_type']:continue
        saved=old.get(p['url']);raw=None
        if saved and FILE_PATH.fullmatch(saved.get('path','')):
            candidate=persist/saved['path']
            if candidate.is_file() and candidate.stat().st_size<=20*1024*1024:
                cached=candidate.read_bytes()
                if hashlib.sha256(cached).hexdigest()==Path(saved['path']).stem and downloads.valid_file(cached,'.'+p['file_type']):raw=cached
        if raw is None and time.monotonic()-started<budget:
            try:
                raw,filename,_=downloads.document_file(p['url'],m.connection,m.catalog,m.canonical,m.permitted,m.fetch)
                saved={'path':'files/'+hashlib.sha256(raw).hexdigest()+'.'+p['file_type'],'name':filename,'copied_at':m.now()}
            except downloads.DownloadError:
                errors.append({'url':p['url'],'error':'Не удалось получить копию файла из источника'});continue
        if raw is None:continue
        if total+len(raw)>MAX_ARCHIVE:continue
        total+=len(raw)
        (persist/saved['path']).write_bytes(raw)
        output=site/saved['path'];output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(raw)
        manifest[p['url']]=saved
        p.update(download_path=saved['path'],download_name=saved['name'],copied_at=saved['copied_at'])
    retained={Path(x['path']).name for x in manifest.values()}
    for entry in persisted.iterdir():
        if entry.is_file() and entry.name not in retained:entry.unlink()
    write_json(path,manifest)
    return {'files':len(manifest),'bytes':total,'errors':errors}

def export_site(site,repository='',archive=True,persist=None,archive_budget=90):
    site.mkdir(parents=True,exist_ok=True)
    for p in (ROOT/'github_static').iterdir():
        if p.is_file():shutil.copyfile(p,site/p.name)
    # Each release gets content-based asset URLs. A cached previous app must not
    # hide new controls or interpret a newly published data schema incorrectly.
    index=site/'index.html'
    html=index.read_text(encoding='utf-8')
    for name in ('style.css','github-client.js','help.js','digest.js','app.js'):
        version=hashlib.sha256((site/name).read_bytes()).hexdigest()[:16]
        html=html.replace('./'+name+'"','./'+name+'?v='+version+'"')
    index.write_text(html,encoding='utf-8')
    (site/'.nojekyll').write_text('',encoding='utf-8')
    pubs=publications()
    archive_report=archive_files(pubs,persist,site,budget=archive_budget) if archive else {'files':0,'bytes':0,'errors':[]}
    state=m.app_state()
    with m.connection() as con:
        last=con.execute('SELECT finished_at FROM runs WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1').fetchone()
    sources=[]
    for s in state['sources']:
        sources.append({k:s.get(k) for k in ('id','name','group','description','seeds','homepage','status','detail','checked_at','last_success','enabled','transport_note','network_origin')})
        if s.get('fallback_seeds'):sources[-1]['fallback_seeds']=s['fallback_seeds']
    for s in sources:
        if not s['homepage']:
            value=urllib.parse.urlsplit(s['seeds'][0]);s['homepage']=urllib.parse.urlunsplit((value.scheme,value.netloc,'/','',''))
    source_reader.export(site,sources,pubs,m.connection)
    # Not checked in GitHub yet must stay pending even when the seed contains publications.
    data={'schema':1,'generated_at':m.now(),'last_checked_at':last[0] if last else None,
          'repository':repository,'workflow_url':'https://github.com/'+repository+'/actions/workflows/collect.yml' if re.fullmatch(r'[\w.-]+/[\w.-]+',repository) else None,
          'sources':sources,'settings':state['settings'],'topics':state['topics'],'publications':pubs,
          'archive':archive_report,'schedule':{'enabled':True,'interval_hours':6},'scanning':False,'local_test':False}
    data['profile']=scope.PROFILE
    data['digests']=daily_digest.load_history(persist/'digests.json') if persist else {'schema':1,'reports':[]}
    write_json(site/'data.json',data)
    write_json(site/'source-check.json',{'checked_at':data['last_checked_at'],'sources':sources,'archive':archive_report})
    return data

def compact_state(persist):
    """Use SQLite backup, drop disposable cache, retain crawl/retry state and history."""
    persist.mkdir(parents=True,exist_ok=True)
    target=persist/'state.sqlite'
    if target.exists():target.unlink()
    with m.connection() as src,contextlib.closing(sqlite3.connect(target)) as dest:src.backup(dest)
    with contextlib.closing(sqlite3.connect(target)) as con:
        con.execute('PRAGMA journal_mode=DELETE')
        con.execute('DELETE FROM http_cache')
        con.execute('DELETE FROM document_outcomes WHERE expires<?',(time.time()-86400,))
        con.execute('UPDATE publications SET reviewed=0')
        con.execute('DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY started_at DESC LIMIT 20)')
        con.commit();con.execute('VACUUM')
        if con.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('State integrity check failed')
    raw=target.read_bytes()
    if len(raw)>64*1024*1024:raise ValueError('History too large; inspect repository storage')
    (persist/'state.sqlite.gz').write_bytes(gzip.compress(raw,mtime=0));target.unlink()

def restore_state(persist,data):
    path=persist/'state.sqlite.gz'
    if not path.exists():return False
    with gzip.open(path,'rb') as stream:raw=stream.read(64*1024*1024+1)
    if len(raw)>64*1024*1024 or not raw.startswith(b'SQLite format 3\x00'):raise ValueError('Invalid persisted history')
    data.mkdir(parents=True,exist_ok=True)
    db=data/'monitor.sqlite'
    if db.exists():raise ValueError('Refusing to replace existing working history')
    db.write_bytes(raw)
    with contextlib.closing(sqlite3.connect(db)) as con:
        if con.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('History integrity check failed')
    return True

def main():
    m.PUBLICATION_SINCE=date_policy.SINCE
    parser=argparse.ArgumentParser()
    parser.add_argument('--state-dir',type=Path,default=Path('.state'))
    parser.add_argument('--site-dir',type=Path,default=Path('site'))
    parser.add_argument('--export-only',action='store_true')
    parser.add_argument('--retry-failed',action='store_true')
    args=parser.parse_args()
    m.DATA=args.state_dir/'data';persist=args.state_dir/'persist'
    fresh=not (m.DATA/'monitor.sqlite').exists()
    restored=restore_state(persist,m.DATA) if fresh else False
    m.initialize()
    if fresh and not restored and (ROOT/'seed.json').exists():seed_history(ROOT/'seed.json')
    # sources.json is the owner-editable configuration; browser settings are read-only.
    cfg=m.catalog()
    with m.connection() as con:
        con.execute('UPDATE settings SET data=? WHERE id=1',(json.dumps({'enabled_sources':[s['id'] for s in cfg if s.get('enabled',True)]}),))
    if not args.export_only:
        # Bound runtime while preserving source queues for subsequent executions.
        bounded=[{**s,'page_limit':max(min(s.get('page_limit',6),16),len(s['seeds'])+2),'document_limit':min(s.get('document_limit',100),80),'time_budget':s.get('github_time_budget',100)} for s in cfg]
        original=m.catalog;m.catalog=lambda:bounded
        try:
            if args.retry_failed:
                print(json.dumps({'retried_sources':retry_failed_sources(bounded)},ensure_ascii=False),flush=True)
            else:
                result=m.scan()
                if result.get('error'):raise RuntimeError(result['error'])
        finally:m.catalog=original
    data=export_site(args.site_dir,os.environ.get('GITHUB_REPOSITORY',''),True,persist,0 if args.export_only else 90)
    compact_state(persist)
    successful=sum(s['status'] in ('collected','partial') for s in data['sources'] if s['id'] in data['settings']['enabled_sources'])
    print(json.dumps({'records':len(data['publications']),'catalogs_responding':successful,'enabled':len(data['settings']['enabled_sources']),'archive_files':data['archive']['files'],'checked_at':data['last_checked_at']},ensure_ascii=False),flush=True)
    summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary,'a',encoding='utf-8') as out:
            out.write('# Проверка МедПраво\n\nИстория: '+str(len(data['publications']))+' публикаций.\n\n| Источник | Результат |\n|---|---|\n')
            for s in data['sources']:
                if s['id'] in data['settings']['enabled_sources']:out.write('| '+s['name']+' | '+str(s['status'])+' |\n')
            out.write('\nКопии ТФОМС: '+str(data['archive']['files'])+'. Частичный сбор не подтверждает полный охват.\n')

if __name__=='__main__':main()
