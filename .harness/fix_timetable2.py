import pathlib

path = pathlib.Path(r"E:\项目\agent_study\dsh_work2\docs\ui-proposals\05-timetable.html")
t = path.read_text(encoding="utf-8")

# 扩到 23:00，回酒店块有正常高度；空档补“自由时间”带，避免大片空白
t = t.replace('    <div class="axis">\n' , '    <div class="axis">\n')
axis_old = t[t.index('    <div class="axis">'):t.index('    <!-- 11-20 -->')]
axis_new = '    <div class="axis">\n' + "\n".join(
    '      <div class="h"><span>%02d:00</span></div>' % h for h in range(8, 24)
) + '\n    </div>\n\n'
t = t.replace(axis_old, axis_new)
t = t.replace("min-height:784px}", "min-height:896px}")
t = t.replace('.col .slot{height:56px;border-bottom:1px solid var(--line)}',
              '.col .slot{height:56px;border-bottom:1px solid var(--line)}\n  .col .slot:nth-child(n+15){border-bottom:none}')

# 每列补一个空槽（共 16 行）+ 自由时间带 + 回酒店块高度 56
t = t.replace('<div class="col"><div class="slot"></div>' * 1, '<div class="col"><div class="slot"></div>')
t = t.replace('<div class="slot"></div></div>', '<div class="slot"></div><div class="slot"></div></div>')

free = '<div class="blk free" style="top:%dpx;height:%dpx"><span class="t">%s</span><span class="n">自由时间</span><span class="s">%s</span></div>'
t = t.replace('<div class="blk hotel" style="top:728px;height:28px">', '<div class="blk hotel" style="top:728px;height:56px">')

t = t.replace('<div class="blk sight" style="top:270px;height:112px">' + '<span class="t">12:50–14:50</span><span class="n">春熙路太古里</span><span class="s">免费 · 0.5km</span></div>',
              '<div class="blk sight" style="top:270px;height:112px"><span class="t">12:50–14:50</span><span class="n">春熙路太古里</span><span class="s">免费 · 0.5km</span></div>\n' + free % (392, 196, "14:50–18:30", "回酒店休息或就近逛街"))
for top, end, note, anchor in (
    (392, "15:00–18:30", "回酒店休息，晚餐就近", '<div class="blk sight" style="top:280px;height:112px"><span class="t">13:00–15:00</span><span class="n">宽窄巷子</span>'),
):
    t = t.replace(anchor, anchor + '</div>' if False else anchor)  # no-op guard
t = t.replace('<div class="blk sight" style="top:280px;height:112px"><span class="t">13:00–15:00</span><span class="n">宽窄巷子</span><span class="s">免费 · 打车 20 分钟</span></div>',
              '<div class="blk sight" style="top:280px;height:112px"><span class="t">13:00–15:00</span><span class="n">宽窄巷子</span><span class="s">免费 · 打车 20 分钟</span></div>\n' + free % (392, 196, "15:00–18:30", "自由时间 · 晚餐就近"))
t = t.replace('<div class="blk sight" style="top:280px;height:112px"><span class="t">13:00–15:00</span><span class="n">四川博物院</span><span class="s">免费 · 车程 15 分钟</span></div>',
              '<div class="blk sight" style="top:280px;height:112px"><span class="t">13:00–15:00</span><span class="n">四川博物院</span><span class="s">免费 · 车程 15 分钟</span></div>\n' + free % (392, 196, "15:00–18:30", "雨天回酒店休息"))
t = t.replace('<div class="blk meal" style="top:224px;height:56px"><span class="t">12:00–13:00</span><span class="n">午餐（就地）</span><span class="s">锦里内解决</span></div>',
              '<div class="blk meal" style="top:224px;height:56px"><span class="t">12:00–13:00</span><span class="n">午餐（就地）</span><span class="s">锦里内解决</span></div>\n' + free % (280, 308, "13:00–18:30", "自由时间 · 可回酒店午休"))

# free 样式
t = t.replace('.blk.warn{box-shadow:inset 3px 0 0 #d97706}',
              '.blk.warn{box-shadow:inset 3px 0 0 #d97706}\n  .blk.free{background:#f7f8fa;border:1px dashed #dfe4ea;color:#8a929e}\n  .blk.free .n{font-weight:500}\n  .blk.free .s{font-size:10px}')

t = t.replace('时间轴 08:00–22:00 · 灰色小条为 21:00 回酒店', '时间轴 08:00–23:00 · 虚线块为自由时间')
path.write_text(t, encoding="utf-8")
print("patched", len(t))
