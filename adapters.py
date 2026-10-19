"""Official catalog adapters. No page execution or AI is required."""
import datetime as dt
import html
import json
import re
import urllib.parse
import titles
import xml.etree.ElementTree as ET
import document_metadata


def validate_publication_reserve(text,base,source,found,parse_page):
    """The official publisher must confirm the requested issuing authority."""
    u=urllib.parse.urlsplit(base)
    configured={urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get('SignatoryAuthorityId',[''])[0]
                for url in source.get('fallback_seeds',[])}
    authority=urllib.parse.parse_qs(u.query).get('SignatoryAuthorityId',[''])[0]
    if u.hostname!='publication.pravo.gov.ru' or not authority or authority not in configured:
        raise ValueError('Резервный каталог не подтвердил орган, принявший акт')
    selected=[]
    for tag in re.findall(r'<option\b[^>]*>',text,re.I):
        attrs=document_metadata.attrs(tag)
        if re.search(r'\bselected(?:\s|=|>)',tag,re.I):selected.append(attrs.get('value'))
    if authority not in selected:raise ValueError('Портал не подтвердил фильтр по органу')
    expected=source.get('fallback_issuer_pattern') or r'Федеральной службы по надзору в сфере защиты прав потребителей|Главного государственного санитарного врача Российской Федерации'
    if any(not re.search(expected,p['title'],re.I) for p in found):
        raise ValueError('Резерв вернул акты другого ведомства')
    empty=not found and bool(re.search(r'не найден|не обнаружен',parse_page(text).visible,re.I))
    if not found and not empty:raise ValueError('В резервном каталоге не подтверждены документы или пустой результат')
    return empty


def government_rows(text,base,parse_page):
    """Each government news card dates its own publication, independently of signing."""
    rows=[]
    for block in re.split(r'<div\b(?=[^>]*\bclass=["\']headline(?:\s|["\']))[^>]*>',text,flags=re.I)[1:]:
        stamp=re.search(r'<span\b[^>]*class=["\']headline_date["\'][^>]*>\s*<time\b([^>]*)>',block,re.I|re.S)
        if not stamp:continue
        day=document_metadata.normalize(document_metadata.attrs(stamp[1]).get('datetime'))
        for href,title in parse_page(block).links:
            if not re.fullmatch(r'/docs/\d+/?',urllib.parse.urlsplit(href).path):continue
            if not re.search(r'\b(?:постановлен|распоряжен|закон|приказ)\w*\b',title,re.I):continue
            rows.append({'url':urllib.parse.urljoin(base,href),'title':titles.clean(title),'published_at':day,
                         'date_basis':'Дата публикации в ленте Правительства России','source_page':base})
    return rows


def municipal_rows(text,base,parse_page):
    """Keep the act card, its publication date and subject together, not generic file buttons."""
    found=[]
    for block in re.split(r'<div\b(?=[^>]*\bclass=["\']news-item["\'])[^>]*>',text,flags=re.I)[1:]:
        heading=re.search(r'<h3\b[^>]*>(.*?)</h3>',block,re.I|re.S)
        date=re.search(r'<div\b[^>]*class=["\']news-item-date["\'][^>]*>(.*?)</div>',block,re.I|re.S)
        if not heading:continue
        act=next(((u,t) for u,t in parse_page(heading[1]).links if re.search(r'/munitsipalnye-pravovye-akty/\d+/?$',urllib.parse.urlsplit(u).path)),None)
        if not act:continue
        subject=re.search(r'<p\b[^>]*>(.*?)</p>',block[heading.end():],re.I|re.S)
        original=act[1]
        found.append({'url':urllib.parse.urljoin(base,act[0]),'title':titles.combine(original,parse_page(subject[1]).visible if subject else ''),
                      'original_title':original,'published_at':document_metadata.normalize(parse_page(date[1]).visible) if date else None,
                      'date_basis':'Дата размещения в карточке муниципального каталога','source_page':base})
    return found

def parse_clinrec(text,source):
    value=json.loads(text)
    records=value.get('Data')
    if not isinstance(records,list):raise ValueError('Изменилась схема API клинических рекомендаций')
    links=[]
    for row in records:
        identifier=row.get('CodeVersion')
        if not re.fullmatch(r'\d+_\d+',str(identifier)):raise ValueError('API не вернул идентификатор версии КР')
        links.append({'url':'https://cr.minzdrav.gov.ru/preview-cr/'+identifier,
            'title':'Клинические рекомендации: '+row['Name']+' · '+str(row.get('AgeCategoryStr','')),
            'published_at':row.get('PublishDateStr'),'metadata':True,
            'text':json.dumps(row.get('Developers',[]),ensure_ascii=False)})
    total=value.get('TotalRecords',value.get('Total',value.get('TotalCount')))
    return links,int(total) if total is not None else None

def parse_projects(text,source):
    root=ET.fromstring(text)
    if root.tag!='projects':raise ValueError('Изменилась схема открытых данных проектов')
    links=[]
    for row in root.findall('project'):
        identifier=row.get('id','')
        if not identifier.isdigit():raise ValueError('API не вернул идентификатор проекта')
        # The individual official XML resource is a verified route, not an inferred UI URL.
        links.append({'url':'https://regulation.gov.ru/api/npalist/'+identifier,
            'title':'Проект: '+' '.join((row.findtext('title') or '').split()),
            'published_at':row.findtext('publishDate'),'metadata':True,
            'text':row.findtext('department') or ''})
    return links,int(root.get('total','0'))

