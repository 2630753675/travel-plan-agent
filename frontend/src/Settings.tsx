import { useEffect, useState } from 'react'

export type LLMSettings = {
  enabled: boolean
  provider: string
  base_url: string
  model: string
  api_key: string
  api_key_set?: boolean
  temperature: number
}

type Orchestration = {
  max_revisions: number
  node_timeout_s: number
  max_runtime_s: number
  max_model_calls: number
  max_agent_turns: number
  max_output_tokens: number | null
  token_budget: number
}

type DataSource = {
  kind: 'mock' | 'http'
  http_base_url: string
}

type Settings = {
  llm: LLMSettings
  orchestration: Orchestration
  datasource: DataSource
}

type RunMetric = {
  run_id: string
  status: string
  created_at: string
  duration_ms: number
  calls: number
  total_tokens: number
  latency_ms: number
  passed: boolean | null
  total_cost: number | null
  version: number | null
  revision: number | null
  failure_type: string
  message: string
}

type Props = {
  onClose: () => void
  onSaved: (s: Settings) => void
}

export default function SettingsPanel({ onClose, onSaved }: Props) {
  const [s, setS] = useState<Settings | null>(null)
  const [keyInput, setKeyInput] = useState('')
  const [busy, setBusy] = useState<'load' | 'save' | 'test' | null>('load')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [runs, setRuns] = useState<RunMetric[] | null>(null)
  const [runsError, setRunsError] = useState('')

  // 组件由父级条件挂载：每次打开都是新实例，首屏即为"读取中"，
  // 不存在旧值先渲染再被覆盖的窗口期。
  useEffect(() => {
    let alive = true
    fetch('/api/config')
      .then((r) => r.json())
      .then((data) => {
        if (alive) setS(data)
      })
      .catch(() => {
        if (alive) setMsg({ ok: false, text: '读取配置失败' })
      })
      .finally(() => {
        if (alive) setBusy(null)
      })
    return () => {
      alive = false
    }
  }, [])

  useEffect(() => {
    let alive = true
    const token = localStorage.getItem('trip.accessToken')
    fetch('/api/metrics/runs?limit=10', {
      headers: token ? { 'X-Access-Token': token } : {},
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((data) => {
        if (alive) setRuns(Array.isArray(data?.runs) ? data.runs : [])
      })
      .catch((e) => {
        if (alive) setRunsError(String(e))
      })
    return () => {
      alive = false
    }
  }, [])

  function patch<K extends keyof Settings>(section: K, value: Partial<Settings[K]>) {
    setS((prev) => (prev ? { ...prev, [section]: { ...prev[section], ...value } } : prev))
  }

  /** 用户没改 key 就回传掩码，后端据此保留原值 */
  function payload() {
    if (!s) return null
    return {
      ...s,
      llm: { ...s.llm, api_key: keyInput || s.llm.api_key },
    }
  }

  async function save() {
    const body = payload()
    if (!body) return
    setBusy('save')
    setMsg(null)
    try {
      const res = await fetch('/api/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
      setS(data)
      setKeyInput('')
      onSaved(data)
      setMsg({ ok: true, text: '已保存，下次规划立即生效' })
    } catch (e) {
      setMsg({ ok: false, text: `保存失败：${String(e)}` })
    } finally {
      setBusy(null)
    }
  }

  async function test() {
    const body = payload()
    if (!body) return
    setBusy('test')
    setMsg(null)
    try {
      const res = await fetch('/api/config/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ llm: body.llm }),
      })
      const data = await res.json()
      if (data.ok && !body.llm.enabled) {
        setMsg({
          ok: true,
          text: `连接正常：${data.reply}。若要用于规划，请勾选「启用模型」并保存。`,
        })
      } else {
        setMsg(
          data.ok
            ? { ok: true, text: `连接成功：${data.reply}` }
            : { ok: false, text: `连接失败：${data.error ?? '未知错误'}` },
        )
      }
    } catch (e) {
      setMsg({ ok: false, text: `请求失败：${String(e)}` })
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>设置</h2>
          <button className="ghost" onClick={onClose}>
            关闭
          </button>
        </header>

        {!s && <p className="muted">读取中…</p>}

        {s && (
          <>
            <section className="cfg">
              <h3>模型连接</h3>
              <p className="cfg-hint">
                未启用时用内置规则引擎生成行程；启用后由模型决定每天去哪些景点，硬约束仍由规则校验。
              </p>
              <label className="check">
                <input
                  type="checkbox"
                  checked={s.llm.enabled}
                  onChange={(e) => patch('llm', { enabled: e.target.checked })}
                />
                启用模型
              </label>
              <label>
                接口地址 base_url
                <input
                  value={s.llm.base_url}
                  placeholder="https://api.deepseek.com/v1"
                  onChange={(e) => patch('llm', { base_url: e.target.value })}
                />
              </label>
              <div className="row">
                <label>
                  模型名
                  <input
                    value={s.llm.model}
                    placeholder="deepseek-chat"
                    onChange={(e) => patch('llm', { model: e.target.value })}
                  />
                </label>
                <label>
                  温度
                  <input
                    type="number"
                    min={0}
                    max={2}
                    step={0.1}
                    value={s.llm.temperature}
                    onChange={(e) => patch('llm', { temperature: Number(e.target.value) })}
                  />
                </label>
              </div>
              <label>
                API Key
                <input
                  type="password"
                  value={keyInput}
                  placeholder={s.llm.api_key_set ? `${s.llm.api_key}（已保存，留空则不修改）` : '未设置'}
                  onChange={(e) => setKeyInput(e.target.value)}
                />
              </label>
            </section>

            <section className="cfg">
              <h3>编排参数</h3>
              <div className="row">
                <label>
                  最大运行时长（秒）
                  <input
                    type="number"
                    min={30}
                    max={600}
                    value={s.orchestration.max_runtime_s}
                    onChange={(e) => patch('orchestration', { max_runtime_s: Number(e.target.value) })}
                  />
                </label>
                <label>
                  最大模型请求数
                  <input
                    type="number"
                    min={1}
                    max={80}
                    value={s.orchestration.max_model_calls}
                    onChange={(e) => patch('orchestration', { max_model_calls: Number(e.target.value) })}
                  />
                </label>
              </div>
              <div className="row">
                <label>
                  最大 Agent 回合数
                  <input
                    type="number"
                    min={1}
                    max={40}
                    value={s.orchestration.max_agent_turns}
                    onChange={(e) => patch('orchestration', { max_agent_turns: Number(e.target.value) })}
                  />
                </label>
                <label>
                  节点超时（秒）
                  <input
                    type="number"
                    min={5}
                    max={300}
                    value={s.orchestration.node_timeout_s}
                    onChange={(e) => patch('orchestration', { node_timeout_s: Number(e.target.value) })}
                  />
                </label>
              </div>
              <p className="cfg-hint">
                当前深度团队最多使用 {s.orchestration.max_model_calls} 次供应商请求、{s.orchestration.max_agent_turns} 个 Agent 回合。
                两种预算分别计数；工具调用也会消耗供应商请求额度。单次运行最长 {s.orchestration.max_runtime_s} 秒。
                单次输出 token 不再设硬上限，只记录实际用量；运行中可随时取消。
                达到任一上限会暂停并说明实际原因；请求额度不足时可提高设置后恢复，不会自动扩大费用上限。
              </p>
            </section>

            <section className="cfg">
              <h3>数据源</h3>
              <label>
                来源
                <select
                  value={s.datasource.kind}
                  onChange={(e) => patch('datasource', { kind: e.target.value as 'mock' | 'http' })}
                >
                  <option value="mock">mock（内置演示数据）</option>
                  <option value="http">HTTP 接口（需自行提供）</option>
                </select>
              </label>
              {s.datasource.kind === 'http' && (
                <label>
                  接口地址
                  <input
                    value={s.datasource.http_base_url}
                    placeholder="http://127.0.0.1:9000"
                    onChange={(e) => patch('datasource', { http_base_url: e.target.value })}
                  />
                </label>
              )}
              <p className="cfg-hint">
                HTTP 适配器已实现 /flights /hotels /pois /weather 四类映射，但本机没有真实接口，未做实测。
              </p>
            </section>

            <section className="cfg">
              <h3>运行指标</h3>
              <p className="cfg-hint">最近 10 次运行（含失败）：调用数、token、耗时与失败原因。</p>
              {runsError && <p className="cfg-hint" style={{ color: '#f0b872' }}>读取失败：{runsError}</p>}
              {!runs && !runsError && <p className="muted">读取中…</p>}
              {runs && runs.length === 0 && <p className="muted">暂无运行记录</p>}
              {runs && runs.length > 0 && (
                <div style={{ overflowX: 'auto' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                    <thead>
                      <tr style={{ color: '#626c7c', textAlign: 'left' }}>
                        <th style={{ padding: '4px 6px', fontWeight: 500 }}>时间</th>
                        <th style={{ padding: '4px 6px', fontWeight: 500 }}>状态</th>
                        <th style={{ padding: '4px 6px', fontWeight: 500 }}>调用</th>
                        <th style={{ padding: '4px 6px', fontWeight: 500 }}>tokens</th>
                        <th style={{ padding: '4px 6px', fontWeight: 500 }}>耗时</th>
                        <th style={{ padding: '4px 6px', fontWeight: 500 }}>结果</th>
                      </tr>
                    </thead>
                    <tbody>
                      {runs.map((r) => (
                        <tr key={r.run_id} style={{ borderTop: '1px solid rgba(255,255,255,.07)', verticalAlign: 'top' }}>
                          <td style={{ padding: '6px', color: '#9aa4b4', whiteSpace: 'nowrap' }}>
                            {new Date(r.created_at).toLocaleString('zh-CN', {
                              month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
                            })}
                          </td>
                          <td style={{ padding: '6px', color: r.status === 'completed' ? '#6fe3c0' : '#f0b872', whiteSpace: 'nowrap' }}>
                            {r.status}
                          </td>
                          <td style={{ padding: '6px', fontFamily: 'ui-monospace, Consolas, monospace' }}>{r.calls}</td>
                          <td style={{ padding: '6px', fontFamily: 'ui-monospace, Consolas, monospace' }}>
                            {r.total_tokens.toLocaleString()}
                          </td>
                          <td style={{ padding: '6px', fontFamily: 'ui-monospace, Consolas, monospace' }}>
                            {Math.round(r.duration_ms / 1000)}s
                          </td>
                          <td style={{ padding: '6px', color: '#9aa4b4', maxWidth: 220 }}>
                            {r.passed === true ? '通过' : r.passed === false ? '未通过' : '—'}
                            {r.message && (
                              <div title={r.message} style={{ color: '#626c7c', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {r.message}
                              </div>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            {msg && <div className={`cfg-msg ${msg.ok ? 'ok' : 'bad'}`}>{msg.text}</div>}

            <footer className="modal-foot">
              <button className="ghost" onClick={test} disabled={busy !== null}>
                {busy === 'test' ? '测试中…' : '测试连接'}
              </button>
              <button className="primary small" onClick={save} disabled={busy !== null}>
                {busy === 'save' ? '保存中…' : '保存'}
              </button>
            </footer>
          </>
        )}
      </div>
    </div>
  )
}
