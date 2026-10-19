/* 外观偏好独立保存；切换不刷新页面、不改动票据和表单。 */
(() => {
  'use strict';
  const themes = [
    {id:'classic',name:'经典蓝白'},
    {id:'paper',name:'暖纸墨绿'},
    {id:'graphite',name:'石墨深色'},
    {id:'swiss',name:'黑白橙色'}
  ];
  const key = 'piaoyu-appearance-v1';
  let current = 'classic';
  function apply(value, persist){
    current = themes.some(theme=>theme.id===value)?value:'classic';
    document.documentElement.dataset.theme=current;
    const index=themes.findIndex(theme=>theme.id===current);
    const button=document.getElementById('theme-switch');
    if(button){
      button.querySelector('span').textContent=themes[index].name;
      button.setAttribute('aria-label',`切换界面风格，当前${themes[index].name}`);
      button.title=`点击切换至${themes[(index+1)%themes.length].name}`;
    }
    if(persist){try{localStorage.setItem(key,current);}catch(_){/* 受限浏览器仍可在本次页面切换。 */}}
  }
  try{current=localStorage.getItem(key)||'classic';}catch(_){}
  apply(current,false);
  document.addEventListener('DOMContentLoaded',()=>apply(current,false));
  document.addEventListener('click',event=>{
    if(!event.target.closest('#theme-switch'))return;
    const index=themes.findIndex(theme=>theme.id===current);
    apply(themes[(index+1)%themes.length].id,true);
  });
  window.addEventListener('storage',event=>{
    if(event.key===key||event.key===null)apply(event.newValue,false);
  });
})();