def miac_rows(text,base,parse_page):
    links=[]
    for block in re.findall(r'<tr\b[^>]*>(.*?)</tr>',text,re.S|re.I):
        page=parse_page(block)
        found={}
        for href,title in page.links:
            if re.fullmatch(r'/documents/\d+',urllib.parse.urlsplit(href).path):
                url=urllib.parse.urljoin(base,href)
                if url not in found or len(title)>len(found[url]):found[url]=title
        for url,title in found.items():
            if not title or title=='Скачать документ':continue
            date=re.search(r'\b(\d{2})/(\d{2})/(\d{4})\b',page.visible)
            links.append({'url':url,'title':title,'published_at':f'{date[3]}-{date[2]}-{date[1]}' if date else None})
    return links

def api_request(source,position=0,limit=50):
    if source['adapter']=='clinrec':
        return ('https://apicr.minzdrav.gov.ru/api.ashx?op=GetJsonClinrecsFilterV2',
            {'filters':[{'fieldName':'status','filterType':1,'filterValueType':2,'value1':0,'value2':'','values':[]}],
             'sortOption':{'fieldName':'publishdate','sortType':2},'pageSize':limit,
             'currentPage':position+1,'useANDoperator':True,'columns':[]})
    if source['adapter']=='projects':
        return ('https://regulation.gov.ru/api/npalist?'+urllib.parse.urlencode({'limit':limit,'offset':position*limit,'sort':'desc'}),None)
    raise ValueError('Неизвестный API-адаптер')

def ffoms_rows(text,base,parse_page):
    links=[]
    for block in re.split(r'<div[^>]+class="structure-list[^"\n]*"[^>]*>',text)[1:]:
        page=parse_page(block)
        job=re.search(r'<div[^>]+class="job"[^>]*>(.*?)</div>',block,re.S)
        if not job:continue
        title=parse_page(job[1]).visible
        files=[(href,label) for href,label in page.links if re.search(r'\.(pdf|docx?|xlsx?)(?:\?|$)',href,re.I)]
        link=next((href for href,_ in page.links if '/documents/' in href and href.rstrip('/')!=urllib.parse.urlsplit(base).path.rstrip('/')),None)
        if (files or link) and title:
            name=re.search(r'<div[^>]+class="name"[^>]*>(.*?)</div>',block,re.S)
            subject=parse_page(name[1]).visible if name else ''
            for href,label in (files or [(link,'')]):
                name=titles.combine(title,subject)
                if len(label)>8 and label not in name and not re.match(r'скачать|download|pdf\b',label,re.I):name+=' · '+label
                links.append({'url':urllib.parse.urljoin(base,href),'title':name,
                              'original_title':title,'published_at':None,'source_page':base})
    return links

def grls_rows(text,base,parse_page):
    months={'янв':1,'фев':2,'мар':3,'апр':4,'мая':5,'июн':6,'июл':7,'авг':8,'сен':9,'окт':10,'ноя':11,'дек':12}
    found=[]
    for block in re.split(r'<div\b[^>]*class="info-panel__data"[^>]*>',text,re.I)[1:]:
        date=re.search(r'class="info-panel__date-time"[^>]*>\s*(\d{1,2})\s+([а-яё]+)\s+(\d{4})',block,re.I)
        title=re.search(r'class="info-panel__topic"[^>]*>(.*?)</div>',block,re.I|re.S)
        page=parse_page(block)
        topic=next((href for href,_ in page.links if 'ForumTopic.aspx?' in href),None)
        if not date or not title or not topic:continue
        month=months.get(date[2][:3].lower())
        if not month:continue
        try:day=dt.date(int(date[3]),month,int(date[1])).isoformat()
        except ValueError:continue
        found.append({'url':urllib.parse.urljoin(base,topic),'title':parse_page(title[1]).visible,'published_at':day,'date_basis':'Дата публикации сообщения в ГРЛС','source_page':base})
    pages=[urllib.parse.urljoin(base,h) for h,_ in parse_page(text).links if re.search(r'Forum(?:Default)?\.aspx\?',h,re.I)]
    return found,list(dict.fromkeys(pages))[:5]

def duma_rows(text,base,parse_page):
    found=[]
    for match in re.finditer(r'<div\b[^>]*data-clickopen=["\x27](/bill/(\d+-\d+))["\x27][^>]*>',text,re.I):
        block=text[match.end():];end=re.search(r'<div\b[^>]*data-clickopen=',block,re.I)
        if end:block=block[:end.start()]
        title=re.search(r'class="o_txt"[^>]*>\s*<p[^>]*>(.*?)</p>',block,re.I|re.S)
        registered=re.search(r'Зарегистрирован\s+(\d{2})\.(\d{2})\.(\d{4})',parse_page(block).visible,re.I)
        if not title:continue
        day=f'{registered[3]}-{registered[2]}-{registered[1]}' if registered else None
        found.append({'url':urllib.parse.urljoin(base,match[1]),'title':'Законопроект № '+match[2]+': '+parse_page(title[1]).visible,'published_at':day,'date_basis':'Дата регистрации законопроекта Госдумой (не принятия закона)','source_page':base})
    pages=[urllib.parse.urljoin(base,h) for h,_ in parse_page(text).links if re.search(r'[?&](?:page|p)=\d+',h)]
    return list({p['url']:p for p in found}.values()),list(dict.fromkeys(pages))[:20]
