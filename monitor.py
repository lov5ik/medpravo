"""Independent, non-AI publication monitor. Python 3.12+, pinned pypdf for bounded attachment reading."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import base64
import collector
import adapters
import transport
import titles
import relevance as scope
import downloads
import document_metadata
import text_extract
import concurrent.futures
import contextlib
import datetime as dt
import email.utils
import hmac
import html
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
MOSCOW = dt.timezone(dt.timedelta(hours=3))
LEGAL = re.compile(r"приказ|постановлен|распоряжен|закон|тариф|соглашен|санпин|норматив|клиническ|информационн.{0,15}письм|изменени|медицинск.{0,30}(издел|безопас)|регистрационн", re.I)
MEDICAL = re.compile(r"медицин|здравоохран|омс|страхован|томограф|мрт|рентген|радиац|контраст|анестез|реаним|педиатр|детей|стационар|санитар|санпин|лицензи|лекарств|клиническ|труд|налог|персональн|реклам|охран.{0,8}труда|siemens|magnetom|harmony|toshiba|aquilion|canon", re.I)
PAGE = re.compile(r"(^|\s)(следующ|далее|next)|[?&](page|index|PAGEN_\d+)=", re.I)
ALLOWED_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".rtf", ".zip", ".rar", ".7z"}
LOCK = threading.Lock()
FETCH_LOCK = threading.Lock()
LAST_HOST_FETCH = {}

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def day():
    return dt.datetime.now(MOSCOW).date().isoformat()

@contextlib.contextmanager
def connection():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / "monitor.sqlite", timeout=30)
    con.row_factory = sqlite3.Row
    con.create_function("CASEFOLD",1,lambda value: str(value or "").casefold())
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def catalog():
    config = DATA / "sources.json"
    values = json.loads((config if config.exists() else ROOT / "sources.json").read_text(encoding="utf-8"))
    if len({s["id"] for s in values}) != len(values):
        raise ValueError("Duplicate source identifier")
    return values

def settings():
    with connection() as con:
        return json.loads(con.execute("SELECT data FROM settings WHERE id=1").fetchone()[0])

def canonical(url):
    value = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(value.query, keep_blank_values=True)
    query = [(k,v) for k,v in query if not k.startswith("utm_") and k not in ("ysclid",)]
    query = [(k,v) for k,v in query if v or not re.fullmatch(r'page|index|PAGEN_\d+',k,re.I)]
    if (value.hostname or '').endswith('rospotrebnadzor.ru'):
        # Public pagination must not inherit Bitrix admin-return URLs or old
        # portal UI session parameters from the source's navigation widgets.
        query=[(k,v) for k,v in query if k!='back_url_admin' and not k.startswith('_49_') and (v or not k.startswith('arrFilter_'))]
        if not any(k.startswith('arrFilter_') for k,v in query):query=[(k,v) for k,v in query if k!='set_filter']
    return urllib.parse.urlunsplit((value.scheme, value.netloc.lower(), urllib.parse.quote(value.path,safe="/%:@!$&'()*+,;=-._~"), urllib.parse.urlencode(query), ""))

def permitted(url, source):
    try:
        value = urllib.parse.urlsplit(url)
        hostname = value.hostname or ""
        return value.scheme in ("http", "https") and not value.username and not value.password and value.port in (None,80,443) and not value.path.startswith('/bitrix/admin/') and any(hostname == d or hostname.endswith("." + d) for d in source["allowed_domains"])
    except ValueError:
        return False

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, source):
        super().__init__()
        self.source = source
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not permitted(newurl, self.source):
            raise ValueError("Redirect outside approved source domains")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def fetch(url,source,payload=None):
    return transport.fetch(url,source,ROOT,connection,permitted,canonical,SafeRedirect,payload)

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.element_stack = []
        self.blocks = []
        self.links = []
        self.anchor = None
        self.title = False
        self.title_parts = []
        self.timestamps = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        pagination=re.search(r'pagination|pager|page-navigation|постранич|страниц',attrs.get('class','')+' '+attrs.get('aria-label',''),re.I)
        ignored=(tag in ("script", "style", "header", "footer", "aside") or tag=='nav' and not pagination or attrs.get('role') in ('banner','contentinfo') or re.search(r'(?:^|\s)(?:footer\S*|site-footer|b-footer|main-nav|site-header)(?:\s|$)',attrs.get('class',''),re.I))
        if tag not in ('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'):
            self.element_stack.append((tag,bool(ignored)))
        if ignored:
            self.skip += 1
        if tag == "a":
            self.anchor = {"href":attrs.get("href",""), "parts":[], "label":attrs.get("title",""), "ignored":bool(self.skip)}
        if tag == "title":
            self.title = True
        if tag == "time" and attrs.get("datetime") and attrs.get("itemprop")=="datePublished":
            self.timestamps.append(attrs["datetime"])
        if tag == "meta" and attrs.get("property","").lower() == "article:published_time":
            self.timestamps.append(attrs.get("content",""))
    def handle_endtag(self, tag):
        if tag == "a" and self.anchor:
            if not self.anchor['ignored']:self.links.append((self.anchor["href"], " ".join(self.anchor["parts"]).strip() or self.anchor["label"]))
            self.anchor = None
        for i in range(len(self.element_stack)-1,-1,-1):
            if self.element_stack[i][0]==tag:
                self.skip=max(0,self.skip-sum(ignored for _,ignored in self.element_stack[i:]))
                del self.element_stack[i:]
                break
        if tag == "title":
            self.title = False
    def handle_data(self, data):
        if self.title:
            self.title_parts.append(data)
        if self.anchor:
            self.anchor["parts"].append(data)
        if not self.skip:
            self.blocks.append(data)
    @property
    def visible(self):
        return " ".join(" ".join(self.blocks).split())

def parse_page(text):
    page = PageParser()
    page.feed(text)
    return page

def discover(text, base, source):
    """Read feeds or visible anchors; never infer that a JS catalog was checked."""
    links, pages = [], []
    if text.lstrip().startswith("<?xml") or re.search(r"<(rss|feed)(\s|>)", text[:2000]):
        root = ET.fromstring(text)
        for item in list(root.findall(".//item")) + list(root.findall(".//{http://www.w3.org/2005/Atom}entry")):
            def content(name):
                node = item.find(name)
                if node is None:
                    node = item.find("{http://www.w3.org/2005/Atom}" + name)
                return "" if node is None else "".join(node.itertext()).strip()
            url = content("link")
            if not url:
                node = item.find("{http://www.w3.org/2005/Atom}link")
                url = node.get("href","") if node is not None else ""
            if permitted(url, source) and is_publication_link(url,content("title"),source):
                links.append({"url":canonical(url), "title":content("title"), "published_at":content("pubDate") or content("published") or None})
        return links, pages
    page = parse_page(text)
    for href, title in page.links:
        url = canonical(urllib.parse.urljoin(base, href))
        if not permitted(url, source) or not href or href.startswith(("#","mailto:","tel:")):
            continue
        if PAGE.search(href+" "+title):
            pages.append(url)
            continue
        if any(re.search(pattern,urllib.parse.urlsplit(url).path) for pattern in source.get('follow_patterns',[])) and url not in source['seeds']:
            pages.append(url)
        if source.get("publication_prefixes"):
            number=re.search(r"/(?:document|Document/View)/([0-9]{16})(?:/|$)",urllib.parse.urlsplit(url).path,re.I)
            if not number or not any(number[1].startswith(prefix) for prefix in source["publication_prefixes"]):
                continue
        attachment = Path(urllib.parse.urlsplit(url).path).suffix.lower() in ALLOWED_EXT
        if is_publication_link(url,title,source):
            links.append({"url":url, "title":" ".join(title.split()) or Path(urllib.parse.urlsplit(url).path).name, "published_at":None})
    for row in links:row['source_page']=canonical(base)
    links=titles.enrich_links(links,text,base,source,parse_page)
    return document_metadata.listing_dates(links,text,base,source,parse_page), list(dict.fromkeys(pages))

def is_publication_link(url,title,source):
    path=urllib.parse.urlsplit(url).path.lower()
    title=" ".join(title.split())
    if source.get('publication_paths') and not any(re.search(pattern,path,re.I) for pattern in source['publication_paths']):return False
    if source['id']=='rkn' and path.startswith(('/activity/connection/','/activity/mass-media/')):return False
    if '/news/' in path or path in ('','/') or canonical(url) in [canonical(seed) for seed in source['seeds']]:
        return False
    if Path(path).suffix in ALLOWED_EXT:
        return True
    if re.fullmatch(r'(нормативные(?: правовые)? акты|нормативные документы|налоговое законодательство и письма фнс россии|информационные письма о медицинских изделиях)',title,re.I):
        return False
    if path.rstrip('/').endswith(('/documents','/tax_legislation','/unreg')):
        return False
    if re.match(r'^(Приказы|Постановления|Распоряжения|Законы|Соглашения)\b',title,re.I) and not re.search(r'№|\d{4}',title):
        return False
    return bool(len(title)>12 and (re.search(r'\b(приказ\w*|постановлен\w*|распоряжен\w*|закон(?:а|ом|у|ы|ов)?|соглашен\w*|санпин|письмо)\b',title,re.I) and re.search(r'№|\d{4}|\bоб?\s',title,re.I) or re.match(r'О(?:б)? (?:внесении|утверждении|признании|порядке)',title,re.I)))

TOPICS = {
    "ОМС": r"\bомс\b|обязательн.{0,20}медицинск.{0,20}страхован|фомс|тарифн.{0,15}соглашен",
    "МРТ / КТ": r"томограф|\bмрт\b|\bкт\b|рентген|радиац|ионизир|магнитно.{0,5}резонанс|siemens|magnetom|harmony|toshiba|aquilion|canon|контраст",
    "Дети": r"педиатр|детей|детям|дети|детск|реб[её]н|несовершеннолет",
    "Наркоз": r"наркоз|анестез|реаним|седац",
    "Дневной стационар": r"стационар",
    "Анализы": r"лаборатор|биоматериал|забор.{0,15}(кров|анализ)|диагност",
    "Работа центра": r"лицензи|санитар|санпин|медицин|здравоохран|лекарств|клиническ|врач|сестрин|аккредитац|медиздел",
    "Общие требования": r"трудов|охран.{0,8}труда|налог|персональн.{0,8}данн|реклам|пожарн|отход|потребител",
}

def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    with connection() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0,
          checked_at TEXT, status TEXT NOT NULL DEFAULT 'pending', detail TEXT, last_success TEXT);
        CREATE TABLE IF NOT EXISTS publications (url TEXT PRIMARY KEY, title TEXT NOT NULL,
          source_id TEXT NOT NULL, published_at TEXT, first_seen TEXT NOT NULL,
          initial INTEGER NOT NULL, relevance TEXT NOT NULL, topics TEXT NOT NULL,
          category TEXT NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
          finished_at TEXT, summary TEXT);
        CREATE INDEX IF NOT EXISTS publications_seen ON publications(first_seen);
        CREATE TABLE IF NOT EXISTS publication_sources(url TEXT,source_id TEXT,PRIMARY KEY(url,source_id));
        INSERT OR IGNORE INTO publication_sources SELECT url,source_id FROM publications;
        CREATE TABLE IF NOT EXISTS publication_labels(url TEXT PRIMARY KEY,original_title TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS publication_content(url TEXT PRIMARY KEY,text TEXT NOT NULL,text_status TEXT NOT NULL,date_basis TEXT NOT NULL,date_evidence TEXT NOT NULL,text_source TEXT NOT NULL,extractor_version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS publication_locations(url TEXT,source_id TEXT,source_page TEXT NOT NULL,PRIMARY KEY(url,source_id));
        INSERT OR IGNORE INTO publication_labels SELECT url,title FROM publications;
        CREATE TABLE IF NOT EXISTS title_migrations(version INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS http_cache (url TEXT PRIMARY KEY,etag TEXT,modified TEXT,body BLOB,
          content_type TEXT,charset TEXT,final_url TEXT,fetched REAL);
        CREATE INDEX IF NOT EXISTS cache_fetched ON http_cache(fetched);
        CREATE TABLE IF NOT EXISTS source_cursor (source_id TEXT PRIMARY KEY,queue TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_readers(source_id TEXT,url TEXT,checked_at TEXT,text TEXT,links TEXT,PRIMARY KEY(source_id,url));
        CREATE TABLE IF NOT EXISTS api_cursor (source_id TEXT PRIMARY KEY,page INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS document_outcomes(source_id TEXT,url_hash TEXT,signature TEXT,expires REAL,reason TEXT,PRIMARY KEY(source_id,url_hash));
        CREATE TABLE IF NOT EXISTS pending_documents (source_id TEXT,url TEXT,title TEXT,published_at TEXT,
          metadata INTEGER NOT NULL DEFAULT 0,text TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0,
          next_retry REAL NOT NULL DEFAULT 0,created_at TEXT,PRIMARY KEY(source_id,url));
        CREATE INDEX IF NOT EXISTS pending_retry ON pending_documents(source_id,next_retry,created_at);
        """)
        if 'original_title' not in [row[1] for row in con.execute('PRAGMA table_info(pending_documents)')]:
            con.execute('ALTER TABLE pending_documents ADD COLUMN original_title TEXT')
        if 'source_page' not in [row[1] for row in con.execute('PRAGMA table_info(pending_documents)')]:
            con.execute('ALTER TABLE pending_documents ADD COLUMN source_page TEXT')
        if 'date_basis' not in [row[1] for row in con.execute('PRAGMA table_info(pending_documents)')]:
            con.execute('ALTER TABLE pending_documents ADD COLUMN date_basis TEXT')
        if 'network_origin' not in [row[1] for row in con.execute('PRAGMA table_info(sources)')]:
            con.execute('ALTER TABLE sources ADD COLUMN network_origin TEXT')
        if not con.execute("SELECT 1 FROM settings WHERE id=1").fetchone():
            cfg = {"enabled_sources": [s["id"] for s in catalog() if s.get("enabled", True)]}
            con.execute("INSERT INTO settings VALUES (1,?)", (json.dumps(cfg),))
        for source in catalog():
            con.execute("INSERT OR IGNORE INTO sources(id) VALUES (?)", (source["id"],))
    refresh_cached_titles()
    backfill_source_pages()
    with connection() as con:transport.trim_cache(con)
    separate_reference_pages()

