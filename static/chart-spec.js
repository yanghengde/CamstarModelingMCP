/* Fixed chart templates shared by the Worker and the small browser controller. */
(function(root) {
    'use strict';
    function label(value, limit = 1600) {
        if (typeof value !== 'string' || !value.trim() || value.length > limit || value.includes('\0')) throw Error('图表文字无效');
        return value;
    }
    function numeric(value) {
        if (typeof value !== 'string' || !value.trim() || value.length > 128 || !Number.isFinite(Number(value)) || Math.abs(Number(value)) > 1e30) throw Error('图表数值无效');
        return Number(value);
    }
    function validate(payload) {
        if (!payload || payload.version !== 1 || !Array.isArray(payload.panels) || !payload.panels.length || payload.panels.length > 2) throw Error('图表格式暂不支持');
        for (const key of ['title','subtitle','unit','reference','note']) label(payload[key]);
        if (!/^[a-f0-9]{64}$/.test(payload.source_digest) || typeof payload.complete !== 'boolean') throw Error('图表来源无效');
        for (const panel of payload.panels) {
            label(panel.title);
            if (!['line','bar','trend'].includes(panel.kind) || !Array.isArray(panel.rows) || !panel.rows.length || panel.rows.length > 500) throw Error('图表点数超界');
            const ids = new Set();
            for (const row of panel.rows) {
                label(row.id,128); label(row.label,768); label(row.detail);
                if (ids.has(row.id) || typeof row.signal !== 'boolean') throw Error('图表点重复或信号无效');
                ids.add(row.id); numeric(row.value);
                if (panel.kind === 'line') for (const key of ['center','lcl','ucl']) numeric(row[key]);
                if (panel.kind === 'trend' && (row.signal || typeof row.excluded!=='boolean' || ['center','lcl','ucl'].some(key=>key in row))) throw Error('原始趋势不可附带判异或控制限');
            }
        }
        return payload;
    }
    const height = payload => 150 + payload.panels.length * 260;
    const visibleRows = (panel, count) => panel.rows.slice(-count);
    function option(payload, width, count = 500) {
        validate(payload);
        if (!Number.isFinite(width) || width < 240 || width > 2400 || ![50,100,500].includes(count)) throw Error('图表尺寸无效');
        const titles = [{text:payload.title, subtext:payload.subtitle, top:10, left:12,
            textStyle:{fontSize:14,fontWeight:500,color:'#263445'}, subtextStyle:{fontSize:11,color:'#667080',width:width-30,overflow:'truncate'}}];
        const grids = [], xs = [], ys = [], series = [];
        payload.panels.forEach((panel,index) => {
            const rows = visibleRows(panel,count), top = 110+index*260;
            titles.push({text:panel.title, left:66, top:top-48, textStyle:{fontSize:12,fontWeight:500}});
            grids.push({left:66,right:26,top,height:180});
            xs.push({type:'category',gridIndex:index,data:rows.map(row=>panel.kind==='trend'?row.label.replace('T','\n').replace(/(?:Z|\+00:00)$/,''):row.label),boundaryGap:panel.kind==='bar',
                axisLabel:{fontSize:10,lineHeight:13,hideOverlap:true,width:100,overflow:'truncate'},axisLine:{lineStyle:{color:'#b7c0cc'}}});
            ys.push({type:'value',gridIndex:index,scale:true,name:payload.unit,nameTextStyle:{fontSize:10},
                axisLabel:{fontSize:10},splitLine:{lineStyle:{color:'#edf0f4'}}});
            series.push({name:panel.title,type:panel.kind==='bar'?'bar':'line',xAxisIndex:index,yAxisIndex:index,
                data:rows.map(row=>numeric(row.value)),showSymbol:panel.kind==='trend'||rows.length===1,symbolSize:4,silent:true,
                itemStyle:{color:'#2675b9'},lineStyle:{width:1.5,color:'#2675b9'},animation:false});
            if (panel.kind==='line') {
                for (const [key,name,color] of [['center','CL','#6b7e89'],['ucl','UCL','#b25d38'],['lcl','LCL','#b25d38']]) {
                    series.push({name,type:'line',xAxisIndex:index,yAxisIndex:index,data:rows.map(row=>numeric(row[key])),
                        showSymbol:false,silent:true,lineStyle:{type:'dashed',width:1,color},itemStyle:{color},animation:false,
                        ...(rows.length===1?{markLine:{silent:true,symbol:'none',animation:false,
                            label:{show:true,formatter:name,position:'insideEndTop',fontSize:10},
                            lineStyle:{type:'dashed',width:1,color},data:[{yAxis:numeric(rows[0][key])}]}}:{})});
                }
                const signals = rows.map((row,i)=>row.signal?[i,numeric(row.value)]:null).filter(Boolean);
                series.push({name:'后端判异点',type:'scatter',xAxisIndex:index,yAxisIndex:index,data:signals,
                    symbolSize:7,z:5,silent:true,itemStyle:{color:'#c83e43'},animation:false});
            }
            if (panel.kind==='trend') {
                series.push({name:'存在未冲销的排除记录',type:'scatter',xAxisIndex:index,yAxisIndex:index,
                    data:rows.flatMap((row,i)=>row.excluded?[[i,numeric(row.value)]]:[]),symbol:'diamond',
                    symbolSize:8,z:5,silent:true,itemStyle:{color:'#7d8791',opacity:1},animation:false});
            }
        });
        const isBar=payload.panels.every(p=>p.kind==='bar');
        const isTrend=payload.panels.every(p=>p.kind==='trend');
        return {animation:false,textStyle:{fontFamily:'Microsoft YaHei,Arial,sans-serif'},
            title:titles,grid:grids,xAxis:xs,yAxis:ys,series,
            graphic:[{type:'text',left:12,bottom:38,style:{text:isBar?'蓝：各工序关联容器数（不等于产量）':isTrend?'蓝：原始值；灰菱形：未冲销排除记录\n仅展示，不计算控制限或判异':'蓝：原值/统计量   虚线：CL / UCL / LCL   红：后端判异点',fill:'#667080',fontSize:10}},
                     {type:'text',left:12,bottom:18,style:{text:'快照 '+payload.source_digest.slice(0,12)+' · 显示最多'+count+'点 · '+(payload.complete?'声明范围完整':'部分范围'),fill:'#667080',fontSize:10}}]};
    }
    const api = {validate,numeric,height,visibleRows,option};
    root.ChartSpec = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
