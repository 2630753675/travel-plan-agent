import { useEffect, useMemo, useRef, useState } from 'react'
import SettingsPanel from './Settings'

type RunEvent = {
  schema_version: number
  run_id: string
  plan_version: number
  event_id: string
  seq: number
  occurred_at: string
  event_type: string
  stage: string
  actor: string
  parent_id: string | null
  payload: Record<string, any>
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

const TOKEN_KEY = 'trip.accessToken'

let onAuthRequired: (() => void) | null = null

/** 所有接口请求都从这里走：带上访问口令，遇到 401 就提示补录。 */
async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  const token = localStorage.getItem(TOKEN_KEY) ?? ''
  const headers = new Headers(init?.headers ?? {})
  if (token) headers.set('X-Access-Token', token)
  const res = await fetch(input, { ...init, headers })
  if (res.status === 401) onAuthRequired?.()
  return res
}

function describeError(e: unknown): string {
  if (e instanceof TypeError) {
    // fetch 抛 TypeError 基本只有一种情况：连不上后端
    return '连不上后端服务。请确认服务已启动，然后重试。'
  }
  const text = String(e)
  const http = text.match(/^HTTP (\d{3})$/)
  if (http) {
    return `后端返回 ${http[1]}，请稍后重试；若持续出现，检查设置里的模型配置。`
  }
  return text
}

type CostItem = { key: string; label: string; amount: number; share: number }

type Summary = {
  cost: { total: number; budget: number; items: CostItem[] }
  source: { kind: string; label: string; fetched_at: string; note: string }
  warning_groups: { category: string; label: string; items: string[] }[]
}

type TeamDocument = {
  title: string
  overview: string
  days: { date: string; summary: string }[]
  practical_notes: string | string[]
  approved_version?: number | null
}

type TeamTask = {
  owner?: string
  status?: string
  rework?: string[]
}

type LlmStats = {
  calls?: number
  max_calls?: number | null
  input_chars?: number
  output_chars?: number
  latency_ms?: number
  usage_available?: boolean
  usage_calls?: number
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
}

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
  events?: RunEvent[]
  activity?: LogEntry[]
  decision?: string | null
  version?: number
  summary?: Summary
  document?: TeamDocument | null
  team_tasks?: Record<string, TeamTask>
  approved_version?: number | null
  llm_stats?: LlmStats
  share?: string
}

type VersionInfo = {
  version: number
  created_at: string
  source: string
  note: string
  total_cost: number
  day_count: number
  passed: boolean
  blockers: number
}

const VERSION_SOURCE: Record<string, string> = {
  initial: '首次生成',
  refine: '单日重排',
  downgrade: '按你的选择降配',
  resume: '中断后继续',
  rollback: '回滚',
}

const COST_COLOR: Record<string, string> = {
  transport: 'var(--cost-1)',
  hotel: 'var(--cost-2)',
  meal: 'var(--cost-3)',
  sight: 'var(--cost-4)',
  other: 'var(--cost-5)',
}

function RunUsage({ stats }: { stats?: LlmStats }) {
  if (!stats || stats.calls === undefined) return null
  return (
    <div className="run-usage" role="status">
      <strong>本次模型用量</strong>
      <span>{stats.calls} 次请求{stats.max_calls ? ` / 上限 ${stats.max_calls}` : ''}</span>
      <span>{stats.usage_available
        ? `供应商报告 ${stats.total_tokens ?? 0} tokens（输入 ${stats.prompt_tokens ?? 0} / 输出 ${stats.completion_tokens ?? 0}）`
        : `供应商未返回 token usage；输入/输出 ${stats.input_chars ?? 0}/${stats.output_chars ?? 0} 字符`}</span>
      <span>模型等待 {(Math.max(0, stats.latency_ms ?? 0) / 1000).toFixed(1)} 秒</span>
    </div>
  )
}

function CostBreakdown({ summary }: { summary: Summary }) {
  const rows = summary.cost.items.filter((i) => i.amount > 0)
  if (rows.length === 0) return null
  return (
    <section className="cost">
      <div className="section-head">
        <h2>花费构成</h2>
        <span className="hint">共 {summary.cost.total.toFixed(0)} 元</span>
      </div>
      <div className="cost-bar">
        {rows.map((row) => (
          <span
            key={row.key}
            className="cost-seg"
            style={{ width: `${Math.max(row.share * 100, 1.5)}%`, background: COST_COLOR[row.key] ?? 'var(--muted)' }}
            title={`${row.label} ${row.amount.toFixed(0)} 元`}
          />
        ))}
      </div>
      <ul className="cost-legend">
        {rows.map((row) => (
          <li key={row.key}>
            <span className="cost-dot" style={{ background: COST_COLOR[row.key] ?? 'var(--muted)' }} />
            <span className="cost-label">{row.label}</span>
            <span className="cost-amount">{row.amount.toFixed(0)} 元</span>
            <span className="cost-share">{(row.share * 100).toFixed(0)}%</span>
          </li>
        ))}
      </ul>
    </section>
  )
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
  planning_mode: 'fast' | 'expert' | 'peer'
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
  planning_mode: 'fast',
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
  event_id?: string
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
  retrieval: '查询工具',
  collab: '工具协作 Workflow',
  plan: '规划 Agent',
  review: 'Agent 评审',
  peer: '专业团队协作',
  check: '硬约束校验 Workflow',
  negotiation: '预算与体验协商',
  confirm: '等你决定',
  misc: '其他',
}

