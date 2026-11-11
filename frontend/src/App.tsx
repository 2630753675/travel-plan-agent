import { useEffect, useRef, useState } from 'react'
import SettingsPanel from './Settings'

type Trace = {
  phase: 'start' | 'end' | 'edge' | 'refine'
  node?: string
  summary?: string
  from?: string
  to?: string
  reason?: string
}

type Card = {
  key: string
  kind: 'node' | 'edge'
  node: string
  status: 'waiting' | 'running' | 'done'
  summary?: string
  reason?: string
}

type Item = {
  slot: string
  start_time: string
  end_time: string
  type: string
  title: string
  cost: number
  duration_min: number
  note: string
}

type Day = {
  date: string
  items: Item[]
  day_cost: number
  notes: string[]
}

type Issue = { severity: 'blocker' | 'warning'; code: string; message: string }

type Result = {
  plan_id: string
  brief: { destination: string; origin: string; budget_total: number }
  itinerary: {
    days: Day[]
    total_cost: number
    budget_total: number
    warnings: string[]
  }
  critique: { passed: boolean; issues: Issue[]; suggestions: string[] }
  revision: number
  errors: string[]
}

const NODE_LABEL: Record<string, string> = {
  clarify: '需求澄清',
  transport: '交通',
  lodging: '住宿',
  poi: '景点',
  weather: '天气',
  planner: '行程规划',
  critic: '可行性校验',
}

const PARALLEL = ['transport', 'lodging', 'poi', 'weather']

const PREFERENCES = ['美食', '人文', '自然', '购物', '慢节奏', '夜景']
const PACE = ['轻松', '适中', '紧凑'] as const

const TODAY = new Date().toISOString().slice(0, 10)

type Form = {
  origin: string
  destination: string
  start_date: string
  end_date: string
  travelers: number
  budget_total: number
  elder: boolean
  child: boolean
  preferences: string[]
  pace: (typeof PACE)[number]
}

const DEFAULT_FORM: Form = {
  origin: '上海',
  destination: '成都',
  start_date: '2026-11-20',
  end_date: '2026-11-24',
  travelers: 2,
  budget_total: 8000,
  elder: true,
  child: false,
  preferences: ['美食', '人文'],
  pace: '适中',
}

async function consume(res: Response, onEvent: (name: string, data: any) => void) {
  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      let name = 'message'
      let data = ''
      for (const line of block.split('\n')) {
        if (line.startsWith('event: ')) name = line.slice(7).trim()
        else if (line.startsWith('data: ')) data += line.slice(6)
      }
      if (data) onEvent(name, JSON.parse(data))
    }
  }
}

function hhmm(t: string) {
  return t.slice(0, 5)
}

function weekday(d: string) {
  return '日一二三四五六'[new Date(d + 'T00:00:00').getDay()]
}

