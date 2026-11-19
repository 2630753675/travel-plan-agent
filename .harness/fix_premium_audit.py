"""Remediate 07-premium.html per the design review (a11y, affordance, honesty, language)."""

import pathlib
import re

p = pathlib.Path(r"E:\项目\agent_study\dsh_work2\docs\ui-system\07-premium.html")
t = p.read_text(encoding="utf-8")

# ---------- 1. 可见性 / 对比度 / 字号 ----------
t = t.replace("--ink:#f3f5f8; --sub:#a6afba; --dim:#6a7480;",
              "--ink:#f3f5f8; --sub:#b3bcc7; --dim:#8b95a1;")
t = t.replace(".sr{position:absolute;opacity:0;width:0;height:0;pointer-events:none}",
              ".sr{position:absolute!important;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}")
t = t.replace(".blk .t{font-family:var(--mono);font-size:8.5px;color:rgba(255,255,255,.5);display:block}",
              ".blk .t{font-family:var(--mono);font-size:10px;color:rgba(255,255,255,.72);display:block}")
t = t.replace(".blk .n{font-size:10.5px;font-weight:600;display:block;line-height:1.3;color:rgba(255,255,255,.92)}",
              ".blk .n{font-size:11.5px;font-weight:600;display:block;line-height:1.3;color:rgba(255,255,255,.96)}")
t = t.replace(".blk .s{font-size:9px;color:rgba(255,255,255,.45);display:block}",
              ".blk .s{font-size:10.5px;color:rgba(255,255,255,.66);display:block}")
t = t.replace(".tt .axis span{position:absolute;right:6px;font-family:var(--mono);font-size:9px;color:var(--dim);transform:translateY(-50%);background:#0d1015;padding:0 2px}",
              ".tt .axis span{position:absolute;right:6px;font-family:var(--mono);font-size:10px;color:var(--dim);transform:translateY(-50%);background:#0d1015;padding:0 2px}")
t = t.replace(".tt .dh .c{font-family:var(--mono);font-size:10px;color:var(--gold);margin-top:3px}",
              ".tt .dh .c{font-family:var(--mono);font-size:10.5px;color:var(--sub);margin-top:3px}")
t = t.replace(".legend{display:flex;gap:14px;flex-wrap:wrap;padding:11px 13px;border-top:1px solid var(--line);font-size:10px;color:var(--dim)}",
              ".legend{display:flex;gap:14px;flex-wrap:wrap;padding:11px 13px;border-top:1px solid var(--line);font-size:11px;color:var(--dim)}")
t = t.replace(".sat .st{font-family:var(--mono);font-size:9.5px;color:var(--dim);margin-top:2px;letter-spacing:.06em}",
              ".sat .st{font-family:var(--mono);font-size:10.5px;color:var(--dim);margin-top:2px;letter-spacing:.06em}")
t = t.replace(".core small{display:block;font-family:var(--mono);font-size:9.5px;color:var(--dim);letter-spacing:.1em;margin-top:2px}",
              ".core small{display:block;font-family:var(--mono);font-size:10.5px;color:var(--dim);letter-spacing:.1em;margin-top:2px}")
t = t.replace(".foot{margin-top:34px;display:flex;gap:18px;align-items:center;color:var(--dim);font-size:10.5px;",
              ".foot{margin-top:34px;display:flex;gap:18px;align-items:center;color:var(--dim);font-size:11px;")

# 自由时间可读性
t = t.replace(".blk.free{background:rgba(255,255,255,.025);border:1px dashed rgba(255,255,255,.16);color:rgba(255,255,255,.5);font-style:italic}",
              ".blk.free{background:rgba(255,255,255,.035);border:1px dashed rgba(255,255,255,.22);color:rgba(255,255,255,.62);font-style:italic}")

# ---------- 2. 文案与语言 ----------
t = t.replace("五天只安排了 8 处景点", "五天只安排了 7 处景点")
t = t.replace('<span>from</span>', '<span>出发</span>').replace('<span>to</span>', '<span>到达</span>')
t = t.replace('<span>dates</span>', '<span>日期</span>').replace('<span>pax</span>', '<span>人数</span>')
t = t.replace('<span>budget</span>', '<span>预算</span>')
t = t.replace("行程协作 <small>premium · mock</small>", "行程协作 <small>零脚本原型</small>")
t = t.replace("<span>PREMIUM PROTOTYPE · 零脚本交互 · MOCK DATA</span>",
              "<span>交互原型 · 演示数据 · 零脚本</span>")
t = t.replace("<span>MOCK DATA · 成都 5 天 · 7,960 / 8,000</span>",
              "<span>演示数据 · 成都 5 天 · 7,960 / 8,000</span>")

# ---------- 3. 星图：去掉假的“处理中”，进度到 100% ----------
t = t.replace('#play:checked ~ .run-wrap .sat .st:after{content:" · 处理中"}\n', "")
t = t.replace("@keyframes fill3{from{width:2%}to{width:96%}}", "@keyframes fill3{from{width:2%}to{width:100%}}")