def separate_reference_pages():
    """Keep old records and read flags, but do not present catalogues as individual acts."""
    with connection() as con:
        if con.execute('SELECT count(*) FROM title_migrations WHERE version IN (3,4)').fetchone()[0]==2:return
        for row in con.execute("SELECT url,source_id FROM publications WHERE source_id IN ('duma','rkn','roszdrav','devices')").fetchall():
            path=urllib.parse.urlsplit(row['url']).path
            reference=(row['source_id']=='duma' and not re.fullmatch(r'/bill/\d+-\d+/?',path)) or (row['source_id']=='rkn' and path.startswith(('/activity/connection/','/activity/mass-media/')))
            # Verified website privacy policy, linked from the Roszdrav footer.
            reference=reference or (row['source_id'] in ('roszdrav','devices') and path=='/i/upload/images/2025/5/6/1746514411.03751-1-982307.pdf')
            if reference:con.execute("UPDATE publications SET category='Справочная страница',relevance='other',topics='[]' WHERE url=?",(row['url'],))
        con.execute('INSERT OR IGNORE INTO title_migrations VALUES (3)')
        con.execute('INSERT OR IGNORE INTO title_migrations VALUES (4)')

def remember_source_page(con,candidate,source):
    page=candidate.get('source_page')
    if page and permitted(page,source) and permitted(candidate['url'],source):
        con.execute('INSERT OR IGNORE INTO publication_locations VALUES (?,?,?)',
                    (canonical(candidate['url']),source['id'],canonical(page)))

