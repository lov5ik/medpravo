"""Dates explicitly labelled as publication dates by the official source."""
import datetime as dt
import email.utils
import html
import re
import urllib.parse

MONTHS='января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split()
DATE=r'(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4}|\d{1,2}\s+(?:'+'|'.join(MONTHS)+r')\s+\d{4})'

def normalize(value):
    if not isinstance(value,str):return None
    value=html.unescape(value).strip()
    try:return dt.datetime.fromisoformat(value.replace('Z','+00:00')).date().isoformat()
    except ValueError:pass
    match=re.fullmatch(r'(\d{1,2})[./](\d{1,2})[./](\d{4})(?:[ T,]+\d{1,2}:\d{2}(?::\d{2})?)?',value)
    if match:parts=(int(match[3]),int(match[2]),int(match[1]))
    else:
        match=re.fullmatch(r'(\d{1,2})\s+('+'|'.join(MONTHS)+r')\s+(\d{4})(?:\s*г(?:ода|\.)?)?(?:[, ]+\d{1,2}:\d{2}(?::\d{2})?)?',value,re.I)
        if match:parts=(int(match[3]),MONTHS.index(match[2].lower())+1,int(match[1]))
        else:
            try:return email.utils.parsedate_to_datetime(value).date().isoformat()
            except (ValueError,TypeError,OverflowError):return None
    try:return dt.date(*parts).isoformat()
    except ValueError:return None

def attrs(tag):
    return {m[1].lower():html.unescape(m[3] or m[4] or '') for m in re.finditer(r'([\w:-]+)\s*=\s*(?:(["\'])(.*?)\2|([^\s>]+))',tag,re.S)}

def publication(text,source,parse_page):
    """No document signing dates, effective dates, HTTP dates or file timestamps."""
    candidates=[]
    for tag in re.findall(r'<meta\b[^>]*>',text,re.I):
        values=attrs(tag)
        if (values.get('property','').lower()=='article:published_time' or values.get('itemprop','').lower()=='datepublished'):
            candidates.append((normalize(values.get('content')),'Метаданные datePublished'))
    for match in re.finditer(r'<time\b([^>]*)>(.*?)</time>',text,re.I|re.S):
        values=attrs(match[1]);label=parse_page(match[2]).visible
        if values.get('itemprop','').lower()=='datepublished' or re.match(r'\s*опубликован',label,re.I):
            candidates.append((normalize(values.get('datetime')),'Дата публикации в элементе time'))
        elif source['id'] in ('roszdrav','devices') and 'printpage' in text[max(0,match.start()-180):match.start()]:
            candidates.append((normalize(values.get('datetime')),'Дата на странице документа Росздравнадзора'))
    # Read structured fields, not arbitrary dates inside the legal text.
    visible=parse_page(text).visible
    for match in re.finditer(r'(?:Дата\s+(?:официального\s+)?опубликования|Дата\s+публикации|Опубликовано\s+на\s+сайте)\s*:?\s*('+DATE+r')',visible,re.I):
        candidates.append((normalize(match[1]),'Поле «Дата опубликования / публикации»'))
    valid=[(day,basis) for day,basis in candidates if day]
    dates={day for day,_ in valid}
    if len(dates)==1:return {'date':valid[0][0],'basis':valid[0][1]}
    return {'date':None,'conflict':bool(dates),'basis':'Противоречивые даты источника' if dates else 'Дата публикации не установлена'}

def listing_dates(links,text,base,source,parse_page):
    if source['id']=='pravo' or urllib.parse.urlsplit(base).hostname=='publication.pravo.gov.ru':
        dated={}
        # Every portal card has its own labelled publication date. Never use a
        # different card's date or the signing date embedded in an act's title.
        for block in re.split(r'<div\b[^>]*class=["\'][^"\']*\bdocuments-items\b[^"\']*["\'][^>]*>',text,flags=re.I)[1:]:
            found=publication(block,source,parse_page)
            if found['date']:
                for href,_ in parse_page(block).links:
                    if re.search(r'/(?:document|Document/View)/\d{16}(?:/|$)',href,re.I):
                        dated[urllib.parse.urljoin(base,href)]=found
        for row in links:
            if row['url'] in dated:
                row['published_at']=dated[row['url']]['date'];row['date_basis']='Дата опубликования в карточке портала'
    # One image link and one title link can point to the same act.
    unique={}
    for row in links:
        old=unique.get(row['url'])
        if old is None or len(row['title'])>len(old['title']):unique[row['url']]=row
    return list(unique.values())

def attachments(text,base,source,parse_page,permitted):
    choices=[]
    for href,label in parse_page(text).links:
        url=urllib.parse.urljoin(base,href);path=urllib.parse.urlsplit(url).path
        if not permitted(url,source):continue
        if re.search(r'\.(?:pdf|docx)(?:$)',path,re.I) or urllib.parse.urlsplit(url).hostname=='publication.pravo.gov.ru' and path=='/file/pdf':
            choices.append(url)
    return list(dict.fromkeys(choices))[:2]

def document_date(title,text=''):
    """Date of the current act, never its cited predecessor or a file timestamp."""
    kind=r'(?:дополнительное\s+соглашение|тарифное\s+соглашение|федеральный\s+закон|приказ|постановление|распоряжение|решение|протокол|письмо|соглашение|закон)'
    for value in (title,' '.join((text or '').split())[:1000]):
        value=' '.join((value or '').split())
        header=re.search(r'\b'+kind+r'\b',value,re.I)
        if not header or header.start()>450:continue
        if re.search(r'во\s+исполнение|в\s+соответствии|на\s+основании|внесении\s+изменений',value[:header.start()],re.I):continue
        segment=value[header.start():header.end()+250]
        match=re.search(r'\bот\s+('+DATE+r')',segment,re.I)
        if not match:
            # A signed PDF often has "ПРИКАЗ 07.10.2026 № 8", without "от".
            # Accept only its compact header, before the subject or act number.
            match=re.search(r'('+DATE+r')',segment,re.I)
            if match:
                bare_prefix=segment[header.end()-header.start():match.start()]
                if len(bare_prefix)>160 or re.search(r'№|[«"]|\b(?:об?|в|к|с|на|срок|действует|вступает)\b',bare_prefix,re.I):match=None
        if not match:continue
        prefix=segment[:match.start()]
        if re.search(r'внесени|изменени|\bк\s+(?:тарифному|соглашению|приказу)|[«"]|\bоб?\s+(?:утверждении|признании|порядке)',prefix,re.I):continue
        day=normalize(match[1])
        if day:return day
    return None
