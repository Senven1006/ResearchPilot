
import { useEffect, useRef, useState } from 'react'
import {
  Activity, BookOpen, Bot, FileText, FolderOpen,
  GraduationCap, MessageSquare, Plus, Send, Settings2,
  Sparkles, Upload, Trash2, PanelLeftClose,
} from 'lucide-react'
import './App.css'

const API = '/api'

export default function App() {
  const [expandedSource, setExpandedSource] = useState(null)
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
  const fileRef = useRef(null)
  const chatRef = useRef(null)

  
async function loadHealth() {
  try {
    const res = await fetch(`${API}/health`)
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

useEffect(() => {
  loadHealth()
  const timer = setInterval(loadHealth, 15000)
  return () => clearInterval(timer)
}, [])


 async function uploadPaper(event) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return

    if (!file.name.toLowerCase().endsWith('.pdf')) {
      setNotice('目前只支持 PDF 文件。')
      return
    }

    const form = new FormData()
    form.append('file', file)
    setUploading(true)
    setNotice('')

    try {
      const res = await fetch('/api/papers/index', {
        method: 'POST',
        body: form,
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '论文上传失败')

      
const newPaper = {
  id: `${file.name}-${Date.now()}`,
  name: file.name,
  result: data,
}

setPapers(prev => [
  newPaper,
  ...prev.filter(p => p.name !== file.name),
])

setActivePaper(newPaper)

      setNotice(`已提交：${file.name}。请确认后端返回的入库结果。`)
      await loadHealth()
    } catch (error) {
      setNotice(`上传失败：${error.message}`)
    } finally {
      setUploading(false)
    }
  }

  async function sendQuestion(event) {
    event?.preventDefault()
    const text = question.trim()
    if (!text || loading) return
    if (!activePaper) {
  setNotice('请先上传并选择一篇论文，再开始提问。')
  return
}

    setMessages(prev => [...prev, { role: 'user', content: text }])
    setQuestion('')
    setLoading(true)
    setNotice('')

    try {
      const res = await fetch(`${API}/papers/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
       body: JSON.stringify({
  question: text,
  model,
  filename: activePaper?.name ?? null,
}),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '问答请求失败')

      const answer =
        data.answer ??
        data.response ??
        data.result ??
        data.message ??
        JSON.stringify(data, null, 2)

      setMessages(prev => [
        ...prev,
        {
          role: 'assistant',
          content: typeof answer === 'string' ? answer : JSON.stringify(answer, null, 2),
          sources: data.sources || data.references || data.contexts || [],
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
          <button className="nav-item" onClick={() => setNotice('引用图谱模块将在后续版本中实现。')}>
            <BookOpen size={18} /> 引用与文献
          </button>
          <button className="nav-item" onClick={() => setNotice('研究规划 Agent 将在后续版本中实现。')}>
            <Sparkles size={18} /> 研究任务规划
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
          
) : papers.map(paper => (
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
  </div>
))}

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
  const target = document.getElementById(
    `source-${index}-${sourceId}`
  )

  console.log("点击证据编号：", sourceId)
  console.log("找到来源卡片：", !!target)

  if (target) {
    target.open = true
    target.scrollIntoView({
      behavior: "smooth",
      block: "center",
    })
  } else {
    console.error("没有找到对应的来源卡片")
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
  key={`${source.filename || 'paper'}-${source.page ?? 'na'}-${source.chunk_number ?? i}-${i}`}
  onToggle={(event) => {
  console.log("来源卡片展开状态：", event.currentTarget.open)
}}
  open={
    expandedSource?.messageIndex === index &&
    expandedSource?.sourceId === i + 1
  }
>


            <summary className="source-summary">
              <div className="source-title">
                <FileText size={15} />
                <span>来源 {i + 1}</span>
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
                {source.filename || source.title || '未知论文'}
              </div>

              {source.chunk_number != null && (
                <div className="source-meta">
                  片段编号：{source.chunk_number}
                </div>
              )}

              {source.excerpt ? (
                <div className="source-excerpt">
                  {source.excerpt}
                </div>
              ) : (
                <div className="source-empty">
                  后端暂未返回原文摘录。
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
                <textarea
                  value={question}
                  onChange={e => setQuestion(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      sendQuestion()
                    }
                  }}
                  placeholder="例如：这篇论文采用了什么研究方法？"
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
                  <option value="local">Qwen 2.5 · 本地模型</option>
                </select>
                <div className="model-note">
                  <span className={`status-dot ${model === 'deepseek' ? 'blue-dot' : 'green-dot'}`} />
                  {model === 'deepseek' ? '通过 API 调用 DeepSeek' : '通过本机 Ollama 调用 Qwen'}
                </div>
                <div className="divider" />
                <div className="capability-row"><span>论文检索</span><span className="capability-enabled">已接入</span></div>
                <div className="capability-row"><span>持久化知识库</span><span className="capability-enabled">已接入</span></div>
                <div className="capability-row"><span>Agent 自动规划</span><span className="capability-soon">开发中</span></div>
              </section>

              <section className="side-card upload-card">
                <div className="side-card-heading">
                  <div className="heading-icon compact"><Upload size={18} /></div>
                  <h3>论文资料</h3>
                </div>
                <p className="side-card-description">
                  上传 PDF，解析论文内容并将文本索引到知识库。
                </p>
                <button
                  className="upload-button"
                  onClick={() => fileRef.current?.click()}
                  disabled={uploading}
                >
                  <Upload size={16} />
                  {uploading ? '正在上传与索引…' : '上传 PDF 论文'}
                </button>
                <input
                  ref={fileRef}
                  type="file"
                  accept=".pdf,application/pdf"
                  hidden
                  onChange={uploadPaper}
                />
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
                  <span>建议使用可复制文本的 PDF 文件。</span>
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
            <div className="notice-bar">
              <span>{notice}</span>
              <button onClick={() => setNotice('')} aria-label="关闭提示">×</button>
            </div>
          )}

          <footer className="page-footer">
            <span>ResearchPilot · AI Research Workspace</span>
            <span>回答应结合论文原文核验</span>
          </footer>
        </section>
      </main>
    </div>
  )
}
