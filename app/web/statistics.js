'use strict';
let statisticsEditor=null, statisticsSaving=false;
function statisticsRowHtml(row,index){
  const field=(key,label,readonly=false)=>`<label>${label}<input data-stat-field="${key}" value="${esc(row[key]??'')}" ${readonly?'readonly':''} ${key==='safety'&&row.category!=='试剂'?'disabled':''} maxlength="${key==='url'?2000:key==='safety'?100:key==='company'?200:300}" placeholder="${key==='company'?'未识别销售方，请补填':'可留空'}"></label>`;
  return `<fieldset class="receipt-manual-item statistics-item" data-stat-index="${index}"><legend>商品 ${index+1}</legend><div class="form-grid"><label>类别<select data-stat-field="category">${['试剂','仪器','耗材'].map(value=>`<option ${value===row.category?'selected':''}>${value}</option>`).join('')}</select></label>${field('name','名称（与入库单一致）',true)}${field('price','价格（含税，元）',true)}${field('purpose','用途')}${field('company','公司（自动识别，可修改）')}${field('url','网址（默认留空）')}${field('safety','安全等级（仅试剂，选填）')}${field('notes','备注')}</div></fieldset>`;
}
async function openStatistics(id){
  if(statisticsSaving)return;
  statisticsEditor=await api('/api/receipt-jobs/'+encodeURIComponent(id)+'/statistics');
  $('#statistics-body').innerHTML=`<p class="receipt-note">公司自动识别销售方，可修改。类别仅限试剂、仪器、耗材；未明确识别的默认耗材，可修改。网址及安全等级不自动填写，安全等级仅供试剂填写。</p><form id="statistics-form" novalidate>${statisticsEditor.rows.map(statisticsRowHtml).join('')}<div class="receipt-manual-actions"><span id="statistics-status" role="status" aria-live="polite"></span><button class="button primary" type="submit">保存并生成统计表格</button></div></form>`;
  $('#statistics-dialog').showModal();
}
document.addEventListener('click',event=>{
  const open=event.target.closest('[data-statistics-job]');
  if(open){openStatistics(open.dataset.statisticsJob).catch(report);return;}
  if(event.target.closest('#close-statistics')&&!statisticsSaving)$('#statistics-dialog').close();
});
document.addEventListener('change',event=>{
  if(event.target.dataset.statField!=='category')return;
  const safety=event.target.closest('[data-stat-index]').querySelector('[data-stat-field="safety"]');
  safety.disabled=event.target.value!=='试剂';
  if(safety.disabled)safety.value='';
});
document.addEventListener('submit',async event=>{
  if(event.target.id!=='statistics-form')return;
  event.preventDefault();
  if(statisticsSaving)return;
  const form=event.target;
  const rows=[...form.querySelectorAll('[data-stat-index]')].map(section=>{
    const original=statisticsEditor.rows[Number(section.dataset.statIndex)];
    return {...original,...Object.fromEntries([...section.querySelectorAll('[data-stat-field]')].map(input=>[input.dataset.statField,input.value]))};
  });
  statisticsSaving=true;
  form.querySelectorAll('input,select,button').forEach(input=>{input.disabled=true;});
  $('#statistics-status').textContent='正在保存并生成…';
  try{
    await api('/api/receipt-jobs/'+encodeURIComponent(statisticsEditor.job_id)+'/statistics',{rows});
    await load();
    $('#statistics-dialog').close();
    toast('统计表格已更新，可下载');
  }catch(error){$('#statistics-status').textContent=error.message||'保存失败，请重试';}
  finally{
    statisticsSaving=false;
    form.querySelectorAll('input,select,button').forEach(input=>{input.disabled=false;});
    form.querySelectorAll('[data-stat-index]').forEach(section=>{
      section.querySelector('[data-stat-field="safety"]').disabled=section.querySelector('[data-stat-field="category"]').value!=='试剂';
    });
  }
});
document.addEventListener('cancel',event=>{if(event.target.id==='statistics-dialog'&&statisticsSaving)event.preventDefault();},true);
