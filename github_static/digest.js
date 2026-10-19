"use strict";
let digestDate="";
function digestTime(value){
 const at=new Date(value);
 return value&&!Number.isNaN(at.getTime())?new Intl.DateTimeFormat('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Moscow'}).format(at)+' МСК':'не указано';
}
function digestURL(item){
 try{
  const url=new URL(item.url);
  if(!['https:','http:'].includes(url.protocol)||url.username||url.password)return null;
  const project=item.source_id==='regulation'?/^https:\/\/regulation\.gov\.ru\/api\/npalist\/(\d+)\/?$/.exec(url.href):null;
  return project?'https://regulation.gov.ru/projects/'+project[1]+'/':url.href;
 }catch{return null;}
}
function renderDigestView(data){
 const history=data.digests||{reports:[]};
 const reports=Array.isArray(history.reports)?history.reports:[];
 const report=reports.find(r=>r.date===digestDate)||reports[reports.length-1];
 if(!report){
  $('body').innerHTML='<section class="panel digest-empty"><span class="badge neutral">Каждый день · 18:00 МСК</span><h2>Здесь появится ИИ-сводка за день</h2><p>Коротко о новых публикациях: предмет документа, возможная связь с центром и что сверить в оригинале. ОМС рассматривается в первую очередь. GitHub может запускать задания с задержкой.</p><p class="note">Открытая модель Qwen3 работает на вычислителе GitHub Actions. API-ключи и платные сервисы не нужны.</p></section>';
  return;
 }
 const status={complete:'Сводка готова',empty:'Новых публикаций в ленте нет',partial:'Разобрана часть документов',unavailable:'ИИ временно недоступен'}[report.status]||'Проверка результата';
 const items=Array.isArray(report.items)?report.items:[];
 const oms=items.filter(p=>(p.topics||[]).includes('ОМС')).length;
 const projects=items.filter(p=>p.category==='Проект'||p.date_kind==='registration').length;
 const issues=Array.isArray(report.source_issues)?report.source_issues:[];
 const lastAttempt=history.last_attempt;
 const liveEnabled=(Array.isArray(data.sources)?data.sources:[]).filter(s=>(data.settings?.enabled_sources||[]).includes(s.id));
 const liveResponding=liveEnabled.filter(s=>['collected','partial'].includes(s.status)).length;
 $('body').innerHTML='<div class="digest-toolbar"><div><span class="badge '+(report.status==='complete'||report.status==='empty'?'teal':'amber')+'">'+escape(status)+'</span>'+(report.provisional?'<span class="badge neutral">Предварительная · до 18:00</span>':'')+'</div><label class="digest-date-label">Дата сводки<select id="digest-date" aria-label="Дата сводки">'+reports.slice().reverse().map(r=>'<option value="'+escape(r.date)+'">'+escape(date(r.date))+'</option>').join('')+'</select></label></div>'+
 '<section class="panel digest-overview"><h2>'+escape(date(report.date))+'</h2><p class="digest-period">Найдены сборщиком: '+escape(digestTime(report.period_start))+' — '+escape(digestTime(report.period_end))+'</p><div class="digest-counts"><span>Документов <strong>'+Number(report.total||0)+'</strong></span><span>Разобрано ИИ <strong>'+Number(report.analyzed||0)+'</strong></span><span>ОМС <strong>'+oms+'</strong></span><span>Проектов <strong>'+projects+'</strong></span>'+(report.manual_review_count?'<span>Нужна проверка <strong>'+Number(report.manual_review_count)+'</strong></span>':'')+'</div><p class="note">'+escape(report.note||'Сверяйте выводы с оригиналами документов.')+'</p><p class="digest-generated">Сводка: '+escape(digestTime(report.generated_at))+' · Данные сбора: '+escape(digestTime(report.collection_checked_at))+(report.model?' · Qwen3 4B · GitHub Actions':' · Без обращения к ИИ')+'</p></section>'+
 (report.error?'<div class="alert error">'+escape(report.error)+'</div>':'')+
 (lastAttempt&&lastAttempt.date===report.date?'<div class="alert error">Повторная попытка не завершена. Показана предыдущая готовая сводка. '+escape(lastAttempt.error||'')+'</div>':'')+
 (liveEnabled.length?'<p class="plain-note digest-current-coverage"><a class="text-link" href="#sources" id="digest-current-sources">Текущие статусы источников: '+liveResponding+' / '+liveEnabled.length+' →</a> · Последний полный сбор: '+escape(digestTime(data.last_checked_at))+'</p>':'')+
 '<details class="selection-note digest-coverage"><summary><span>На момент этой сводки: '+Number(report.responding_sources||0)+' / '+Number(report.enabled_sources||0)+(issues.length?' · были ограничения сбора':'')+'</span><span class="disclosure-hint">Подробнее</span></summary><div class="notice-content"><p><strong>Это сохранённые статусы при подготовке сводки '+escape(digestTime(report.generated_at))+'.</strong> Они не меняются после исправления источников. Свежие результаты доступны во вкладке «Источники».</p><p>Обзор относится к найденным публикациям. Отсутствие записи не подтверждает отсутствие изменений во всём законодательстве.</p>'+issues.map(s=>'<p><strong>'+escape(s.name)+'</strong>: '+escape(labels[s.status]||s.status)+'. '+escape(s.detail||'')+'</p>').join('')+'</div></details>'+
 (items.length?'<section class="panel digest-list">'+items.map(item=>{
  const needsReview=item.manual_review||/за\s+исключением|не\s+распространяется/i.test(item.title||'');
  const analysis=needsReview?null:item.analysis,href=digestURL(item);
  return '<article class="digest-item"><div class="doc-badges"><span class="badge neutral">'+escape(item.category||'Документ')+'</span>'+((item.topics||[]).includes('ОМС')?'<span class="badge teal">ОМС</span>':'')+'<span class="badge neutral">'+(item.basis==='title_only'?'Только название':'По доступному фрагменту')+'</span></div><h3>'+escape(item.title)+'</h3>'+(analysis?'<p class="digest-summary">'+escape(analysis.summary)+'</p><details class="document-details"><summary>Связь с центром, проверка и цитата</summary><div class="document-details-content"><p><strong>По предварительному отбору.</strong> '+escape(item.scope_note||'Связь с центром и круг адресатов необходимо проверить в оригинале.')+'</p><p><strong>Чек-лист проверки.</strong> '+escape(item.checklist||'Сверить полный текст, круг адресатов, регион и даты. Не принимать решение по одному резюме.')+'</p>'+((item.category==='Проект'||item.date_kind==='registration')?'<p>Это проект. Принятие и действующую редакцию нужно проверить отдельно.</p>':'')+'<blockquote>'+escape(analysis.evidence)+'</blockquote><p class="note">Цитата сверена с переданным ИИ названием или фрагментом. Это не проверка полного текста акта.</p></div></details>':'<p class="digest-pending">'+escape(needsReview?(item.review_reason||'В названии есть исключение: резюме скрыто, нужен полный текст.'):'ИИ-анализ ещё не получен. Документ сохранён для следующей попытки.')+'</p>')+'<div class="button-row">'+(href?'<a class="text-link" href="'+escape(href)+'" target="_blank" rel="noopener noreferrer">'+(item.category==='Проект'?'Открыть проект ↗':'Открыть оригинал ↗')+'</a>':'')+'</div></article>';
 }).join('')+'</section>':'<div class="digest-empty-day"><p>За этот период в доступной ленте не найдено новых публикаций по темам центра. Проверяйте ограничения источников выше.</p></div>')+
 '<p class="plain-note digest-footnote">Запуск ежедневно в 18:00 по Москве; возможна задержка GitHub. Сводки хранятся за последние 90 дат. Платный резерв не подключён.</p>';
 $('digest-date').value=report.date;
 if($('digest-current-sources'))$('digest-current-sources').onclick=e=>{e.preventDefault();setView('sources');};
 $('digest-date').onchange=()=>{digestDate=$('digest-date').value;renderDigestView(data);};
}
