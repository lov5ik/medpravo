"""Read-only genuine Yandex diagnostic on a disposable GitHub runner.
Uses normal certificate validation and sandbox; positive control checks launch.
"""
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request
from playwright.sync_api import sync_playwright
import monitor as m
import transport

IDS={'ffoms','tfoms','kaluga-health','kaluga-gov','rospotreb','kaluga-rospotreb','rostrud','rkn','fas'}
DENIED=r'Доступ из вашего региона запрещ|Access Denied|Forbidden|ERR_CONNECTION|ERR_CERT|ERR_TIMED_OUT|cannot be reached|Не удается получить доступ'

def probe(context,source,control=False):
    start=time.monotonic();url=source['seeds'][0]
    result={'source':source['id'],'requested':url,'browser':'Yandex'}
    page=context.new_page()
    try:
        response=page.goto(url,wait_until='domcontentloaded',timeout=25000)
        html=page.content();parsed=m.parse_page(html)
        visible=parsed.visible[:3000]
        result.update(status=response.status if response else None,final_url=page.url,title=page.title())
        if re.search(DENIED,visible,re.I) or response and response.status>=400:
            result.update(ok=False,error=visible[:600] or 'HTTP '+str(response.status))
        else:
            links,pages=m.discover(html,url,source) if not control else ([],[])
            result.update(ok=bool(visible) if control else bool(links),links=len(links),pages=len(pages),excerpt=visible[:500])
            if not control and not links:result['error']='Страница открылась, но ссылки на документы не найдены'
    except Exception as exc:result.update(ok=False,error=type(exc).__name__+': '+str(exc)[:700])
    finally:page.close()
    result['seconds']=round(time.monotonic()-start,2)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return result

def main():
    browser=shutil.which('yandex-browser-stable') or shutil.which('yandex-browser')
    if not browser:raise SystemExit('Yandex Browser is not installed')
    print(subprocess.check_output([browser,'--version'],text=True).strip(),flush=True)
    results=[];control=None
    proxy=transport.relay_url(os.environ.get('MEDPRAVO_HTTPS_PROXY',''))
    ids=set(filter(None,os.environ.get('PROBE_SOURCE_IDS','').split(','))) or IDS
    with tempfile.TemporaryDirectory() as profile:
        env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','LC_ALL','DISPLAY','XAUTHORITY','XDG_RUNTIME_DIR')}
        with open(Path(profile)/'browser.log','w+',encoding='utf-8') as log:
            process=subprocess.Popen([browser,'--no-first-run','--user-data-dir='+profile]+(['--proxy-server='+proxy,'--proxy-bypass-list=<-loopback>'] if proxy else [])+[
                '--remote-debugging-address=127.0.0.1','--remote-debugging-port=9222','about:blank'],
                stdout=log,stderr=log,env=env,start_new_session=True)
            try:
                endpoint='http://127.0.0.1:9222';ready=False
                for _ in range(60):
                    try:
                        with urllib.request.urlopen(endpoint+'/json/version',timeout=1) as response:
                            version=json.load(response);ready=True;break
                    except Exception:time.sleep(.5)
                if not ready:
                    log.seek(0);raise RuntimeError('Browser did not start: '+log.read()[-1800:])
                print('CDP:',version.get('Browser'),flush=True)
                with sync_playwright() as playwright:
                    instance=playwright.chromium.connect_over_cdp(endpoint)
                    context=instance.contexts[0]
                    control=probe(context,{'id':'positive-control','seeds':['https://minzdrav.gov.ru/']},True)
                    if not control['ok']:raise RuntimeError('Positive control failed; site access cannot be assessed')
                    for source in m.catalog():
                        if source['id'] in ids:results.append(probe(context,source))
                    instance.close()
            finally:
                with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)
                    process.wait(timeout=5)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf-8') as out:
            out.write('# Яндекс Браузер на GitHub\n\nПоложительный контроль: '+str(bool(control and control['ok']))+'\n\n| Источник | Каталог прочитан | Ссылок | Причина |\n|---|---|---|---|\n')
            for row in results:out.write('| '+row['source']+' | '+str(row['ok'])+' | '+str(row.get('links',0))+' | '+row.get('error','').replace('|',' ').replace('\n',' ')+' |\n')
    Path('yandex-check.json').write_text(json.dumps({'control':control,'sources':results},ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':main()
