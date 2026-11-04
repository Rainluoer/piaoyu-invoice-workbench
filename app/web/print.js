'use strict';
const status=document.querySelector('#print-status');
const printButton=document.querySelector('#print-button');
const pagesElement=document.querySelector('#pages');
const urls=[];
let ready=false;

function printPrepared(){
  if(!ready)return;
  try{window.print();}catch(error){status.textContent='打印预览已准备好，请点击“打印”或按 Ctrl+P。';}
}
printButton.addEventListener('click',printPrepared);
document.querySelector('#close-print').addEventListener('click',()=>window.close());
window.addEventListener('beforeunload',()=>urls.forEach(url=>URL.revokeObjectURL(url)));

async function jsonResponse(response){
  const data=await response.json();
  if(!response.ok)throw Error(data.error||'准备打印失败');
  return data;
}

async function preparePrint(){
  const controller=new AbortController();
  try{
    const ids=decodeURIComponent(location.hash.slice(1)).split(',').filter(Boolean);
    if(!ids.length)throw Error('请先在工作台勾选需要打印的发票');
    const state=await jsonResponse(await fetch('/api/state',{signal:controller.signal}));
    const manifest=await jsonResponse(await fetch('/api/print-invoices',{
      method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Key':state.key},
      body:JSON.stringify({ids}),signal:controller.signal
    }));
    const sheets=new Array(manifest.pages.length);
    let next=0,completed=0;
    async function worker(){
      while(next<manifest.pages.length){
        const index=next++,page=manifest.pages[index];
        const response=await fetch(`/api/print-page/${encodeURIComponent(page.id)}?page=${page.page}&sha=${encodeURIComponent(page.sha256)}`,{signal:controller.signal});
        if(!response.ok){const data=await response.json();throw Error(`${page.filename}：${data.error||'页面读取失败'}`);}
        const blob=await response.blob();
        if(controller.signal.aborted)return;
        const image=new Image();
        const url=URL.createObjectURL(blob);urls.push(url);
        image.src=url;image.alt=`${page.filename} 第 ${page.page} 页`;
        try{await image.decode();}catch(error){throw Error(`${page.filename}：打印页面加载失败`);}
        if(controller.signal.aborted)return;
        const sheet=document.createElement('section');sheet.className='sheet';sheet.append(image);sheets[index]=sheet;
        status.textContent=`正在准备打印：${++completed} / ${manifest.pages.length} 页`;
      }
    }
    await Promise.all(Array.from({length:Math.min(3,manifest.pages.length)},worker));
    if(!sheets.length)throw Error('没有可打印的页面');
    pagesElement.replaceChildren(...sheets);
    status.textContent=`${manifest.invoice_count} 张发票，共 ${sheets.length} 页。可在打印窗口选择打印机和份数。`;
    document.title=`发票批量打印_${manifest.invoice_count}张_${sheets.length}页`;
    ready=true;printButton.disabled=false;document.body.dataset.ready='true';
    requestAnimationFrame(()=>requestAnimationFrame(printPrepared));
  }catch(error){
    controller.abort();ready=false;printButton.disabled=true;document.body.dataset.ready='error';
    pagesElement.replaceChildren();urls.splice(0).forEach(url=>URL.revokeObjectURL(url));
    status.textContent=error.message||'准备打印失败，请返回工作台重试';
  }
}
preparePrint();
