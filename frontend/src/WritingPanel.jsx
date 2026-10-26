import { useState } from 'react'
import {
  AlertTriangle, BookOpen, ChartLine, Languages, PenLine, X,
} from 'lucide-react'

const TABS = [
  { id: 'outline', label: '论文框架', icon: PenLine },
  { id: 'dataviz', label: '数据可视化', icon: ChartLine },
  { id: 'translate', label: '学术翻译', icon: Languages },
]

function Card({ children, style }) {
  return (
    <div style={{
      background: '#faf9ff',
      border: '1px solid #eceaf8',
      borderRadius: '12px',
      padding: '14px 16px',
      ...style,
    }}>
      {children}
    </div>
  )
}

function SectionTitle({ children }) {
  return (
    <div style={{ fontSize: '13px', fontWeight: 600, color: '#241f4d', marginBottom: '8px' }}>
      {children}
    </div>
  )
}

function OutlineTab() {
  const [idea, setIdea] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')

  async function run() {
    const text = idea.trim()
    if (text.length < 4) {
      setError('请至少写 4 个字符的 idea。')
      return
    }

    setLoading(true)
    setError('')
    setResult(null)

    try {
      const res = await fetch('/api/writing/outline', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ idea: text, top_k: 5 }),
      })
      const payload = await res.json()
      if (!res.ok) throw new Error(payload.detail || '生成失败')
      setResult(payload)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
      <Card>
        <SectionTitle>研究 idea</SectionTitle>
        <textarea
          value={idea}
          onChange={event => setIdea(event.target.value)}
          rows={3}
          placeholder="例如：用稀疏注意力降低长文本推理成本，同时保持翻译质量"
          style={{ width: '100%', borderRadius: '10px', border: '1px solid #ddd8f8', padding: '10px', fontSize: '13px' }}
        />
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginTop: '8px' }}>
          <button
            type="button"
            onClick={run}
            disabled={loading}
            style={{
              border: 'none', background: '#5b4bd6', color: '#fff',
              borderRadius: '10px', padding: '9px 16px', fontSize: '13px', cursor: 'pointer',
            }}
          >
            {loading ? '生成中…' : '生成框架'}
          </button>
          <span style={{ fontSize: '11.5px', color: '#8b87a8' }}>
            参考文献只从你已索引的论文中取，库里没有就不会生成
          </span>
        </div>
        {error && (
          <div style={{ marginTop: '10px', color: '#a12b2b', fontSize: '12.5px' }}>
            <AlertTriangle size={14} /> {error}
          </div>
        )}
      </Card>

      {result && (
        <>
          <Card>
            <SectionTitle>框架</SectionTitle>
            {result.sections?.length ? result.sections.map(section => (
              <div key={section.title} style={{ marginBottom: '12px' }}>
                <div style={{ fontSize: '12.5px', fontWeight: 600, color: '#4c3fd6', marginBottom: '4px' }}>
                  {section.title}
                </div>
                <div style={{ fontSize: '13px', lineHeight: 1.8, color: '#2c274d', whiteSpace: 'pre-wrap' }}>
                  {section.content}
                </div>
              </div>
            )) : (
              <div style={{ fontSize: '12.5px', color: '#8b87a8' }}>模型未返回结构化的三段内容。</div>
            )}
          </Card>

          <Card>
            <SectionTitle>参考文献（来自库内论文，可核对）</SectionTitle>
            <ol style={{ margin: 0, paddingLeft: '18px', fontSize: '12.8px', lineHeight: 1.8, color: '#2c274d' }}>
              {result.references?.map(item => (
                <li key={item.ref_id} style={{ marginBottom: '6px' }}>
                  <span style={{ color: '#4c3fd6', fontWeight: 600 }}>[{item.ref_id}]</span>{' '}
                  {item.title}
                  {item.authors?.length ? ` · ${item.authors.slice(0, 3).join('、')}` : ''}
                  {' · '}
                  {item.year || '年份未识别'}
                  <div style={{ fontSize: '11.5px', color: '#8b87a8' }}>
                    文件：{item.filename} · 定位：第 {item.page} 页 · {item.section || '正文'}
                  </div>
                </li>
              ))}
            </ol>
            <div style={{ marginTop: '8px', fontSize: '11.5px', color: result.citations?.ok ? '#177a45' : '#a12b2b' }}>
              引用校验：{[...(result.citations?.cited || [])].length ? `已引用 R${result.citations.cited.join('、R')}` : '正文未引用任何文献'}
              {result.citations?.out_of_range?.length ? `；越界编号 ${result.citations.out_of_range.join('、')}` : ''}
              {result.citations?.stray_formats?.length ? `；非授权格式 ${result.citations.stray_formats.join(' ')}` : ''}
            </div>
          </Card>

          <Card style={{ background: '#fffaf0', borderColor: '#ffe3ae' }}>
            <SectionTitle>使用说明</SectionTitle>
            <ul style={{ margin: 0, paddingLeft: '18px', fontSize: '12px', lineHeight: 1.8, color: '#8a6100' }}>
              {result.caveats?.map((item, index) => <li key={index}>{item}</li>)}
            </ul>
          </Card>
        </>
      )}
    </div>
  )
}

