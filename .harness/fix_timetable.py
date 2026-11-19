import pathlib

path = pathlib.Path(r"E:\项目\agent_study\dsh_work2\docs\ui-proposals\05-timetable.html")
t = path.read_text(encoding="utf-8")

t = t.replace(".axis .h{\n    height:64px;", ".axis .h{\n    height:56px;")
t = t.replace(".col{position:relative;border-right:1px solid var(--line);min-height:64px}",
              ".col{position:relative;border-right:1px solid var(--line);min-height:784px}")
t = t.replace(".col .slot{height:64px;border-bottom:1px solid var(--line)}",
              ".col .slot{height:56px;border-bottom:1px solid var(--line)}")
t = t.replace(".col:nth-child(7){border-right:none}", ".col:last-of-type{border-right:none}")

axis = "\n".join('      <div class="h"><span>%02d:00</span></div>' % h for h in range(8, 22))
slots = "".join('<div class="slot"></div>' for _ in range(14))


def blk(cls, top, height, inner):
    return '      <div class="blk %s" style="top:%dpx;height:%dpx">%s</div>' % (cls, top, height, inner)


def item(t0, title, sub):
    return '<span class="t">%s</span><span class="n">%s</span><span class="s">%s</span>' % (t0, title, sub)


columns = {
    "11-20": [
        blk("flight", 4, 182, item("08:05–11:20", "MU5401 上海 → 成都", "2 人 ¥1,960 · 11:20 落地")),
        blk("transfer", 186, 75, item("11:20–12:40", "机场 → 酒店", "含取行李")),
        blk("sight", 270, 112, item("12:50–14:50", "春熙路太古里", "免费 · 0.5km")),
        blk("meal", 588, 75, item("18:30–19:50", "晚餐窗口", "就近解决")),
        blk("hotel", 728, 28, item("21:00", "入住 · 第 1 晚", "¥520")),
    ],
    "11-21": [
        blk("sight", 56, 84, item("09:00–10:30", "杜甫草堂", "50 元/人 · 地势平缓")),
        blk("meal", 224, 56, item("12:00–13:00", "午休", "回酒店 1 小时")),
        blk("sight", 280, 112, item("13:00–15:00", "宽窄巷子", "免费 · 打车 20 分钟")),
        blk("meal", 588, 75, item("18:30–19:50", "晚餐窗口", "就近解决")),
        blk("hotel", 728, 28, item("21:00", "回酒店", "¥520")),
    ],
    "11-22": [
        blk("sight", 56, 112, item("09:00–11:00", "成都博物馆", "免费 · 雨天室内")),
        blk("meal", 196, 56, item("11:30–12:30", "午餐", "馆内或附近")),
        blk("sight", 280, 112, item("13:00–15:00", "四川博物院", "免费 · 车程 15 分钟")),
        blk("meal", 588, 75, item("18:30–19:50", "晚餐窗口", "就近解决")),
        blk("hotel", 728, 28, item("21:00", "回酒店", "¥520")),
    ],
    "11-23": [
        blk("sight", 28, 84, item("08:30–10:00", "武侯祠", "50 元/人 · 早到避人流")),
        blk("sight", 140, 84, item("10:30–12:00", "锦里古街", "免费 · 相邻步行")),
        blk("meal", 224, 56, item("12:00–13:00", "午餐（就地）", "锦里内解决")),
        blk("meal", 588, 75, item("18:30–19:50", "晚餐窗口", "就近解决")),
        blk("hotel", 728, 28, item("21:00", "回酒店", "¥520")),
    ],
    "11-24": [
        blk("transfer warn", 75, 65, item("09:20–10:30", "酒店 → 天府机场", "提前 150 分钟到达")),
        blk("flight", 280, 173, item("13:00–16:05", "MU5402 成都 → 上海", "2 人 ¥1,920")),
    ],
}

cols_html = ""
for day, blocks in columns.items():
    cols_html += '\n    <!-- %s -->\n    <div class="col">%s\n%s\n    </div>\n' % (day, slots, "\n".join(blocks))

start = t.index('<div class="axis">')
end = t.index('  </div>\n\n  <div class="legend">')
new_grid = '    <div class="axis">\n' + axis + '\n    </div>\n' + cols_html + '\n'
t = t[:start] + new_grid + t[end:]

t = t.replace('<span style="margin-left:auto">时间轴 08:00–17:00，夜间回酒店为 21:00 固定约定</span>',
              '<span style="margin-left:auto">时间轴 08:00–22:00 · 灰色小条为 21:00 回酒店</span>')
path.write_text(t, encoding="utf-8")
print("timetable rebuilt", len(t))