def backfill_source_pages():
    with connection() as con:
        if con.execute('SELECT 1 FROM title_migrations WHERE version=2').fetchone():return
        for cached in con.execute('SELECT url,body,charset,final_url FROM http_cache').fetchall():
            text=bytes(cached['body']).decode(cached['charset'] or 'utf-8',errors='replace')
            base=cached['final_url'] or cached['url']
            for source in catalog():
                if not permitted(base,source):continue
                try:
                    found,_=discover(text,base,source)
                    if source.get('adapter')=='ffoms':found=adapters.ffoms_rows(text,base,parse_page)
                    if source.get('adapter')=='miac':found=adapters.miac_rows(text,base,parse_page)
                    for candidate in found:
                        if con.execute('SELECT 1 FROM publications WHERE url=?',(canonical(candidate['url']),)).fetchone():
                            remember_source_page(con,{**candidate,'source_page':base},source)
                except (ValueError,ET.ParseError):continue
        con.execute('INSERT INTO title_migrations VALUES (2)')

def update_publication_title(con,candidate):
    """Enrichment changes presentation only: no new event or reset of read/date flags."""
    url=canonical(candidate['url'])
    row=con.execute('SELECT title FROM publications WHERE url=?',(url,)).fetchone()
    if not row:return False
    original=candidate.get('original_title') or row[0]
    proposed=candidate['title'][:1500]
    if proposed == row[0] or len(proposed)<=len(row[0]):return False
    if not (titles.HAS_SUBJECT.search(proposed) or 'к Тарифному соглашению' in proposed):return False
    con.execute('INSERT OR IGNORE INTO publication_labels VALUES (?,?)',(url,original))
    con.execute('UPDATE publications SET title=? WHERE url=?',(proposed,url))
    return True

