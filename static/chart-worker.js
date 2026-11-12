/* All ECharts parsing, layout and SVG generation stays off the chat UI thread. */
'use strict';
importScripts('/static/chart-spec.js?v=2', '/static/vendor/echarts-6.0.0.common.min.js');
self.onmessage = async event => {
    const {id,payload,width,count} = event.data || {};
    let chart, raster, canvas;
    try {
        const started = performance.now();
        const option = ChartSpec.option(payload,width,count);
        chart = echarts.init(null,null,{renderer:'svg',ssr:true,width,height:ChartSpec.height(payload)});
        chart.setOption(option);
        const escape=text=>String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        const description=escape([payload.subtitle,payload.reference,payload.note,'快照：'+payload.source_digest].join('\n'));
        const svg=chart.renderToSVGString().replace(/(<svg\b[^>]*>)/,(_,root)=>root+'<desc>'+description+'</desc>');
        // Browsers can parse/paint SVG images on the UI thread despite async
        // decode. Rasterize here as well; the UI receives a PNG, never SVG DOM.
        if (typeof OffscreenCanvas!=='function') {
            self.postMessage({id,svg,error:'当前浏览器不支持后台图像绘制，可查看原始数据或导出SVG。'});
            return;
        }
        echarts.setPlatformAPI({createCanvas:()=>new OffscreenCanvas(1,1)});
        canvas=new OffscreenCanvas(width,ChartSpec.height(payload));
        raster=echarts.init(canvas,null,{renderer:'canvas',ssr:true,width,height:ChartSpec.height(payload),devicePixelRatio:1.5});
        raster.setOption(option); raster.getZr().refreshImmediately();
        const blob=await canvas.convertToBlob({type:'image/png'});
        self.postMessage({id,svg,blob,render_ms:performance.now()-started});
    } catch (error) {
        self.postMessage({id,error:'图表绘制暂时失败，文字结果和原始数据仍可查看。'});
    } finally {
        chart?.dispose();
        raster?.dispose();
        if (canvas) { canvas.width=1; canvas.height=1; }
    }
};
