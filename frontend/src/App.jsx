import { apiFetch, withToken } from './api'
import { detectBatchConflicts } from './precheck'

import { useEffect, useRef, useState } from 'react'
import {
  Activity, BookOpen, Bot, Download, FileText, FolderOpen,
  GraduationCap, MessageSquare, Plus, Send, Settings2,
  ShieldCheck, Sparkles, Upload, PanelLeftClose,
} from 'lucide-react'
import './App.css'
import StructurePanel from './StructurePanel'
import WritingPanel from './WritingPanel'

const API = '/api'
const MAX_BATCH_FILES = 20

function isPdfFile(file) {
  return file.name.toLowerCase().endsWith('.pdf')
}

function formatSemanticWarning(file, data) {
  const best = data.best
  const percent = Math.round((best.content_similarity ?? 0) * 1000) / 10
  const pages = (best.changed_pages || []).slice(0, 6).join('、')
  const lines = [
    `【${file.name}】与已入库 ${best.filename}（v${best.version || '未知'}）正文语义相似度 ${percent}%`,
    `  新增 ${best.added_count} 句 / 删除 ${best.removed_count} 句` +
      (pages ? `，变化涉及第 ${pages} 页` : ''),
    `  判定：${best.verdict_label || '需人工确认'}`,
    `  建议：${data.recommendation || '请人工确认要保留的版本'}`,
  ]

  const example = best.added_examples?.[0]?.text
  if (example) {
    lines.push(`  新增示例：${example.slice(0, 70)}…`)
  }

  return lines.join('\n') + '\n'
}

const VERDICT_STYLES = {
  supported: { label: '已核验', background: '#e8f8ef', color: '#177a45' },
  weak: { label: '弱支持', background: '#fff5e5', color: '#9a6100' },
  unsupported: { label: '未支持', background: '#fff0f0', color: '#a12b2b' },
  no_citation: { label: '无引用', background: '#f3f4f6', color: '#4b5563' },
  unverified: { label: '未核验', background: '#eef4ff', color: '#1d4ed8' },
}

