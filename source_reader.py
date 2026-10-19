"""Script-free, escaped reading copies of public source catalogs and documents."""
import hashlib
import html
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def safe_url(value):
    try:
        u=urlsplit(value or '')
        u.port  # Reject malformed authorities as well as unsafe schemes.
        return u.scheme in ('http','https') and bool(u.hostname) and not u.username and not u.password
    except ValueError:return False


def document_path(url):
    return 'readers/documents/'+hashlib.sha256(url.encode()).hexdigest()+'.html'


def source_path(identifier):
    if not re.fullmatch(r'[a-z0-9-]+',identifier):raise ValueError('Invalid source identifier')
    return 'readers/sources/'+identifier+'.html'


def remember(connection,source,url,text,found,checked_at,permitted,parse_page):
    # Keep only configured catalog pages, not an unbounded crawl or HTTP cache.
    if url not in source['seeds']+source.get('fallback_seeds',[]) and source.get('adapter') not in ('clinrec','projects'):return
    links=[{'url':p['url'],'title':p['title']} for p in found if safe_url(p['url']) and permitted(p['url'],source)][:300]
    visible='' if source.get('adapter') in ('clinrec','projects') else parse_page(text).visible[:30000]
    with connection() as con:
        con.execute('INSERT OR REPLACE INTO source_readers VALUES (?,?,?,?,?)',
                    (source['id'],url,checked_at,visible,json.dumps(links,ensure_ascii=False)))
        con.execute('DELETE FROM source_readers WHERE source_id=? AND url NOT IN (SELECT url FROM source_readers WHERE source_id=? ORDER BY checked_at DESC,url LIMIT 32)',(source['id'],source['id']))


def original_url(pub):
    match=re.fullmatch(r'https://regulation\.gov\.ru/api/npalist/(\d+)/?',pub['url'])
    return 'https://regulation.gov.ru/projects/'+match[1]+'/' if match and pub['source_id']=='regulation' else pub['url']


def link(url,title):
    return '<a href="'+html.escape(url,quote=True)+'" target="_blank" rel="noopener noreferrer">'+html.escape(title)+'</a>' if safe_url(url) else html.escape(title)


def page(title,body):
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'"><title>'+html.escape(title)+' · МедПраво</title><style>body{margin:0;background:#f5f7f4;color:#173b3e;font:16px/1.6 system-ui,sans-serif}main{max-width:900px;margin:32px auto;padding:24px;background:white;border:1px solid #dde5df;border-radius:12px}h1{font-size:1.5rem;line-height:1.4}h2{font-size:1.1rem}a{color:#16766f;overflow-wrap:anywhere}p,li{overflow-wrap:anywhere}.note{color:#596c6c;font-size:.9rem}.text{white-space:pre-wrap}details{border-top:1px solid #e6ebe6;padding:14px 0}summary{cursor:pointer}li{margin:10px 0}@media(max-width:600px){main{margin:0;padding:20px;border:0;border-radius:0}}</style><main><a href="../../">← МедПраво</a><h1>'+html.escape(title)+'</h1>'+body+'</main></html>'


def export(site,sources,pubs,connection):
    with connection() as con:
        snapshots=[dict(r) for r in con.execute('SELECT * FROM source_readers ORDER BY checked_at DESC,url')]
        texts={r['url']:r['text'] for r in con.execute('SELECT url,text FROM publication_content')}
    by_source={s['id']:[] for s in sources};by_url={p['url']:p for p in pubs}
    for p in pubs:
        p['reader_path']=document_path(p['url'])
        for sid in p.get('source_ids') or [p['source_id']]:
            if sid in by_source:by_source[sid].append(p)
        body='<p class="note">Сохранённая карточка публикации. Дата: '+html.escape(p.get('published_at') or 'не подтверждена')+'.</p><p>'+link(original_url(p),'Открыть оригинал ↗')+'</p>'
        if p.get('source_page') and p['source_page']!=original_url(p):body+='<p>'+link(p['source_page'],'Страница публикации в источнике ↗')+'</p>'
        text=texts.get(p['url']) or p.get('text_excerpt') or ''
        if text:
            body+='<h2>Извлечённый текст</h2><p class="note">Это сохранённый фрагмент, а не заверенная копия. Текст может быть неполным и содержать ошибки распознавания. Сверяйте значимые положения с оригиналом.</p><div class="text">'+html.escape(text[:60000])+'</div>'
        else:body+='<p class="note">Полный текст не получен. Сохранены название и сведения из каталога; они не заменяют содержание документа.</p>'
        if p.get('download_path'):body+='<p><a href="../../'+html.escape(p['download_path'],quote=True)+'">Скачать сохранённый файл</a></p>'
        path=site/p['reader_path'];path.parent.mkdir(parents=True,exist_ok=True);path.write_text(page(p['title'],body),encoding='utf-8')
    for s in sources:
        s['reader_path']=source_path(s['id'])
        body='<p class="note">Сохранённые материалы источника. Доступ к оригинальному сайту проверяется отдельно; эта страница открывается с GitHub.</p><p>'+link(s.get('homepage') or s['seeds'][0],'Открыть сайт источника ↗')+'</p>'
        for url in s.get('fallback_seeds',[]):body+='<p>'+link(url,'Официальный резерв: опубликованные акты ↗')+'</p>'
        body+='<p class="note">Последняя попытка сбора: '+html.escape(s.get('checked_at') or 'ещё не выполнялась')+'</p><details><summary>Результат проверки</summary><p>'+html.escape(s.get('detail') or 'Нет результата')+'</p></details>'
        rows=by_source[s['id']]
        body+='<h2>Собранные публикации с 01.10.2026: '+str(len(rows))+'</h2>'
        if rows:body+='<ul>'+''.join('<li><a href="../documents/'+Path(p['reader_path']).name+'">'+html.escape(p['title'])+'</a> · '+html.escape(p.get('published_at') or '')+'</li>' for p in rows)+'</ul>'
        else:body+='<p>Новых документов с подтверждённой датой пока не сохранено. Это не подтверждает отсутствие публикаций у источника.</p>'
        for snap in (r for r in snapshots if r['source_id']==s['id']):
            body+='<details><summary>Сохранённый раздел · '+html.escape(snap['checked_at'])+'</summary><p>'+link(snap['url'],'Оригинальный раздел ↗')+'</p><p class="note">Каталог может содержать старые документы и другие темы. Дата копии не является датой документа.</p><ul>'
            for item in json.loads(snap['links']):
                p=by_url.get(item['url'])
                href='../documents/'+Path(p['reader_path']).name if p else item['url']
                body+='<li>'+('<a href="'+html.escape(href,quote=True)+'">'+html.escape(item['title'])+'</a>' if p else link(href,item['title']))+'</li>'
            body+='</ul><div class="text">'+html.escape(snap['text'])+'</div></details>'
        path=site/s['reader_path'];path.parent.mkdir(parents=True,exist_ok=True);path.write_text(page(s['name'],body),encoding='utf-8')