def refresh_cached_titles():
    """One-time upgrade from already downloaded HTML; no network requests or new records."""
    sources=catalog()
    with connection() as con:
        if con.execute('SELECT 1 FROM title_migrations WHERE version=1').fetchone():return
        cache=con.execute('SELECT url,body,charset,final_url FROM http_cache').fetchall()
        for cached in cache:
            text=bytes(cached['body']).decode(cached['charset'] or 'utf-8',errors='replace')
            base=cached['final_url'] or cached['url']
            for source in sources:
                if not permitted(base,source):continue
                try:
                    found,_=discover(text,base,source)
                    if source.get('adapter')=='ffoms':found=adapters.ffoms_rows(text,base,parse_page)
                    for candidate in found:update_publication_title(con,candidate)
                except (ValueError,ET.ParseError):continue
            row=con.execute('SELECT title FROM publications WHERE url=?',(canonical(cached['url']),)).fetchone()
            if row:
                enriched=titles.detail_title(row[0],text,parse_page)
                update_publication_title(con,{'url':cached['url'],'title':enriched})
        con.execute('INSERT INTO title_migrations VALUES (1)')

def classify(title, text, source):
    evaluation=scope.evaluate(title,text,source)
    relevance,topics=evaluation['relevance'],evaluation['topics']
    category = source.get("kind", "Документ")
    if re.search(r"проект|законопроект", title, re.I):
        category = "Проект"
    elif re.search(r"разъяснен|информационн.{0,15}письм|письмо", title, re.I):
        category = "Письмо / разъяснение"
    elif re.search(r"клиническ.{0,20}рекомендац", title, re.I):
        category = "Клинические рекомендации"
    elif re.search(r"соглашен|тариф", title, re.I):
        category = "ОМС / тарифы" if source.get("medical") or "ОМС" in topics else "Тариф / соглашение"
    return relevance, topics, category