function VerificationBlock({ verification, abstained, coverage, generationError }) {
  const claims = verification?.claims || []
  const grounding = verification?.grounding || {}
  const mode = verification?.mode || 'semantic'
  const numericOnly = mode === 'numeric_only'
  const score = typeof grounding.score === 'number'
    ? Math.round(grounding.score * 100)
    : null
  const coverageEntries = Object.entries(coverage || {})

  if (!claims.length && !abstained && !generationError) return null

  return (
    <div className="sources" style={{ marginTop: '10px' }}>
      <div className="sources-heading">
        <ShieldCheck size={16} />
        <strong>抗幻觉核验</strong>
        {grounding.total > 0 && (
          <span className="sources-count">
            {numericOnly
              ? `离线模式 · 仅数字核验 · 共 ${grounding.total} 条断言`
              : `支持度 ${score ?? 0}% · 共 ${grounding.total} 条断言`}
          </span>
        )}
      </div>

      {numericOnly && (
        <div style={{
          margin: '8px 0',
          padding: '8px 10px',
          borderRadius: '8px',
          background: '#eef4ff',
          color: '#1d4ed8',
          fontSize: '12px',
          lineHeight: 1.6,
        }}>
          当前未启用 Ollama 语义嵌入，核验只检查数字与引用编号是否出现在证据中；
          语义支持度需要启动 Ollama 并重新索引后才会给出。
        </div>
      )}

      {(abstained || generationError) && (
        <div style={{
          margin: '8px 0',
          padding: '8px 10px',
          borderRadius: '8px',
          background: '#fff5e5',
          color: '#9a6100',
          fontSize: '12.5px',
          lineHeight: 1.6,
        }}>
          {generationError
            ? `模型调用失败：${generationError}（证据仍保留，可展开原文核对）`
            : '模型依据不足已弃答，建议缩小范围或指定论文后重问。'}
        </div>
      )}

      {coverageEntries.length > 1 && (
        <div style={{ fontSize: '12px', color: '#8b87a8', margin: '6px 0' }}>
          证据分布：
          {coverageEntries.map(([name, count]) => (
            <span key={name} style={{ marginLeft: '6px' }}>
              {name}×{count}
            </span>
          ))}
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
        {claims.map((claim, index) => {
          const style = VERDICT_STYLES[claim.verdict] || VERDICT_STYLES.no_citation
          return (
            <div key={index} style={{ fontSize: '12.5px', lineHeight: 1.65 }}>
              <span style={{
                ...style,
                padding: '1px 7px',
                borderRadius: '999px',
                fontSize: '11.5px',
                marginRight: '6px',
              }}>
                {style.label}
                {typeof claim.confidence === 'number' ? ` ${Math.round(claim.confidence * 100)}%` : ''}
              </span>
              <span style={{ color: '#3f3a63', whiteSpace: 'pre-wrap' }}>
                {claim.text}
              </span>
              {claim.evidence?.length > 0 && (
                <span style={{ color: '#8b87a8', marginLeft: '6px' }}>
                  → 证据 {claim.evidence.join('、')}
                </span>
              )}
              {claim.issues?.length > 0
                && claim.verdict !== 'supported'
                && claim.verdict !== 'unverified' && (
                <div style={{ color: '#a12b2b', fontSize: '11.5px', marginTop: '2px' }}>
                  {claim.issues.join('；')}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default function App() {
  const [health, setHealth] = useState(null)
  const [papers, setPapers] = useState([])
  const [activePaper, setActivePaper] = useState(null)
  const [messages, setMessages] = useState([
    {
      role: 'assistant',
      content: '你好！我是 ResearchPilot，你的 AI 科研助手。你可以上传论文，然后询问研究方法、核心结论、创新点或论文之间的联系。',
    },
  ])
  const [question, setQuestion] = useState('')
  const [model, setModel] = useState('deepseek')
  const [loading, setLoading] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [notice, setNotice] = useState('')
  const [structure, setStructure] = useState(null)
  const [structureOpen, setStructureOpen] = useState(false)
  const [structureLoading, setStructureLoading] = useState(false)
  const [structureError, setStructureError] = useState('')
  const [compareMode, setCompareMode] = useState(false)
  const [comparePapers, setComparePapers] = useState([])
  const [libraryFiles, setLibraryFiles] = useState([])
  const [libraryEntries, setLibraryEntries] = useState([])
  const [writingOpen, setWritingOpen] = useState(false)
  const fileRef = useRef(null)
  const chatRef = useRef(null)

  
async function loadHealth() {
  try {
    const res = await apiFetch(`${API}/health`)
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`)
    }

    const data = await res.json()
    console.log('ResearchPilot health:', data)
    setHealth(data)
  } catch (error) {
    console.error('Health check failed:', error)
    setHealth(null)
  }
}

async function loadLibrary() {
  try {
    const res = await apiFetch(`${API}/papers/status`)
    if (!res.ok) return
    const data = await res.json()
    setLibraryFiles(data.indexed_files || [])

    // files[] 里带 sha256 与 downloadable/file_url，用来决定是否显示"下载原文"
    const entries = Array.isArray(data.files) ? data.files : []
    setLibraryEntries(entries)

    // 刷新后列表为空时，用已入库的文件回填，否则只有本次上传的论文可见
    if (entries.length > 0) {
      setPapers(prev => (
        prev.length > 0
          ? prev
          : entries.map(entry => ({
            id: `library:${entry.filename}`,
            name: entry.filename,
            result: null,
            file: null,
          }))
      ))
    }
  } catch (error) {
    console.error('读取论文库失败：', error)
  }
}

useEffect(() => {
  loadHealth()
  loadLibrary()
  const timer = setInterval(loadHealth, 15000)
  return () => clearInterval(timer)
}, [])


  async function precheckPaperConflict(file, options = {}) {
    // 上传前检查：字节完全相同 + 同编号的其他版本，返回冲突说明文本
    let warnings = ''
    
    const exactForm = new FormData()
    exactForm.append('file', file)

    const exactRes = await apiFetch(`${API}/papers/duplicates/check-file`, {
      method: 'POST',
      body: exactForm,
    })

    if (!exactRes.ok) {
      const error = new Error(
        '内容重复检查暂时不可用，已停止上传，请稍后重试。'
      )
      error.precheck = true
      throw error
    }

    const exactData = await exactRes.json()

    if (exactData.is_duplicate && exactData.matches?.length > 0) {
      const matchedFiles = exactData.matches
        .map(p => `已入库：${p.filename}`)
        .join('、')

      warnings += `【${file.name}】与 ${matchedFiles} 内容完全相同，继续上传会覆盖同名论文。\n`
    }

    // 上传前检查：正文级语义比对（同编号其他版本或疑似重复论文）
    const arxivMatch = file.name.match(
      /(?<!\d)(\d{4}\.\d{4,5})(?:v(\d+))?/i
    )

    const shouldSemanticCheck =
      Boolean(arxivMatch) || Boolean(options.allowSemantic)

    if (!shouldSemanticCheck) return warnings

    try {
      const semanticForm = new FormData()
      semanticForm.append('file', file)

      const semanticRes = await apiFetch(
        `${API}/papers/duplicates/semantic-check`,
        { method: 'POST', body: semanticForm }
      )

      const semanticData = await semanticRes.json()

        
      if (!semanticRes.ok) return warnings

      if (!semanticData.best) return warnings

      warnings += formatSemanticWarning(file, semanticData)
    } catch (error) {
      // 与原单文件上传保持一致：语义检查失败不阻断上传
      console.error('语义查重失败：', error)
    }

    return warnings
  }

      
  async function uploadPapers(event) {
    const picked = Array.from(event.target.files || [])
    event.target.value = ''
    if (picked.length === 0 || uploading) return

    const pdfFiles = picked.filter(isPdfFile)
    const ignoredCount = picked.length - pdfFiles.length

    if (pdfFiles.length === 0) {
      setNotice('目前只支持 PDF 文件。')
      return
    }

    if (pdfFiles.length > MAX_BATCH_FILES) {
      setNotice(
        `一次最多批量上传 ${MAX_BATCH_FILES} 个 PDF，本次选择了 ${pdfFiles.length} 个。`
      )
      return
    }

    setUploading(true)
    setNotice('')

    try {
      const fileWarnings = []
      // 批次内部先比一次：库为空时两个版本谁也发现不了谁
      const batchWarnings = detectBatchConflicts(pdfFiles)

      for (let i = 0; i < pdfFiles.length; i += 1) {
        setNotice(`正在检查 ${i + 1}/${pdfFiles.length}：${pdfFiles[i].name}`)
        const warning = await precheckPaperConflict(pdfFiles[i], {
          // 批量较大且文件名没有 arXiv 编号时，跳过昂贵的语义比对
          allowSemantic: pdfFiles.length <= 3,
        })
        if (warning) fileWarnings.push(warning)
      }

      if (fileWarnings.length > 0 || batchWarnings.length > 0) {
        const sections = []

        if (fileWarnings.length > 0) {
          sections.push(
            `与已入库论文重复或版本冲突（${fileWarnings.length} 个文件）：\n\n` +
              fileWarnings.join('\n')
          )
        }

        if (batchWarnings.length > 0) {
          sections.push(batchWarnings.join(''))
        }

        const confirmed = window.confirm(
          `${sections.join('\n')}\n` +
          `继续上传会重新索引这些文件。\n` +
          `选择“取消”不会修改已有论文。`
        )

        if (!confirmed) {
          setNotice('已取消批量上传，现有论文未修改。')
          return
        }
      }

      setNotice(`正在上传并索引 ${pdfFiles.length} 个 PDF，请稍候…`)

      const form = new FormData()
      pdfFiles.forEach(file => form.append('files', file))

      const res = await apiFetch(`${API}/papers/batch-index`, {
        method: 'POST',
        body: form,
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '批量上传失败')

      const results = data.results || []
      const succeeded = results.filter(
        item => item.status === 'indexed' && item.filename
      )
      const problems = results
        .filter(item => item.status !== 'indexed')
        .map(item => `${item.filename}：${item.error || '未知原因'}`)

      if (succeeded.length > 0) {
        const filesByName = new Map(pdfFiles.map(file => [file.name, file]))
        const uploaded = succeeded.map(item => ({
          id: `${item.filename}-${Date.now()}`,
          name: item.filename,
          result: item,
          file: filesByName.get(item.filename) || null,
        }))

        setPapers(prev => {
          const names = new Set(uploaded.map(p => p.name))
          return [...uploaded, ...prev.filter(p => !names.has(p.name))]
        })

        setActivePaper(uploaded[uploaded.length - 1])
      }

      const summary =
        `批量上传结束：成功 ${data.indexed_count ?? succeeded.length} 个` +
        (data.skipped_count ? `，跳过 ${data.skipped_count} 个` : '') +
        (data.failed_count ? `，失败 ${data.failed_count} 个` : '') +
        (ignoredCount > 0 ? `；另有 ${ignoredCount} 个非 PDF 文件已忽略` : '')

      setNotice(
        problems.length > 0
          ? `${summary}\n失败原因：\n${problems.join('\n')}`
          : summary
      )

      await loadHealth()
      await loadLibrary()
    } catch (error) {
      setNotice(
        error.precheck
          ? error.message
          : `批量上传失败：${error.message}`
      )
    } finally {
      setUploading(false)
    }
  }

  async function sendQuestion(event) {
    event?.preventDefault()
    const text = question.trim()
    if (!text || loading) return

    const compare = compareMode && comparePapers.length >= 2
    if (!compare && !activePaper) {
      setNotice('请先上传并选择一篇论文，再开始提问。')
      return
    }

    setMessages(prev => [...prev, { role: 'user', content: text }])
    setQuestion('')
    setLoading(true)
    setNotice('')

    const payload = compare
      ? {
          question: text,
          model,
          mode: 'compare',
          filenames: comparePapers,
          top_k: 6,
        }
      : {
          question: text,
          model,
          mode: 'single',
          filename: activePaper?.name ?? null,
        }

    try {
      const res = await apiFetch(`${API}/papers/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '问答请求失败')

      const answer =
        data.answer ||
        data.message ||
        data.response ||
        data.result ||
        '（模型未返回内容）'

      setMessages(prev => [
        ...prev,
        {
          role: 'assistant',
          content: typeof answer === 'string' ? answer : JSON.stringify(answer, null, 2),
          sources: data.evidence || data.sources || [],
          verification: data.verification || null,
          abstained: Boolean(data.abstained),
          coverage: data.coverage || null,
          generationError: data.error || '',
        },
      ])
    } catch (error) {
      setMessages(prev => [
        ...prev,
        {
          role: 'assistant',
          content: `请求失败：${error.message}\n\n请确认后端仍在运行，并检查 /papers/ask 接口的请求字段格式。`,
          error: true,
        },
      ])
    } finally {
      setLoading(false)
    }
  }

  async function openStructure(paper) {
    const target = paper || activePaper
    if (!target) {
      setNotice('请先上传并选择一篇论文，再做结构化解析。')
      return
    }
    if (!target.file) {
      setNotice('该论文缺少原始文件（可能来自上一次会话），请重新上传后再解析。')
      return
    }

    setStructureOpen(true)
    setStructureLoading(true)
    setStructureError('')
    setStructure(null)

    try {
      const form = new FormData()
      form.append('file', target.file)

      const res = await apiFetch(
        `${API}/papers/structure?ocr=true&ocr_max_pages=20`,
        { method: 'POST', body: form }
      )
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '结构化解析失败')

      setStructure(data)
    } catch (error) {
      setStructureError(error.message)
    } finally {
      setStructureLoading(false)
    }
  }

  function newChat() {
    setMessages([
      {
        role: 'assistant',
        content: '新对话已开启。你想研究什么问题？',
      },
    ])
    setNotice('')
  }

  const chunkCount = health?.chroma_chunks ?? '—'

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon"><GraduationCap size={23} /></div>
          <div>
            <div className="brand-name">ResearchPilot</div>
            <div className="brand-subtitle">AI RESEARCH STUDIO</div>
          </div>
          <button className="icon-button sidebar-toggle" title="侧边栏">
            <PanelLeftClose size={17} />
          </button>
        </div>

        <button className="new-chat-button" onClick={newChat}>
          <Plus size={17} /> 新建科研对话
        </button>

        <div className="nav-label">工作空间</div>
        <nav className="navigation">
          <button className="nav-item active">
            <MessageSquare size={18} /> AI 科研助手
          </button>
          <button className="nav-item" onClick={() => fileRef.current?.click()}>
            <FolderOpen size={18} /> 论文资料库
          </button>
          <button className="nav-item" onClick={() => setWritingOpen(true)}>
            <Sparkles size={18} /> 写作辅助
          </button>
        </nav>

        <div className="sidebar-section-heading">
          <span>最近上传</span>
          <button className="icon-button" onClick={() => fileRef.current?.click()} title="上传论文">
            <Plus size={16} />
          </button>
        </div>

        <div className="paper-list">
          {papers.length === 0 ? (
            <div className="empty-papers">
              <FileText size={23} />
              <span>上传论文后显示在这里</span>
            </div>
          
) : papers.map(paper => {
  const entry = libraryEntries.find(item => item.filename === paper.name)
  const canDownload = Boolean(entry?.downloadable && entry?.file_url)

  return (
  <div
    className={`paper-item ${
      activePaper?.id === paper.id ? 'active' : ''
    }`}
    key={paper.id}
    onClick={() => setActivePaper(paper)}
    title={`点击切换到：${paper.name}`}
    style={{ cursor: 'pointer' }}
  >
    <FileText size={16} />
    <span title={paper.name}>{paper.name}</span>
    {canDownload && (
      <a
        className="paper-download"
        href={withToken(entry.file_url)}
        download={paper.name}
        title={`下载原文 PDF：${paper.name}`}
        onClick={event => event.stopPropagation()}
      >
        <Download size={14} />
      </a>
    )}
  </div>
  )
})}

        </div>

        <div className="sidebar-bottom">
          <div className="profile-avatar">R</div>
          <div className="profile-info">
            <div className="profile-name">Research Workspace</div>
            <div className="profile-plan">个人科研工作区</div>
          </div>
          <Settings2 size={17} className="muted-icon" />
        </div>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div className="breadcrumb">
            <span>工作空间</span>
            <span className="breadcrumb-slash">/</span>
            <strong>AI 科研助手</strong>
          </div>
          <div className="topbar-right">
            <div className={`system-status ${health ? 'online' : 'offline'}`}>
              <span className="status-dot" />
              {health ? '后端已连接' : '等待后端连接'}
            </div>
            <button className="avatar-small">R</button>
          </div>
        </header>

        <section className="workspace">
          <div className="welcome-row">
            <div>
              <div className="eyebrow"><Sparkles size={14} /> YOUR AI RESEARCH PARTNER</div>
              <h1>让科研，从一个好问题开始。</h1>
              <p className="welcome-description">
                阅读论文、梳理证据、探索研究思路，把繁琐工作交给 AI。
              </p>
            </div>
            <div className="welcome-art">
              <div className="art-orbit orbit-one" />
              <div className="art-orbit orbit-two" />
              <div className="art-core"><GraduationCap size={35} /></div>
              <div className="art-spark spark-one">✦</div>
              <div className="art-spark spark-two">✧</div>
            </div>
          </div>

          <div className="stats-grid">
            <div className="stat-card">
              <div className="stat-icon purple"><FileText size={19} /></div>
              <div>
                <div className="stat-label">当前会话上传</div>
                <div className="stat-value">{papers.length}<span> 篇</span></div>
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-icon blue"><BookOpen size={19} /></div>
              <div>
                <div className="stat-label">知识库文本块</div>
                <div className="stat-value">{chunkCount}<span> 块</span></div>
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-icon green"><Activity size={19} /></div>
              <div>
                <div className="stat-label">服务状态</div>
                <div className="stat-value small-value">{health ? '运行正常' : '未连接'}</div>
              </div>
            </div>
          </div>

          <div className="content-grid">
            <section className="chat-card">
              <div className="card-heading">
                <div className="heading-icon"><Bot size={20} /></div>
                <div>
                  <h2>科研问答</h2>
                  <p>基于你的论文资料进行检索与分析</p>
                </div>
                <span className="beta-pill">AI POWERED</span>
              </div>

              <div className="chat-messages" ref={chatRef}>
                {messages.map((message, index) => (
                  <div className={`message-row ${message.role}`} key={index}>
                    {message.role === 'assistant' && (
                      <div className="message-avatar"><Sparkles size={15} /></div>
                    )}
                    <div className={`message-bubble ${message.error ? 'message-error' : ''}`}>
                      <div className="message-role">
                        {message.role === 'assistant' ? 'ResearchPilot' : '你'}
                      </div>
                      <div className="message-content">
  {message.content.split(/(\[证据\s*\d+\])/g).map((part, index) => {
    const match = part.match(/^\[证据\s*(\d+)\]$/)

    if (!match) {
      return <span key={index}>{part}</span>
    }

    const sourceId = Number(match[1])

    return (
      <button
        key={index}
        type="button"
        className="citation-link"
        
onClick={() => {
  const citationId = `source-${index}-${sourceId}`
  const target = document.getElementById(citationId)

  console.log("当前消息 index：", index)
  console.log("目标来源 ID：", citationId)
  console.log("找到来源卡片：", !!target)

  if (target) {
    target.open = true
    target.scrollIntoView({
      behavior: "smooth",
      block: "center",
    })
  }
}}

      >
        {part}
      </button>
    )
  })}
</div>
                      
{message.sources?.length > 0 && (
  <div className="sources">
    <div className="sources-heading">
      <BookOpen size={16} />
      <strong>检索依据</strong>
      <span className="sources-count">
        {message.sources.length} 个片段
      </span>
    </div>

    <div className="sources-list">
      {message.sources.map((source, i) => {
        if (typeof source === 'string') {
          return (
            <div key={i} className="source-item">
              <div className="source-title">
                <FileText size={15} />
                <span>来源 {i + 1}</span>
              </div>
              <div className="source-excerpt">{source}</div>
            </div>
          )
        }

        return (
          

<details
  id={`source-${index}-${i + 1}`}
  className="source-item"
  key={`${source.filename || 'paper'}-${source.page ?? 'na'}-${source.anchor_type ?? 'chunk'}-${source.ref || ''}-${i}`}
>


            <summary className="source-summary">
              <div className="source-title">
                <FileText size={15} />
                <span>来源 {i + 1}</span>
                {source.anchor_label && (
                  <span className="source-page">{source.anchor_label}</span>
                )}
                {source.page != null && (
                  <span className="source-page">
                    第 {source.page} 页
                  </span>
                )}
              </div>
              <span className="source-expand">查看原文</span>
            </summary>

            <div className="source-details">
              <div className="source-filename">
                {source.title || source.filename || '未知论文'}
              </div>

              {source.filename && (
                <div className="source-meta">文件：{source.filename}</div>
              )}

              {source.section && (
                <div className="source-meta">章节：{source.section}</div>
              )}

              {source.ref && (
                <div className="source-meta">
                  编号：{source.ref}
                  {source.year ? ` · 年份 ${source.year}` : ''}
                </div>
              )}

              {source.chunk_number != null && (
                <div className="source-meta">
                  片段编号：{source.chunk_number}
                </div>
              )}

              {source.excerpt || source.text ? (
                <div className="source-excerpt">
                  {source.excerpt || source.text}
                </div>
              ) : (
                <div className="source-empty">
                  后端暂未返回原文摘录。
                </div>
              )}

              {typeof source.score === 'number' && (
                <div className="source-meta">
                  相关度：{source.score.toFixed(2)}
                </div>
              )}

              {typeof source.distance === 'number' && (
                <div className="source-meta">
                  向量距离：{source.distance.toFixed(4)}
                </div>
              )}
            </div>
          </details>
        )
      })}
    </div>
  </div>
)}

{message.verification && (
  <VerificationBlock
    verification={message.verification}
    abstained={message.abstained}
    coverage={message.coverage}
    generationError={message.generationError}
  />
)}

                    </div>
                    {message.role === 'user' && <div className="user-avatar">你</div>}
                  </div>
                ))}
                {loading && (
                  <div className="message-row assistant">
                    <div className="message-avatar"><Sparkles size={15} /></div>
                    <div className="message-bubble">
                      <div className="message-role">ResearchPilot</div>
                      <div className="typing-indicator"><i /><i /><i /> 正在检索与思考…</div>
                    </div>
                  </div>
                )}
              </div>

              <form className="composer" onSubmit={sendQuestion}>
                <div style={{ display: 'flex', gap: '10px', alignItems: 'center', marginBottom: '8px', flexWrap: 'wrap' }}>
                  <label style={{ display: 'flex', gap: '6px', alignItems: 'center', fontSize: '12.5px', color: '#5c5878' }}>
                    <input
                      type="checkbox"
                      checked={compareMode}
                      onChange={e => {
                        setCompareMode(e.target.checked)
                        if (!e.target.checked) setComparePapers([])
                      }}
                    />
                    多篇对比
                  </label>

                  {compareMode && (
                    <span style={{ fontSize: '12px', color: comparePapers.length >= 2 ? '#4c3fd6' : '#a12b2b' }}>
                      已选 {comparePapers.length} 篇（至少 2 篇；库中共 {libraryFiles.length} 篇）
                    </span>
                  )}

                  {!compareMode && activePaper && (
                    <span style={{ fontSize: '12px', color: '#8b87a8' }}>
                      当前论文：{activePaper.name}
                    </span>
                  )}
                </div>

                {compareMode && (
                  <div style={{
                    display: 'flex',
                    flexWrap: 'wrap',
                    gap: '6px',
                    marginBottom: '8px',
                    maxHeight: '92px',
                    overflowY: 'auto',
                  }}>
                    {libraryFiles.length === 0 && (
                      <span style={{ fontSize: '12px', color: '#a12b2b' }}>
                        论文库为空，请先上传论文。
                      </span>
                    )}
                    {libraryFiles.map(name => {
                      const picked = comparePapers.includes(name)
                      return (
                        <button
                          key={name}
                          type="button"
                          onClick={() => {
                            setComparePapers(prev => (
                              prev.includes(name)
                                ? prev.filter(item => item !== name)
                                : [...prev, name]
                            ))
                          }}
                          style={{
                            border: `1px solid ${picked ? '#7c6cff' : '#e2dffa'}`,
                            background: picked ? '#f0edff' : '#fff',
                            color: picked ? '#4c3fd6' : '#5c5878',
                            borderRadius: '999px',
                            padding: '4px 10px',
                            fontSize: '12px',
                            cursor: 'pointer',
                          }}
                          title={name}
                        >
                          {picked ? '✓ ' : ''}{name.length > 34 ? `${name.slice(0, 31)}…` : name}
                        </button>
                      )
                    })}
                  </div>
                )}

                <textarea
                  value={question}
                  onChange={e => setQuestion(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      sendQuestion()
                    }
                  }}
                  placeholder={compareMode ? '例如：对比两篇论文在训练配置和评价指标上的差异' : '例如：这篇论文采用了什么研究方法？'}
                  rows={2}
                  disabled={loading}
                />
                <div className="composer-footer">
                  <span className="composer-hint">Enter 发送 · Shift + Enter 换行</span>
                  <button className="send-button" type="submit" disabled={loading || !question.trim()}>
                    <Send size={16} /> 发送
                  </button>
                </div>
              </form>
            </section>

            <aside className="right-column">
              <section className="side-card model-card">
                <div className="side-card-heading">
                  <div className="heading-icon compact"><Bot size={18} /></div>
                  <h3>模型设置</h3>
                </div>
                <label className="field-label" htmlFor="model-select">当前对话模型</label>
                <select id="model-select" value={model} onChange={e => setModel(e.target.value)} className="model-select">
                  <option value="deepseek">DeepSeek · 在线模型</option>
                  <option value="local">
                    Qwen 2.5 · 本地模型{health?.local_chat_available === false ? '（未就绪）' : ''}
                  </option>
                </select>
                <div className="model-note">
                  <span className={`status-dot ${model === 'deepseek' ? 'blue-dot' : 'green-dot'}`} />
                  {model === 'deepseek' ? '通过 API 调用 DeepSeek' : '通过本机 Ollama 调用 Qwen'}
                </div>

                {health && (
                  <div style={{
                    marginTop: '8px',
                    padding: '8px 10px',
                    borderRadius: '8px',
                    background: health.fallback_mode ? '#fffaf0' : '#f3fbf6',
                    color: health.fallback_mode ? '#8a6100' : '#177a45',
                    fontSize: '11.5px',
                    lineHeight: 1.75,
                  }}>
                    <div>
                      嵌入后端：
                      {health.embedding?.backend === 'ollama'
                        ? 'Ollama · nomic-embed-text'
                        : '离线哈希嵌入（已降级，无需 Ollama）'}
                    </div>
                    <div>
                      本地模型：
                      {health.local_chat_available
                        ? `${health.local_chat_model} 可用`
                        : '不可用 · 提问与翻译将自动使用在线模型'}
                    </div>
                    {health.fallback_mode && !health.deepseek_configured && (
                      <div style={{ color: '#a12b2b' }}>
                        在线模型未配置（DEEPSEEK_API_KEY 为空），请补充配置。
                      </div>
                    )}
                  </div>
                )}

                <div className="divider" />
                <div className="capability-row"><span>论文检索</span><span className="capability-enabled">已接入</span></div>
                <div className="capability-row"><span>持久化知识库</span><span className="capability-enabled">已接入</span></div>
                <div className="capability-row"><span>版本语义查重</span><span className="capability-enabled">已接入</span></div>
                <div className="capability-row"><span>结构化解析</span><span className="capability-enabled">已接入</span></div>
              </section>

              <section className="side-card upload-card">
                <div className="side-card-heading">
                  <div className="heading-icon compact"><Upload size={18} /></div>
                  <h3>论文资料</h3>
                </div>
                <p className="side-card-description">
                  支持单个或批量上传 PDF（一次最多 {MAX_BATCH_FILES} 篇），解析后索引到知识库。
                </p>
                <button
                  className="upload-button"
                  onClick={() => fileRef.current?.click()}
                  disabled={uploading}
                >
                  <Upload size={16} />
                  {uploading ? '正在上传与索引…' : '上传 PDF 论文（可多选）'}
                </button>
                <input
                  ref={fileRef}
                  type="file"
                  accept=".pdf,application/pdf"
                  multiple
                  hidden
                  onChange={uploadPapers}
                />
                <button
                  className="upload-button"
                  style={{ marginTop: '8px' }}
                  onClick={() => openStructure()}
                  disabled={uploading || structureLoading || !activePaper}
                  title="抽取标题树、作者机构、表格、图表、公式与参考文献"
                >
                  <FileText size={16} />
                  {structureLoading ? '正在结构化解析…' : '结构化解析当前论文'}
                </button>
                {papers.length > 0 && (
                  <div className="uploaded-summary">
                    <div className="uploaded-check">✓</div>
                    <div>
                      <strong>已提交 {papers.length} 篇论文</strong>
                      <span>可在左侧查看本次上传记录</span>
                    </div>
                  </div>
                )}
                <div className="upload-tip">
                  <FileText size={15} />
                  <span>扫描件会自动 OCR；解析可抽取标题树、表格、图表与公式。</span>
                </div>
              </section>

              <section className="tip-card">
                <div className="tip-icon"><Sparkles size={18} /></div>
                <div>
                  <strong>科研小提示</strong>
                  <p>提问时指定论文、研究对象或需要对比的指标，通常更容易得到可核查的回答。</p>
                </div>
              </section>
            </aside>
          </div>

          
{notice && (
  <div
    style={{
      position: 'fixed',
      top: '20px',
      left: '50%',
      transform: 'translateX(-50%)',
      zIndex: 99999,
      background: '#fff',
      color: '#333',
      border: '1px solid #7c6cff',
      borderRadius: '8px',
      padding: '14px 20px',
      boxShadow: '0 4px 16px rgba(0,0,0,0.15)',
      maxWidth: '760px',
      maxHeight: '70vh',
      overflowY: 'auto',
    }}
  >
    <span style={{ whiteSpace: 'pre-line' }}>{notice}</span>
    <button
      onClick={() => setNotice('')}
      style={{ marginLeft: '16px', cursor: 'pointer' }}
      aria-label="关闭提示"
    >
      ×
    </button>
  </div>
)}


          <StructurePanel
            open={structureOpen}
            loading={structureLoading}
            data={structure}
            error={structureError}
            onClose={() => setStructureOpen(false)}
          />

          <WritingPanel
            open={writingOpen}
            onClose={() => setWritingOpen(false)}
          />

          <footer className="page-footer">
            <span>ResearchPilot · AI Research Workspace</span>
            <span>回答应结合论文原文核验</span>
          </footer>
        </section>
      </main>
    </div>
  )
}
