'use strict';
let receiptSelected = new Set(), receiptSelectionInitialized = false;
let receiptStatus = '待报销', receiptStarting = false, receiptPollTimer = null, receiptPollFailures = 0;

function receiptCandidates(){
  return state.invoices.filter(row=>!row.duplicate_of&&!row.missing&&row.category==='科研材料'&&row.filename.toLowerCase().endsWith('.pdf')&&(receiptStatus===''||reimbursementStatus(row.status)===receiptStatus));
}
function latestReceiptJob(){return (state.receipt_jobs||[])[0]||null;}
function receiptMoney(value){return value==null?'金额留空':fullMoney(value);}
function receiptRunning(){return receiptStarting||latestReceiptJob()?.status==='running';}
function receiptJobHtml(job){
  if(!job)return '';
  const names={pending:'待执行',running:'进行中',done:'完成',failed:'失败'};
  return `<div class="receipt-job-heading"><h3>${job.status==='done'?(job.statistics_document_id?'入库单与统计表格已生成':'入库单已生成'):job.status==='failed'?(job.manual_available?'提取未完成':'生成失败'):'正在生成入库单'}</h3><span>${esc(new Date(job.created_at).toLocaleString('zh-CN'))}</span></div><ol class="receipt-steps">${job.steps.map((step,index)=>`<li class="${esc(step.status)}"><strong>${index+1}. ${esc(step.title)}</strong><span>${names[step.status]}</span>${step.summary?`<p>${esc(step.summary)}</p>`:''}</li>`).join('')}</ol>${job.error?`<p class="receipt-error">${esc(job.error)}</p>`:''}${job.retention_error?`<p class="receipt-error">${esc(job.retention_error)}</p>`:''}${job.statistics_error?`<p class="receipt-error">${esc(job.statistics_error)}</p>`:''}${job.manual_available?`<div class="receipt-manual-entry"><button class="button primary" data-manual-receipt="${esc(job.id)}">补填发票信息</button><button class="button" data-resume-receipt="${esc(job.id)}">直接生成（缺失留空）</button><span>可只补充需要的信息，也可直接生成。</span></div>`:''}${job.status==='done'?`<div class="receipt-result"><div><strong>${job.invoice_count} 张发票 · ${job.item_count} 项商品 · ${receiptMoney(job.total_cents)}</strong><p>大写：${esc(job.chinese_total||'留空')}</p></div>${receiptDownloadsHtml({id:job.document_id,job_id:job.id,statistics:job.statistics_document_id?{id:job.statistics_document_id}:null},true)}</div><p class="receipt-note">请核对并补全项目名称、项目代号、验收人和保管人。</p>`:''}`;
}
function receiptDownloadsHtml(doc,primary=false){
  return `<div class="receipt-download-actions"><a class="button ${primary?'primary':'small'}" href="/api/document/${esc(doc.id)}">${icon('download')}下载入库单</a>${doc.statistics?`<a class="button ${primary?'primary':'small'}" href="/api/document/${esc(doc.statistics.id)}">${icon('download')}下载统计表格</a>`:''}${doc.job_id?`<button class="button small" type="button" data-statistics-job="${esc(doc.job_id)}">${doc.statistics?'编辑统计表格':'生成统计表格'}</button>`:''}</div>`;
}
function receiptPanelHtml(){
  const rows=receiptCandidates(), currentIds=new Set(rows.map(row=>row.id));
  if(!receiptSelectionInitialized){receiptSelected=new Set(currentIds);receiptSelectionInitialized=true;}
  else receiptSelected=new Set([...receiptSelected].filter(id=>currentIds.has(id)));
  const job=latestReceiptJob();
  if(job?.status==='running')scheduleReceiptPoll(job.id);
  const picked=rows.filter(row=>receiptSelected.has(row.id));
  const ready=state.receipt_skill?.available;
  return `<section class="extra-card receipt-builder"><div class="receipt-builder-heading"><h2>生成入库单与统计表格</h2><button class="button primary" id="generate-receipt-button" ${!ready||!picked.length||receiptRunning()?'disabled':''}>${icon('document')}${receiptRunning()?'正在生成…':'一键生成入库单与统计表格'}</button></div><div class="receipt-toolbar"><label>科研材料 PDF<select id="receipt-status-filter" aria-label="入库单发票状态">${['待报销','已报销',''].map(value=>`<option value="${value}" ${value===receiptStatus?'selected':''}>${value||'全部状态'}</option>`).join('')}</select></label><span id="receipt-source-summary">已选 ${picked.length} 张 · 票面合计 ${fullMoney(sum(picked))}</span></div>${ready?'':`<p class="receipt-error">${esc(state.receipt_skill?.reason||'生成服务未就绪，请重启工作台')}</p>`}${state.receipt_skill?.maintenance_warning?`<p class="receipt-error">${esc(state.receipt_skill.maintenance_warning)}</p>`:''}<p class="receipt-note">按 ruku 模板和 PDF 票面识别生成；只处理所选发票的副本。</p><div class="receipt-selection-heading"><label><input type="checkbox" id="receipt-select-all" aria-label="全选入库单发票" ${rows.length&&picked.length===rows.length?'checked':''}> 全选 ${rows.length} 张</label></div><div class="receipt-source-list">${rows.map(row=>`<label class="receipt-source-row"><input type="checkbox" data-receipt-select="${row.id}" aria-label="选择生成入库单：${esc(row.title)}" ${receiptSelected.has(row.id)?'checked':''}><span class="receipt-source-text"><strong>${esc(row.title)}</strong><small>${esc(row.invoice_no||'票号待填写')} · ${esc(row.date||'日期待填写')}${row.project?' · '+esc(row.project):''}</small></span><span class="receipt-source-amount">${fullMoney(row.amount_cents)}</span></label>`).join('')||'<p class="receipt-note">没有符合条件的科研材料 PDF，可切换状态或导入发票。</p>'}</div></section><section id="receipt-job" class="extra-card ${job?'':'hidden'}" aria-live="polite">${receiptJobHtml(job)}</section><section class="extra-card"><h2>已生成的入库单与统计表格 <span class="muted">${state.documents.length}</span></h2><p class="receipt-note">保留最近 5 组入库单及配套统计表格，超出后自动删除最旧一组。</p>${state.documents.map(doc=>`<div class="document-row">${icon('document')}<div><h3>${esc(doc.name)}</h3><p>${doc.generated?`${esc(new Date(doc.created_at).toLocaleString('zh-CN'))} · ${doc.invoice_count} 张发票 · ${doc.item_count} 项商品 · ${receiptMoney(doc.total_cents)}`:esc(doc.relative)} · ${(doc.size/1024).toFixed(1)} KB</p></div>${receiptDownloadsHtml(doc)}${doc.statistics_error?`<p class="receipt-error">${esc(doc.statistics_error)}</p>`:''}</div>`).join('')||'<p class="receipt-note">暂无生成的入库单。</p>'}</section>`;
}
function updateReceiptSelection(){
  const rows=receiptCandidates(),picked=rows.filter(row=>receiptSelected.has(row.id));
  const selectAll=$('#receipt-select-all');
  if(!selectAll)return;
  selectAll.checked=rows.length>0&&picked.length===rows.length;
  selectAll.indeterminate=picked.length>0&&picked.length<rows.length;
  $('#receipt-source-summary').textContent=`已选 ${picked.length} 张 · 票面合计 ${fullMoney(sum(picked))}`;
  $('#generate-receipt-button').disabled=!state.receipt_skill?.available||!picked.length||receiptRunning();
}
function scheduleReceiptPoll(id){
  if(receiptPollTimer)return;
  receiptPollTimer=setTimeout(async()=>{
    receiptPollTimer=null;
    try{
      const job=await api('/api/receipt-jobs/'+encodeURIComponent(id));
      receiptPollFailures=0;
      state.receipt_jobs=[job,...(state.receipt_jobs||[]).filter(item=>item.id!==id)];
      if(job.status==='running'){
        if(view==='documents'){$('#receipt-job').innerHTML=receiptJobHtml(job);updateReceiptSelection();}
        scheduleReceiptPoll(id);
      }else{
        await load();
        toast(job.status==='done'?(job.statistics_error||`入库单与统计表格已生成：${job.item_count} 项，${receiptMoney(job.total_cents)}`):job.error);
      }
    }catch(error){
      receiptPollFailures++;
      if(receiptPollFailures===1)toast('生成进度暂时无法连接，正在自动重试…');
      scheduleReceiptPoll(id);
    }
  },Math.min(800*2**receiptPollFailures,5000));
}
async function generateReceipt(){
  if(receiptRunning())return;
  const ids=receiptCandidates().filter(row=>receiptSelected.has(row.id)).map(row=>row.id);
  if(!ids.length){toast('请勾选需要入库的发票');return;}
  receiptStarting=true;updateReceiptSelection();
  try{
    const job=await api('/api/receipt-jobs',{ids});
    state.receipt_jobs=[job,...(state.receipt_jobs||[]).filter(item=>item.id!==job.id)];
    if(view==='documents')render();
    if(job.status==='running')scheduleReceiptPoll(job.id);else await load();
  }finally{receiptStarting=false;updateReceiptSelection();}
}
document.addEventListener('change',event=>{
  if(event.target.dataset.receiptSelect){event.target.checked?receiptSelected.add(event.target.dataset.receiptSelect):receiptSelected.delete(event.target.dataset.receiptSelect);updateReceiptSelection();}
  if(event.target.id==='receipt-select-all'){
    receiptSelected=event.target.checked?new Set(receiptCandidates().map(row=>row.id)):new Set();
    $$('[data-receipt-select]').forEach(input=>{input.checked=receiptSelected.has(input.dataset.receiptSelect);});updateReceiptSelection();
  }
  if(event.target.id==='receipt-status-filter'){receiptStatus=event.target.value;receiptSelectionInitialized=false;render();}
});
document.addEventListener('click',event=>{if(event.target.closest('#generate-receipt-button'))generateReceipt().catch(report);});

