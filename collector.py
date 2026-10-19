"""Resumable per-source crawl; new links survive pagination and network failures."""
import json
import time
import re
from types import SimpleNamespace
import urllib.parse
import adapters
import relevance
import os
import hashlib
import source_reader

PARSER_VERSION=4
def candidate_key(candidate):
    raw=json.dumps([PARSER_VERSION,relevance.VERSION,candidate['title'],candidate.get('published_at')],ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()

def scan_source(namespace,source):
    m=SimpleNamespace(**namespace)
    with m.connection() as con:
        previous=con.execute('SELECT * FROM sources WHERE id=?',(source['id'],)).fetchone()
        cursor=con.execute('SELECT queue FROM source_cursor WHERE source_id=?',(source['id'],)).fetchone()
        seed_cursor=con.execute('SELECT page FROM api_cursor WHERE source_id=?',(source['id'],)).fetchone()
    # Refresh every configured section before continuing the saved frontier.
    # An old pagination queue must not starve the other OMS sections.
    seed_offset=(seed_cursor[0] if seed_cursor and source.get('adapter') not in ('clinrec','projects') else 0)%len(source['seeds'])
    roots=source['seeds'][seed_offset:]+source['seeds'][:seed_offset]
    queue=list(dict.fromkeys(m.canonical(url) for url in roots+(json.loads(cursor[0]) if cursor else [])))
    queue=[url for url in queue if url not in source.get('obsolete_seeds',[]) and not any(url.startswith(p) for p in source.get('obsolete_prefixes',[]))]
    visited,failures,failed_pages=set(),[],[]
    found_count,pages_read,added,excluded,date_excluded=0,0,0,0,0
    page_limit,doc_limit=source.get('page_limit',6),source.get('document_limit',100)
    started=time.monotonic()
    budget=source.get('time_budget',120)
    api_kind=source.get('adapter') in ('clinrec','projects')
    position=0
    limited=False
    valid_empty=False
    routes=set()
    past_pages={}
    def remember(found):
        nonlocal found_count,excluded
        found_count+=len(found)
        with m.connection() as con:
            for c in found:
                if c.get('published_at'):c['published_at']=m.normalized_date(c['published_at'])
                if not c.get('published_at') and source.get('allow_document_date'):
                    day=m.document_metadata.document_date(c['title'])
                    if day:c['published_at']=day;c['date_basis']='Дата самого акта; дата публикации не указана'
                if namespace.get('PUBLICATION_SINCE'):
                    import date_policy
                    day=date_policy.date(c.get('published_at'))
                    # An older signing date may precede a new publication.
                    # Read its detail page before deciding; direct old files
                    # have no separate publication field to establish there.
                    old_act_page=(c.get('date_basis') or '').startswith('Дата самого акта') and not re.search(r'\.(?:pdf|docx?|xlsx?|rtf|zip|rar|7z)$',urllib.parse.urlsplit(c['url']).path,re.I)
                    if day and day<date_policy.SINCE and not old_act_page:excluded+=1;continue
                if not m.permitted(c['url'],source):continue
                if not relevance.should_fetch(c['title'],source):excluded+=1;continue
                key=hashlib.sha256(m.canonical(c['url']).encode()).hexdigest()
                outcome=con.execute('SELECT signature,expires FROM document_outcomes WHERE source_id=? AND url_hash=?',(source['id'],key)).fetchone()
                if outcome and outcome['signature']==candidate_key(c) and outcome['expires']>time.time():continue
                if con.execute('SELECT 1 FROM publications WHERE url=?',(m.canonical(c['url']),)).fetchone():
                    con.execute('INSERT OR IGNORE INTO publication_sources VALUES (?,?)',(m.canonical(c['url']),source['id']))
                    m.update_publication_title(con,c)
                    m.remember_source_page(con,c,source)
                    content=con.execute('SELECT extractor_version FROM publication_content WHERE url=?',(m.canonical(c['url']),)).fetchone()
                    if content and content[0]>=m.text_extract.VERSION:continue
                con.execute('INSERT INTO pending_documents(source_id,url,title,published_at,metadata,text,created_at,original_title,source_page,date_basis) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,url) DO UPDATE SET title=CASE WHEN length(excluded.title)>length(pending_documents.title) THEN excluded.title ELSE pending_documents.title END,published_at=COALESCE(excluded.published_at,pending_documents.published_at),date_basis=COALESCE(excluded.date_basis,pending_documents.date_basis),source_page=COALESCE(excluded.source_page,pending_documents.source_page)',
                    (source['id'],m.canonical(c['url']),c['title'],c.get('published_at'),int(c.get('metadata',False)),c.get('text',''),m.now(),c.get('original_title') or c['title'],c.get('source_page'),c.get('date_basis')))
    if api_kind:
        with m.connection() as con:
            row=con.execute('SELECT page FROM api_cursor WHERE source_id=?',(source['id'],)).fetchone()
        tail=row[0] if row else 0
        positions=list(dict.fromkeys([0]+list(range(max(1,tail),max(1,tail)+max(0,page_limit-1)))))
        for position in positions:
            if time.monotonic()-started>budget:limited=True;break
            try:
                url,payload=adapters.api_request(source,position,source.get('api_page_size',50))
                result=m.fetch(url,source,payload)
                routes.add(result.get('network_route','primary'))
                parser=adapters.parse_clinrec if source['adapter']=='clinrec' else adapters.parse_projects
                found,total=parser(result['text'],source)
                source_reader.remember(m.connection,source,url,result['text'],found,m.now(),m.permitted,m.parse_page)
                remember(found);pages_read+=1
                has_more=len(found)==source.get('api_page_size',50) and (total is None or (position+1)*len(found)<total)
                if namespace.get('PUBLICATION_SINCE') and found:
                    import date_policy
                    days=[date_policy.date(m.normalized_date(p.get('published_at'))) for p in found]
                    if all(day and day<date_policy.SINCE for day in days):has_more=False
                limited=has_more
                valid_empty=not found and total==0
                with m.connection() as con:
                    con.execute('INSERT OR REPLACE INTO api_cursor VALUES (?,?)',(source['id'],position+1 if has_more else 0))
                if not has_more:break
            except Exception as exc:failures.append(str(exc)[:240]);break
        queue=[]
    else:
        while queue and len(visited)<page_limit and time.monotonic()-started<budget:
            url=queue.pop(0)
            if url in visited:continue
            visited.add(url)
            try:
                result=m.fetch(url,source)
                routes.add(result.get('network_route','primary'))
                if result['type']!='text':raise ValueError('Каталог не является текстовой страницей')
                found,pages=m.discover(result['text'],result['url'],source)
                if source.get('adapter')=='miac':found=adapters.miac_rows(result['text'],result['url'],m.parse_page)
                if source.get('adapter')=='ffoms':found=adapters.ffoms_rows(result['text'],result['url'],m.parse_page)
                if source.get('adapter')=='grls':found,pages=adapters.grls_rows(result['text'],result['url'],m.parse_page)
                if source.get('adapter')=='duma':found,pages=adapters.duma_rows(result['text'],result['url'],m.parse_page)
                if source.get('adapter')=='municipal':found=adapters.municipal_rows(result['text'],result['url'],m.parse_page)
                if source.get('adapter')=='municipal' and namespace.get('PUBLICATION_SINCE') and found:
                    import date_policy
                    days=[date_policy.date(p.get('published_at')) for p in found]
                    if all(day and day<date_policy.SINCE for day in days):
                        value=urllib.parse.urlsplit(url);number=int(urllib.parse.parse_qs(value.query).get('PAGEN_1',['1'])[0])
                        past_pages[value.path]=min(number,past_pages.get(value.path,number));pages=[]
                source_reader.remember(m.connection,source,url,result['text'],found,m.now(),m.permitted,m.parse_page)
                if source['id']=='pravo' and not found and re.search(r'не найден|не обнаружен',m.parse_page(result['text']).visible,re.I):valid_empty=True
                if source.get('adapter')=='rostrud' and not found:
                    if re.search(r'class="b-listing-link"',result['text']) and re.search(r'<h1[^>]*>Переч(?:ень|ни)',result['text']):valid_empty=True
                for candidate in found:candidate.setdefault('source_page',result['url'])
                remember(found);pages_read+=1
                queue.extend(p for p in pages if p not in visited and p not in queue)
                if past_pages:
                    queue=[p for p in queue if urllib.parse.urlsplit(p).path not in past_pages or int(urllib.parse.parse_qs(urllib.parse.urlsplit(p).query).get('PAGEN_1',['1'])[0])<past_pages[urllib.parse.urlsplit(p).path]]
            except Exception as exc:
                failures.append(type(exc).__name__+': '+str(exc)[:220])
                failed_pages.append(url)
        queue=list(dict.fromkeys(queue+failed_pages))
        limited=bool(queue)
        with m.connection() as con:con.execute('INSERT OR REPLACE INTO api_cursor VALUES (?,?)',(source['id'],(seed_offset+sum(url in visited for url in roots))%len(roots)))
    with m.connection() as con:
        con.execute('INSERT OR REPLACE INTO source_cursor VALUES (?,?)',(source['id'],json.dumps(queue)))
        pending=con.execute('SELECT * FROM pending_documents WHERE source_id=? AND next_retry<=? ORDER BY CASE WHEN published_at IS NULL THEN 1 ELSE 0 END,published_at DESC,created_at,url',
            (source['id'],time.time())).fetchall()
    # Give explicit clinic/OMS subjects priority over generic amendment titles.
    # Preserve date order inside each group; unclear acts remain in the queue.
    eligible=[]
    with m.connection() as con:
        for row in pending:
            path=urllib.parse.urlsplit(row['url']).path
            invalid_path=source.get('publication_paths') and not any(re.search(pattern,path,re.I) for pattern in source['publication_paths'])
            old_binary=False
            if namespace.get('PUBLICATION_SINCE') and source.get('allow_document_date') and re.search(r'\.(?:pdf|docx?|xlsx?|rtf|zip|rar|7z)$',path,re.I):
                import date_policy
                day=date_policy.date(row['published_at'] or m.document_metadata.document_date(row['title']))
                old_binary=bool(day and day<date_policy.SINCE)
            if invalid_path or old_binary or not relevance.should_fetch(row['title'],source):
                con.execute('DELETE FROM pending_documents WHERE source_id=? AND url=?',(source['id'],row['url']))
                if old_binary:date_excluded+=1
                else:excluded+=1
            else:eligible.append(row)
    pending=sorted(eligible,key=lambda row:(relevance.evaluate(row['title'],'',source)['relevance']!='medical',row['attempts']>0))[:doc_limit]
    successful=0
    for row in pending:
        if time.monotonic()-started>budget:limited=True;break
        candidate=dict(row)
        if not relevance.should_fetch(row['title'],source):
            excluded+=1
            with m.connection() as con:con.execute('DELETE FROM pending_documents WHERE source_id=? AND url=?',(source['id'],row['url']))
            continue
        if source.get('publication_paths') and not any(re.search(pattern,urllib.parse.urlsplit(row['url']).path,re.I) for pattern in source['publication_paths']):
            with m.connection() as con:con.execute('DELETE FROM pending_documents WHERE source_id=? AND url=?',(source['id'],row['url']))
            continue
        try:
            result={'type':'metadata','text':row['text']} if row['metadata'] else m.fetch(row['url'],source)
            routes.add(result.get('network_route','primary'))
            added+=m.store_publication(source,candidate,result,not previous['initialized'])
            if result.get('date_rejected'):
                date_excluded+=1
                with m.connection() as con:
                    con.execute('INSERT OR REPLACE INTO document_outcomes VALUES (?,?,?,?,?)',(source['id'],hashlib.sha256(m.canonical(row['url']).encode()).hexdigest(),candidate_key(candidate),time.time()+7*86400,'publication_date'))
            successful+=1
            with m.connection() as con:con.execute('DELETE FROM pending_documents WHERE source_id=? AND url=?',(source['id'],row['url']))
        except Exception as exc:
            failures.append(type(exc).__name__+': '+str(exc)[:180])
            with m.connection() as con:
                con.execute('UPDATE pending_documents SET attempts=attempts+1,next_retry=? WHERE source_id=? AND url=?',
                    (time.time()+min(21600,60*2**min(row['attempts'],8)),source['id'],row['url']))
    with m.connection() as con:
        remaining=con.execute('SELECT COUNT(*) FROM pending_documents WHERE source_id=?',(source['id'],)).fetchone()[0]
        retained=con.execute("SELECT COUNT(*) FROM publication_sources ps JOIN publications p ON p.url=ps.url WHERE ps.source_id=? AND p.category!='Справочная страница'",(source['id'],)).fetchone()[0]
    limited=limited or remaining>0
    if pages_read==0:
        status='blocked' if failures else 'needs_adapter'
    elif not found_count:
        status='partial' if failures or limited else ('collected' if retained or valid_empty else 'needs_adapter')
    else:status='partial' if failures or limited else 'collected'
    detail=f'Прочитано страниц: {pages_read}; извлечено ссылок: {found_count}; добавлено: {added}; в очереди: {remaining}; исключено по предмету: {excluded}. '
    if date_excluded:detail+=f'Не добавлено из-за даты публикации: {date_excluded}. '
    detail+='Проверены только настроенные разделы. '
    if limited:detail+=f'Обход продолжится: страниц каталога в очереди — {len(queue)}; документов — {remaining}. '
    if status=='needs_adapter':detail+='Каталог не прочитан: отсутствие новых публикаций не подтверждено. '
    if valid_empty and not found_count:detail+='Настроенный каталог прочитан; по этому запросу документов нет. '
    if failures:detail+='Ошибки: '+'; '.join(failures[:3])
    with m.connection() as con:
        origin=os.environ.get('MEDPRAVO_NETWORK_ORIGIN') or ('GitHub Actions' if os.environ.get('GITHUB_ACTIONS')=='true' else 'Компьютер владельца')
        if 'direct-fallback' in routes:origin+='; использован резервный прямой маршрут с проверкой TLS'
        con.execute('UPDATE sources SET initialized=?,checked_at=?,status=?,detail=?,last_success=COALESCE(?,last_success),network_origin=? WHERE id=?',
            (int(bool(previous['initialized']) or successful>0 or (status=='collected' and pages_read>0)),m.now(),status,detail,m.now() if status=='collected' else None,origin,source['id']))
    return {'source':source['id'],'status':status,'added':added,'pending':remaining,'pages':pages_read}
