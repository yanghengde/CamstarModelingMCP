const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/workspace.js'), 'utf8');
function section(start, end) { return source.slice(source.indexOf(start), source.indexOf(end)); }
class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this._text = ''; this.className = ''; this.open = false; }
    append(...items) { for (const item of items) { item.parentElement = this; this.children.push(item); } }
    replaceChildren() { this.children = []; this._text = ''; }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
    set textContent(value) { this.children = []; this._text = value; }
    querySelector() { return null; }
    remove() { this.parentElement.children = this.parentElement.children.filter(c => c !== this); }
}
function setup() {
    const elements = {msgs: new Element('main'), notice: new Element('aside')};
    const context = vm.createContext({
        document: {createElement: tag => new Element(tag)}, $: id => elements[id],
        renderMarkdown: (element, text) => { element.textContent = text; }, scrollBottom() {},
        status() {}, setBusy() {}, refreshSessions: async () => {}, refreshWorkflow: async () => {},
        detachRunView() {}, sessionId: 's', username: 'u', viewVersion: 0, loading: false,
        activeRun: {id: 'r', session_id: 's', status: 'running'}, activeRunStates: new Set(['queued','running','cancelling']),
        statusLabels: {completed: '任务已完成'}, runClock: null,
        AbortController, TextDecoder, setTimeout, clearTimeout, clearInterval,
        runIsActive() { return context.activeRunStates.has(context.activeRun.status); }
    });
    vm.runInContext(section('function appendMessage(', 'let scrollFrame'), context);
    vm.runInContext(section('function createWorkSummary(', 'async function refreshWorkflow'), context);
    vm.runInContext(section('async function loadHistory(', 'function clearAttachment'), context);
    vm.runInContext(section('async function observeRun(', 'function requestIdentity'), context);
    return {context, elements};
}
test('history puts tool-call narration in closed details and keeps the answer visible', async () => {
    const {context, elements} = setup();
    context.api = async () => ({messages: [
        {role: 'user', content: '查询IQC_DEMO_AQL'},
        {role: 'assistant', content: '按持久ID再尝试。', tool_calls: [{id: 'c'}]},
        {role: 'tool', content: 'private tool response'},
        {role: 'assistant', content: '已找到该表，明细暂时无法读取。'}
    ]});
    await context.loadHistory();
    const details = elements.msgs.children.find(c => c.tagName === 'details');
    assert.equal(details.open, false);
    assert.match(details.textContent, /按持久ID再尝试/);
    const answers = elements.msgs.children.filter(c => c.className === 'msg-wrapper agent-wrapper');
    assert.equal(answers.length, 1);
    assert.match(answers[0].textContent, /已找到该表/);
    assert.doesNotMatch(answers[0].textContent, /持久ID/);
});
test('error detail is collapsed and treated as plain text', () => {
    const {context, elements} = setup();
    context.appendTaskError('明细暂时无法读取。', '<img src=x onerror=alert(1)> HTTP 500');
    const bubble = elements.msgs.children[0].children[0];
    const details = bubble.children[0];
    assert.equal(details.open, false);
    assert.equal(details.children[1].tagName, 'pre');
    assert.match(details.children[1].textContent, /<img/);
});
for (const legacy of [false, true]) test(`journal replay preserves details and hides intermediate narration (legacy=${legacy})`, async () => {
    const {context, elements} = setup();
    const frames = [{type:'stream_start'}, {type:'stream_chunk',content:'换参数再查一次。'},
        ...(!legacy ? [{type:'stream_end',audience:'activity'}] : []),
        {type:'step',func:'get_quality_reference_details'},
        {type:'tool_diagnostic',func:'get_quality_reference_details',details:'明细ID重复、字段缺失或父级不一致。'},
        {type:'stream_start'}, {type:'stream_chunk',content:'已找到该表，明细暂时无法读取。'},
        {type:'stream_end',audience:'answer'}, {type:'run_state',status:'completed'}];
    let sent = false;
    context.fetch = async () => ({ok:true,body:{getReader: () => ({read: async () => {
        if (sent) return {done:true};
        sent=true; return {done:false,value:new TextEncoder().encode(frames.map((f,i) => `id: ${i+1}\ndata: ${JSON.stringify(f)}\n\n`).join(''))};
    }})}});
    context.api = async () => ({id:'r',session_id:'s',status:'completed'});
    const work = context.createWorkSummary('正在处理');
    await context.observeRun(context.activeRun, 0, work, () => {});
    const answers = elements.msgs.children.filter(c => c.className === 'msg-wrapper agent-wrapper');
    assert.equal(answers.length, 1);
    assert.match(answers[0].textContent, /已找到该表/);
    assert.doesNotMatch(answers[0].textContent, /换参数|明细ID/);
    assert.match(work.steps.textContent, /换参数/);
    assert.match(work.steps.textContent, /父级不一致/);
    assert.equal(work.details.open, false);
});