function DatavizTab() {
  const [file, setFile] = useState(null)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')

  async function run() {
    if (!file) {
      setError('请选择一个 CSV 文件。')
      return
    }

    setLoading(true)
    setError('')
    setResult(null)

    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch('/api/dataviz/report', { method: 'POST', body: form })
      const payload = await res.json()
      if (!res.ok) throw new Error(payload.detail || '生成失败')
      setResult(payload)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
      <Card>
        <SectionTitle>上传原始数据（CSV）</SectionTitle>
        <input
          type="file"
          accept=".csv,text/csv"
          onChange={event => {
            setFile(event.target.files?.[0] || null)
            event.target.value = ''
          }}
          style={{ fontSize: '12.5px' }}
        />
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginTop: '10px' }}>
          <button
            type="button"
            onClick={run}
            disabled={loading || !file}
            style={{
              border: 'none', background: '#5b4bd6', color: '#fff',
              borderRadius: '10px', padding: '9px 16px', fontSize: '13px', cursor: 'pointer',
            }}
          >
            {loading ? '解析中…' : '生成表格与图表'}
          </button>
          <span style={{ fontSize: '11.5px', color: '#8b87a8' }}>
            {file ? `已选择：${file.name}` : '支持数值列 + 类别列组合'}
          </span>
        </div>
        {error && (
          <div style={{ marginTop: '10px', color: '#a12b2b', fontSize: '12.5px' }}>
            <AlertTriangle size={14} /> {error}
          </div>
        )}
      </Card>

      {result && (
        <>
          <Card>
            <SectionTitle>
              数据画像（{result.row_count} 行 × {result.column_count} 列）
            </SectionTitle>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
              {result.columns?.map(column => (
                <span key={column.name} style={{
                  fontSize: '11.5px',
                  padding: '2px 8px',
                  borderRadius: '999px',
                  background: column.kind === 'numeric' ? '#eef7f2' : column.kind === 'categorical' ? '#f0edff' : '#f3f4f6',
                  color: column.kind === 'numeric' ? '#177a45' : column.kind === 'categorical' ? '#4c3fd6' : '#4b5563',
                }}>
                  {column.name} · {column.kind}
                  {column.kind === 'numeric' ? ` · ${column.min}~${column.max}` : ''}
                </span>
              ))}
            </div>
            <div
              style={{ marginTop: '10px', overflowX: 'auto', fontSize: '12px' }}
              dangerouslySetInnerHTML={{ __html: result.table?.html || '' }}
            />
          </Card>

          {result.charts?.map(chart => (
            <Card key={chart.file}>
              <SectionTitle>{chart.title}</SectionTitle>
              <img
                src={chart.chart_url}
                alt={chart.title}
                style={{ width: '100%', maxWidth: '760px', display: 'block', margin: '0 auto' }}
              />
              <div style={{ marginTop: '10px', fontSize: '12.5px', lineHeight: 1.8, color: '#2c274d' }}>
                <strong style={{ color: '#241f4d' }}>Figure Caption（可直接用于论文）：</strong>
                <div style={{ marginTop: '4px' }}>{chart.caption}</div>
              </div>
            </Card>
          ))}

          <Card style={{ background: '#fffaf0', borderColor: '#ffe3ae' }}>
            <div style={{ fontSize: '12px', color: '#8a6100', lineHeight: 1.8 }}>
              {result.note}
            </div>
          </Card>
        </>
      )}
    </div>
  )
}

