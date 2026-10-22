/* Modeling workspace: existing chat/upload APIs and durable approval state. */
'use strict';
const $ = id => document.getElementById(id);
const initialDraft = new URLSearchParams(location.search).get('draft');
let username = '', sessionId = null, sessions = [], attachment = null;
let busy = false, controller = null, pendingApproval = false, initialized = false;
let view = location.pathname === '/records' ? 'records' : 'chat';
let recordLimit = 30;
function matchingRecords() {
    const query = $('recordSearch').value.trim().toLocaleLowerCase();
    return sessions.filter(session => (session.title || '未命名对话').toLocaleLowerCase().includes(query));
}
const statusLabels = {idle:'准备就绪', complete:'任务已完成', cancelled:'待确认操作已取消', limited:'已达到执行上限', incomplete:'上次流程尚未完成，请核对执行结果后继续', unavailable:'流程状态暂不可用', approval_required:'等待确认操作'};
async function api(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(`请求失败（${response.status}）`);
    return response.json();
}
function notice(message) { $('notice').textContent = message; $('notice').hidden = false; }
function status(message) { $('workflowStatus').textContent = message; }
function renderMarkdown(element, text) {
    if (window.marked && window.DOMPurify) element.innerHTML = DOMPurify.sanitize(marked.parse(text));
    else element.textContent = text;
}
function setBusy(value) {
    busy = value;
    for (const id of ['inputBox','sendBtn','uploadBtn','newChatTopBtn','removeAttachmentBtn','approveBtn','rejectBtn']) $(id).disabled = value || !initialized;
    $('sendBtn').hidden = value && !!controller;
    $('stopBtn').hidden = !value || !controller;
    $('msgs').setAttribute('aria-busy', String(value));
    document.querySelectorAll('.session-item,.record-card').forEach(el => el.disabled = value);
}
function rememberSession() {
    if (!sessionId) return;
    try { sessionStorage.setItem(`modeling.session.${username}`, sessionId); } catch (_) { /* URL remains the fallback. */ }
    const url = new URL(location.href);
    url.searchParams.set('session_id', sessionId);
    history.replaceState({}, '', url.pathname + url.search);
}
function storedSession() {
    try { return sessionStorage.getItem(`modeling.session.${username}`); } catch (_) { return null; }
}
function closeNavigation() { document.body.classList.remove('sidebar-open'); $('menuToggle').setAttribute('aria-expanded','false'); }
function setView(next, update = true) {
    view = next;
    $('chatColumn').hidden = view !== 'chat'; $('recordsView').hidden = view !== 'records';
    $('chatNav').classList.toggle('active', view === 'chat'); $('recordsNav').classList.toggle('active', view === 'records');
    $('pageTitle').textContent = view === 'chat' ? '制造助手' : '操作记录';
    if (update) {
        const url = (view === 'chat' ? '/' : '/records') + (sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '');
        if (url !== location.pathname + location.search) history.pushState({}, '', url);
    }
    closeNavigation();
}
function renderSessions() {
    $('sessionListUI').replaceChildren(); $('recordList').replaceChildren();
    const matches = matchingRecords();
    const visibleIds = new Set(matches.slice(0, recordLimit).map(session => session.id));
    let lastDay = null;
    for (const [index, session] of sessions.entries()) {
        const targets = [];
        if (index < 30) targets.push('sessionListUI');
        if (visibleIds.has(session.id)) targets.push('recordList');
        for (const target of targets) {
            const button = document.createElement('button');
            button.className = target === 'recordList' ? 'record-card' : 'session-item' + (session.id === sessionId ? ' active' : '');
            button.textContent = session.title || '未命名对话'; button.disabled = busy;
            if (target === 'recordList') {
                const date = new Date(Number(session.updated_at) * 1000);
                const valid = Number(session.updated_at) > 0 && !Number.isNaN(date.getTime());
                const day = valid ? date.toLocaleDateString('zh-CN', {year:'numeric', month:'long', day:'numeric'}) : '时间未知';
                if (day !== lastDay) {
                    const heading = document.createElement('h3');
                    heading.className = 'record-date'; heading.textContent = day;
                    $('recordList').append(heading); lastDay = day;
                }
                const time = document.createElement('time');
                time.className = 'record-time';
                time.textContent = valid ? '最近更新 ' + date.toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', hour12:false}) : '暂无时间信息';
                if (valid) time.dateTime = date.toISOString();
                button.prepend(time);
            }
            button.onclick = () => selectSession(session.id);
            $(target).append(button);
        }
    }
    if (!sessions.length) { $('sessionListUI').textContent = '还没有历史对话'; $('recordList').textContent = '开始一次对话，记录将显示在这里。'; }
    else if (!matches.length) $('recordList').textContent = '没有找到匹配的记录';
    $('recordLoadStatus').textContent = matches.length ? `已显示 ${Math.min(recordLimit, matches.length)} / ${matches.length} 条` + (recordLimit < matches.length ? '，向下滚动加载更多' : '，已显示全部') : '';
}
document.addEventListener('input', event => {
    if (event.target.id !== 'recordSearch') return;
    recordLimit = 30;
    $('recordsView').scrollTop = 0;
    renderSessions();
});
$('recordsView').addEventListener('scroll', () => {
    const panel = $('recordsView');
    if (panel.scrollHeight - panel.scrollTop - panel.clientHeight > 160 || recordLimit >= matchingRecords().length) return;
    recordLimit += 30;
    renderSessions();
}, {passive:true});
async function refreshSessions() {
    const data = await api(`/sessions/${encodeURIComponent(username)}`);
    sessions = (data.sessions || []).slice().reverse();
    if (!sessions.some(s => s.id === sessionId)) {
        const candidates = [new URLSearchParams(location.search).get('session_id'), storedSession(), data.active_session, sessions[0]?.id];
        sessionId = candidates.find(id => sessions.some(s => s.id === id)) || null;
    }
    rememberSession();
    renderSessions();
}
function appendMessage(role, text) {
    $('msgs').querySelector('.welcome')?.remove();
    const wrapper = document.createElement('div'); wrapper.className = `msg-wrapper ${role}-wrapper`;
    const bubble = document.createElement('div'); bubble.className = `bubble ${role}-bubble`;
    if (role === 'agent') renderMarkdown(bubble, text); else bubble.textContent = text;
    wrapper.append(bubble); $('msgs').append(wrapper); scrollBottom(); return bubble;
}
function scrollBottom() { $('msgs').scrollTop = $('msgs').scrollHeight; }
function createWorkSummary(label) {
    const details = document.createElement('details'); details.className = 'work-summary';
    const summary = document.createElement('summary'); summary.textContent = label;
    const steps = document.createElement('div'); steps.className = 'work-steps';
    steps.textContent = '正在处理请求…';
    details.append(summary, steps); $('msgs').append(details);
    return {summary, steps};
}
async function refreshWorkflow() {
    if (!sessionId) return;
    const data = await api(`/workflow/${encodeURIComponent(username)}/${encodeURIComponent(sessionId)}`);
    pendingApproval = data.status === 'approval_required';
    $('approvalCard').hidden = !pendingApproval;
    $('approvalText').textContent = data.approval?.message || '';
    status(statusLabels[data.status] || '准备就绪');
}
async function loadHistory() {
    const data = await api(`/history/${encodeURIComponent(username)}?session_id=${encodeURIComponent(sessionId)}`);
    $('msgs').replaceChildren();
    let needsSummary = false;
    for (const message of data.messages || []) {
        if (message.role === 'user') { appendMessage('user', message.content || ''); needsSummary = true; }
        else if (message.role === 'assistant' && message.content) {
            if (needsSummary) { createWorkSummary('执行记录').steps.textContent = '此历史对话未记录页面耗时，详细过程可在运行日志中查看。'; needsSummary = false; }
            appendMessage('agent', message.content);
        }
    }
    if (!$('msgs').children.length) $('msgs').append($('welcomeTemplate').content.cloneNode(true));
    await refreshWorkflow();
}
function clearAttachment() { attachment = null; $('attachmentArea').hidden = true; }
async function selectSession(id) {
    if (busy) return;
    setBusy(true); sessionId = id; rememberSession(); clearAttachment(); $('inputBox').value = ''; setView('chat'); renderSessions();
    try { await loadHistory(); } catch (error) { notice(error.message); } finally { setBusy(false); }
}
async function newSession() {
    const data = await api(`/sessions/${encodeURIComponent(username)}/new`, {method:'POST'});
    sessionId = data.session_id;
    rememberSession();
}
async function send(explicitText) {
    if (busy || !initialized) return;
    const text = explicitText ?? $('inputBox').value.trim();
    if (!text && !attachment) return;
    if (explicitText && attachment) { notice('请先移除附件，再确认或取消当前操作。'); return; }
    const file = attachment;
    const message = file ? `${text}\n\n(已附带上传文件 [${file.filename}]，本地路径: [${file.file_path}])` : text;
    controller = new AbortController(); setBusy(true); setView('chat'); $('notice').hidden = true;
    let outcome = 'complete', streamBubble = null, streamText = '';
    let work = null, timer = null, started = 0;
    try {
        if (!sessionId) { await newSession(); await refreshSessions(); }
        appendMessage('user', text + (file ? `\n附件：${file.filename}` : ''));
        started = performance.now(); work = createWorkSummary('Working…');
        timer = setInterval(() => { work.summary.textContent = `Working for ${Math.floor((performance.now() - started) / 1000)}s`; }, 1000);
        $('inputBox').value = ''; clearAttachment(); $('approvalCard').hidden = true; status('正在分析需求…');
        const response = await fetch('/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username,session_id:sessionId,message}), signal:controller.signal});
        if (!response.ok || !response.body) throw new Error(`聊天请求失败（${response.status}）`);
        const reader = response.body.getReader(), decoder = new TextDecoder();
        let buffer = '';
        function event(line) {
            if (!line.startsWith('data:')) return;
            const data = JSON.parse(line.slice(5).trim());
            if (data.type === 'step') {
                status(`正在执行：${data.func || '工具调用'}`);
                if (work.steps.textContent === '正在处理请求…') work.steps.textContent = '';
                const step = document.createElement('div'); step.textContent = data.func || '工具调用'; work.steps.append(step);
            }
            else if (data.type === 'stream_start') { streamBubble = appendMessage('agent',''); streamText = ''; status('正在生成回复…'); }
            else if (data.type === 'stream_chunk') { if (!streamBubble) streamBubble = appendMessage('agent',''); streamText += data.content || ''; renderMarkdown(streamBubble,streamText); scrollBottom(); }
            else if (data.type === 'stream_end') streamBubble = null;
            else if (data.type === 'done') appendMessage('agent',data.reply || '');
            else if (data.type === 'error') { outcome = 'error'; appendMessage('agent',data.message || '执行失败'); }
        }
        while (true) {
            const {done,value} = await reader.read();
            buffer += done ? decoder.decode() : decoder.decode(value,{stream:true});
            const lines = buffer.split('\n'); buffer = lines.pop(); lines.forEach(event);
            if (done) { if (buffer.trim()) event(buffer); break; }
        }
    } catch (error) {
        outcome = error.name === 'AbortError' ? 'stopped' : 'error';
        appendMessage('agent', outcome === 'stopped' ? '已停止接收回答。已完成的操作不会撤销，请查询结果后再继续。' : error.message);
    } finally {
        clearInterval(timer);
        if (work) {
            const seconds = Math.max(1, Math.round((performance.now() - started) / 1000));
            work.summary.textContent = `${outcome === 'complete' ? 'Worked' : outcome === 'stopped' ? 'Stopped after' : 'Failed after'} ${outcome === 'complete' ? 'for ' : ''}${seconds}s`;
            if (work.steps.textContent === '正在处理请求…') work.steps.textContent = outcome === 'complete' ? '回复生成完成，未记录工具调用。' : '请求已结束，请检查回复中的说明。';
        }
        try { await refreshSessions(); await refreshWorkflow(); } catch (error) { notice(`状态刷新失败：${error.message}`); }
        if (outcome === 'error') status('执行出现错误，请检查回复与运行日志');
        if (outcome === 'stopped') status('已停止接收回答，请核对实际执行结果');
        controller = null; setBusy(false); $('inputBox').focus();
    }
}
$('composerForm').onsubmit = event => { event.preventDefault(); send(); };
$('inputBox').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); } };
$('stopBtn').onclick = () => controller?.abort();
$('approveBtn').onclick = () => send('确认执行'); $('rejectBtn').onclick = () => send('取消执行');
$('newChatTopBtn').onclick = async () => { if (busy) return; setBusy(true); try { await newSession(); clearAttachment(); $('inputBox').value=''; await refreshSessions(); await loadHistory(); setView('chat'); } catch(error) { notice(error.message); } finally { setBusy(false); } };
$('uploadBtn').onclick = () => $('fileInput').click();
$('removeAttachmentBtn').onclick = clearAttachment;
$('fileInput').onchange = async () => {
    const file = $('fileInput').files[0]; if (!file || busy) return;
    setBusy(true); status(`正在上传 ${file.name}…`);
    try { const body = new FormData(); body.append('file',file); const result = await api('/api/upload',{method:'POST',body}); if (!result.success) throw new Error(result.message || '上传失败'); attachment = result; $('attachmentName').textContent = result.filename; $('attachmentArea').hidden = false; status('附件已就绪，请补充操作要求'); }
    catch(error) { notice(error.message); status('附件上传失败'); }
    finally { $('fileInput').value=''; setBusy(false); }
};
for (const [id,next] of [['chatNav','chat'],['recordsNav','records']]) $(id).onclick = event => { event.preventDefault(); setView(next); };
window.onpopstate = async () => {
    if (busy) { rememberSession(); return; }
    setBusy(true);
    try {
        sessionId = new URLSearchParams(location.search).get('session_id');
        await refreshSessions(); clearAttachment(); $('inputBox').value = '';
        setView(location.pathname === '/records' ? 'records' : 'chat', false);
        await loadHistory();
    } catch (error) { notice(error.message); } finally { setBusy(false); }
};
$('guideToggle').onclick = () => { $('modelingGuide').hidden = !$('modelingGuide').hidden; $('guideToggle').setAttribute('aria-expanded',String(!$('modelingGuide').hidden)); };
$('menuToggle').onclick = () => { const open = document.body.classList.toggle('sidebar-open'); $('menuToggle').setAttribute('aria-expanded',String(open)); };
$('sidebarBackdrop').onclick = closeNavigation;
document.addEventListener('keydown',event => { if (event.key === 'Escape') closeNavigation(); });
$('msgs').onclick = event => { const card = event.target.closest('[data-prompt]'); if (card && !busy) { $('inputBox').value = card.dataset.prompt; $('inputBox').focus(); } };
async function initialize() {
    setBusy(true); setView(view,false);
    try { const config = await api('/config'); username = config.username; $('username').textContent = username; $('connectionText').textContent = config.agent_engine === 'langgraph' ? 'LangGraph 已连接' : '传统执行引擎'; $('connection').classList.add('connected'); await refreshSessions(); if (!sessionId) { await newSession(); await refreshSessions(); } await loadHistory(); initialized = true; }
    catch(error) { $('connectionText').textContent = '连接失败'; $('connection').classList.add('error'); notice(`加载失败，请刷新重试：${error.message}`); }
    finally { setBusy(false); }
}
initialize().then(() => { if (initialDraft) { $('inputBox').value = initialDraft; $('inputBox').focus(); } });
