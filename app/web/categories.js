'use strict';
let editingCategoryId = null, savingCategory = false;

function categoryOptions(){
  return ['其他票据','出行交通','科研材料','铁路客票'];
}
function categorySelectOptions(current){
  const options=categoryOptions();
  return (options.includes(current)?'':'<option value="" disabled selected>请选择发票类型</option>')+options.map(name=>`<option value="${name}" ${name===current?'selected':''}>${name}</option>`).join('');
}
function categoryHistoryHtml(records){
  if(!records.length)return '<p class="category-history-empty">暂无类型修改记录</p>';
  return `<ol class="category-history-list">${records.map(record=>`<li><div><span>${esc(record.before||'未设置')}</span><span aria-label="改为"> → </span><strong>${esc(record.after)}</strong></div><time datetime="${esc(record.created_at)}">${esc(new Date(record.created_at).toLocaleString('zh-CN',{hour12:false}))}</time></li>`).join('')}</ol>`;
}
async function loadCategoryHistory(id,targetId){
  const element=document.getElementById(targetId);
  if(!element)return;
  const request=String((+(element.dataset.request||0))+1);
  element.dataset.request=request;
  const current=()=>document.getElementById(targetId)===element&&element.dataset.invoiceId===id&&element.dataset.request===request;
  try{
    const records=await api('/api/category-history/'+encodeURIComponent(id));
    if(current())element.innerHTML=categoryHistoryHtml(records);
  }catch(error){if(current())element.textContent='修改记录暂时无法读取，请重新打开查看。';}
}
function openCategoryEditor(id){
  const row=state.invoices.find(item=>item.id===id);
  if(!row)return;
  editingCategoryId=id;
  $('#category-editor-body').innerHTML=`<p class="category-invoice-title">${esc(row.title)}</p><p class="category-current">当前类型：<strong id="category-current-name">${esc(row.category||'未设置')}</strong></p><form id="category-form"><label for="category-new-value">新的发票类型</label><select id="category-new-value" name="category" required>${categorySelectOptions(row.category)}</select><div class="category-form-actions"><span id="category-save-status" role="status"></span><button class="button primary" type="submit">保存类型</button></div></form><section class="category-history-section"><h3>类型修改记录</h3><div id="category-editor-history" data-invoice-id="${id}" aria-live="polite">正在读取记录…</div></section>`;
  $('#category-dialog').showModal();
  $('#category-new-value').focus();
  loadCategoryHistory(id,'category-editor-history');
}
function activityDetail(event){
  if(event.action==='修改发票类型'){
    try{const change=JSON.parse(event.detail);return `${change.before||'未设置'} → ${change.after}`;}catch(_){}
  }
  return event.action==='修改'?'更新台账信息':event.detail;
}
document.addEventListener('submit',async event=>{
  if(event.target.id!=='category-form')return;
  event.preventDefault();
  if(savingCategory)return;
  const category=$('#category-new-value').value.trim();
  if(!categoryOptions().includes(category)){$('#category-save-status').textContent='请选择发票类型';return;}
  const id=editingCategoryId,button=event.target.querySelector('button[type="submit"]');
  savingCategory=true;button.disabled=true;button.textContent='正在保存…';
  $('#category-save-status').textContent='正在保存…';
  try{
    const before=state.invoices.find(row=>row.id===id)?.category;
    await api('/api/update',{ids:[id],patch:{category}});
    await load();
    if(editingCategoryId===id){
      $('#category-current-name').textContent=category;
      $('#category-save-status').textContent=before===category?'类型未改变': '已保存并记录';
      await loadCategoryHistory(id,'category-editor-history');
    }
  }catch(error){if(editingCategoryId===id)$('#category-save-status').textContent=error.message||'保存失败，请重试';}
  finally{savingCategory=false;button.disabled=false;button.textContent='保存类型';}
});
document.addEventListener('click',event=>{
  if(event.target.closest('#close-category-dialog'))$('#category-dialog').close();
});
document.addEventListener('DOMContentLoaded',()=>{
  $('#category-dialog').addEventListener('close',()=>{editingCategoryId=null;});
});
