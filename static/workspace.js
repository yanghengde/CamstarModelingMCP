/* Modeling workspace: existing chat/upload APIs and durable approval state. */
'use strict';
const $ = id => document.getElementById(id);
const initialDraft = new URLSearchParams(location.search).get('draft');
let username = '', sessionId = null, sessions = [], attachment = null;
let busy = false, loading = false, controller = null, pendingApproval = false, initialized = false;
let activeRun = null, viewVersion = 0, runClock = null;
const activeRunStates = new Set(['queued','running','cancelling']);
const runIsActive = () => !!activeRun && activeRunStates.has(activeRun.status);
let view = location.pathname === '/records' ? 'records' : 'chat';
const RECORD_PAGE_SIZE = 50;
let recordLimit = RECORD_PAGE_SIZE;
function resizeComposer() {
    const input = $('inputBox');
    if (!input.getClientRects().length) return;
    input.style.height = 'auto';
    input.style.height = `${input.scrollHeight}px`;
    input.style.overflowY = input.scrollHeight > input.clientHeight ? 'auto' : 'hidden';
}
function setComposerText(text) {
    $('inputBox').value = text;
    resizeComposer();
}
$('inputBox').addEventListener('input', resizeComposer);
window.addEventListener('resize', resizeComposer);
function startedSessions() {
    return sessions.filter(session => {
        if (session.run) return true;
        if (typeof session.has_messages === 'boolean') return session.has_messages;
        // Older running servers do not expose message metadata yet.
        // Their untouched drafts have an automatically numbered title.
        return !/^新对话 \d+$/.test(session.title || '');
    });
}
function matchingRecords() {
    const query = $('recordSearch').value.trim().toLocaleLowerCase();
    return startedSessions().filter(session => (session.title || '未命名对话').toLocaleLowerCase().includes(query));
}
const statusLabels = {idle:'准备就绪', complete:'任务已完成', completed:'任务已完成', queued:'任务已提交，正在后台启动', running:'后台执行中 · 可切换页面', cancelling:'正在停止执行…', cancelled:'执行已停止，请核实已提交操作', failed:'执行出现错误，请检查回复与运行日志', interrupted:'上次执行已中断，请核实实际结果', limited:'已达到执行上限', incomplete:'上次流程尚未完成，请核对执行结果后继续', unavailable:'流程状态暂不可用', approval_required:'等待确认操作'};
async function api(url, options) {
    const response = await fetch(url, options);
    let data;
    try { data = await response.json(); }
    catch (_) {
        throw new Error(response.ok ? `接口返回了无效数据（${url}）` : `请求失败（HTTP ${response.status}，${url}），请查看运行日志`);
    }
    if (!response.ok) {
        const error = new Error(typeof data.detail === 'string' ? data.detail : data.detail?.message || `请求失败（${response.status}）`);
        error.detail = data.detail;
        error.status = response.status;
        throw error;
    }
    return data;
}
function notice(message) { $('notice').textContent = message; $('notice').hidden = false; }
function status(message) { $('workflowStatus').textContent = message; }
function renderMarkdown(element, text) {
    if (window.marked && window.DOMPurify) element.innerHTML = DOMPurify.sanitize(marked.parse(text));
    else element.textContent = text;
}
function setBusy(value) {
    loading = value; busy = value || runIsActive();
    renderConfirmationMode();
    if (value) $('confirmationMenu').hidePopover();
    for (const id of ['inputBox','sendBtn','uploadBtn','newChatTopBtn','removeAttachmentBtn','approveBtn','rejectBtn']) $(id).disabled = value || !initialized;
    for (const id of ['inputBox','sendBtn','uploadBtn','removeAttachmentBtn','approveBtn','rejectBtn']) $(id).disabled = busy || !initialized;
    $('sendBtn').hidden = runIsActive();
    $('stopBtn').hidden = !runIsActive();
    $('stopBtn').disabled = loading || activeRun?.status === 'cancelling';
    $('msgs').setAttribute('aria-busy', String(runIsActive()));
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
    if (view === 'chat') resizeComposer();
    $('chatNav').classList.toggle('active', view === 'chat'); $('recordsNav').classList.toggle('active', view === 'records');
    $('pageTitle').textContent = view === 'chat' ? '制造助手' : '操作记录';
    if (update) {
        const url = (view === 'chat' ? '/' : '/records') + (sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '');
        if (url !== location.pathname + location.search) history.pushState({}, '', url);
    }
    closeNavigation();
}
function updateSessionDivider() {
    document.querySelector('.recent-heading')?.classList.toggle('is-scrolled', $('sessionListUI').scrollTop > 0);
}
$('sessionListUI').addEventListener('scroll', updateSessionDivider, {passive: true});
function renderSessions() {
    $('sessionListUI').replaceChildren(); $('recordList').replaceChildren();
    const matches = matchingRecords();
    const visibleIds = new Set(matches.slice(0, recordLimit).map(session => session.id));
    let lastDay = null;
    const started = startedSessions();
    document.querySelector('.recent-heading').hidden = !started.length;
    for (const [index, session] of started.entries()) {
        const targets = [];
        if (index < 30) targets.push('sessionListUI');
        if (visibleIds.has(session.id)) targets.push('recordList');
        for (const target of targets) {
            const button = document.createElement('button');
            button.className = target === 'recordList' ? 'record-card' : 'session-item' + (session.id === sessionId ? ' active' : '');
            const title = document.createElement('span'); title.className = 'session-title'; title.textContent = session.title || '未命名对话'; button.append(title);
            const label = {queued:'执行中',running:'执行中',cancelling:'停止中',approval_required:'待确认',interrupted:'需核实',incomplete:'需核实',limited:'达到上限',failed:'出错'}[session.run?.status];
            if (label) { const badge = document.createElement('span'); badge.className = 'run-badge ' + session.run.status; badge.textContent = label; button.append(badge); }
            button.disabled = loading || !initialized;
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
    if (!started.length) { $('recordList').textContent = '开始一次对话，记录将显示在这里。'; }
    else if (!matches.length) $('recordList').textContent = '没有找到匹配的记录';
    $('recordLoadStatus').textContent = matches.length ? `已显示 ${Math.min(recordLimit, matches.length)} / ${matches.length} 条` + (recordLimit < matches.length ? '，向下滚动加载更多' : '，已显示全部') : '';
    updateSessionDivider();
}
document.addEventListener('input', event => {
    if (event.target.id !== 'recordSearch') return;
    recordLimit = RECORD_PAGE_SIZE;
    $('recordsView').scrollTop = 0;
    renderSessions();
});
$('recordsView').addEventListener('scroll', () => {
    const panel = $('recordsView');
    if (panel.scrollHeight - panel.scrollTop - panel.clientHeight > 160 || recordLimit >= matchingRecords().length) return;
    recordLimit += RECORD_PAGE_SIZE;
    renderSessions();
}, {passive:true});
async function refreshSessions() {
    const data = await api(`/sessions/${encodeURIComponent(username)}`);
    const nextSessions = (data.sessions || []).slice().reverse();
    const changed = JSON.stringify(sessions) !== JSON.stringify(nextSessions);
    sessions = nextSessions;
    const previousSession = sessionId;
    if (!sessionId) {
        const candidates = [new URLSearchParams(location.search).get('session_id'), storedSession(), data.active_session, sessions[0]?.id];
        sessionId = candidates.find(id => sessions.some(s => s.id === id)) || null;
    }
    if (changed || previousSession !== sessionId) {
        rememberSession();
        renderSessions();
        renderConfirmationMode();
    }
}
function appendMessage(role, text) {
    $('msgs').querySelector('.welcome')?.remove();
    const wrapper = document.createElement('div'); wrapper.className = `msg-wrapper ${role}-wrapper`;
    const bubble = document.createElement('div'); bubble.className = `bubble ${role}-bubble`;
    if (role === 'agent') renderMarkdown(bubble, text); else bubble.textContent = text;
    wrapper.append(bubble); $('msgs').append(wrapper); scrollBottom(); return bubble;
}
let scrollFrame = null;
function scrollBottom() {
    if (scrollFrame !== null) return;
    scrollFrame = requestAnimationFrame(() => {
        scrollFrame = null;
        $('msgs').scrollTop = $('msgs').scrollHeight;
    });
}
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
    const target = sessionId, token = viewVersion;
    const data = await api(`/workflow/${encodeURIComponent(username)}/${encodeURIComponent(sessionId)}`);
    if (sessionId !== target || token !== viewVersion) return;
    pendingApproval = !runIsActive() && data.status === 'approval_required';
    $('approvalCard').hidden = !pendingApproval;
    $('approvalText').textContent = data.approval?.message || '';
    const details = $('approvalDetails');
    details.hidden = !data.approval?.execution_details;
    details.open = false;
    $('approvalDetailsText').textContent = data.approval?.execution_details || '';
    status(statusLabels[activeRun?.status] || statusLabels[data.status] || '准备就绪');
}
function detachRunView() {
    ++viewVersion;
    controller?.abort(); controller = null; clearInterval(runClock); activeRun = null;
    pendingApproval = false; $('approvalCard').hidden = true;
}
async function loadHistory() {
    detachRunView();
    const target = sessionId, token = viewVersion;
    setBusy(true);
    const data = await api(`/history/${encodeURIComponent(username)}?session_id=${encodeURIComponent(target)}`);
    if (token !== viewVersion || target !== sessionId) return;
    $('msgs').replaceChildren();
    let needsSummary = false;
    const messages = data.run ? (data.messages || []).slice(0, data.run.history_start) : data.messages || [];
    for (const message of messages) {
        if (message.role === 'user') { appendMessage('user', message.content || ''); needsSummary = true; }
        else if (message.role === 'assistant' && message.content) {
            if (needsSummary) { createWorkSummary('执行记录').steps.textContent = '此历史对话未记录页面耗时，详细过程可在运行日志中查看。'; needsSummary = false; }
            appendMessage('agent', message.content);
        }
    }
    if (data.run) {
        const key = `modeling.pending.${username}.${target}`;
        try {
            if (JSON.parse(sessionStorage.getItem(key))?.request_id === data.run.request_id) {
                sessionStorage.removeItem(key); setComposerText(''); clearAttachment(); $('notice').hidden = true;
            }
        } catch (_) { /* storage is optional */ }
        attachRun(data.run, token);
    }
    else {
        if (!$('msgs').children.length) $('msgs').append($('welcomeTemplate').content.cloneNode(true));
        await refreshWorkflow();
    }
}
function clearAttachment() { attachment = null; $('attachmentArea').hidden = true; }
async function selectSession(id) {
    if (loading) return;
    setBusy(true); sessionId = id; rememberSession(); clearAttachment(); setComposerText(''); setView('chat'); renderSessions();
    try { await loadHistory(); } catch (error) { notice(error.message); } finally { setBusy(false); }
}
async function newSession() {
    const data = await api(`/sessions/${encodeURIComponent(username)}/new`, {method:'POST'});
    sessionId = data.session_id;
    rememberSession();
}
function attachRun(run, token) {
    activeRun = run;
    appendMessage('user', run.message);
    const work = createWorkSummary('后台执行中…');
    const updateClock = () => {
        const end = activeRun?.finished_at || Date.now() / 1000;
        const seconds = Math.max(0, Math.floor(end - run.started_at));
        work.summary.textContent = `${activeRunStates.has(activeRun?.status) ? '后台执行中' : statusLabels[activeRun?.status] || '执行记录'} · ${seconds} 秒`;
    };
    updateClock();
    if (runIsActive()) runClock = setInterval(updateClock, 1000);
    status(statusLabels[run.status] || '正在恢复执行记录…');
    setBusy(false);
    observeRun(run, token, work, updateClock).catch(error => {
        if (token === viewVersion) { notice(error.message); setBusy(loading); }
    });
}
async function observeRun(run, token, work, updateClock) {
    let cursor = 0, streamBubble = null, streamText = '';
    const current = () => token === viewVersion && sessionId === run.session_id;
    let renderTimer = null, dirty = false;
    function flushStream() {
        clearTimeout(renderTimer); renderTimer = null;
        if (!dirty || !streamBubble || !current()) return;
        const panel = $('msgs');
        const follow = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 100;
        renderMarkdown(streamBubble, streamText);
        dirty = false;
        if (follow) scrollBottom();
    }
    function queueStreamRender() {
        dirty = true;
        // Coalesce token bursts and journal replay into one Markdown/layout pass.
        if (renderTimer === null) renderTimer = setTimeout(flushStream, 80);
    }
    function event(frame) {
        if (!current()) return;
        const lines = frame.split('\n');
        const raw = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (!raw) return;
        const data = JSON.parse(raw);
        if (data.type === 'step') {
            status(`后台执行：${data.func || '工具调用'} · 可切换页面`);
            if (work.steps.textContent === '正在处理请求…') work.steps.textContent = '';
            const step = document.createElement('div'); step.textContent = data.func || '工具调用'; work.steps.append(step);
        } else if (data.type === 'stream_start') {
            flushStream();
            streamBubble = appendMessage('agent', ''); streamText = '';
        } else if (data.type === 'stream_chunk') {
            if (!streamBubble) { streamBubble = appendMessage('agent', ''); streamText = ''; }
            streamText += data.content || ''; queueStreamRender();
        } else if (data.type === 'stream_end') { flushStream(); streamBubble = null; }
        else if (data.type === 'done' && data.reply) appendMessage('agent', data.reply);
        else if (data.type === 'error') appendMessage('agent', data.message || '执行失败');
        else if (data.type === 'run_state') {
            if (!activeRunStates.has(data.status)) flushStream();
            activeRun.status = data.status;
            if (!activeRunStates.has(data.status)) activeRun.finished_at ||= Date.now() / 1000;
            if (data.message) appendMessage('agent', data.message);
            status(statusLabels[data.status] || '正在执行…'); updateClock(); setBusy(loading);
        }
        const id = lines.find(line => line.startsWith('id:'));
        if (id) cursor = Number(id.slice(3).trim());
    }
    while (current()) {
        controller = new AbortController();
        try {
            const response = await fetch(`/api/chat/runs/${encodeURIComponent(run.id)}/events?username=${encodeURIComponent(username)}&after=${cursor}`, {signal:controller.signal});
            if (!response.ok || !response.body) throw new Error(`进度连接失败（${response.status}）`);
            const reader = response.body.getReader(), decoder = new TextDecoder();
            let buffer = '';
            while (current()) {
                const {done, value} = await reader.read();
                buffer += done ? decoder.decode() : decoder.decode(value, {stream:true});
                let boundary;
                while ((boundary = buffer.indexOf('\n\n')) >= 0) {
                    event(buffer.slice(0, boundary)); buffer = buffer.slice(boundary + 2);
                }
                if (done) break;
            }
            flushStream();
            if (!current()) return;
            const latest = await api(`/api/chat/runs/${encodeURIComponent(run.id)}?username=${encodeURIComponent(username)}`);
            if (!current()) return;
            activeRun = latest;
            if (!runIsActive()) {
                clearInterval(runClock); updateClock();
                if (work.steps.textContent === '正在处理请求…') work.steps.textContent = '本次执行未记录工具调用。';
                controller = null; setBusy(loading);
                await refreshSessions();
                if (current()) await refreshWorkflow();
                return;
            }
        } catch (error) {
            flushStream();
            if (!current() || error.name === 'AbortError') return;
            status('进度连接中断，后台任务继续执行，正在重新连接…');
        }
        await new Promise(resolve => setTimeout(resolve, 1200));
    }
}
function requestIdentity(message, target) {
    const key = `modeling.pending.${username}.${target}`;
    let previous;
    try { previous = JSON.parse(sessionStorage.getItem(key)); } catch (_) { /* no saved request */ }
    const request_id = previous?.message === message ? previous.request_id : (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
    try { sessionStorage.setItem(key, JSON.stringify({message, request_id})); } catch (_) { /* active run still prevents duplicate execution */ }
    return {key, request_id};
}
async function send(explicitText) {
    if (busy || !initialized) return;
    const text = explicitText ?? $('inputBox').value.trim();
    if (!text && !attachment) return;
    if (explicitText && attachment) { notice('请先移除附件，再确认或取消当前操作。'); return; }
    const file = attachment;
    const message = file ? `${text}\n\n(已附带上传文件 [${file.filename}]，本地路径: [${file.file_path}])` : text;
    detachRunView(); setBusy(true); setView('chat'); $('notice').hidden = true;
    let target, token;
    try {
        if (!sessionId) await newSession();
        target = sessionId; token = viewVersion;
        const identity = requestIdentity(message, target);
        const run = await api('/api/chat/runs', {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({username, session_id:target, message, request_id:identity.request_id})});
        try { sessionStorage.removeItem(identity.key); } catch (_) { /* optional storage */ }
        if (sessionId === target && token === viewVersion) {
            setComposerText(''); clearAttachment(); await loadHistory();
        }
        await refreshSessions();
    } catch (error) {
        // A lost POST response must not automatically submit the same action again.
        notice(`${error.message}。可重新进入此对话恢复后台进度，勿重复提交未核实的操作。`);
        if (target === sessionId && token === viewVersion) {
            try { await loadHistory(); } catch (_) { /* keep the original submission error */ }
        }
    } finally { setBusy(false); }
}
$('composerForm').onsubmit = event => { event.preventDefault(); send(); };
$('inputBox').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); } };
$('stopBtn').onclick = async () => {
    if (!activeRun || !runIsActive()) return;
    const runId = activeRun.id, token = viewVersion;
    try {
        const run = await api(`/api/chat/runs/${encodeURIComponent(runId)}/cancel?username=${encodeURIComponent(username)}`, {method:'POST'});
        if (token === viewVersion) { activeRun = run; setBusy(false); status(statusLabels[run.status]); }
    } catch (error) { notice(error.message); }
};
$('approveBtn').onclick = () => send('确认执行'); $('rejectBtn').onclick = () => send('取消执行');
function renderConfirmationMode() {
    const enabled = sessions.find(item => item.id === sessionId)?.auto_confirm === true;
    const label = enabled ? '自动确认' : '标准确认';
    $('confirmationModeLabel').textContent = label;
    $('confirmationModeBtn').classList.toggle('is-auto', enabled);
    $('confirmationModeBtn').disabled = loading || !initialized;
    $('confirmationModeBtn').setAttribute('aria-label', `操作确认方式：${label}`);
    for (const option of $('confirmationMenu').querySelectorAll('[data-confirmation]')) {
        option.setAttribute('aria-checked', String((option.dataset.confirmation === 'auto') === enabled));
    }
}
function positionConfirmationMenu() {
    const menu = $('confirmationMenu');
    if (!menu.matches(':popover-open')) return;
    const anchor = $('confirmationModeBtn').getBoundingClientRect();
    menu.style.left = `${Math.max(12, Math.min(anchor.left, window.innerWidth - menu.offsetWidth - 12))}px`;
    menu.style.top = `${Math.max(12, anchor.top - menu.offsetHeight - 8)}px`;
}
$('confirmationMenu').addEventListener('toggle', event => {
    const open = event.newState === 'open';
    $('confirmationModeBtn').setAttribute('aria-expanded', String(open));
    if (open) {
        positionConfirmationMenu();
        $('confirmationMenu').querySelector('[aria-checked="true"]').focus();
    }
});
window.addEventListener('resize', positionConfirmationMenu);
$('confirmationMenu').addEventListener('keydown', event => {
    const options = [...$('confirmationMenu').querySelectorAll('[data-confirmation]')];
    const index = options.indexOf(document.activeElement);
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        options[event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 :
            (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length].focus();
    }
    if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation();
        $('confirmationMenu').hidePopover(); $('confirmationModeBtn').focus();
    }
});
$('confirmationMenu').onclick = async event => {
    const option = event.target.closest('[data-confirmation]');
    if (!option) return;
    const enabled = option.dataset.confirmation === 'auto', target = sessionId;
    if (!target || loading) return;
    $('confirmationMenu').hidePopover(); $('confirmationModeBtn').focus();
    if (enabled === (sessions.find(item => item.id === target)?.auto_confirm === true)) return;
    const resume = enabled && pendingApproval && !busy;
    setBusy(true);
    try {
        const result = await api(`/sessions/${encodeURIComponent(username)}/${encodeURIComponent(target)}/confirmation`, {
            method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({auto_confirm:enabled})
        });
        const session = sessions.find(item => item.id === target);
        if (session) session.auto_confirm = result.auto_confirm;
        $('notice').hidden = true;
    } catch (error) {
        notice(error.status === 404 && error.message === 'Not Found'
            ? '确认方式未保存：后台尚未加载此功能，请重启服务后刷新页面。'
            : error.message);
        return;
    }
    finally { setBusy(false); }
    if (resume && sessionId === target) await send('确认执行');
};
$('newChatTopBtn').onclick = async () => { if (loading) return; detachRunView(); setBusy(true); try { await newSession(); clearAttachment(); setComposerText(''); await refreshSessions(); await loadHistory(); setView('chat'); } catch(error) { notice(error.message); } finally { setBusy(false); } };
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
    if (loading) { rememberSession(); return; }
    setBusy(true);
    try {
        sessionId = new URLSearchParams(location.search).get('session_id');
        await refreshSessions(); clearAttachment(); setComposerText('');
        setView(location.pathname === '/records' ? 'records' : 'chat', false);
        await loadHistory();
    } catch (error) { notice(error.message); } finally { setBusy(false); }
};
$('guideToggle').onclick = () => { $('modelingGuide').hidden = !$('modelingGuide').hidden; $('guideToggle').setAttribute('aria-expanded',String(!$('modelingGuide').hidden)); };
$('menuToggle').onclick = () => { const open = document.body.classList.toggle('sidebar-open'); $('menuToggle').setAttribute('aria-expanded',String(open)); };
$('sidebarBackdrop').onclick = closeNavigation;
document.addEventListener('keydown',event => { if (event.key === 'Escape') closeNavigation(); });
$('msgs').onclick = event => {
    if (event.target.closest('[data-capabilities]') && !busy && initialized) {
        send('你具备哪些能力？请根据实际能力目录简要介绍，并说明尚不支持的内容。');
        return;
    }
    const card = event.target.closest('[data-prompt]');
    if (card && !busy) { setComposerText(card.dataset.prompt); $('inputBox').focus(); }
};
async function initialize() {
    setBusy(true); setView(view,false);
    try { const config = await api('/config'); username = config.username; $('username').textContent = username; $('connectionText').textContent = config.agent_engine === 'langgraph' ? 'LangGraph 已连接' : '传统执行引擎'; $('connection').classList.add('connected'); await refreshSessions(); if (!sessionId) { await newSession(); await refreshSessions(); } await loadHistory(); initialized = true; }
    catch(error) { $('connectionText').textContent = '连接失败'; $('connection').classList.add('error'); notice(`加载失败，请刷新重试：${error.message}`); }
    finally { setBusy(false); }
}
initialize().then(() => { if (initialDraft && !runIsActive()) { setComposerText(initialDraft); $('inputBox').focus(); } });

// Session badges keep updating even when another chat/records page is visible.
let refreshingSessions = false;
setInterval(async () => {
    if (!initialized || loading || refreshingSessions || document.hidden) return;
    refreshingSessions = true;
    try {
        await refreshSessions();
        const latest = sessions.find(item => item.id === sessionId)?.run;
        if (latest && latest.id !== activeRun?.id && !loading) await loadHistory();
    } catch (_) { /* reconnecting the progress stream handles transient outages */ }
    finally { refreshingSessions = false; }
}, 5000);