const STAGE_STEPS = [
  { key: 'clarify', label: '澄清需求' },
  { key: 'retrieval', label: '并行检索' },
  { key: 'peer', label: '专业团队协作' },
  { key: 'plan', label: '排行程' },
  { key: 'check', label: '校验' },
  { key: 'negotiation', label: '协商' },
  { key: 'confirm', label: '等你决定' },
]

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

function Conversation({ events }: { events: RunEvent[] }) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const groups: { key: string; label: string; items: RunEvent[] }[] = []
  const pendingTools = new Map<string, RunEvent>()
  const messageEvents: RunEvent[] = []

  // 先把 tool_call / tool_result 配对，工具仍然是证据，不抢 Agent 发言的位置。
  const hasStructuredMessages = events.some((ev) => Boolean(ev.payload?.message_id))
  for (const ev of events) {
    if (['transport', 'lodging', 'poi', 'weather'].includes(ev.actor) && ev.event_type !== 'tool_call' && ev.event_type !== 'tool_result') continue
    if (ev.event_type === 'agent_message' && !ev.payload?.message_id) {
      // 旧 activity 投影中的查询工具行不是 Agent 消息；工具调用单独显示为证据卡片。
      if (['transport', 'lodging', 'poi', 'weather'].includes(ev.actor)) continue
      if (hasStructuredMessages) continue
    }
    if (ev.event_type === 'tool_call') {
      const id = String(ev.payload?.tool_call_id ?? ev.event_id)
      pendingTools.set(id, ev)
      continue
    }
    if (ev.event_type === 'tool_result') {
      const id = String(ev.payload?.tool_call_id ?? '')
      const call = pendingTools.get(id)
      if (call) {
        call.payload = { ...call.payload, _result: ev.payload, _resultEventId: ev.event_id }
        pendingTools.delete(id)
        messageEvents.push(call)
      } else {
        messageEvents.push(ev)
      }
      continue
    }
    if (ev.event_type === 'tool_exchange') continue
    if (ev.event_type === 'tool_call') {
      messageEvents.push(ev)
      continue
    }
    if (['agent_message', 'route_decision', 'validation_result', 'user_question', 'user_answer', 'agent_resumed'].includes(ev.event_type)) {
      messageEvents.push(ev)
    }
  }

  for (const ev of messageEvents) {
    const key = ev.stage || 'misc'
    const last = groups[groups.length - 1]
    if (last?.key === key) last.items.push(ev)
    else groups.push({ key, label: GROUP_LABEL[key] ?? key, items: [ev] })
  }

  const actorLabel = (actor: string) => ACTOR_LABEL[actor] ?? actor
  const actorClass = (actor: string) => actor.replace(/[^a-zA-Z0-9_-]/g, '-')
  const messageText = (ev: RunEvent) => {
    const p = ev.payload ?? {}
    if (ev.event_type === 'tool_call') {
      return `工具调用：${String(p.tool_name ?? '查询工具')}`
    }
    if (ev.event_type === 'validation_result') {
      const issues = Array.isArray(p.issues) ? p.issues : []
      const blockers = issues.filter((i: any) => i?.severity === 'blocker').length
      return p.passed ? `检查通过${issues.length ? `，有 ${issues.length} 条提醒` : ''}` : `检查未通过，需要处理 ${blockers} 个问题`
    }
    if (ev.event_type === 'agent_resumed') return '我已经从中断处继续处理。'
    return String(p.text ?? p.message ?? p.summary ?? '')
  }
  const messageKind = (ev: RunEvent) => {
    if (ev.event_type === 'tool_call') return 'tool'
    if (ev.event_type === 'user_question') return 'question'
    if (ev.event_type === 'user_answer') return 'answer'
    if (ev.event_type === 'validation_result' || ev.event_type === 'route_decision') return 'decision'
    return 'speech'
  }

  return (
    <div className="conversation">
      {groups.map((group, gi) => {
        const groupId = `${group.key}-${gi}`
        const isCollapsed = collapsed[groupId]
        return (
          <section key={groupId} className={`conversation-group ${isCollapsed ? 'collapsed' : ''}`}>
            <button
              className="conversation-stage"
              onClick={() => setCollapsed((current) => ({ ...current, [groupId]: !current[groupId] }))}
            >
              <span>{isCollapsed ? '▶' : '▼'}</span>
              <strong>{group.label}</strong>
              <span className="conversation-count">{group.items.length} 条消息</span>
            </button>
            {!isCollapsed && (
              <div className="conversation-messages">
                {group.items.map((ev) => {
                  const tools = events.filter((candidate) => candidate.event_type === 'tool_call' && candidate.parent_id === ev.event_id)
                  const orphanTools = tools.length === 0 && ev.event_type === 'agent_message'
                    ? events.filter((candidate) => candidate.event_type === 'tool_call' && candidate.actor === ev.actor && candidate.seq < ev.seq && ev.seq - candidate.seq <= 3)
                    : []
                  const evidence = ev.event_type === 'tool_call'
                    ? [ev]
                    : tools.length
                      ? tools
                      : orphanTools
                  const evidenceKey = `${ev.event_id}-evidence`
                  const evidenceOpen = collapsed[evidenceKey]
                  return (
                    <article key={ev.event_id} className={`conversation-message ${actorClass(ev.actor)} kind-${messageKind(ev)}`}>
                      <div className="message-avatar">{actorLabel(ev.actor).slice(0, 1)}</div>
                      <div className="message-main">
                        <div className="message-meta">
                          <strong>{actorLabel(ev.actor)}</strong>
                          <span>{fmtClock(ev.occurred_at)}</span>
                          {(() => {
                            const recipients = Array.isArray(ev.payload?.recipients)
                              ? ev.payload.recipients
                              : ev.payload?.receiver ? [ev.payload.receiver] : []
                            const names = recipients.filter((recipient: unknown): recipient is string => typeof recipient === 'string')
                            return names.length > 0 && <span className="message-route">→ {names.map(actorLabel).join('、')}</span>
                          })()}
                          {ev.event_type === 'route_decision' && <em>做了决定</em>}
                        </div>
                        <div className="message-bubble">{messageText(ev) || '已完成这一项处理。'}</div>
                        {ev.payload?.reason && <div className="message-reason">依据：{String(ev.payload.reason)}</div>}
                        {evidence.length > 0 && (
                          <div className="evidence">
                            <button
                              className="evidence-toggle"
                              onClick={() => setCollapsed((current) => ({ ...current, [evidenceKey]: !current[evidenceKey] }))}
                            >
                              {evidenceOpen ? '收起依据' : '查看依据'} · {evidence.length} 次工具调用
                            </button>
                            {evidenceOpen && (
                              <div className="evidence-body">
                                {evidence.map((tool) => {
                                  const result = tool.payload?._result
                                  return (
                                    <div key={tool.event_id} className="evidence-row">
                                      <span className="evidence-tool">{String(tool.payload?.tool_name ?? '工具')}</span>
                                      <span>{result?.summary ?? result?.status ?? '已执行'}</span>
                                    </div>
                                  )
                                })}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    </article>
                  )
                })}
              </div>
            )}
          </section>
        )
      })}
    </div>
  )
}

function ActivityLog({ entries, events }: { entries: LogEntry[]; events: RunEvent[] }) {
  if ((!entries || entries.length === 0) && (!events || events.length === 0)) return null
  return (
    <section className="activity">
      <div className="section-head">
        <h2>Agent 对话</h2>
        <span className="hint">{events.length || entries.length} 条 · 可展开查看依据</span>
      </div>
      {events.length > 0 ? <Conversation events={events} /> : <LogGroups entries={entries} />}
    </section>
  )
}

const ACTOR_LABEL: Record<string, string> = {
  system: '系统',
  clarify: '需求澄清',
  transport: '航班查询工具',
  lodging: '酒店查询工具',
  poi: '景点查询工具',
  weather: '天气查询工具',
  planner: '规划 Agent',
  critic: '硬约束校验 Workflow',
  budget_review: '预算评审 Agent',
  experience_review: '体验评审 Agent',
  budget_reviewer: '预算评审 Agent',
  experience_reviewer: '体验评审 Agent',
  intake: '需求分析 Agent',
  flight_scout: '航班侦察 Agent',
  hotel_scout: '酒店侦察 Agent',
  sight_scout: '景点侦察 Agent',
  weather_scout: '天气侦察 Agent',
  budget_critic: '预算评审 Agent',
  experience_critic: '体验评审 Agent',
  orchestrator: '团队统筹',
  logistics: '交通住宿专家',
  local_expert: '当地体验专家',
  critic_agent: '方案评审',
  writer: '行程撰稿',
  team_protocol: '协作协议',
  peer_run: '专业团队协作',
  peer_ask: '请示你',
  peer_finalize: '硬约束校验',
  negotiation: '预算与体验协商',
  budget_officer: '预算评审 Agent',
  experience_officer: '体验评审 Agent',
  mediator: '规则裁决',
  confirm: '等待确认',
  ask: '等待确认',
  user: '你',
}

function fmtArgs(args: unknown): string {
  if (!args || typeof args !== 'object') return ''
  const entries = Object.entries(args as Record<string, unknown>)
  if (entries.length === 0) return ''
  return entries.map(([k, v]) => `${k}=${String(v)}`).join(', ')
}

function fmtClock(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}

/** 把统一事件投影成展示行；只影响渲染，不改变存储与回放的原事件。 */
function eventToLogEntry(ev: RunEvent): LogEntry | null {
  const p = ev.payload ?? {}
  const base: Omit<LogEntry, 'kind' | 'text' | 'depth'> = {
    event_id: ev.event_id,
    ts: (p.ts as string) ?? fmtClock(ev.occurred_at),
    actor: ACTOR_LABEL[ev.actor] ?? ev.actor,
    group: ev.stage,
    detail: '',
    tool: '',
  }
  switch (ev.event_type) {
    case 'stage_started':
      return { ...base, kind: 'stage', text: p.text ?? `阶段: ${GROUP_LABEL[ev.stage] ?? ev.stage}`, depth: 0 }
    case 'agent_started':
      // 每个 agent 的动作行已经说明了它在做什么，单独一条"开始"是噪音
      return null
    case 'agent_resumed':
      // 恢复是值得看见的：同一个节点在中断后继续跑
      return { ...base, kind: 'action', text: `${base.actor}继续执行（中断后恢复）`, depth: 1 }
    case 'agent_message':
    case 'route_decision':
      return {
        ...base,
        kind: (p.kind as string) ?? (ev.event_type === 'route_decision' ? 'decision' : 'action'),
        text: p.text ?? '',
        depth: (p.depth as number) ?? 1,
      }
    case 'user_question':
      return { ...base, kind: 'question', text: p.text ?? '', depth: 1 }
    case 'user_answer':
      return { ...base, kind: 'answer', text: p.text ?? '', depth: 1 }
    case 'tool_call':
      return {
        ...base,
        kind: 'tool',
        text: `${p.tool_name}(${fmtArgs(p.arguments)})`,
        depth: 2,
        tool: String(p.tool_name ?? ''),
      }
    case 'tool_result':
      return {
        ...base,
        kind: 'tool',
        text: `→ ${p.summary ?? p.status ?? ''}`,
        depth: 2,
        tool: String(p.tool_name ?? ''),
      }
    case 'tool_exchange':
      return { ...base, kind: 'tool', text: p.text ?? '', depth: 2, tool: String(p.tool ?? '') }
    case 'agent_completed':
      // 节点的具体动作已由日志行记录，这里不再重复一条摘要
      return null
    case 'validation_result': {
      const issues = Array.isArray(p.issues) ? p.issues : []
      const blockers = issues.filter((i: any) => i?.severity === 'blocker')
      return {
        ...base,
        kind: 'decision',
        text: p.passed ? `校验通过（${issues.length} 条提醒）` : `校验未通过，${blockers.length} 个阻断项`,
        depth: 1,
      }
    }
    default:
      return null
  }
}

function projectEvents(events: RunEvent[]): LogEntry[] {
  const rows: LogEntry[] = []
  for (const ev of events) {
    const row = eventToLogEntry(ev)
    if (row) rows.push(row)
  }
  return rows
}

/** 历史记录只有 activity 投影；这里把它还原成同一套事件，保证回放视图一致。 */
function activityToEvent(row: LogEntry, seq: number): RunEvent {
  const type =
    row.kind === 'stage'
      ? 'stage_started'
      : row.kind === 'tool'
        ? 'tool_exchange'
        : row.kind === 'question'
          ? 'user_question'
          : row.kind === 'answer'
            ? 'user_answer'
            : row.kind === 'decision'
              ? 'route_decision'
              : 'agent_message'
  return {
    schema_version: 1,
    run_id: 'legacy',
    plan_version: 1,
    event_id: `legacy-${seq}-${row.ts}-${row.text.slice(0, 8)}`,
    seq,
    occurred_at: new Date().toISOString(),
    event_type: type,
    stage: row.group,
    actor: row.actor,
    parent_id: null,
    payload: { ts: row.ts, text: row.text, detail: row.detail, kind: row.kind, depth: row.depth, tool: row.tool },
  }
}

function activityToEvents(activity: LogEntry[]): RunEvent[] {
  return activity.map((row, i) => activityToEvent(row, i + 1))
}

type InterruptOption = { value: string; label: string }

type InterruptInfo = {
  type: string
  message?: string
  question?: string
  from?: string
  total?: number
  budget?: number
  over?: number
  ratio?: number
  options: InterruptOption[]
  plan_id: string
  negotiation?: Negotiation | null
  events?: RunEvent[]
  activity?: LogEntry[]
}

export default function App() {
  const [form, setForm] = useState<Form>(DEFAULT_FORM)
  const [phase, setPhase] = useState<'form' | 'running' | 'interrupt' | 'done' | 'error' | 'stopped'>('form')
  const [result, setResult] = useState<Result | null>(null)
  const [interruptInfo, setInterruptInfo] = useState<InterruptInfo | null>(null)
  const [error, setError] = useState('')
  const [errorStats, setErrorStats] = useState<LlmStats | undefined>()
  const [recoveryStatus, setRecoveryStatus] = useState<string | undefined>()
  const [refining, setRefining] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Record<string, string>>({})
  const [refineNote, setRefineNote] = useState<Record<string, string>>({})
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [agentMode, setAgentMode] = useState<'fast' | 'expert' | 'peer'>('fast')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [liveEvents, setLiveEvents] = useState<RunEvent[]>([])
  const [versions, setVersions] = useState<VersionInfo[]>([])
  const [needToken, setNeedToken] = useState(false)
  const [tokenDraft, setTokenDraft] = useState('')
  const drawerRef = useRef<HTMLDivElement>(null)
  const liveLogs = useMemo(() => projectEvents(liveEvents), [liveEvents])

  useEffect(() => {
    onAuthRequired = () => setNeedToken(true)
    return () => {
      onAuthRequired = null
    }
  }, [])

  useEffect(() => {
    apiFetch('/api/config')
      .then((r) => r.json())
      .then((c) =>
        setAgentMode(
          (c?.orchestration?.agent_mode ?? 'fast') as 'fast' | 'expert' | 'peer',
        ),
      )
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    const params = new URLSearchParams(location.search)
    const id = params.get('plan')
    if (!id) return
    // 分享链接带签名，外部访问时不需要总口令
    const share = params.get('share')
    const url = share ? `/api/plan/${id}?share=${encodeURIComponent(share)}` : `/api/plan/${id}`
    apiFetch(url)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('not found'))))
      .catch(() => {
        if (share) return Promise.reject(new Error('not found'))
        return apiFetch(`/api/run/${id}`).then((r) => (r.ok ? r.json() : Promise.reject(new Error('not found'))))
      })
      .then((rec) => {
        if (rec?.status === 'interrupted' || rec?.status === 'failed' || rec?.status === 'failed_non_resumable' || rec?.status === 'checkpoint_lost' || rec?.status === 'needs_input') {
          const payload = rec.payload ?? {}
          setErrorStats(payload.llm_stats as LlmStats | undefined)
          setRecoveryStatus(payload.recovery_status)
          if (rec.status === 'interrupted') {
            const info = { ...(payload.interrupt ?? {}), plan_id: rec.run_id } as InterruptInfo
            setInterruptInfo(info)
            setPhase('interrupt')
            if (Array.isArray(info.activity)) setLiveEvents(activityToEvents(info.activity))
          } else {
            const text = [
              String(payload.message ?? '规划失败'),
              ...(Array.isArray(payload.blockers) ? payload.blockers : []),
              ...(Array.isArray(payload.reasons) ? payload.reasons : []),
              ...(Array.isArray(payload.issues) ? payload.issues.map((i: any) => i?.message ?? '') : []),
            ].filter(Boolean).join('\n')
            setError(text)
            setPhase(rec.status === 'needs_input' ? 'stopped' : 'error')
          }
          return
        }
        setResult({
          plan_id: rec.id,
          brief: rec.brief,
          itinerary: rec.itinerary,
          critique: rec.critique,
          revision: rec.revision ?? 0,
          errors: [],
          negotiation: rec.negotiation ?? null,
          version: rec.version,
          share: share ?? undefined,
          document: rec.document,
          team_tasks: rec.team_tasks,
          approved_version: rec.approved_version,
          llm_stats: rec.llm_stats,
        })
        // 新数据走统一事件；旧记录回落到 activity 投影，保证分享链接仍可打开。
        if (Array.isArray(rec.events) && rec.events.length > 0) {
          setLiveEvents(rec.events)
        } else if (Array.isArray(rec.activity)) {
          setLiveEvents(activityToEvents(rec.activity))
        }
        setPhase('done')
        void loadVersions(rec.id)
      })
      .catch(() => setError('该规划不存在或已失效'))
  }, [])

  // 轨迹面板跟随最新一条自动滚动
  useEffect(() => {
    if (!drawerOpen) return
    drawerRef.current?.scrollTo({ top: drawerRef.current.scrollHeight, behavior: 'smooth' })
  }, [liveLogs, drawerOpen])

  function appendEvent(ev: RunEvent) {
    setLiveEvents((prev) => (prev.some((e) => e.event_id === ev.event_id) ? prev : [...prev, ev]))
  }

  function handleEvent(name: string, data: any) {
    if (name === 'run_event') {
      const ev = data as RunEvent
      appendEvent(ev)
      if (ev.event_type === 'run_completed') {
        const payload = ev.payload?.result as Result | undefined
        if (payload) {
          setResult(payload)
          setPhase('done')
          setInterruptInfo(null)
          void loadVersions(payload.plan_id)
          const url = new URL(location.href)
          url.searchParams.set('plan', payload.plan_id)
          history.replaceState(null, '', url)
        }
      } else if (ev.event_type === 'run_interrupted') {
        setInterruptInfo((ev.payload?.interrupt ?? null) as InterruptInfo | null)
        setErrorStats((ev.payload?.interrupt?.llm_stats ?? undefined) as LlmStats | undefined)
        setRecoveryStatus('resumable')
        setPhase('interrupt')
        const pid = ev.payload?.plan_id ?? ev.payload?.interrupt?.plan_id
        if (pid) {
          const url = new URL(location.href)
          url.searchParams.set('plan', pid)
          history.replaceState(null, '', url)
        }
      } else if (ev.event_type === 'run_needs_input') {
        setErrorStats(ev.payload?.llm_stats as LlmStats | undefined)
        setRecoveryStatus('stopped')
        const reason = [
          String(ev.payload?.message ?? '本次运行未完成'),
          ...(Array.isArray(ev.payload?.issues) ? ev.payload.issues.map((i: any) => i?.message ?? '') : []),
        ].filter(Boolean).join('\n')
        setError(reason)
        setPhase('stopped')
        const pid = ev.payload?.plan_id
        if (pid) {
          const url = new URL(location.href)
          url.searchParams.set('plan', pid)
          history.replaceState(null, '', url)
        }
      } else if (ev.event_type === 'run_failed') {
        setErrorStats(ev.payload?.llm_stats as LlmStats | undefined)
        setRecoveryStatus(ev.payload?.recovery_status)
        const reason = [
          String(ev.payload?.message ?? '规划失败'),
          ...(Array.isArray(ev.payload?.blockers) ? ev.payload.blockers : []),
          ...(Array.isArray(ev.payload?.reasons) ? ev.payload.reasons : []),
        ].filter(Boolean).join('\n')
        setError(reason)
        setPhase('error')
        const pid = ev.payload?.plan_id
        if (pid) {
          const url = new URL(location.href)
          url.searchParams.set('plan', pid)
          history.replaceState(null, '', url)
        }
      }
      return
    }

    // 兼容旧服务端（trace/done/interrupt/error）
    if (name === 'trace') {
      if (data.phase === 'log' && data.entry) {
        setLiveEvents((prev) => [...prev, activityToEvent(data.entry, prev.length + 1)])
      }
    } else if (name === 'done') {
      setResult(data)
      setPhase('done')
      setInterruptInfo(null)
      if (Array.isArray(data.activity)) setLiveEvents(activityToEvents(data.activity))
      const url = new URL(location.href)
      url.searchParams.set('plan', data.plan_id)
      history.replaceState(null, '', url)
    } else if (name === 'interrupt') {
      setInterruptInfo(data)
      setPhase('interrupt')
      if (Array.isArray(data.activity)) setLiveEvents(activityToEvents(data.activity))
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
      const res = await apiFetch(`/api/plan/${pid}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          decision,
          kind: interruptInfo?.type === 'peer_escalation' ? 'peer_review_risk' : interruptInfo?.type,
          accepted_issue_codes: decision === 'accept_risk' ? ['REVIEW_RISK_ACCEPTED'] : [],
          note: decision === 'accept_risk' ? '用户确认接受当前评审风险' : '',
        }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await consume(res, handleEvent)
      setPhase((p) => (p === 'running' ? 'done' : p))
    } catch (e) {
      setError(describeError(e))
      setPhase('error')
    }
  }

  async function runPlan() {
    setPhase('running')
    setResult(null)
    setInterruptInfo(null)
    setLiveEvents([])
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
      planning_mode: form.planning_mode,
    }
    try {
      const res = await apiFetch('/api/plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await consume(res, handleEvent)
      setPhase((p) => (p === 'running' ? 'done' : p))
    } catch (e) {
      setError(describeError(e))
      setPhase('error')
    }
  }

  async function refine(day: string) {
    if (!result) return
    setRefining(day)
    try {
      const res = await apiFetch(`/api/plan/${result.plan_id}/refine`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date: day, feedback: feedback[day] ?? '' }),
      })
      if (!res.ok) {
        const detail = await res.json().catch(() => null)
        throw new Error(typeof detail?.detail === 'string' ? detail.detail : `HTTP ${res.status}`)
      }
      const data = await res.json()
      const sightsOf = (it: any, d: string) =>
        (it.days.find((x: any) => x.date === d)?.items ?? [])
          .filter((i: any) => i.type === 'sight')
          .map((i: any) => i.title)
      const before = sightsOf(result.itinerary, day)
      const after = sightsOf(data.itinerary, day)
      const changed = JSON.stringify(before) !== JSON.stringify(after)
      setResult({
        ...result,
        itinerary: data.itinerary,
        critique: data.critique,
        revision: data.revision ?? result.revision,
        version: data.version ?? result.version,
      })
      await loadVersions(result.plan_id)
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
      setError(describeError(e))
    } finally {
      setRefining(null)
    }
  }

  async function loadVersions(pid: string) {
    try {
      const r = await apiFetch(`/api/plan/${pid}/versions`)
      if (!r.ok) return
      const data = await r.json()
      setVersions(data.versions ?? [])
    } catch {
      // 版本面板拿不到不影响主流程
    }
  }

  async function rollback(version: number) {
    if (!result) return
    try {
      const r = await apiFetch(`/api/plan/${result.plan_id}/rollback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const data = await r.json()
      setResult({
        ...result,
        itinerary: data.itinerary,
        critique: data.critique,
        version: data.version,
      })
      await loadVersions(result.plan_id)
    } catch (e) {
      setError(describeError(e))
    }
  }

  const lastStageKey = useMemo(() => {
    for (let i = liveEvents.length - 1; i >= 0; i--) {
      if (liveEvents[i].stage) return liveEvents[i].stage
    }
    return ''
  }, [liveEvents])
  const runningStage = lastStageKey ? `正在${GROUP_LABEL[lastStageKey] ?? lastStageKey}` : '正在启动'
  const stageReached = (key: string) => liveEvents.some((e) => e.stage === key)

  const doneCount = new Set(
    liveEvents.filter((e) => e.event_type === 'agent_completed').map((e) => e.actor),
  ).size

  return (
    <div className="shell">
      {needToken && (
        <div className="token-bar">
          <span>这个服务需要访问口令才能使用。</span>
          <input
            type="password"
            placeholder="粘贴访问口令"
            value={tokenDraft}
            onChange={(e) => setTokenDraft(e.target.value)}
          />
          <button
            className="ghost"
            onClick={() => {
              localStorage.setItem(TOKEN_KEY, tokenDraft.trim())
              setNeedToken(false)
              location.reload()
            }}
            disabled={!tokenDraft.trim()}
          >
            使用
          </button>
        </div>
      )}
      <header className="topbar">
        <div>
          <h1>旅行规划多 Agent 编排</h1>
          <p className="sub">LangGraph 编排 · 四路并行检索 · 校验回退 · 单日重排</p>
        </div>
        <div className="topbar-right">
          <span className="mode-pill">{form.planning_mode === 'peer' ? '深度团队协作 · 高成本' : form.planning_mode === 'expert' ? '专家模式 · 最多 3 次模型请求' : '快速规划 · 默认最多 1 次模型请求'}</span>
          <div className="status-pill">
            {phase === 'form' && '待提交'}
            {phase === 'running' && `执行中 ${doneCount}/7`}
            {phase === 'interrupt' && '等待你的决定'}
            {phase === 'done' && (result?.critique.passed ? '已完成' : '已完成 · 有未解项')}
            {phase === 'stopped' && '未完成 · 待调整需求'}
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
              <Conversation events={liveEvents} />
            )}
          </div>
        </aside>
      )}

      {settingsOpen && (
        <SettingsPanel
          onClose={() => setSettingsOpen(false)}
          onSaved={(s) =>
            setAgentMode((s.orchestration?.agent_mode ?? 'fast') as 'fast' | 'expert' | 'peer')
          }
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
            <span className="chip-label">规划模式</span>
            {([
              ['fast', '快速', '默认，少量模型调用'],
              ['expert', '专家', '复杂约束时使用'],
              ['peer', '深度团队', '高成本，显式启用'],
            ] as const).map(([mode, label, title]) => (
              <button
                key={mode}
                title={title}
                className={`chip ${form.planning_mode === mode ? 'on' : ''}`}
                disabled={phase === 'running'}
                onClick={() => setForm({ ...form, planning_mode: mode })}
              >
                {label}
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

          <button className="primary" onClick={runPlan} disabled={phase === 'running' || needToken}>
            {needToken ? '先输入访问口令' : phase === 'running' ? '规划中…' : phase === 'form' ? '开始规划' : '重新规划'}
          </button>
        </aside>

        <main className="main">
          {error && (
            <div className="alert">
              <span>{error}</span>
              {phase === 'stopped' ? (
                <button
                  className="ghost"
                  onClick={() => {
                    setPhase('form')
                    setError('')
                    setErrorStats(undefined)
                    setRecoveryStatus(undefined)
                    setInterruptInfo(null)
                  }}
                >
                  调整需求后重新规划
                </button>
              ) : recoveryStatus !== 'resumable' ? (
                <button className="ghost" onClick={runPlan} disabled={phase === 'running'}>
                  用同样的需求重试
                </button>
              ) : (
                <span>当前运行仍可从检查点恢复。</span>
              )}
              <RunUsage stats={errorStats} />
            </div>
          )}

          {phase === 'running' && !result && !interruptInfo && (
            <section className="running">
              <div className="running-head">
                <span className="spinner" />
                <strong>{runningStage}</strong>
              </div>
              <p className="muted">
                已产出 {liveLogs.length} 条执行记录，可以随时打开右侧「执行轨迹」查看细节。
              </p>
              <div className="running-steps">
                {STAGE_STEPS.map((step) => (
                  <span key={step.key} className={`running-step ${stageReached(step.key) ? 'done' : ''}`}>
                    {step.label}
                  </span>
                ))}
              </div>
            </section>
          )}

          {interruptInfo && (
            <section className="interrupt">
              <div className="interrupt-head">
                <h3>
                  {interruptInfo.type === 'agent_turn_failed'
                    ? 'Agent 回合需要重试'
                    : interruptInfo.type === 'model_call_budget_exhausted'
                      ? '模型请求额度已用尽'
                      : interruptInfo.type === 'hard_constraint_unresolved'
                        ? '硬约束尚未解决'
                        : interruptInfo.type === 'peer_escalation'
                          ? 'Agent 团队需要你确认'
                          : interruptInfo.type === 'negotiation_deadlock'
                            ? '预算和体验谈不拢，需要你定方向'
                            : '预算超支，需要你决定'}
                </h3>
                {typeof interruptInfo.ratio === 'number' && (
                  <span className="interrupt-ratio">超出 {Math.round(interruptInfo.ratio * 100)}%</span>
                )}
              </div>
              {['peer_escalation', 'agent_turn_failed', 'model_call_budget_exhausted', 'hard_constraint_unresolved'].includes(interruptInfo.type) && (
                <p className="interrupt-body">
                  <strong>{interruptInfo.question ?? 'Agent 团队遇到需要你决定的问题'}</strong>
                  {interruptInfo.from && (
                    <>
                      <br />
                      提出方：{ACTOR_LABEL[interruptInfo.from] ?? interruptInfo.from}
                    </>
                  )}
                </p>
              )}
              {interruptInfo.type === 'negotiation_deadlock' && interruptInfo.message && (
                <p className="interrupt-body">
                  <strong>{interruptInfo.message}</strong>
                  <br />
                  当前方案总花费 <strong>{(interruptInfo.total ?? 0).toFixed(0)}</strong> 元，
                  预算 <strong>{(interruptInfo.budget ?? 0).toFixed(0)}</strong> 元。
                  你给个倾向，两位会带着这个倾向再谈一轮：
                </p>
              )}
              {!['negotiation_deadlock', 'peer_escalation', 'agent_turn_failed', 'model_call_budget_exhausted', 'hard_constraint_unresolved'].includes(interruptInfo.type) && (
                <p className="interrupt-body">
                  当前方案总花费 <strong>{(interruptInfo.total ?? 0).toFixed(0)}</strong> 元，
                  预算 <strong>{(interruptInfo.budget ?? 0).toFixed(0)}</strong> 元，
                  超出 <strong>{(interruptInfo.over ?? 0).toFixed(0)}</strong> 元。
                  系统没有替你决定，请选择：
                </p>
              )}
              <div className="interrupt-actions">
                {(interruptInfo.options ?? []).map((o) => (
                  <button key={o.value} onClick={() => resumePlan(o.value)}>
                    {o.label}
                  </button>
                ))}
              </div>
              {liveLogs.length > 0 && (
                <button className="ghost" onClick={() => setDrawerOpen(true)}>
                  查看执行轨迹（{liveLogs.length} 步）
                </button>
              )}
              {interruptInfo.negotiation && <NegotiationTrail neg={interruptInfo.negotiation} />}
            </section>
          )}

          {result?.negotiation && <NegotiationTrail neg={result.negotiation} />}

          {result && (
            <section className="result">
              {result.document && (
                <section className="team-document">
                  <div className="section-head">
                    <h2>{typeof result.document.title === 'string' ? result.document.title : '旅行方案'}</h2>
                    {(result.document.approved_version ?? result.approved_version) != null && (
                      <span className="hint">审核版本 v{result.document.approved_version ?? result.approved_version}</span>
                    )}
                  </div>
                  {typeof result.document.overview === 'string' && <p>{result.document.overview}</p>}
                  {Array.isArray(result.document.days) && result.document.days.map((day, index) => (
                    <div key={index}>
                      {typeof day?.date === 'string' && <h3>{day.date}</h3>}
                      {typeof day?.summary === 'string' && <p>{day.summary}</p>}
                    </div>
                  ))}
                  {Array.isArray(result.document.practical_notes) ? (
                    <ul>{result.document.practical_notes.filter((note) => typeof note === 'string').map((note, index) => <li key={index}>{note}</li>)}</ul>
                  ) : typeof result.document.practical_notes === 'string' && <p>{result.document.practical_notes}</p>}
                </section>
              )}
              {result.team_tasks && Object.keys(result.team_tasks).length > 0 && (
                <section className="team-tasks">
                  <h2>团队任务</h2>
                  <table>
                    <thead><tr><th scope="col">任务</th><th scope="col">负责人</th><th scope="col">状态</th><th scope="col">返工次数</th></tr></thead>
                    <tbody>
                      {Object.entries(result.team_tasks).map(([id, task]) => (
                        <tr key={id}>
                          <th scope="row">{id}</th>
                          <td>{typeof task?.owner === 'string' ? ACTOR_LABEL[task.owner] ?? task.owner : '—'}</td>
                          <td>{typeof task?.status === 'string' ? ({ pending: '待处理', running: '处理中', delivered: '已交付' } as Record<string, string>)[task.status] ?? task.status : '—'}</td>
                          <td>{Array.isArray(task?.rework) ? task.rework.length : 0}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </section>
              )}
                      <RunUsage stats={result.llm_stats} />
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
                  onClick={() => {
                    const url = result.share
                      ? `${location.origin}/?plan=${result.plan_id}&share=${encodeURIComponent(result.share)}`
                      : location.href
                    navigator.clipboard.writeText(url)
                  }}
                  title="复制只读分享链接（不需要访问口令）"
                >
                  复制链接
                </button>
              </div>

              {result.summary && result.summary.source && (
                <p className="source-line">
                  <span className={`source-pill ${result.summary.source.kind}`}>
                    {result.summary.source.label}
                  </span>
                  {result.summary.source.note}
                </p>
              )}

              {result.summary?.cost && <CostBreakdown summary={result.summary} />}

              {result.summary && result.summary.warning_groups.length > 0 ? (
                <section className="notice-groups">
                  {result.summary.warning_groups.map((g) => (
                    <div key={g.category} className="notice-group">
                      <span className="notice-tag">{g.label}</span>
                      <ul>
                        {g.items.map((item, i) => (
                          <li key={i}>{item}</li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </section>
              ) : (
                result.itinerary.warnings.length > 0 && (
                  <ul className="warnings">
                    {result.itinerary.warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                )
              )}

              {versions.length > 0 && (
                <section className="versions">
                  <div className="section-head">
                    <h2>版本</h2>
                    <span className="hint">
                      当前 v{result.version ?? versions[versions.length - 1].version} · 共 {versions.length} 个
                    </span>
                  </div>
                  <ul className="version-list">
                    {versions.map((v) => {
                      const isCurrent = v.version === (result.version ?? -1)
                      return (
                        <li key={v.version} className={`version-row ${isCurrent ? 'current' : ''}`}>
                          <span className="version-tag">v{v.version}</span>
                          <span className="version-source">{VERSION_SOURCE[v.source] ?? v.source}</span>
                          <span className="version-note">{v.note || '—'}</span>
                          <span className="version-cost">{v.total_cost.toFixed(0)} 元</span>
                          {isCurrent ? (
                            <span className="version-current">当前</span>
                          ) : (
                            <button className="ghost" onClick={() => rollback(v.version)}>
                              回到这一版
                            </button>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                </section>
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

          {phase === 'form' && liveEvents.length === 0 && (
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
