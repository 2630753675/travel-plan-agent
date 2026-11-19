import pathlib

root = pathlib.Path(r"E:\项目\agent_study\dsh_work2\docs\ui-proposals")

# ---- M: 行程小卡的金额列错位 ----
p = root / "13-widget-desktop.html"
t = p.read_text(encoding="utf-8")
t = t.replace(".day-mini{display:grid;grid-template-columns:48px 1fr;gap:10px;align-items:baseline;padding:6px 0;border-bottom:1px dashed #202833}",
              ".day-mini{display:grid;grid-template-columns:48px 1fr auto;gap:10px;align-items:baseline;padding:6px 0;border-bottom:1px dashed #202833}")
t = t.replace(".day-mini .c{font-family:var(--mono);font-size:11px;color:var(--dim);margin-left:auto}",
              ".day-mini .c{font-family:var(--mono);font-size:11px;color:var(--dim);text-align:right}")
# 时刻表小卡补一点点时间标记，避免看成纯色块
t = t.replace('<div class="mini-col">\n          <div class="b f" style="top:4%;height:34%"></div>',
              '<div class="mini-col">\n          <span style="position:absolute;left:3px;top:2px;font-size:8px;color:#6b7c93;font-family:var(--mono)">08</span>\n          <div class="b f" style="top:4%;height:34%"></div>')
p.write_text(t, encoding="utf-8")

# ---- N: 页面可滚动，应用补满 5 天 ----
p = root / "14-dock-stage.html"
t = p.read_text(encoding="utf-8")
t = t.replace("body{margin:0;background:var(--stage-bg);color:var(--ink);font:13.5px/1.6 \"Segoe UI\",\"Microsoft YaHei\",system-ui,sans-serif;overflow:hidden;height:100vh}",
              "body{margin:0;background:var(--stage-bg);color:var(--ink);font:13.5px/1.6 \"Segoe UI\",\"Microsoft YaHei\",system-ui,sans-serif}")
extra_days = '''        <div class="day">
          <div><div class="d">11-23</div><div class="w">周一 · 老城</div><div class="c">¥1,000</div></div>
          <div>
            <h4>武侯祠与锦里，一条路走完</h4>
            <ul class="legs">
              <li><span class="t">08:30</span><div><b>武侯祠</b><small>50 元/人 · 早到避人流</small></div></li>
              <li><span class="t">10:30</span><div><b>锦里古街</b><small>免费 · 与祠相邻 · 午餐就地</small></div></li>
            </ul>
          </div>
        </div>
        <div class="day">
          <div><div class="d">11-24</div><div class="w">周二 · 返程</div><div class="c">¥2,200</div></div>
          <div>
            <h4>上午只做一件事：准时到机场</h4>
            <ul class="legs">
              <li><span class="t">09:20</span><div><b>酒店 → 天府机场</b><small>提前 150 分钟 · 中雨 +20 分钟</small></div></li>
              <li><span class="t">13:00</span><div><b>MU5402 成都 → 上海</b><small>16:05 抵达 · 2 人 ¥1,920</small></div></li>
            </ul>
          </div>
        </div>
'''
anchor = '''        </div>
      </div>
    </section>

    <aside class="ctx">'''
t = t.replace(anchor, '''        </div>
''' + extra_days + '''      </div>
    </section>

    <aside class="ctx">''')
p.write_text(t, encoding="utf-8")

# ---- O: 时刻表列加高，块内文字不再互相压 ----
p = root / "15-workspace-tabs.html"
t = p.read_text(encoding="utf-8")
t = t.replace(".tt .col{position:relative;height:300px;border-left:1px solid #1f2731}",
              ".tt .col{position:relative;height:430px;border-left:1px solid #1f2731}")
t = t.replace(".tt .axis{position:relative;height:300px;border-left:none}",
              ".tt .axis{position:relative;height:430px;border-left:none}")
p.write_text(t, encoding="utf-8")
print("patched M/N/O")