def normalized_date(value):
    return document_metadata.normalize(value)

def store_publication(source, candidate, result, initial):
    url, title = canonical(candidate["url"]), candidate["title"][:1500]
    original_title=candidate.get('original_title') or title
    with connection() as con:
        existing=con.execute("SELECT 1 FROM publications WHERE url=?", (url,)).fetchone()
        if existing:
            con.execute('INSERT OR IGNORE INTO publication_sources VALUES (?,?)',(url,source['id']))
            update_publication_title(con,candidate)
            remember_source_page(con,candidate,source)
            content=con.execute('SELECT extractor_version FROM publication_content WHERE url=?',(url,)).fetchone()
            if content and content[0]>=text_extract.VERSION:return False
    text=''
    status='metadata_only';text_source=url
    published=normalized_date(candidate.get('published_at'))
    date_basis=candidate.get('date_basis') or 'Дата публикации в каталоге / API источника'
    date_evidence=candidate.get('source_page') or url
    document_fallback=source.get('allow_document_date',False)
    if result["type"] == "text":
        # Navigation and footer text are excluded by PageParser. Relevance is a keyword hint.
        body=re.search(r'<(main|article)\b[^>]*>(.*?)</\1>',result['text'],re.I|re.S)
        text = parse_page(body[2]).visible[:500000] if body else ""
        title=titles.detail_title(title,result['text'],parse_page)
        details=document_metadata.publication(result['text'],source,parse_page)
        if details.get('conflict'):
            result['date_rejected']=True;return False
        if details['date']:
            if published and published!=details['date'] and not date_basis.startswith('Дата самого акта'):
                result['date_rejected']=True;return False
            published=details['date'];date_basis=details['basis'];date_evidence=url
        status='page_text' if text.strip() else 'metadata_only'
    if not published and document_fallback:
        published=document_metadata.document_date(title)
        if published:date_basis='Дата самого акта; дата публикации не указана'
    if globals().get('PUBLICATION_SINCE'):
        import date_policy
        if published and not date_policy.eligible(published) or not published and not document_fallback:result['date_rejected']=True;return False
    # Only fetch attachments once the publication date has passed the cutoff.
    if result['type']=='binary':
        extracted=text_extract.extract(result.get('raw',b''));text=extracted['text'];status=extracted['status']
    elif result['type']=='text' and (published or document_fallback):
        for attachment in document_metadata.attachments(result['text'],result.get('url',url),source,parse_page,permitted):
            try:
                file=fetch(attachment,source)
                if file['type']!='binary':continue
                extracted=text_extract.extract(file.get('raw',b''));status=extracted['status'];text_source=attachment
                if extracted['text']:text=extracted['text']
                break
            except (OSError,ValueError):status='attachment_unavailable'
    if not published and document_fallback:
        published=document_metadata.document_date(title,text)
        if published:date_basis='Дата самого акта; дата публикации не указана';date_evidence=text_source
    if globals().get('PUBLICATION_SINCE') and not date_policy.eligible(published):result['date_rejected']=True;return False
    relevance, topics, category = classify(title, text, source)
    with connection() as con:
        cursor = con.execute("INSERT OR IGNORE INTO publications VALUES (?,?,?,?,?,?,?,?,?,0)",
            (url,title,source["id"],published,now(),int(initial),relevance,json.dumps(topics,ensure_ascii=False),category))
        con.execute('INSERT OR IGNORE INTO publication_sources VALUES (?,?)',(url,source['id']))
        con.execute('INSERT OR IGNORE INTO publication_labels VALUES (?,?)',(url,original_title))
        remember_source_page(con,candidate,source)
        con.execute('INSERT OR REPLACE INTO publication_content VALUES (?,?,?,?,?,?,?)',
            (url,text[:text_extract.MAX_TEXT],status,date_basis,date_evidence,text_source,text_extract.VERSION))
        return bool(cursor.rowcount)