# ---------- 4. 焦点可见 ----------
focus_css = """
  /* 键盘焦点：所有隐藏输入获得焦点时，让对应标签显形 */
  #step1:focus-visible ~ .chrome .steps label[for=step1],
  #step2:focus-visible ~ .chrome .steps label[for=step2],
  #step3:focus-visible ~ .chrome .steps label[for=step3],
  #step4:focus-visible ~ .chrome .steps label[for=step4],
  #app-tt:focus-visible ~ .tabs label[for=app-tt],
  #app-budget:focus-visible ~ .tabs label[for=app-budget],
  #app-change:focus-visible ~ .tabs label[for=app-change],
  #day-20:focus-visible ~ .plan .layout .daybtn[for=day-20],
  #day-21:focus-visible ~ .plan .layout .daybtn[for=day-21],
  #day-22:focus-visible ~ .plan .layout .daybtn[for=day-22],
  #day-23:focus-visible ~ .plan .layout .daybtn[for=day-23],
  #day-24:focus-visible ~ .plan .layout .daybtn[for=day-24],
  .sr:focus-visible + .blk,
  .chips label:has(input:focus-visible),
  .opt:has(input:focus-visible),
  .scen label:has(input:focus-visible),
  .cmdchip:focus-visible,
  .cmdreset:focus-visible,
  .cta:focus-visible,
  .ghost:focus-visible{
    outline:2px solid var(--gold); outline-offset:3px;
  }
"""
t = t.replace("  @media (max-width:1080px){", focus_css + "  @media (max-width:1080px){")

# ---------- 5. 移动端：时间表横向滚动 ----------
t = t.replace("""  @media (max-width:1080px){
    .hero{grid-template-columns:1fr;gap:28px}""",
"""  @media (max-width:1080px){
    .tt{overflow-x:auto}
    .tt .grid{min-width:760px}
    .hero{grid-template-columns:1fr;gap:28px}""")

# ---------- 6. 让所有时间块可点：统一 radio + 生成浮卡 ----------
def pop_for(inner: str) -> str:
    time = re.search(r'<span class="t">(.*?)</span>', inner)
    name = re.search(r'<span class="n">(.*?)</span>', inner)
    sub = re.search(r'<span class="s">(.*?)</span>', inner)
    title = name.group(1) if name else "安排"
    when = time.group(1) if time else ""
    note = sub.group(1) if sub else "价格与开放时间以现场为准"
    return f'<b>{title}</b>{when} · {note}'

# 先把三个已有的 checkbox 统一成同名 radio
t = re.sub(r'<input class="sr" type="checkbox" id="(k[123])">',
           r'<input class="sr" type="radio" name="blk" id="\1">', t)

# 再给没有 for 的块补 input + 浮卡
counter = [10]
def add_interaction(m: re.Match) -> str:
    cls, style, inner = m.group(1), m.group(2) or "", m.group(3)
    if 'for=' in inner or 'class="pop"' in inner:
        return m.group(0)
    counter[0] += 1
    bid = f"b{counter[0]}"
    return (f'<input class="sr" type="radio" name="blk" id="{bid}">'
            f'<label class="blk {cls}" for="{bid}"{style}>{inner}'
            f'<span class="pop">{pop_for(inner)}</span></label>')

t = re.sub(r'<label class="blk ([a-z]+)"( style="[^"]*")?>(.*?)</label>', add_interaction, t, flags=re.S)

# 最右列的浮卡反向对齐，避免出屏
t = t.replace("  .col input:checked + .blk .pop{display:block}",
"""  .col input:checked + .blk .pop{display:block}
  .col:last-child .pop{left:auto;right:-4px}
  .col:last-child .pop:after{left:auto;right:16px}""")

# ---------- 7. 指令条：从假输入改成可执行/可还原的指令 ----------
t = t.replace('<input class="sr" type="checkbox" id="cmd-indoor">',
              '<input class="sr" type="radio" name="cmd" id="cmd-none" checked>\n    <input class="sr" type="radio" name="cmd" id="cmd-indoor">')

old_bar = re.search(r'<div class="cmdbar">.*?</div>\s*<div class="cmdhint">.*?</div>', t, flags=re.S)
new_bar = """<div class="cmdbar">
                <span class="slash">/</span>
                <span class="text">把 11-21 下午换成室内（演示指令，可执行也可还原）</span>
                <label class="cmdchip" for="cmd-indoor">执行</label>
                <label class="cmdreset" for="cmd-none">还原</label>
              </div>
              <div class="cmdhint">
                <label for="app-budget"><input type="checkbox" hidden>看替代方案</label>
                <label for="app-change"><input type="checkbox" hidden>看变更</label>
              </div>"""
if old_bar:
    t = t[:old_bar.start()] + new_bar + t[old_bar.end():]

# 指令条样式
t = t.replace("  .cmdhint{display:flex;gap:6px;margin-top:9px}",
"""  .cmdchip{cursor:pointer;font-size:11.5px;font-weight:600;color:#0a0b0d;background:linear-gradient(180deg,var(--gold2),var(--gold));
    padding:6px 13px;border-radius:8px}
  .cmdreset{cursor:pointer;font-size:11px;color:var(--dim);padding:6px 10px;border:1px solid var(--line);border-radius:8px}
  #cmd-indoor:checked ~ .plan .cmdchip{opacity:.45}
  #cmd-indoor:checked ~ .plan .cmdreset{color:var(--ink);border-color:var(--line2)}
  #cmd-none:checked ~ .plan .text:after{content:"　· 未执行";color:var(--dim)}
  .cmdhint{display:flex;gap:6px;margin-top:9px}""")

# ---------- 8. 右栏：把“待判断”变成可执行入口 ----------
t = t.replace("""<div class="warnbox">换酒店或加景点都会超支；要改建议先看「预算」里的替代方案。</div>""",
"""<div class="warnbox">换酒店或加景点都会超支；要改建议先看「预算」里的替代方案。</div>
            <div style="display:flex;gap:8px;margin-top:10px">
              <label class="ghost" for="app-budget" style="font-size:11.5px">看替代方案</label>
              <label class="ghost" for="app-change" style="font-size:11.5px">看变更</label>
            </div>""")

p.write_text(t, encoding="utf-8")
print("remediated:", len(t), "bytes")
