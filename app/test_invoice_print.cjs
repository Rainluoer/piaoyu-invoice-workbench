const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'web/print.js'),'utf8');

function fixture(fail=false){
  const elements=Object.fromEntries(['#print-status','#print-button','#pages','#close-print'].map(id=>[id,{
    disabled:true,textContent:'',children:[],
    addEventListener(event,handler){this[event]=handler;},
    replaceChildren(...children){this.children=children;}
  }]));
  const manifest={invoice_count:2,pages:[{id:'a',page:1,sha256:'hash-a',filename:'甲.pdf'},
    {id:'a',page:2,sha256:'hash-a',filename:'甲.pdf'}, {id:'b',page:1,sha256:'hash-b',filename:'乙.png'}]};
  const printed=[],created=[],revoked=[];
  const document={body:{dataset:{}},querySelector:selector=>elements[selector],
    createElement(){return {append(image){this.image=image;}};}};
  const context=vm.createContext({
    document,location:{hash:'#a%2Cb'},AbortController,
    Image:class{async decode(){await new Promise(resolve=>setTimeout(resolve,this.src.includes('a-1')?8:0));}},
    URL:{createObjectURL(blob){const url='blob:'+blob.name;created.push(url);return url;},revokeObjectURL(url){revoked.push(url);}},
    window:{addEventListener(){},close(){},print(){
      assert.equal(document.body.dataset.ready,'true');
      assert.equal(elements['#pages'].children.length,3);printed.push('print');
    }},
    requestAnimationFrame:callback=>callback(),
    fetch:async (url,options={})=>{
      if(url==='/api/state')return {ok:true,json:async()=>({key:'local-key'})};
      if(url==='/api/print-invoices'){
        assert.equal(options.headers['X-Workbench-Key'],'local-key');
        assert.deepEqual(JSON.parse(options.body).ids,['a','b']);
        return {ok:true,json:async()=>manifest};
      }
      if(fail&&url.includes('page=2'))return {ok:false,json:async()=>({error:'原票已变化'})};
      const parsed=new URL(url,'http://localhost');
      return {ok:true,blob:async()=>({name:parsed.pathname.split('/').pop()+'-'+parsed.searchParams.get('page')})};
    }
  });
  return {context,elements,printed,created,revoked};
}

(async()=>{
  const f=fixture();await vm.runInContext(source,f.context);
  assert.deepEqual(f.elements['#pages'].children.map(sheet=>sheet.image.src),['blob:a-1','blob:a-2','blob:b-1']);
  assert.equal(f.elements['#print-button'].disabled,false);
  assert.equal(f.printed.length,1);
  assert.match(f.elements['#print-status'].textContent,/2 张发票，共 3 页/);
  f.elements['#print-button'].click();assert.equal(f.printed.length,2);
  const failed=fixture(true);await vm.runInContext(source,failed.context);
  await new Promise(resolve=>setTimeout(resolve,15));
  assert.equal(failed.context.document.body.dataset.ready,'error');
  assert.equal(failed.elements['#print-button'].disabled,true);
  assert.equal(failed.elements['#pages'].children.length,0);
  assert.equal(failed.printed.length,0);
  assert.match(failed.elements['#print-status'].textContent,/原票已变化/);
  const app=fs.readFileSync(path.join(__dirname,'web/app.js'),'utf8');
  const block=app.slice(app.indexOf('function printSelectedInvoices('),app.indexOf('async function exportSelectedZip('));
  const ids=Array.from({length:10},(_,i)=>'ticket-'+i),selected=new Set(ids),calls=[],messages=[];
  const popup={};
  const context=vm.createContext({selected,busy:false,window:{open:(...args)=>{calls.push(args);return popup;}},toast:message=>messages.push(message)});
  vm.runInContext(block,context);context.printSelectedInvoices();
  assert.equal(decodeURIComponent(calls[0][0].split('#')[1]),ids.join(','));
  assert.equal(popup.opener,null);assert.equal(selected.size,10);
  context.window.open=()=>null;context.printSelectedInvoices();assert.match(messages[0],/允许.*打印窗口/);
  context.busy=true;context.printSelectedInvoices();assert.equal(messages.length,1);
  console.log('PASS: batch print waits for all pages, preserves order/selection, and stops on errors.');
})().catch(error=>{console.error(error);process.exitCode=1;});