function TranslateTab() {
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)
  const [translation, setTranslation] = useState('')
  const [error, setError] = useState('')

  async function run() {
    const source = text.trim()
    if (source.length < 8) {
      setError('请至少输入 8 个字符。')
      return
    }

    setLoading(true)
    setError('')
    setTranslation('')

    try {
      const res = await fetch('/api/writing/translate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: source }),
      })
      const payload = await res.json()
      if (!res.ok) throw new Error(payload.detail || '翻译失败')
      setTranslation(payload.translation || '')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
      <Card>
        <SectionTitle>中文原文（摘要或全文片段）</SectionTitle>
        <textarea
          value={text}
          onChange={event => setText(event.target.value)}
          rows={7}
          placeholder="粘贴中文摘要或段落，翻译为符合顶会/期刊规范的学术英文"
          style={{ width: '100%', borderRadius: '10px', border: '1px solid #ddd8f8', padding: '10px', fontSize: '13px' }}
        />
        <div style={{ marginTop: '8px', display: 'flex', gap: '10px', alignItems: 'center' }}>
          <button
            type="button"
            onClick={run}
            disabled={loading}
            style={{
              border: 'none', background: '#5b4bd6', color: '#fff',
              borderRadius: '10px', padding: '9px 16px', fontSize: '13px', cursor: 'pointer',
            }}
          >
            {loading ? '翻译中…' : '译为学术英文'}
          </button>
          <span style={{ fontSize: '11.5px', color: '#8b87a8' }}>
            公式、符号、模型名与指标名保留英文
          </span>
        </div>
        {error && (
          <div style={{ marginTop: '10px', color: '#a12b2b', fontSize: '12.5px' }}>
            <AlertTriangle size={14} /> {error}
          </div>
        )}
      </Card>

      {translation && (
        <Card>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
            <BookOpen size={15} style={{ color: '#4c3fd6' }} />
            <SectionTitle>Academic English</SectionTitle>
          </div>
          <div style={{ fontSize: '13px', lineHeight: 1.85, color: '#2c274d', whiteSpace: 'pre-wrap' }}>
            {translation}
          </div>
        </Card>
      )}
    </div>
  )
}

export default function WritingPanel({ open, onClose }) {
  const [tab, setTab] = useState('outline')

  if (!open) return null

  return (
    <div style={{
      position: 'fixed',
      inset: 0,
      background: 'rgba(24, 20, 60, 0.45)',
      zIndex: 99992,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '24px',
      overflowY: 'auto',
    }}>
      <div style={{
        background: '#fff',
        width: 'min(980px, 96vw)',
        height: '88vh',
        maxHeight: '100%',
        borderRadius: '16px',
        boxShadow: '0 24px 60px rgba(20, 16, 60, 0.25)',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        flexShrink: 0,
      }}>
        <header style={{
          display: 'flex',
          alignItems: 'center',
          gap: '12px',
          padding: '14px 18px',
          borderBottom: '1px solid #eceaf8',
          flexShrink: 0,
        }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: '12px', color: '#7c6cff', fontWeight: 600 }}>写作 Copilot</div>
            <div style={{ fontSize: '16px', fontWeight: 700, color: '#241f4d' }}>
              无幻觉框架 · 数据图表 · 学术翻译
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            style={{
              border: 'none', background: '#f4f2ff', borderRadius: '10px',
              width: '34px', height: '34px', cursor: 'pointer',
              display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#4c3fd6',
            }}
          >
            <X size={18} />
          </button>
        </header>

        <nav style={{
          display: 'flex', gap: '4px', padding: '10px 18px 0',
          borderBottom: '1px solid #eceaf8',
          flexShrink: 0,
        }}>
          {TABS.map(item => {
            const Icon = item.icon
            const active = tab === item.id
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setTab(item.id)}
                style={{
                  border: 'none',
                  background: active ? '#f0edff' : 'transparent',
                  color: active ? '#4c3fd6' : '#5c5878',
                  borderRadius: '10px 10px 0 0',
                  padding: '9px 13px',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  fontSize: '13.5px',
                  fontWeight: active ? 600 : 500,
                }}
              >
                <Icon size={15} /> {item.label}
              </button>
            )
          })}
        </nav>

        <main style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '16px 18px 28px' }}>
          {tab === 'outline' && <OutlineTab />}
          {tab === 'dataviz' && <DatavizTab />}
          {tab === 'translate' && <TranslateTab />}
        </main>
      </div>
    </div>
  )
}
