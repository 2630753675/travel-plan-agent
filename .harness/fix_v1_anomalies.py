import pathlib

p = pathlib.Path("frontend/src/App.tsx")
t = p.read_text(encoding="utf-8")

# 1) 晚数跟随行程
t = t.replace(
    "choice.stay === 'switch' ? '最后一晚住机场附近' : '四晚同一家',",
    "choice.stay === 'switch' ? '最后一晚住机场附近' : `${nights} 晚同一家`,",
)
t = t.replace(
    "  const days = result?.itinerary.days ?? []",
    "  const nights = Math.max(1, dayCount(form) - 1)\n  const days = result?.itinerary.days ?? []",
)
t = t.replace(
    "['same', '四晚同一家', '最后一天去机场约 80 分钟'],",
    "['same', `${nights} 晚同一家`, '最后一天去机场约 80 分钟'],",
)
t = t.replace(
    "<b>{choice.stay === 'switch' ? '2 家 · 3+1' : '1 家 · 4 晚'}</b>",
    "<b>{choice.stay === 'switch' ? `2 家 · ${nights - 1}+1` : `1 家 · ${nights} 晚`}</b>",
)

# 2) 运行阶段使用真实 stage key
old_stage = """                  {['澄清需求', '并行检索', '专业团队', '排行程', '硬约束校验', '整合文稿'].map((label, i) => {
                    const reached = liveEvents.some((e) => e.stage && [label].includes(label)) || liveEvents.length > i * 2
                    return (
                      <div key={label} className={`v1-asm-row ${reached ? 'on' : ''}`}>
                        <span>{String(i + 1).padStart(2, '0')}</span>
                        <i />
                        <b>{label}</b>
                        <em>{reached ? '完成' : '等待'}</em>
                      </div>
                    )
                  })}"""
new_stage = """                  {[
                    ['clarify', '澄清需求'],
                    ['retrieval', '并行检索'],
                    ['peer', '专业团队'],
                    ['plan', '排行程'],
                    ['check', '硬约束校验'],
                    ['confirm', '整合文稿'],
                  ].map(([key, label], i) => {
                    const reached = liveEvents.some((e) => e.stage === key)
                    return (
                      <div key={key} className={`v1-asm-row ${reached ? 'on' : ''}`}>
                        <span>{String(i + 1).padStart(2, '0')}</span>
                        <i />
                        <b>{label}</b>
                        <em>{reached ? '完成' : '等待'}</em>
                      </div>
                    )
                  })}"""
if old_stage not in t:
    raise SystemExit("stage block not found")
t = t.replace(old_stage, new_stage)

# 3) 计划版本与团队审核版本分开显示
old_ver = '<span className="v1-vpill">v{result.version ?? \'—\'}</span>'
if old_ver not in t:
    raise SystemExit("version pill not found")
new_ver = """<span className="v1-vpill">计划 v{result.version ?? '—'}</span>
              {result.approved_version != null && result.approved_version !== result.version && (
                <span className="v1-vpill">审核 v{result.approved_version}</span>
              )}"""
t = t.replace(old_ver, new_ver)

p.write_text(t, encoding="utf-8")
print("patched ok")
