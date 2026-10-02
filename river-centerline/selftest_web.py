# -*- coding: utf-8 -*-
"""生成带自检脚本的网页副本 -> 无头 Edge 跑 -> 从 title 读回渲染统计与错误。"""
import json
import os
import re
import subprocess
import sys

SRC = "river-centerline-viz.html"
DST = "_selftest.html"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

TEST = r"""
<script>
(function(){
  const out = {errors: [], checks:{}};
  window.onerror = function(m){ out.errors.push(String(m)); };
  function stats(){
    const d = cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data;
    let teal=0, orange=0, amber=0, pink=0, nonbg=0, n=0;
    const near=(r,g,b,c,tol)=>{const dr=r-c[0],dg=g-c[1],db=b-c[2];return (Math.abs(dr)+Math.abs(dg)+Math.abs(db))<=tol;};
    for(let i=0;i<d.length;i+=4){
      n++;
      const r=d[i],g=d[i+1],b=d[i+2];
      if(near(r,g,b,[45,212,191],70)) teal++;
      if(near(r,g,b,[251,146,60],60)) orange++;
      if(near(r,g,b,[251,191,36],60)) amber++;
      if(near(r,g,b,[244,114,182],80)) pink++;
      if(Math.abs(r-6)+Math.abs(g-12)+Math.abs(b-22) > 40) nonbg++;
    }
    return {w:cv.width,h:cv.height,teal:teal,orange:orange,amber:amber,pink:pink,
            nonbg:nonbg, ratio:+(nonbg/n).toFixed(3)};
  }
  try{
    for(let st=0; st<=7; st++){
      setStage(st);
      out.checks['stage'+st]=stats();
    }
    // 方法A 全半径
    S.method='A'; syncMethod();
    const aStats=[];
    for(let r=1;r<=8;r++){ S.r=r; document.getElementById('rSlider').value=r; renderKPI(); renderCharts(); draw();
      aStats.push({r:r, contam:DATA.A[r-1].metrics.contam, comps:DATA.A[r-1].metrics.comps,
                   mean:DATA.A[r-1].metrics.mean, cov2:DATA.A[r-1].metrics.cov2, px:stats().teal}); }
    out.checks.A=aStats;
    S.method='B'; syncMethod();
    // 图层全开
    for(const k in S.layers) S.layers[k]=true;
    S.stage=7; draw();
    out.checks.allLayers=stats();
    // 图表是否画上了东西
    const wc=document.getElementById('wcv'), ec=document.getElementById('ecv');
    const wd=wc.getContext('2d').getImageData(0,0,wc.width,wc.height).data;
    let wpx=0; for(let i=3;i<wd.length;i+=4) if(wd[i]>10) wpx++;
    const ed=ec.getContext('2d').getImageData(0,0,ec.width,ec.height).data;
    let epx=0; for(let i=3;i<ed.length;i+=4) if(ed[i]>10) epx++;
    out.checks.charts={w:wpx, e:epx};
    const t=document.querySelectorAll('#fragTbl tr').length, l=document.querySelectorAll('#linkList details').length;
    out.checks.panel={fragRows:t, linkRows:l};
    out.checks.kpi=document.getElementById('kpi').textContent.replace(/\s+/g,' ').slice(0,220);
    out.checks.params=document.getElementById('paramTbl').textContent.replace(/\s+/g,' ').slice(0,120);
  }catch(e){ out.errors.push('TEST:'+(e && e.message)); }
  document.title = 'SELFTEST::' + JSON.stringify(out);
})();
</script>
"""

html = open(SRC, encoding="utf-8").read()
html = html.replace("</body>", TEST + "</body>")
open(DST, "w", encoding="utf-8").write(html)

dom_file = "_selftest_dom.html"
url = "file:///" + os.path.abspath(DST).replace("\\", "/")
cmd = ('"%s" --headless=new --disable-gpu --no-sandbox --hide-scrollbars '
       '--user-data-dir="%%TEMP%%\\edgeselftest" --virtual-time-budget=9000 --dump-dom "%s" > "%s" 2>nul'
       % (EDGE, url, os.path.abspath(dom_file)))
print("执行:", cmd[:120], "...")
subprocess.run(cmd, shell=True)

dom = open(dom_file, encoding="utf-8", errors="replace").read()
m = re.search(r"<title>(.*?)</title>", dom, re.S)
if not m:
    print("未取到自检结果（标题里没有 SELFTEST::）")
    sys.exit(1)
title = m.group(1)
print(title if not title.startswith("SELFTEST::") else "自检结果标题已获取")
if title.startswith("SELFTEST::"):
    import json
    r = json.loads(title[len("SELFTEST::"):])
    print("JS 运行时错误:", r["errors"] or "无")
    for k, v in r["checks"].items():
        if k == "A":
            print("方法A 各半径:", [(a["r"], a["contam"], a["comps"], round(a["mean"], 2)) for a in v])
        elif k in ("kpi", "params"):
            print(k, "=", v)
        else:
            print(k, "=", v)
    ok = (not r["errors"]) and r["checks"]["stage6"]["orange"] > 0 and r["checks"]["charts"]["w"] > 0
    print("\n自检结论:", "通过" if ok else "存在问题")
    for tmp in (DST, dom_file):
        try:
            os.remove(tmp)
        except OSError:
            pass
    sys.exit(0 if ok else 2)
else:
    print("页面正常加载，标题:", title)
