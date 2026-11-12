/* Async chart cards: no ECharts or chart computations on the main thread. */
(function() {
    'use strict';
    const MAX_VISIBLE = 2;
    let owner = '', session = '', generation = 0, worker = null, active = null, enabled = true;
    let streamBusy = false, requestSequence = 0, scheduled = false;
    const cards = new Map(), queue = new Set(), visibleCards = new Set();
    const panel = () => document.getElementById('msgs');
    let interactionUntil=0;
    const interacting=()=>{interactionUntil=performance.now()+180;};
    for (const event of ['input','keydown','pointerdown','wheel','touchmove']) window.addEventListener(event,interacting,{passive:true,capture:true});
    panel().addEventListener('scroll',interacting,{passive:true});
    function idle(fn) {
        const token=generation;
        function attempt() {
            const pending=globalThis.navigator?.scheduling?.isInputPending?.({includeContinuous:true});
            const delay=interactionUntil-performance.now();
            if (token===generation && (delay>0 || pending)) { setTimeout(attempt,Math.max(20,delay+8)); return; }
            fn();
        }
        if (window.requestIdleCallback) requestIdleCallback(attempt,{timeout:300}); else setTimeout(attempt,16);
    }
    const metrics = {workerRenders:0,maxMounted:0,worker_ms:0};
    function element(tag,css,text) {
        const node = document.createElement(tag); if (css) node.className=css;
        if (text!==undefined) node.textContent=text; return node;
    }
    function setupWorker() {
        if (worker) return worker;
        const instance = new Worker('/static/chart-worker.js?v=3'); worker=instance;
        instance.onmessage = event => {
            if (worker!==instance || !active || event.data.id!==active.request) return;
            const task=active; active=null;
            if (task && event.data.id===task.request && task.generation===generation && task.card.visible && task.card.node.isConnected) {
                task.card.result={...event.data,width:task.width,count:task.count};
                metrics.workerRenders++; metrics.worker_ms+=event.data.render_ms||0;
                if (!streamBusy) idle(()=>mount(task.card));
            }
            schedule();
        };
        instance.onerror=()=>{
            if (worker!==instance) return;
            if (active) { active.card.status.textContent='图表加载失败，可继续查看文字或原始数据。'; active=null; }
            worker?.terminate(); worker=null; queue.clear();
        };
        return worker;
    }
    function schedule() {
        if (scheduled || active || streamBusy) return;
        scheduled=true;
        idle(()=>{
            scheduled=false;
            if (active || streamBusy) return;
            const next=[...queue].find(card=>card.visible && card.data && card.node.isConnected);
            if (!next) return;
            queue.delete(next);
            const width=Math.max(240,Math.min(1200,Math.floor(next.body.clientWidth||600)));
            const request=++requestSequence;
            active={card:next,generation,request,width,count:next.count};
            try { setupWorker().postMessage({id:request,payload:next.data,width,count:next.count}); }
            catch (_) { active=null; next.status.textContent='当前浏览器暂不支持后台绘图，文字结果仍可查看。'; }
        });
    }
    function release(card) {
        if (!card.visible && !card.data && !card.controller && !card.result && !card.imageUrl) return;
        visibleCards.delete(card);
        card.visible=false; card.result=null; card.mountedResult=null; card.data=null;
        if (card.imageUrl) URL.revokeObjectURL(card.imageUrl); card.imageUrl=null;
        card.controller?.abort(); card.controller=null;
        card.exportButton.disabled=true;
        queue.delete(card); card.body.replaceChildren();
        card.tip.hidden=true; card.table.replaceChildren();
        card.status.textContent='滚动到此处时异步加载图表';
    }
    async function mount(card) {
        if (streamBusy || !card.visible || !card.node.isConnected || !card.result || !card.data) return;
        if (card.result.error) { card.status.textContent=card.result.error; card.exportButton.disabled=!card.result.svg; return; }
        // Even SVG image paint can occupy the UI thread in some browsers.
        // The Worker rasterizes PNG; SVG text is kept only for file export.
        const result=card.result, token=generation;
        if (!result.svg || result.count!==card.count || result.svg.length>2*1024*1024 || card.mountedResult===result) return;
        if (!(result.blob instanceof Blob) || result.blob.type!=='image/png') return;
        const url=URL.createObjectURL(result.blob);
        const img=element('img'); img.alt=card.data.title; img.decoding='async'; img.src=url;
        try { await img.decode(); } catch (_) { URL.revokeObjectURL(url); card.status.textContent='图像暂时无法解码，原始数据仍可查看。'; return; }
        // Decode may finish in a busy frame. Recheck input priority before any
        // DOM/layout change rather than mounting from a promise microtask.
        await new Promise(resolve=>idle(resolve));
        if (token!==generation || streamBusy || !card.visible || !card.node.isConnected || card.result!==result) { URL.revokeObjectURL(url); return; }
        if (card.imageUrl) URL.revokeObjectURL(card.imageUrl);
        card.imageUrl=url; card.mountedResult=result;
        card.renderWidth=result.width; card.mountedCount=result.count;
        card.exportButton.disabled=false;
        img.style.width='100%'; img.style.height='auto'; card.body.replaceChildren(img);
        card.status.textContent=card.data.subtitle+' · 单位：'+card.data.unit;
        card.reference.textContent='控制/查询依据：'+card.data.reference;
        card.note.textContent=card.data.note;
        metrics.maxMounted=Math.max(metrics.maxMounted,[...visibleCards].filter(c=>c.body.querySelector('img')).length);
    }
    async function load(card) {
        if (!enabled || streamBusy || !card.visible || card.controller || card.data) return;
        const token=generation; card.status.textContent='图表异步加载中，可继续聊天…';
        const controller=new AbortController(); card.controller=controller;
        try {
            const response=await fetch(`/api/chat/charts/${encodeURIComponent(card.id)}?username=${encodeURIComponent(owner)}&session_id=${encodeURIComponent(session)}`,{signal:controller.signal});
            if (!response.ok) throw Error('图表快照暂时无法读取');
            if (streamBusy) return;
            const raw=await response.text();
            await new Promise((resolve,reject)=>idle(()=>{
                try {
                    if (token!==generation || streamBusy || !card.visible || !card.node.isConnected) return;
                    const data=ChartSpec.validate(JSON.parse(raw));
                    card.data=data;
                    const labels=data.panels.every(p=>p.kind==='bar')?['全部类别','末100类','末50类']:['全部点','最近100点','最近50点'];
                    [...card.select.options].forEach((option,index)=>{option.textContent=labels[index];});
                    card.body.style.height=ChartSpec.height(data)+'px'; queue.add(card); schedule();
                } catch(error) { reject(error); }
                finally { resolve(); }
            }));
        } catch (error) {
            if (token===generation && error.name!=='AbortError') card.status.textContent='图表暂时无法加载，文字结果不受影响。';
        } finally {
            if (card.controller===controller) card.controller=null;
            // A replay can pause and resume within one JS turn, before the
            // aborted fetch settles. Resume it once its controller is cleared.
            if (controller.signal.aborted && token===generation && card.visible && !streamBusy) idle(()=>load(card));
        }
    }
    const observer=window.IntersectionObserver ? new IntersectionObserver(entries=>{
        for (const entry of entries) {
            const card=cards.get(entry.target.dataset.chartId); if (!card || card.node!==entry.target) continue;
            if (!entry.isIntersecting) release(card);
            else {
                card.visible=true; visibleCards.add(card);
                if (visibleCards.size>MAX_VISIBLE) {
                    const old=[...visibleCards].find(c=>c!==card); if (old) release(old);
                }
                load(card);
            }
        }
    },{root:panel(),rootMargin:'30px',threshold:.01}) : null;
    function tablePage(card,page=0) {
        if (!card.data) return;
        const rows=card.data.panels.flatMap(p=>p.rows.map(row=>({...row,panel:p.title}))), size=30;
        const start=Math.max(0,Math.min(page*size,Math.max(0,Math.floor((rows.length-1)/size)*size)));
        const table=element('table','chart-data-table'), head=element('tr');
        const idTitle=card.data.panels.every(p=>p.kind==='bar')?'类别序号':'原始观测ID';
        const isTrend=card.data.panels.every(p=>p.kind==='trend');
        for (const title of (isTrend?['图','原始GMT',idTitle,'原值','说明']:['图','序号/类别',idTitle,'值','CL','LCL','UCL','说明'])) head.append(element('th','',title));
        const thead=element('thead'); thead.append(head); table.append(thead);
        const body=element('tbody');
        for (const row of rows.slice(start,start+size)) {
            const tr=element('tr');
            const values=isTrend?[row.panel,row.label,row.id,row.value,row.detail]:[row.panel,row.label,row.id,row.value,row.center??'—',row.lcl??'—',row.ucl??'—',row.detail];
            for (const value of values) tr.append(element('td','',value));
            body.append(tr);
        }
        table.append(body);
        const controls=element('div','chart-table-controls'), prev=element('button','', '上一页'), next=element('button','','下一页');
        prev.disabled=start===0; next.disabled=start+size>=rows.length;
        prev.onclick=()=>tablePage(card,start/size-1); next.onclick=()=>tablePage(card,start/size+1);
        controls.append(prev,element('span','',`${start+1}–${Math.min(start+size,rows.length)} / ${rows.length}`),next);
        card.table.replaceChildren(controls,table); card.table.hidden=false;
    }
    function download(card) {
        if (!card.result?.svg) return;
        const url=URL.createObjectURL(new Blob([card.result.svg],{type:'image/svg+xml;charset=utf-8'}));
        const link=element('a'); link.href=url; link.download='chart-'+card.id+'.svg'; link.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
    }
    function append(descriptor) {
        if (!enabled || !descriptor || !/^[a-f0-9]{32}$/.test(descriptor.id) || cards.has(descriptor.id)) return;
        const node=element('section','chat-chart'), heading=element('h3','',String(descriptor.title||'统计图表'));
        node.dataset.chartId=descriptor.id;
        const status=element('p','chart-status','滚动到此处时异步加载图表'), body=element('div','chart-body'); body.style.height='290px';
        const actions=element('div','chart-actions'), select=element('select'); select.setAttribute('aria-label','图表显示范围');
        for (const [value,title] of [[500,'全部点'],[100,'最近100点'],[50,'最近50点']]) { const o=element('option','',title); o.value=value; select.append(o); }
        const dataButton=element('button','','查看原始数据'), exportButton=element('button','','导出SVG');
        exportButton.disabled=true;
        const reference=element('p','chart-reference'), note=element('p','chart-note'), tip=element('output','chart-point-tip'); tip.hidden=true;
        const table=element('div','chart-table-wrap'); table.hidden=true;
        actions.append(select,dataButton,exportButton); node.append(heading,status,body,tip,actions,reference,note,table);
        const card={id:descriptor.id,node,status,body,reference,note,tip,table,select,exportButton,count:500,visible:false,data:null,result:null,controller:null};
        cards.set(card.id,card); panel().querySelector('.welcome')?.remove(); panel().append(node);
        select.onchange=()=>{ card.count=Number(select.value); if (card.data) { exportButton.disabled=true; status.textContent='图表更新中，可继续聊天…'; queue.add(card); schedule(); } };
        dataButton.onclick=()=>{ if (card.data) tablePage(card); else load(card); };
        exportButton.onclick=()=>download(card);
        let hoverFrame=null;
        body.onpointermove=event=>{
            if (hoverFrame!==null || !card.data) return;
            const x=event.clientX,y=event.clientY;
            hoverFrame=requestAnimationFrame(()=>{
                hoverFrame=null;
                const image=body.querySelector('img'); if (!image || !card.data) return;
                const rect=image.getBoundingClientRect(), width=card.renderWidth;
                const localX=(x-rect.left)*width/rect.width, localY=(y-rect.top)*ChartSpec.height(card.data)/rect.height;
                const pi=Math.floor((localY-110)/260), p=card.data.panels[pi];
                if (!p || localY-(110+pi*260)>180 || localY<110 || localX<66 || localX>width-26) { tip.hidden=true; return; }
                const rows=ChartSpec.visibleRows(p,card.mountedCount), fraction=(localX-66)/(width-92);
                const index=Math.min(rows.length-1,Math.max(0,p.kind==='bar'?Math.floor(fraction*rows.length):Math.round(fraction*(rows.length-1))));
                const r=rows[index]; tip.textContent=`${r.label} · ${r.value} ${card.data.unit} · ${r.id} · ${r.detail}`+(r.signal?' · 后端判异点':'')+(r.excluded?' · 存在未冲销的排除记录':''); tip.hidden=false;
            });
        };
        body.onpointerleave=()=>{ tip.hidden=true; };
        if (observer) observer.observe(node);
        else { [...visibleCards].forEach(release); card.visible=true; visibleCards.add(card); load(card); }
    }
    window.ChatCharts={
        configure(value){enabled=value!==false;},
        reset(username,sessionId){
            generation++; owner=username; session=sessionId; streamBusy=false;
            observer?.disconnect(); for (const card of [...visibleCards]) release(card);
            cards.clear(); queue.clear(); visibleCards.clear(); worker?.terminate(); worker=null; active=null; scheduled=false;
        },
        append,
        streaming(value){
            streamBusy=!!value;
            for (const card of visibleCards) {
                if (value) card.controller?.abort();
                else if (card.visible) { if (card.result) idle(()=>mount(card)); load(card); }
            }
            if (!value) schedule();
        },
        metrics(){return {...metrics,cards:cards.size,mounted:[...visibleCards].filter(c=>c.body.querySelector('img')).length,
            retainedPayloads:[...cards.values()].filter(c=>c.data).length,queued:queue.size,workerActive:!!active};}
    };
    let resizeTimer;
    window.addEventListener('resize',()=>{
        clearTimeout(resizeTimer); resizeTimer=setTimeout(()=>{for (const card of visibleCards) if(card.data)queue.add(card);schedule();},180);
    },{passive:true});
})();
