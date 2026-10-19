"""Bounded HTTP transport with conditional GET, TLS verification and host pacing."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import functools
import http.client
import network

HOST_LOCK=threading.Lock()
HOST_NEXT={}
CONTEXTS={}
RELAY_HTTP={}
MAX_BYTES=20*1024*1024
CACHE_BYTES=64*1024*1024
CACHE_AGE=14*86400
CHAIN_SHA256='4f49f94e3eff1df8d32bd17b44c0ca7b30f455f33f5b9d0a0e0c3e1f7e31fe31'  # Assigned to the packaged, authenticated certificate bundle below.

def context(source,root):
    bundle=source.get('ca_bundle')
    if bundle not in (None,'russian-chain.pem'):raise ValueError('Недопустимый комплект сертификатов')
    key=bundle or 'system'
    with HOST_LOCK:
        if key not in CONTEXTS:
            ctx=ssl.create_default_context()
            ctx.set_alpn_protocols(['http/1.1'])
            if bundle:
                path=root/'certs'/bundle
                if CHAIN_SHA256 and hashlib.sha256(path.read_bytes()).hexdigest()!=CHAIN_SHA256:
                    raise ValueError('Изменён комплект сертификатов')
                ctx.load_verify_locations(cafile=str(path))
            CONTEXTS[key]=ctx
        return CONTEXTS[key]

def pace(host):
    with HOST_LOCK:
        wait=max(0,HOST_NEXT.get(host,0)-time.monotonic())
        HOST_NEXT[host]=time.monotonic()+wait+0.6
    if wait:time.sleep(wait)

def relay_url(value):
    if not value:return None
    url=urllib.parse.urlsplit(value)
    if url.scheme!='http' or url.hostname!='127.0.0.1' or url.username or url.password or url.path not in ('','/') or url.query or url.fragment or not url.port or not 1024<=url.port<=65535:
        raise ValueError('Релей допускается только через локальный SSH-туннель')
    return value


def proxy_protocols(proxy):
    """Older installed relays remain compatible until their HTTP upgrade."""
    with HOST_LOCK:
        if proxy not in RELAY_HTTP:
            try:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(proxy.rstrip('/')+'/health',timeout=2) as response:
                    data=json.loads(response.read(1024))
                RELAY_HTTP[proxy]=data.get('service')=='medpravo-relay' and data.get('http_sources') is True
            except (OSError,ValueError):RELAY_HTTP[proxy]=False
        return {'http':proxy,'https':proxy} if RELAY_HTTP[proxy] else {'https':proxy}

def trim_cache(con,max_bytes=CACHE_BYTES,at=None):
    at=time.time() if at is None else at
    con.execute('DELETE FROM http_cache WHERE fetched<?',(at-CACHE_AGE,))
    total=con.execute('SELECT COALESCE(SUM(length(body)),0) FROM http_cache').fetchone()[0]
    if total>max_bytes:
        for row in con.execute('SELECT url,length(body) AS bytes FROM http_cache ORDER BY fetched,url').fetchall():
            con.execute('DELETE FROM http_cache WHERE url=?',(row['url'],))
            total-=row['bytes']
            if total<=max_bytes:break

def fetch(url,source,root,connection,permitted,canonical,redirect,payload=None):
    url=canonical(url)
    if not permitted(url,source):raise ValueError('Адрес вне разрешённых доменов источника')
    cached=None
    if payload is None:
        with connection() as con:
            try:cached=con.execute('SELECT * FROM http_cache WHERE url=?',(url,)).fetchone()
            except Exception:pass  # Compatibility with a pre-migration test DB.
    headers={'User-Agent':'Mozilla/5.0 (compatible; MedPravoPublicationMonitor/2.0)',
        'Accept':'text/html,application/json,application/xml,application/pdf,*/*;q=0.5',
        'Accept-Encoding':'gzip'}
    if cached:
        if cached['etag']:headers['If-None-Match']=cached['etag']
        if cached['modified']:headers['If-Modified-Since']=cached['modified']
    data=json.dumps(payload).encode() if payload is not None else None
    if data is not None:headers['Content-Type']='application/json; charset=utf-8'
    tls=context(source,root)
    interface=int(os.environ.get('DIRECT_INTERFACE','0'))
    proxy=relay_url(os.environ.get('MEDPRAVO_HTTPS_PROXY',''))
    if proxy:
        # Certificate checks and source redirect restrictions remain active.
        opener=urllib.request.build_opener(urllib.request.ProxyHandler(proxy_protocols(proxy)),redirect(source),urllib.request.HTTPSHandler(context=tls))
    elif interface and os.name=='nt':
        class DirectHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self,request):
                return self.do_open(functools.partial(network.DirectHTTPSConnection,interface_index=interface),request,context=tls)
        class DirectHTTPHandler(urllib.request.HTTPHandler):
            def http_open(self,request):
                return self.do_open(functools.partial(network.DirectHTTPConnection,interface_index=interface),request)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),redirect(source),DirectHTTPHandler(),DirectHTTPSHandler(context=tls))
    else:opener=urllib.request.build_opener(redirect(source),urllib.request.HTTPSHandler(context=tls))
    direct_opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),redirect(source),urllib.request.HTTPSHandler(context=tls)) if proxy else None
    route='primary'
    attempts=3 if proxy else 2
    for attempt in range(attempts):
        pace(urllib.parse.urlsplit(url).hostname)
        try:
            response=opener.open(urllib.request.Request(url,data=data,headers=headers),
                timeout=max(3,min(60,int(os.environ.get('FETCH_TIMEOUT','15')))))
            break
        except urllib.error.HTTPError as exc:
            if exc.code==304 and cached:
                with connection() as con:con.execute('UPDATE http_cache SET fetched=? WHERE url=?',(time.time(),url))
                return decode(bytes(cached['body']),cached['content_type'],cached['charset'],cached['final_url'],True)
            if exc.code==500:
                snippet=exc.read(4096).decode('utf-8',errors='replace')
                if 'Too many connections' in snippet:
                    exc.msg='Сайт источника временно перегружен: превышен лимит соединений с его базой данных. Проверка будет повторена.'
            if exc.code not in (500,502,503,504) or attempt==attempts-1:raise
        except urllib.error.URLError as exc:
            if attempt==attempts-1:raise
            if isinstance(exc.reason,ssl.SSLCertVerificationError) and not proxy:raise
        except (TimeoutError,ConnectionError,http.client.RemoteDisconnected):
            if attempt==attempts-1:raise
        if proxy and attempt==1:
            # Same approved source and mandatory TLS verification, different route.
            # A failed relay connection must not take down an otherwise reachable site.
            opener=direct_opener;route='direct-fallback'
        time.sleep(0.5)
    with response:
        raw=response.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ValueError('Документ больше 20 МБ')
        if response.headers.get('Content-Encoding')=='gzip':
            import io
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:raw=stream.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES:raise ValueError('Распакованный документ больше 20 МБ')
        content_type=response.headers.get_content_type()
        charset=response.headers.get_content_charset()
        final=canonical(response.url)
        etag,modified=response.headers.get('ETag'),response.headers.get('Last-Modified')
    result=decode(raw,content_type,charset,final)
    result['network_route']=route
    if payload is None and result['type']=='text' and len(raw)<2*1024*1024:
        with connection() as con:
            con.execute('INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?,?,?,?)',
                (url,etag,modified,raw,content_type,charset,final,time.time()))
            trim_cache(con)
    return result

def decode(raw,content_type,charset,url,cached=False):
    binary=raw.startswith(b'%PDF') or content_type=='application/pdf' or content_type in ('application/msword','application/vnd.openxmlformats-officedocument.wordprocessingml.document') or Path(urllib.parse.urlsplit(url).path).suffix.lower() in {'.pdf','.doc','.docx','.xls','.xlsx','.rtf','.zip','.rar','.7z'}
    if binary:return {'raw':raw,'text':'','type':'binary','url':url,'cached':cached}
    for encoding in [charset,'utf-8','cp1251']:
        if encoding:
            try:text=raw.decode(encoding);break
            except (UnicodeDecodeError,LookupError):continue
    else:text=raw.decode('utf-8',errors='replace')
    import re
    if re.search(r'<title[^>]*>[^<]*(access denied|just a moment|проверка браузера|captcha)',text,re.I):
        raise ValueError('Источник требует проверки браузера; каталог не прочитан')
    return {'raw':raw,'text':text,'type':'text','url':url,'cached':cached}