let receiptManual = null, receiptManualSaving = false;
function receiptManualItemHtml(item, index){
  const field=(key,label)=>`<label>${label}<input data-item-field="${key}" value="${esc(item[key]??'')}" placeholder="可留空" ${['quantity','unit_price','amount','tax'].includes(key)?'inputmode="decimal"':''} maxlength="${key==='category'?40:key==='unit'?20:['quantity','unit_price','amount','tax'].includes(key)?40:100}"></label>`;
  return `<fieldset class="receipt-manual-item" data-item-index="${index}"><legend>商品 ${index+1}</legend><div class="form-grid">${field('product','品名')}${field('spec','型号（规格）')}${field('unit','单位')}${field('quantity','数量')}${field('amount','不含税金额（元）')}</div><details class="receipt-optional-fields"><summary>更多字段（选填）</summary><div class="form-grid">${field('category','商品类别')}${field('unit_price','不含税单价（元）')}${field('tax','税额（元）')}</div></details><button type="button" class="text-button receipt-remove-item" data-remove-manual-item="${index}">删除此项</button></fieldset>`;
}
function renderReceiptManual(){
  $('#receipt-manual-body').innerHTML=`<p class="receipt-note">所有字段均可留空，只补充需要的信息；缺失内容在入库单中留空。</p><form id="receipt-manual-form" novalidate>${receiptManual.invoices.map((inv,index)=>`<details class="receipt-manual-invoice" data-invoice-index="${index}" ${inv.extracted_complete?'':'open'}><summary>发票 ${index+1} · ${esc(inv.filename)} <span>${inv.extracted_complete?'已有识别结果，可展开核对':'可选补填'}</span></summary><a class="text-button receipt-original" href="/api/file/${encodeURIComponent(inv.source_id)}" target="_blank" rel="noopener">打开原票</a><div class="form-grid"><label>发票号码（选填）<input data-invoice-field="invoice_no" value="${esc(inv.invoice_no)}" maxlength="80" placeholder="可留空"></label><label>价税合计（元，选填）<input data-invoice-field="total" value="${esc(inv.total)}" inputmode="decimal" maxlength="40" placeholder="可留空"></label></div><div class="receipt-manual-items">${inv.items.map(receiptManualItemHtml).join('')}</div><button type="button" class="button small" data-add-manual-item="${index}">添加商品</button></details>`).join('')}<div class="receipt-manual-actions"><span id="receipt-manual-status" role="status" aria-live="polite"></span><button class="button" type="submit" data-resume="false">保存草稿</button><button class="button primary" type="submit" data-resume="true">按当前信息生成</button></div></form>`;
}
function collectReceiptManual(){
  $$('#receipt-manual-form [data-invoice-index]').forEach(section=>{
    const inv=receiptManual.invoices[Number(section.dataset.invoiceIndex)];
    section.querySelectorAll('[data-invoice-field]').forEach(input=>{inv[input.dataset.invoiceField]=input.value;});
    inv.items=[...section.querySelectorAll('[data-item-index]')].map(fieldset=>Object.fromEntries([...fieldset.querySelectorAll('[data-item-field]')].map(input=>[input.dataset.itemField,input.value])));
    inv.expanded=section.open;
  });
}
async function openReceiptManual(id){
  receiptManual=await api('/api/receipt-jobs/'+encodeURIComponent(id)+'/manual');
  renderReceiptManual();
  $('#receipt-manual-dialog').showModal();
}
document.addEventListener('click',event=>{
  const direct=event.target.closest('[data-resume-receipt]');
  if(direct){directReceiptResume(direct).catch(report);return;}
  const open=event.target.closest('[data-manual-receipt]');
  if(open){openReceiptManual(open.dataset.manualReceipt).catch(report);return;}
  if(event.target.closest('#close-receipt-manual')){$('#receipt-manual-dialog').close();return;}
  const add=event.target.closest('[data-add-manual-item]'), remove=event.target.closest('[data-remove-manual-item]');
  if((!add&&!remove)||receiptManualSaving)return;
  collectReceiptManual();
  const index=Number((add||remove).closest('[data-invoice-index]').dataset.invoiceIndex);
  const inv=receiptManual.invoices[index];
  if(add){if(inv.items.length>=200){toast('每张发票最多 200 项商品');return;}inv.items.push({});}
  else{if(inv.items.length<=1){toast('每张发票至少保留一项商品');return;}inv.items.splice(Number(remove.dataset.removeManualItem),1);}
  renderReceiptManual();
  $$('#receipt-manual-form [data-invoice-index]').forEach(section=>{section.open=!!receiptManual.invoices[Number(section.dataset.invoiceIndex)].expanded;});
});
document.addEventListener('submit',async event=>{
  if(event.target.id!=='receipt-manual-form')return;
  event.preventDefault();
  if(receiptManualSaving)return;
  collectReceiptManual();
  const resume=event.submitter?.dataset.resume==='true';
  receiptManualSaving=true;
  const form=event.target;
  form.querySelectorAll('button,input').forEach(control=>{control.disabled=true;});
  $('#receipt-manual-status').textContent=resume?'正在按当前信息生成…':'正在保存草稿…';
  try{
    const job=await api('/api/receipt-jobs/'+encodeURIComponent(receiptManual.job_id)+'/manual',{invoices:receiptManual.invoices,resume});
    state.receipt_jobs=[job,...(state.receipt_jobs||[]).filter(item=>item.id!==job.id)];
    if(resume){$('#receipt-manual-dialog').close();if(view==='documents')render();if(job.status==='running')scheduleReceiptPoll(job.id);else await load();}
    else $('#receipt-manual-status').textContent='草稿已保存，可关闭后继续补填';
  }catch(error){$('#receipt-manual-status').textContent=error.message||'保存失败，请重试';}
  finally{receiptManualSaving=false;form.querySelectorAll('button,input').forEach(control=>{control.disabled=false;});}
});

async function directReceiptResume(button){
  if(receiptRunning())return;
  receiptStarting=true;button.disabled=true;updateReceiptSelection();
  try{
    const url='/api/receipt-jobs/'+encodeURIComponent(button.dataset.resumeReceipt)+'/manual';
    const data=await api(url);
    const job=await api(url,{invoices:data.invoices,resume:true});
    state.receipt_jobs=[job,...(state.receipt_jobs||[]).filter(item=>item.id!==job.id)];
    if(view==='documents')render();
    if(job.status==='running')scheduleReceiptPoll(job.id);else await load();
  }finally{receiptStarting=false;button.disabled=false;updateReceiptSelection();}
}