def scan_source(source):
    return collector.scan_source(globals(),source)

def scan():
    if not LOCK.acquire(blocking=False):
        return {"busy":True}
    run_id = secrets.token_hex(12)
    try:
        with connection() as con:
            con.execute("INSERT INTO runs(id,started_at) VALUES (?,?)", (run_id,now()))
        enabled = settings()["enabled_sources"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,min(3,int(os.environ.get('MEDPRAVO_WORKERS','2'))))) as pool:
            results = list(pool.map(scan_source,[s for s in catalog() if s["id"] in enabled]))
        with connection() as con:
            con.execute("UPDATE runs SET finished_at=?,summary=? WHERE id=?", (now(),json.dumps(results,ensure_ascii=False),run_id))
        return {"run_id":run_id,"results":results}
    except Exception:
        logging.exception("Collector failed")
        with connection() as con:
            con.execute("UPDATE runs SET finished_at=?,summary=? WHERE id=?", (now(),json.dumps({"error":"Сбой сборщика"},ensure_ascii=False),run_id))
        return {"error":"Сбой сборщика. Проверьте журнал."}
    finally:
        LOCK.release()

def next_collection_at():
    with connection() as con:
        row=con.execute('SELECT started_at FROM runs WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1').fetchone()
    return dt.datetime.fromisoformat(row[0])+dt.timedelta(hours=6) if row else None

