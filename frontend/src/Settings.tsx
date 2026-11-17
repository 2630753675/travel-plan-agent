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
  max_output_tokens: number
  token_budget: number
  agent_mode: 'fast' | 'expert' | 'peer' | 'legacy'
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

type Props = {
  onClose: () => void
  onSaved: (s: Settings) => void
}

export default function SettingsPanel({ onClose, onSaved }: Props) {
  const [s, setS] = useState<Settings | null>(null)
  const [keyInput, setKeyInput] = useState('')
  const [busy, setBusy] = useState<'load' | 'save' | 'test' | null>('load')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

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
                当前 peer 深度团队最多使用 {s.orchestration.max_model_calls} 次供应商请求、{s.orchestration.max_agent_turns} 个 Agent 回合。
                两种预算分别计数；工具调用也会消耗供应商请求额度。单次运行最长 {s.orchestration.max_runtime_s} 秒，单次输出最多 {s.orchestration.max_output_tokens} token。
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