export default function App() {
  const [form, setForm] = useState<Form>(DEFAULT_FORM)
  const [phase, setPhase] = useState<'form' | 'running' | 'done' | 'error'>('form')
  const [cards, setCards] = useState<Card[]>([])
  const [result, setResult] = useState<Result | null>(null)
  const [error, setError] = useState('')
  const [refining, setRefining] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Record<string, string>>({})
  const [refineNote, setRefineNote] = useState<Record<string, string>>({})
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [llmOn, setLlmOn] = useState(false)
  const timelineRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    fetch('/api/config')
      .then((r) => r.json())
      .then((c) => setLlmOn(Boolean(c?.llm?.enabled)))
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    const id = new URLSearchParams(location.search).get('plan')
    if (!id) return
    fetch(`/api/plan/${id}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('not found'))))
      .then((rec) => {
        setResult({
          plan_id: rec.id,
          brief: rec.brief,
          itinerary: rec.itinerary,
          critique: rec.critique,
          revision: rec.revision ?? 0,
          errors: [],
        })
        setCards(
          (rec.trace as Trace[])
            .filter((t) => t.phase === 'end' || t.phase === 'edge')
            .map((t, i) => ({
              key: `${i}`,
              kind: t.phase === 'edge' ? ('edge' as const) : ('node' as const),
              node: t.node ?? t.from ?? '',
              status: 'done' as const,
              summary: t.summary,
              reason: t.reason,
            })),
        )
        setPhase('done')
      })
      .catch(() => setError('该规划不存在或已失效'))
  }, [])

  useEffect(() => {
    timelineRef.current?.scrollTo({ top: timelineRef.current.scrollHeight, behavior: 'smooth' })
  }, [cards])

  function pushTrace(ev: Trace) {
    if (ev.phase === 'start') {
      setCards((prev) => {
        // 优先复用预置骨架里尚未完成的槽位；回退重跑时再追加新卡片
        const idx = prev.findIndex((c) => c.kind === 'node' && c.node === ev.node && c.status !== 'done')
        if (idx >= 0) {
          const next = [...prev]
          next[idx] = { ...next[idx], status: 'running' }
          return next
        }
        return [
          ...prev,
          { key: `${Date.now()}-${Math.random()}`, kind: 'node', node: ev.node!, status: 'running' },
        ]
      })
    } else if (ev.phase === 'end') {
      setCards((prev) => {
        for (let i = prev.length - 1; i >= 0; i--) {
          const c = prev[i]
          if (c.kind === 'node' && c.node === ev.node && c.status === 'running') {
            const next = [...prev]
            next[i] = { ...c, status: 'done', summary: ev.summary }
            return next
          }
        }
        return prev
      })
    } else if (ev.phase === 'edge') {
      setCards((prev) => [
        ...prev,
        {
          key: `${Date.now()}-${Math.random()}`,
          kind: 'edge',
          node: ev.from ?? '',
          status: 'done',
          reason: ev.reason,
        },
      ])
    }
  }

  async function runPlan() {
    setPhase('running')
    setCards(
      ['clarify', 'transport', 'lodging', 'poi', 'weather', 'planner', 'critic'].map((n) => ({
        key: `slot-${n}`,
        kind: 'node' as const,
        node: n,
        status: 'waiting' as const,
      })),
    )
    setResult(null)
    setError('')
    const body = {
      origin: form.origin,
      destination: form.destination,
      start_date: form.start_date,
      end_date: form.end_date,
      travelers: form.travelers,
      budget_total: form.budget_total,
      companions: form.elder || form.child
        ? [...(form.elder ? ['老人'] : []), ...(form.child ? ['儿童'] : [])]
        : ['无'],
      preferences: form.preferences,
      pace: form.pace,
    }
    try {
      const res = await fetch('/api/plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await consume(res, (name, data) => {
        if (name === 'trace') pushTrace(data)
        else if (name === 'done') {
          setResult(data)
          setPhase('done')
          const url = new URL(location.href)
          url.searchParams.set('plan', data.plan_id)
          history.replaceState(null, '', url)
        } else if (name === 'error') {
          setError(data.message)
          setPhase('error')
        }
      })
    } catch (e) {
      setError(String(e))
      setPhase('error')
    }
  }

  async function refine(day: string) {
    if (!result) return
    setRefining(day)
    try {
      const res = await fetch(`/api/plan/${result.plan_id}/refine`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date: day, feedback: feedback[day] ?? '' }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setResult({ ...result, itinerary: data.itinerary, critique: data.critique, revision: data.revision ?? result.revision })
      setFeedback((f) => ({ ...f, [day]: '' }))
      setRefineNote((n) => ({
        ...n,
        [day]: data.refine_source === 'model' ? '已由模型理解意见后重排' : '已按规则重排',
      }))
    } catch (e) {
      setError(String(e))
    } finally {
      setRefining(null)
    }
  }

  const doneCount = cards.filter((c) => c.kind === 'node' && c.status === 'done').length

  function renderAgent(c: Card) {
    return (
      <article key={c.key} className={`agent ${c.status} ${PARALLEL.includes(c.node) ? 'in-group' : ''}`}>
        <div className="agent-head">
          <span className="agent-name">{NODE_LABEL[c.node] ?? c.node}</span>
          <span className={`dot ${c.status}`} />
        </div>
        <p className="agent-summary">
          {c.summary ?? (c.status === 'running' ? '执行中…' : c.status === 'waiting' ? '等待中' : '')}
        </p>
      </article>
    )
  }

  /** 把连续的并行节点聚成一组，视觉上体现同一 superstep 并发 */
  function groupCards(list: Card[]) {
    const out: ({ kind: 'single'; card: Card } | { kind: 'group'; cards: Card[]; key: string })[] = []
    for (const c of list) {
      if (c.kind === 'node' && PARALLEL.includes(c.node)) {
        const last = out[out.length - 1]
        if (last && last.kind === 'group') last.cards.push(c)
        else out.push({ kind: 'group', cards: [c], key: `g-${c.key}` })
      } else {
        out.push({ kind: 'single', card: c })
      }
    }
    return out
  }

  return (
    <div className="shell">
      <header className="topbar">
        <div>
          <h1>旅行规划多 Agent 编排</h1>
          <p className="sub">LangGraph 编排 · 四路并行检索 · 校验回退 · 单日重排</p>
        </div>
        <div className="topbar-right">
          <span className="mode-pill">{llmOn ? '模型生成' : '规则引擎'}</span>
          <div className="status-pill">
            {phase === 'form' && '待提交'}
            {phase === 'running' && `执行中 ${doneCount}/7`}
            {phase === 'done' && (result?.critique.passed ? '已完成' : '已完成 · 有未解项')}
            {phase === 'error' && '失败'}
          </div>
          <button className="ghost" onClick={() => setSettingsOpen(true)}>
            设置
          </button>
        </div>
      </header>

      {settingsOpen && (
        <SettingsPanel
          onClose={() => setSettingsOpen(false)}
          onSaved={(s) => setLlmOn(Boolean(s.llm.enabled))}
        />
      )}

      <div className="layout">
        <aside className="panel">
          <h2>需求</h2>
          <label>
            出发地
            <input
              value={form.origin}
              disabled={phase === 'running'}
              onChange={(e) => setForm({ ...form, origin: e.target.value })}
            />
          </label>
          <label>
            目的地
            <input
              value={form.destination}
              disabled={phase === 'running'}
              onChange={(e) => setForm({ ...form, destination: e.target.value })}
            />
          </label>
          <div className="row">
            <label>
              出发
              <input
                type="date"
                value={form.start_date}
                disabled={phase === 'running'}
                onChange={(e) => setForm({ ...form, start_date: e.target.value })}
              />
            </label>
            <label>
              返回
              <input
                type="date"
                value={form.end_date}
                disabled={phase === 'running'}
                onChange={(e) => setForm({ ...form, end_date: e.target.value })}
              />
            </label>
          </div>
          <div className="row">
            <label>
              人数
              <input
                type="number"
                min={1}
                max={20}
                value={form.travelers}
                disabled={phase === 'running'}
                onChange={(e) => setForm({ ...form, travelers: Number(e.target.value) })}
              />
            </label>
            <label>
              总预算
              <input
                type="number"
                min={1000}
                step={500}
                value={form.budget_total}
                disabled={phase === 'running'}
                onChange={(e) => setForm({ ...form, budget_total: Number(e.target.value) })}
              />
            </label>
          </div>

          <div className="chips">
            <span className="chip-label">同行</span>
            <button
              className={`chip ${form.elder ? 'on' : ''}`}
              onClick={() => setForm({ ...form, elder: !form.elder })}
              disabled={phase === 'running'}
            >
              老人
            </button>
            <button
              className={`chip ${form.child ? 'on' : ''}`}
              onClick={() => setForm({ ...form, child: !form.child })}
              disabled={phase === 'running'}
            >
              儿童
            </button>
          </div>

          <div className="chips">
            <span className="chip-label">偏好</span>
            {PREFERENCES.map((p) => (
              <button
                key={p}
                className={`chip ${form.preferences.includes(p) ? 'on' : ''}`}
                disabled={phase === 'running'}
                onClick={() =>
                  setForm({
                    ...form,
                    preferences: form.preferences.includes(p)
                      ? form.preferences.filter((x) => x !== p)
                      : [...form.preferences, p],
                  })
                }
              >
                {p}
              </button>
            ))}
          </div>

          <div className="chips">
            <span className="chip-label">节奏</span>
            {PACE.map((p) => (
              <button
                key={p}
                className={`chip ${form.pace === p ? 'on' : ''}`}
                disabled={phase === 'running'}
                onClick={() => setForm({ ...form, pace: p })}
              >
                {p}
              </button>
            ))}
          </div>

          <button className="primary" onClick={runPlan} disabled={phase === 'running'}>
            {phase === 'running' ? '规划中…' : phase === 'form' ? '开始规划' : '重新规划'}
          </button>
        </aside>

        <main className="main">
          {error && <div className="alert">{error}</div>}

          {cards.length > 0 && (
            <section className="timeline-wrap">
              <div className="section-head">
                <h2>编排过程</h2>
                <span className="hint">四路检索并行执行，校验不通过会回退重排</span>
              </div>
              <div className="timeline" ref={timelineRef}>
                {groupCards(cards).map((g) =>
                  g.kind === 'group' ? (
                    <div key={g.key} className="parallel-group">
                      <span className="group-label">并行检索（同一 superstep）</span>
                      <div className="parallel-cells">{g.cards.map(renderAgent)}</div>
                    </div>
                  ) : g.card.kind === 'edge' ? (
                    <div key={g.card.key} className="edge">
                      ↳ {g.card.reason}
                    </div>
                  ) : (
                    renderAgent(g.card)
                  ),
                )}
              </div>
            </section>
          )}

          {result && (
            <section className="result">
              <div className="stats">
                <div className="stat">
                  <span className="stat-label">总花费</span>
                  <strong>{result.itinerary.total_cost.toFixed(0)} 元</strong>
                </div>
                <div className="stat">
                  <span className="stat-label">预算</span>
                  <strong>{result.itinerary.budget_total.toFixed(0)} 元</strong>
                </div>
                <div className="stat">
                  <span className="stat-label">使用率</span>
                  <strong>{((result.itinerary.total_cost / result.itinerary.budget_total) * 100).toFixed(0)}%</strong>
                </div>
                <div className="stat">
                  <span className="stat-label">校验</span>
                  <strong className={result.critique.passed ? 'ok' : 'warn'}>
                    {result.critique.passed ? '通过' : `${result.critique.issues.filter((i) => i.severity === 'blocker').length} 项未解决`}
                  </strong>
                </div>
                <div className="stat">
                  <span className="stat-label">重排</span>
                  <strong>{Math.max(0, result.revision - 1)} 次</strong>
                </div>
                <button
                  className="ghost"
                  onClick={() => navigator.clipboard.writeText(location.href)}
                  title="复制分享链接"
                >
                  复制链接
                </button>
              </div>

              {result.itinerary.warnings.length > 0 && (
                <ul className="warnings">
                  {result.itinerary.warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              )}

              <div className="days">
                {result.itinerary.days.map((day) => (
                  <article key={day.date} className="day">
                    <header className="day-head">
                      <div>
                        <h3>
                          {day.date} 周{weekday(day.date)}
                        </h3>
                        <span className="muted">{day.items.filter((i) => i.type === 'sight').length} 个安排</span>
                      </div>
                      <span className="day-cost">{day.day_cost.toFixed(0)} 元</span>
                    </header>

                    <ul className="items">
                      {day.items
                        .filter((i) => i.type !== 'rest')
                        .map((item, i) => (
                          <li key={i} className={`item ${item.type}`}>
                            <span className="time">
                              {hhmm(item.start_time)}
                              {item.end_time !== item.start_time ? `–${hhmm(item.end_time)}` : ''}
                            </span>
                            <span className="slot">{item.slot}</span>
                            <span className="title">{item.title}</span>
                            {item.cost > 0 && <span className="cost">{item.cost.toFixed(0)}</span>}
                            {item.note && <span className="note">{item.note}</span>}
                          </li>
                        ))}
                    </ul>

                    {day.notes.length > 0 && (
                      <ul className="day-notes">
                        {day.notes.map((n, i) => (
                          <li key={i}>{n}</li>
                        ))}
                      </ul>
                    )}

                    <div className="refine">
                      <input
                        placeholder="对这天提意见，例如：太赶了，想轻松点"
                        value={feedback[day.date] ?? ''}
                        onChange={(e) => setFeedback({ ...feedback, [day.date]: e.target.value })}
                      />
                      <button onClick={() => refine(day.date)} disabled={refining === day.date}>
                        {refining === day.date ? '重排中…' : '重排这一天'}
                      </button>
                    </div>
                    {refineNote[day.date] && <p className="refine-note">{refineNote[day.date]}</p>}
                  </article>
                ))}
              </div>

              {result.critique.issues.length > 0 && (
                <section className="issues">
                  <h2>校验明细</h2>
                  <ul>
                    {result.critique.issues.map((issue, i) => (
                      <li key={i} className={issue.severity}>
                        <span className="tag">{issue.severity === 'blocker' ? '阻断' : '提醒'}</span>
                        {issue.message}
                      </li>
                    ))}
                  </ul>
                  {result.critique.suggestions.length > 0 && (
                    <p className="muted">建议：{result.critique.suggestions.join('；')}</p>
                  )}
                </section>
              )}
            </section>
          )}

          {phase === 'form' && cards.length === 0 && (
            <div className="empty">
              <p>填写左侧需求后开始规划。</p>
              <p className="muted">
                系统会并行查询交通、住宿、景点与天气，生成草稿后由校验 agent 检查预算与时间冲突，
                不通过则回退重排，最多 2 轮。
              </p>
            </div>
          )}
        </main>
      </div>
    </div>
  )
}