def scheduler():
    while True:
        try:
            due=next_collection_at()
            if due is None or dt.datetime.now(dt.timezone.utc)>=due:
                scan()
        except Exception:
            logging.exception("Scheduled collection failed")
        time.sleep(60)

def list_publications(params):
    clauses, arguments = [], []
    relevance = params.get("relevance",["profile"])[0]
    if relevance == "profile":
        clauses.append("relevance IN ('medical','review')")
    elif relevance in ("medical","review","other"):
        clauses.append("relevance=?")
        arguments.append(relevance)
    query = params.get("q",[""])[0][:200]
    if query:
        clauses.append("CASEFOLD(title) LIKE CASEFOLD(?) ESCAPE '\\'")
        arguments.append("%"+query.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")+"%")
    source = params.get("source",[""])[0]
    if source:
        clauses.append("EXISTS(SELECT 1 FROM publication_sources ps WHERE ps.url=publications.url AND ps.source_id=?)")
        arguments.append(source)
    topic = params.get("topic",[""])[0]
    if topic in TOPICS:
        clauses.append("topics LIKE ?")
        arguments.append('%"'+topic+'"%')
    if params.get("unread",[""])[0]=="1":
        clauses.append("reviewed=0")
    try:
        offset=max(0,int(params.get("offset",["0"])[0]))
    except ValueError:
        offset=0
    where=" WHERE "+" AND ".join(clauses) if clauses else ""
    with connection() as con:
        total=con.execute("SELECT COUNT(*) FROM publications"+where,arguments).fetchone()[0]
        rows=[dict(r) for r in con.execute("SELECT *, (SELECT original_title FROM publication_labels pl WHERE pl.url=publications.url) AS original_title, (SELECT source_page FROM publication_locations pl WHERE pl.url=publications.url AND pl.source_id=publications.source_id) AS source_page FROM publications"+where+" ORDER BY COALESCE(published_at,substr(first_seen,1,10)) DESC,first_seen DESC,url LIMIT 50 OFFSET ?",arguments+[offset])]
    for row in rows:
        row["topics"]=json.loads(row["topics"])
        row['file_type']=Path(urllib.parse.urlsplit(row['url']).path).suffix.lower().lstrip('.') if Path(urllib.parse.urlsplit(row['url']).path).suffix.lower() in ALLOWED_EXT else None
    return {"publications":rows,"total":total,"offset":offset,"limit":50}

def app_state():
    with connection() as con:
        states={r["id"]:dict(r) for r in con.execute("SELECT * FROM sources")}
        count=con.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
        unread=con.execute("SELECT COUNT(*) FROM publications WHERE reviewed=0 AND relevance IN ('medical','review')").fetchone()[0]
        runs=[dict(r) for r in con.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 10")]
    due=next_collection_at()
    scheduled=os.environ.get('LOCAL_TEST')!='1' and os.environ.get('DISABLE_SCHEDULER')!='1'
    return {"settings":settings(),"sources":[{**s,**states[s["id"]]} for s in catalog()],
        "schedule":{"enabled":scheduled,"interval_hours":6,"next_check_at":due.isoformat() if due and scheduled else None},
        "count":count,"unread":unread,"runs":runs,"scanning":LOCK.locked(),"topics":list(TOPICS),"ai":False,"email":False,"mode":"publications","local_test":os.environ.get('LOCAL_TEST')=='1'}

class Handler(BaseHTTPRequestHandler):
    def authorized(self):
        expected=os.environ.get("ADMIN_PASSWORD","")
        try:
            scheme,token=self.headers.get('Authorization','').split(' ',1)
            if scheme.lower()!='basic':return False
            user,password=base64.b64decode(token,validate=True).decode().split(":",1)
            return len(expected)>=16 and hmac.compare_digest(user.encode(),os.environ.get("ADMIN_USER","owner").encode()) and hmac.compare_digest(password.encode(),expected.encode())
        except (ValueError,IndexError,UnicodeError):
            return False
    def respond(self,status,payload,content_type="application/json; charset=utf-8",extra_headers=None):
        raw=payload if isinstance(payload,bytes) else json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type",content_type)
        self.send_header('Content-Length',str(len(raw)))
        for key,value in (extra_headers or {}).items():self.send_header(key,value)
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'")
        self.end_headers()
        self.wfile.write(raw)
    def guard(self):
        if self.authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate",'Basic realm="MedPravo", charset="UTF-8"')
        self.end_headers()
        return False
    def do_GET(self):
        request=urllib.parse.urlsplit(self.path)
        if request.path=="/health":
            try:
                with connection() as con:
                    if not con.execute('SELECT 1 FROM settings WHERE id=1').fetchone():raise sqlite3.DatabaseError('Settings missing')
                return self.respond(200,{"ok":True})
            except (sqlite3.Error,OSError):return self.respond(503,{'ok':False})
        if not self.guard():
            return
        if request.path=="/api/state":
            return self.respond(200,app_state())
        if request.path=="/api/publications":
            return self.respond(200,list_publications(urllib.parse.parse_qs(request.query)))
        if request.path=='/api/file':
            try:
                url=urllib.parse.parse_qs(request.query).get('url',[''])[0]
                raw,filename,mime=downloads.document_file(url,connection,catalog,canonical,permitted,fetch)
                disposition='attachment; filename="document'+Path(filename).suffix+'"; filename*=UTF-8\'\''+urllib.parse.quote(filename,safe='')
                return self.respond(200,raw,mime,{'Content-Disposition':disposition})
            except downloads.DownloadError as exc:return self.respond(exc.status,{'error':str(exc)})
        assets={"/":("index.html","text/html; charset=utf-8"),"/app.js":("app.js","text/javascript; charset=utf-8"),"/api-client.js":("api-client.js","text/javascript; charset=utf-8"),"/style.css":("style.css","text/css; charset=utf-8")}
        if request.path in assets:
            filename,content_type=assets[request.path]
            return self.respond(200,(ROOT/"static"/filename).read_bytes(),content_type)
        self.respond(404,{"error":"Раздел не найден"})
    def do_POST(self):
        if not self.guard():
            return
        origin=self.headers.get("Origin")
        if self.headers.get("X-MedPravo-Request")!="1" or (origin and urllib.parse.urlsplit(origin).netloc!=self.headers.get("Host")):
            return self.respond(403,{"error":"Запрос отклонён"})
        try:
            length=int(self.headers.get("Content-Length","0"))
            if not 0<length<=20000:
                raise ValueError()
            body=json.loads(self.rfile.read(length))
            path=urllib.parse.urlsplit(self.path).path
            if path=="/api/scan":
                if LOCK.locked():
                    return self.respond(200,{"busy":True})
                threading.Thread(target=scan,daemon=True).start()
                return self.respond(200,{"started":True})
            if path=="/api/settings":
                enabled=body.get("enabled_sources")
                valid={s["id"] for s in catalog()}
                if not isinstance(enabled,list) or not enabled or not all(isinstance(s,str) for s in enabled) or not set(enabled)<=valid:
                    raise ValueError()
                with connection() as con:
                    con.execute("UPDATE settings SET data=? WHERE id=1",(json.dumps({"enabled_sources":list(dict.fromkeys(enabled))}),))
                return self.respond(200,{"saved":True})
            if path=="/api/review":
                url=body.get("url")
                if not isinstance(url,str) or not isinstance(body.get("reviewed"),bool):
                    raise ValueError()
                with connection() as con:
                    cursor=con.execute("UPDATE publications SET reviewed=? WHERE url=?",(int(body["reviewed"]),url))
                return self.respond(200 if cursor.rowcount else 404,{"saved":bool(cursor.rowcount)})
            return self.respond(404,{"error":"Действие не найдено"})
        except (ValueError,TypeError,AttributeError):
            self.respond(400,{"error":"Проверьте заполненные поля"})
    def log_message(self,format,*args):
        logging.info("%s %s",self.address_string(),format%args)

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    if len(os.environ.get("ADMIN_PASSWORD",""))<16:
        raise SystemExit("Set ADMIN_PASSWORD to a unique password of at least 16 characters.")
    initialize()
    if os.environ.get("DISABLE_SCHEDULER")!="1":
        threading.Thread(target=scheduler,daemon=True).start()
    ThreadingHTTPServer((os.environ.get("BIND","127.0.0.1"),int(os.environ.get("PORT","8080"))),Handler).serve_forever()

