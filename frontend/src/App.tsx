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
  negotiation?: Negotiation | null
  activity?: LogEntry[]
}

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

type NegEvidence = { label: string; value: string }

type NegMessage = {
  round: number
  speaker: string
  stance: string
  claim: string
  evidence: NegEvidence[]
  tool_calls: string[]
}

type Negotiation = {
  triggered_by: string
  rounds: NegMessage[]
  outcome: string
  adjustment: { drop_poi_ids: string[]; use_cheaper_hotel: boolean }
  summary: string
}

const SPEAKER_LABEL: Record<string, string> = {
  budget_officer: '预算官',
  experience_officer: '体验官',
  mediator: '调解者',
}

const STANCE_LABEL: Record<string, string> = {
  propose: '提议',
  object: '反对',
  accept: '让步',
  rule: '裁决',
  adopt: '采纳',
  escalate: '升级',
}

const OUTCOME_LABEL: Record<string, string> = {
  adopted: '已达成方案',
  escalated: '未达成一致，交给你决定',
  no_change: '无需调整',
}

function NegotiationTrail({ neg }: { neg: Negotiation }) {
  return (
    <section className="negotiation">
      <div className="neg-head">
        <h2>协商过程</h2>
        <span className={`neg-outcome ${neg.outcome}`}>{OUTCOME_LABEL[neg.outcome] ?? neg.outcome}</span>
      </div>
      <p className="neg-trigger">{neg.triggered_by}</p>
      <ol className="neg-rounds">
        {neg.rounds.map((m, i) => (
          <li key={i} className={`neg-msg ${m.speaker}`}>
            <div className="neg-who">
              <span className="neg-name">{SPEAKER_LABEL[m.speaker] ?? m.speaker}</span>
              <span className={`neg-stance ${m.stance}`}>{STANCE_LABEL[m.stance] ?? m.stance}</span>
            </div>
            <p className="neg-claim">{m.claim}</p>
            {m.evidence.length > 0 && (
              <ul className="neg-evidence">
                {m.evidence.map((e, j) => (
                  <li key={j}>
                    <span className="neg-label">{e.label}</span>
                    <span className="neg-value">{e.value}</span>
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ol>
    </section>
  )
}

type LogEntry = {
  ts: string
  actor: string
  kind: string
  text: string
  detail: string
  group: string
  depth: number
  tool: string
}

const GROUP_LABEL: Record<string, string> = {
  clarify: '需求澄清',
  retrieval: '并行检索',
  plan: '行程规划',
  check: '可行性校验',
  negotiation: '预算与体验协商',
  confirm: '等你决定',
  misc: '其他',
}

/** 按阶段分块：连续同 group 的行归一块（同一阶段多次出现时自然分开） */
function groupByStage(entries: LogEntry[]) {
  const groups: { key: string; label: string; ts: string; items: LogEntry[] }[] = []
  for (const e of entries) {
    const key = e.group || 'misc'
    const last = groups[groups.length - 1]
    if (last && last.key === key) last.items.push(e)
    else groups.push({ key, label: GROUP_LABEL[key] ?? key, ts: e.ts, items: [e] })
  }
  return groups
}

/** 组内按 actor 分块，让"谁做了什么"一眼可读 */
function chunkByActor(items: LogEntry[]) {
  const chunks: { actor: string; items: LogEntry[] }[] = []
  for (const e of items) {
    const last = chunks[chunks.length - 1]
    if (last && last.actor === e.actor) last.items.push(e)
    else chunks.push({ actor: e.actor, items: [e] })
  }
  return chunks
}

const KIND_MARK: Record<string, string> = {
  action: '·',
  decision: '!',
  question: '?',
  answer: '✓',
  tool: '⚙',
  speech: '“',
}

function LogGroups({ entries }: { entries: LogEntry[] }) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const groups = groupByStage(entries)
  return (
    <div className="log-groups">
      {groups.map((g, gi) => {
        const key = `${g.key}-${gi}`
        const isCollapsed = collapsed[key]
        return (
          <div key={key} className={`log-group ${isCollapsed ? 'collapsed' : ''}`}>
            <button
              className="log-group-head"
              onClick={() => setCollapsed((c) => ({ ...c, [key]: !c[key] }))}
            >
              <span className="log-caret">{isCollapsed ? '▶' : '▼'}</span>
              <span className="log-group-label">{g.label}</span>
              <span className="log-group-ts">{g.ts}</span>
            </button>
            {!isCollapsed && (
              <div className="log-group-body">
                {chunkByActor(g.items.filter((e) => e.depth > 0)).map((chunk, ci) => (
                  <div key={ci} className="log-chunk">
                    <div className="log-chunk-head">{chunk.actor}</div>
                    <ul className="log-list">
                      {chunk.items.map((e, i) => (
                        <li key={i} className={`log-row kind-${e.kind} depth-${e.depth}`}>
                          <span className="log-mark">{KIND_MARK[e.kind] ?? '·'}</span>
                          <span className="log-text">{e.text}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function ActivityLog({ entries }: { entries: LogEntry[] }) {
  if (!entries || entries.length === 0) return null
  return (
    <section className="activity">
      <div className="section-head">
        <h2>执行轨迹</h2>
        <span className="hint">{entries.length} 步 · 按阶段分组</span>
      </div>
      <LogGroups entries={entries} />
    </section>
  )
}

type InterruptOption = { value: string; label: string }

type InterruptInfo = {
  type: string
  message?: string
  total: number
  budget: number
  over: number
  ratio: number
  options: InterruptOption[]
  plan_id: string
  negotiation?: Negotiation | null
  activity?: LogEntry[]
}

export default function App() {
  const [form, setForm] = useState<Form>(DEFAULT_FORM)
  const [phase, setPhase] = useState<'form' | 'running' | 'interrupt' | 'done' | 'error'>('form')
  const [cards, setCards] = useState<Card[]>([])
  const [result, setResult] = useState<Result | null>(null)
  const [interruptInfo, setInterruptInfo] = useState<InterruptInfo | null>(null)
  const [error, setError] = useState('')
  const [refining, setRefining] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Record<string, string>>({})
  const [refineNote, setRefineNote] = useState<Record<string, string>>({})
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [llmOn, setLlmOn] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [liveLogs, setLiveLogs] = useState<LogEntry[]>([])
  const drawerRef = useRef<HTMLDivElement>(null)

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
        if (rec.activity) setLiveLogs(rec.activity)
      })
      .catch(() => setError('该规划不存在或已失效'))
  }, [])

  // 轨迹面板跟随最新一条自动滚动
  useEffect(() => {
    if (!drawerOpen) return
    drawerRef.current?.scrollTo({ top: drawerRef.current.scrollHeight, behavior: 'smooth' })
  }, [liveLogs, drawerOpen])

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

  function handleEvent(name: string, data: any) {
    if (name === 'trace') {
      if (data.phase === 'log' && data.entry) {
        // 流式追加：每条轨迹在产生的瞬间就出现
        setLiveLogs((prev) => [...prev, data.entry])
      } else {
        pushTrace(data)
      }
    } else if (name === 'done') {
      setResult(data)
      setPhase('done')
      setInterruptInfo(null)
      if (data.activity) setLiveLogs(data.activity)
      const url = new URL(location.href)
      url.searchParams.set('plan', data.plan_id)
      history.replaceState(null, '', url)
    } else if (name === 'interrupt') {
      setInterruptInfo(data)
      setPhase('interrupt')
      if (data.activity) setLiveLogs(data.activity)
    } else if (name === 'error') {
      setError(data.message)
      setPhase('error')
    }
  }

  async function resumePlan(decision: string) {
    const pid = interruptInfo?.plan_id
    if (!pid) return
    setInterruptInfo(null)
    setPhase('running')
    try {
      const res = await fetch(`/api/plan/${pid}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await consume(res, handleEvent)
      setPhase((p) => (p === 'running' ? 'done' : p))
    } catch (e) {
      setError(String(e))
      setPhase('error')
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
    setInterruptInfo(null)
    setLiveLogs([])
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
      await consume(res, handleEvent)
      setPhase((p) => (p === 'running' ? 'done' : p))
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
      const sightsOf = (it: any, d: string) =>
        (it.days.find((x: any) => x.date === d)?.items ?? [])
          .filter((i: any) => i.type === 'sight')
          .map((i: any) => i.title)
      const before = sightsOf(result.itinerary, day)
      const after = sightsOf(data.itinerary, day)
      const changed = JSON.stringify(before) !== JSON.stringify(after)
      setResult({ ...result, itinerary: data.itinerary, critique: data.critique, revision: data.revision ?? result.revision })
      setFeedback((f) => ({ ...f, [day]: '' }))
      setRefineNote((n) => ({
        ...n,
        [day]: changed
          ? data.refine_source === 'model'
            ? '已由模型理解意见后重排'
            : '已按规则重排'
          : '没有改动：当前安排已符合你的意见',
      }))
    } catch (e) {
      setError(String(e))
    } finally {
      setRefining(null)
    }
  }

  const doneCount = cards.filter((c) => c.kind === 'node' && c.status === 'done').length

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
            {phase === 'interrupt' && '等待你的决定'}
            {phase === 'done' && (result?.critique.passed ? '已完成' : '已完成 · 有未解项')}
            {phase === 'error' && '失败'}
          </div>
          <button
            className={`ghost ${drawerOpen ? 'active' : ''}`}
            onClick={() => setDrawerOpen((v) => !v)}
            disabled={liveLogs.length === 0 && phase === 'form'}
          >
            执行轨迹{liveLogs.length > 0 ? ` · ${liveLogs.length}` : ''}
          </button>
          <button className="ghost" onClick={() => setSettingsOpen(true)}>
            设置
          </button>
        </div>
      </header>

      {drawerOpen && (
        <aside className="drawer">
          <div className="drawer-head">
            <div>
              <h2>执行轨迹</h2>
              <span className="drawer-sub">
                {phase === 'running' ? '实时' : '已完成'} · {liveLogs.length} 步
              </span>
            </div>
            <button className="ghost" onClick={() => setDrawerOpen(false)}>
              收起
            </button>
          </div>
          <div className="drawer-body" ref={drawerRef}>
            {liveLogs.length === 0 ? (
              <p className="muted">提交需求后，各 agent 的讨论与工具调用会实时出现在这里。</p>
            ) : (
              <LogGroups entries={liveLogs} />
            )}
          </div>
        </aside>
      )}

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

          {interruptInfo && (
            <section className="interrupt">
              <div className="interrupt-head">
                <h3>
                  {interruptInfo.type === 'negotiation_deadlock'
                    ? '预算和体验谈不拢，需要你定方向'
                    : '预算超支，需要你决定'}
                </h3>
                <span className="interrupt-ratio">超出 {Math.round(interruptInfo.ratio * 100)}%</span>
              </div>
              {interruptInfo.type === 'negotiation_deadlock' && interruptInfo.message && (
                <p className="interrupt-body">
                  <strong>{interruptInfo.message}</strong>
                  <br />
                  当前方案总花费 <strong>{interruptInfo.total.toFixed(0)}</strong> 元，
                  预算 <strong>{interruptInfo.budget.toFixed(0)}</strong> 元。
                  你给个倾向，两位会带着这个倾向再谈一轮：
                </p>
              )}
              {interruptInfo.type !== 'negotiation_deadlock' && (
                <p className="interrupt-body">
                  当前方案总花费 <strong>{interruptInfo.total.toFixed(0)}</strong> 元，
                  预算 <strong>{interruptInfo.budget.toFixed(0)}</strong> 元，
                  超出 <strong>{interruptInfo.over.toFixed(0)}</strong> 元。
                  系统没有替你决定，请选择：
                </p>
              )}
              <div className="interrupt-actions">
                {interruptInfo.options.map((o) => (
                  <button key={o.value} onClick={() => resumePlan(o.value)}>
                    {o.label}
                  </button>
                ))}
              </div>
              {interruptInfo.activity && interruptInfo.activity.length > 0 && (
                <button className="ghost" onClick={() => setDrawerOpen(true)}>
                  查看执行轨迹（{interruptInfo.activity.length} 步）
                </button>
              )}
              {interruptInfo.negotiation && <NegotiationTrail neg={interruptInfo.negotiation} />}
            </section>
          )}

          {result?.negotiation && <NegotiationTrail neg={result.negotiation} />}

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
