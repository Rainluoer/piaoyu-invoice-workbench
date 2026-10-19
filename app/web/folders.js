'use strict';
let folderPending = false;

function folderSettingsHtml(){
  return `<div class="folder-settings"><div class="folder-heading"><h3>发票目录</h3><button class="button" id="open-folder-button">${icon('folder')}打开目录</button></div><label for="invoice-folder-path">扫描文件夹</label><div class="folder-controls"><input id="invoice-folder-path" type="text" value="${esc(state.root)}" autocomplete="off" spellcheck="false" aria-label="发票文件夹路径"><button class="button" id="choose-folder-button">选择文件夹</button><button class="button primary" id="save-folder-button">保存并扫描</button></div><p class="receipt-note">扫描包含子文件夹。切换后已有台账保留，原票不移动。${state.root_available===false?' 当前文件夹无法访问，请检查路径或磁盘连接。':''}</p></div>`;
}

async function chooseInvoiceFolder(){
  if(folderPending)return;
  folderPending=true;
  const button=$('#choose-folder-button'),save=$('#save-folder-button');
  button.disabled=true;save.disabled=true;button.textContent='请选择文件夹…';
  try{
    const result=await api('/api/pick-folder',{});
    const input=$('#invoice-folder-path');
    if(!result.cancelled&&input)input.value=result.path;
  }finally{
    folderPending=false;button.disabled=false;save.disabled=false;button.textContent='选择文件夹';
  }
}

async function saveInvoiceFolder(){
  if(folderPending||busy)return;
  const path=$('#invoice-folder-path').value.trim();
  if(!path){toast('请先选择或填写发票文件夹');return;}
  folderPending=true;busy=true;
  const button=$('#save-folder-button');
  button.disabled=true;$('#choose-folder-button').disabled=true;button.textContent='正在扫描…';
  try{
    const result=await api('/api/settings/invoice-folder',{path});
    selected.clear();page=1;receiptSelectionInitialized=false;
    await load();
    toast(`目录已保存：新增 ${result.scan.added} 张，更新 ${result.scan.updated} 张${result.scan.errors.length?'；部分文件读取失败：'+result.scan.errors.slice(0,2).join('；'):''}`);
  }finally{
    folderPending=false;busy=false;
    const save=$('#save-folder-button'),choose=$('#choose-folder-button');
    if(save){save.disabled=false;save.textContent='保存并扫描';}
    if(choose)choose.disabled=false;
  }
}

document.addEventListener('click',event=>{
  const target=event.target.closest('button');
  if(target?.id==='choose-folder-button')chooseInvoiceFolder().catch(report);
  if(target?.id==='save-folder-button')saveInvoiceFolder().catch(report);
});
document.addEventListener('keydown',event=>{
  if(event.target.id==='invoice-folder-path'&&event.key==='Enter'){event.preventDefault();saveInvoiceFolder().catch(report);}
});
