const test=require('node:test'), assert=require('node:assert/strict');
const spec=require('../static/chart-spec.js');
const echarts=require('../static/vendor/echarts-6.0.0.common.min.js');
function fixture(n=500) {
    return {version:1,title:'合成测试 · I-MR',subtitle:'明确测试数据',unit:'mm',reference:'test-profile / A',note:'本地分析，不代表原生引擎判定',complete:true,source_digest:'a'.repeat(64),
        panels:[{title:'I',kind:'line',rows:Array.from({length:n},(_,i)=>({id:'P'+i,label:String(i),value:'3.0000000000000000001',center:'0',lcl:'-3',ucl:'3',signal:true,detail:'original precision'}))}]};
}
test('bounded fixed options preserve signals without recomputing JS rounded values',()=>{
    const payload=fixture(); const option=spec.option(payload,600,50);
    assert.equal(option.animation,false); assert.equal(option.series[0].data.length,50);
    assert.equal(option.series[4].data.length,50);
    assert.equal(payload.panels[0].rows[0].value,'3.0000000000000000001');
    assert.equal(option.tooltip,undefined);
});
test('reject oversized, duplicate and nonfinite data instead of truncating analysis',()=>{
    for (const mutate of [p=>p.panels[0].rows.push(p.panels[0].rows[0]),p=>p.panels[0].rows[1].id='P0',p=>p.panels[0].rows[0].value='NaN',p=>p.panels[0].rows[0].value='',p=>p.version=2]) {
        const payload=fixture(); mutate(payload); assert.throws(()=>spec.validate(payload));
    }
});
test('actual pinned ECharts renders server SVG, escapes text, and disposes',()=>{
    const payload=fixture(); payload.title='<script>alert(1)</script>';
    const chart=echarts.init(null,null,{renderer:'svg',ssr:true,width:600,height:spec.height(payload)});
    try {
        chart.setOption(spec.option(payload,600)); const svg=chart.renderToSVGString();
        assert.match(svg,/<svg/); assert.doesNotMatch(svg,/<script>/); assert.match(svg,/&lt;script&gt;/);
    } finally { chart.dispose(); }
    assert.equal(chart.isDisposed(),true);
});
test('categorical order counts do not present a false control-limit legend',()=>{
    const payload=fixture(3); payload.panels[0].kind='bar';
    const option=spec.option(payload,600);
    assert.equal(option.series.length,1);
    assert.match(option.graphic[0].style.text,/不等于产量/);
    assert.doesNotMatch(option.graphic[0].style.text,/CL|UCL|LCL/);
});
function trendFixture(n=100){
    const payload=fixture(n);payload.panels[0].kind='trend';
    for(const row of payload.panels[0].rows){delete row.center;delete row.lcl;delete row.ucl;row.signal=false;row.excluded=true;row.label='2026-11-12T08:00:00.0000001';}
    return payload;
}
test('raw trend has original values and exclusion markers, without control or signal series',()=>{
    const p=trendFixture(),option=spec.option(p,600);
    assert.equal(option.series.length,2);
    assert.equal(option.series[0].type,'line');assert.equal(option.series[1].symbol,'diamond');
    assert.equal(option.series[1].data.length,100);assert.match(option.graphic[0].style.text,/不计算控制限或判异/);
    assert.equal(option.xAxis[0].data[0],'2026-11-12\n08:00:00.0000001');
    assert.equal(p.panels[0].rows[0].label,'2026-11-12T08:00:00.0000001');
    for(const change of [row=>row.ucl='10',row=>row.signal=true,row=>delete row.excluded]){
        const bad=trendFixture();change(bad.panels[0].rows[0]);assert.throws(()=>spec.validate(bad));
    }
});
test('single raw observation renders a visible blue symbol in actual SVG',()=>{
    const p=trendFixture(1);p.panels[0].rows[0].excluded=false;
    const chart=echarts.init(null,null,{renderer:'svg',ssr:true,width:600,height:spec.height(p)});
    try{chart.setOption(spec.option(p,600));assert.match(chart.renderToSVGString(),/<path[^>]*stroke="#2675b9"[^>]*ecmeta_data_index="0"/);}finally{chart.dispose();}
});
test('raw exclusion marker is rendered above the original line and symbols',()=>{
    const p=trendFixture(2),option=spec.option(p,600);
    assert.ok(option.series[1].z>3); // ECharts line series default z=3.
    const chart=echarts.init(null,null,{renderer:'svg',ssr:true,width:600,height:spec.height(p)});
    try{chart.setOption(option);assert.match(chart.renderToSVGString(),/fill="#7d8791"[^>]*ecmeta_series_index="1"/);}finally{chart.dispose();}
});
