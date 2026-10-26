import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, BookOpen, ChevronRight, Download, FileText,
  Image as ImageIcon, Languages, ListTree, Sigma, Table, X,
} from 'lucide-react'

const API = '/api'

const TABS = [
  { id: 'sections', label: '标题树与正文', icon: ListTree },
  { id: 'tables', label: '表格', icon: Table },
  { id: 'figures', label: '图表', icon: ImageIcon },
  { id: 'equations', label: '公式', icon: Sigma },
  { id: 'references', label: '参考文献', icon: BookOpen },
]

const REFERENCES_TITLE_RE = /^(references|bibliography|参考文献)$/i
const CITATION_SOURCE = String.raw`\[\s*(\d{1,3}(?:\s*[,–-]\s*\d{1,3})*)\s*\]`

const PANE_STYLE = {
  flex: 1,
  minWidth: 0,
  minHeight: 0,
  overflowY: 'auto',
  padding: '12px 16px 40px',
  background: '#fff',
}

const HEADING_STYLE = {
  margin: '16px 0 8px',
  fontSize: '13.5px',
  color: '#241f4d',
  borderLeft: '3px solid #7c6cff',
  paddingLeft: '8px',
}

const PRIMARY_BUTTON = {
  border: 'none',
  background: '#5b4bd6',
  color: '#fff',
  borderRadius: '10px',
  padding: '7px 12px',
  cursor: 'pointer',
  display: 'flex',
  alignItems: 'center',
  gap: '6px',
  fontSize: '13px',
  fontWeight: 600,
}

const GHOST_BUTTON = {
  border: '1px solid #ddd8f8',
  background: '#fff',
  borderRadius: '10px',
  padding: '7px 10px',
  cursor: 'pointer',
  display: 'flex',
  alignItems: 'center',
  gap: '6px',
  color: '#4c3fd6',
  fontSize: '13px',
}

/** 把扁平标题按一级标题分组，供左侧树形展示。 */
function groupOutline(outline) {
  const groups = []
  for (const item of outline || []) {
    if (item.level === 1 || groups.length === 0) {
      groups.push({ root: item, children: [] })
    } else {
      groups[groups.length - 1].children.push(item)
    }
  }
  return groups
}

/** 解析 "[12]" / "[3, 4]" / "[12–14]" 里的编号，区间会展开。 */
function parseCitationNumbers(group) {
  const numbers = []
  for (const part of group.split(',')) {
    const range = part.split(/[–-]/).map(value => Number(value.trim()))
    if (
      range.length === 2
      && range.every(Number.isFinite)
      && range[1] >= range[0]
      && range[1] - range[0] < 50
    ) {
      for (let value = range[0]; value <= range[1]; value += 1) {
        numbers.push(value)
      }
    } else if (Number.isFinite(range[0])) {
      numbers.push(range[0])
    }
  }
  return numbers
}

/** 逐个扫描引用标记；每次用新建的正则，避免共享 lastIndex 的副作用。 */
function scanCitations(text, onMatch) {
  const scanner = new RegExp(CITATION_SOURCE, 'g')
  const source = text || ''
  let match = scanner.exec(source)
  while (match) {
    onMatch(match, source)
    match = scanner.exec(source)
  }
}

function Chip({ children, tone = 'default' }) {
  const palette = {
    default: { background: '#f1f0ff', color: '#4c3fd6' },
    green: { background: '#e8f8ef', color: '#177a45' },
    amber: { background: '#fff5e5', color: '#9a6100' },
    gray: { background: '#f3f4f6', color: '#4b5563' },
  }[tone]

  return (
    <span style={{
      ...palette,
      padding: '2px 8px',
      borderRadius: '999px',
      fontSize: '11.5px',
      whiteSpace: 'nowrap',
    }}>
      {children}
    </span>
  )
}

function Empty({ text }) {
  return <p style={{ fontSize: '13px', color: '#8b87a8' }}>{text}</p>
}

/** 正文中的 [n] 渲染成可点击标签，点击跳转到参考文献条目。 */
function CitationText({ text, onCitation }) {
  const parts = []
  let lastIndex = 0

  scanCitations(text, (match, source) => {
    if (match.index > lastIndex) {
      parts.push(
        <span key={`t-${lastIndex}`}>{source.slice(lastIndex, match.index)}</span>
      )
    }

    const numbers = parseCitationNumbers(match[1])
    parts.push(
      <button
        key={`c-${match.index}`}
        type="button"
        onClick={() => (numbers.length ? onCitation?.(numbers[0]) : null)}
        title={`跳转到参考文献 ${numbers.join('、')}`}
        style={{
          border: 'none',
          background: '#f0edff',
          color: '#4c3fd6',
          borderRadius: '5px',
          padding: '0 4px',
          margin: '0 1px',
          fontSize: '11.5px',
          cursor: 'pointer',
        }}
      >
        {match[0]}
      </button>
    )

    lastIndex = match.index + match[0].length
  })

  const source = text || ''
  if (lastIndex < source.length) {
    parts.push(<span key={`t-${lastIndex}`}>{source.slice(lastIndex)}</span>)
  }

  return <>{parts}</>
}

function MetaCard({ meta, stats }) {
  const typeLabel = meta.doc_type === 'book'
    ? '长篇书籍'
    : meta.doc_type === 'paper' ? '论文' : '文档'

  return (
    <div style={{
      background: '#faf9ff',
      border: '1px solid #eceaf8',
      borderRadius: '12px',
      padding: '14px 16px',
    }}>
      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '8px' }}>
        <Chip tone={meta.doc_type === 'book' ? 'green' : 'default'}>{typeLabel}</Chip>
        <Chip tone="gray">{stats.digital_pages ?? 0} 页文本层</Chip>
        {stats.ocr_pages > 0 && <Chip tone="amber">{stats.ocr_pages} 页 OCR</Chip>}
      </div>

      <div style={{ fontSize: '13.5px', color: '#2c274d', lineHeight: 1.9 }}>
        <div><strong>作者：</strong>{meta.authors?.length ? meta.authors.join('、') : '未识别'}</div>
        <div><strong>机构：</strong>{meta.affiliations?.length ? meta.affiliations.join('、') : '未识别'}</div>
        {meta.keywords?.length > 0 && (
          <div><strong>关键词：</strong>{meta.keywords.join('、')}</div>
        )}
      </div>

      {meta.abstract && (
        <details style={{ marginTop: '8px' }}>
          <summary style={{ cursor: 'pointer', fontSize: '13px', color: '#4c3fd6' }}>
            摘要（{meta.abstract.length} 字）
          </summary>
          <p style={{
            margin: '8px 0 0',
            fontSize: '13px',
            lineHeight: 1.8,
            color: '#3f3a63',
            whiteSpace: 'pre-wrap',
          }}>
            {meta.abstract}
          </p>
        </details>
      )}
    </div>
  )
}

function TablesView({ tables }) {
  if (!tables?.length) return <Empty text="未检测到表格。" />

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
      {tables.map(table => (
        <div key={table.id} style={{ border: '1px solid #eceaf8', borderRadius: '12px', padding: '12px' }}>
          <div style={{ fontSize: '12.5px', color: '#5c5878', marginBottom: '8px' }}>
            <Chip tone="gray">p{table.page}</Chip>{' '}
            {table.caption || '（无题注）'}
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ borderCollapse: 'collapse', fontSize: '12.5px', minWidth: '100%' }}>
              <tbody>
                {(table.rows || []).map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {row.map((cell, cellIndex) => (
                      <td
                        key={cellIndex}
                        style={{
                          border: '1px solid #e6e3f5',
                          padding: '6px 8px',
                          background: rowIndex === 0 ? '#f7f5ff' : '#fff',
                          fontWeight: rowIndex === 0 ? 600 : 400,
                          whiteSpace: 'pre-wrap',
                          verticalAlign: 'top',
                        }}
                      >
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  )
}

function FiguresView({ figures }) {
  if (!figures?.length) return <Empty text="未检测到图片或图注。" />

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))',
      gap: '14px',
    }}>
      {figures.map(figure => (
        <div key={figure.id} style={{ border: '1px solid #eceaf8', borderRadius: '12px', overflow: 'hidden' }}>
          {figure.url ? (
            <img
              src={figure.url}
              alt={figure.caption || figure.id}
              loading="lazy"
              style={{ width: '100%', display: 'block', background: '#faf9ff' }}
            />
          ) : (
            <div style={{
              height: '120px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: '#faf9ff',
              color: '#a5a1bb',
              fontSize: '12.5px',
            }}>
              未裁剪到图像
            </div>
          )}
          <div style={{ padding: '8px 10px' }}>
            <div style={{ fontSize: '11.5px', color: '#a5a1bb', marginBottom: '4px' }}>
              p{figure.page} · {figure.source}
            </div>
            <div style={{ fontSize: '12.5px', color: '#3f3a63', lineHeight: 1.6 }}>
              {figure.caption || '（无题注）'}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

function EquationsView({ equations }) {
  if (!equations?.length) return <Empty text="未检测到公式。" />

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
      {equations.map(equation => (
        <div key={equation.id} style={{
          border: '1px solid #eceaf8',
          borderRadius: '10px',
          padding: '10px 12px',
          background: '#fcfbff',
        }}>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '6px' }}>
            <Chip tone="gray">p{equation.page}</Chip>
            <Chip tone={equation.confidence >= 0.6 ? 'green' : 'amber'}>
              置信度 {Math.round((equation.confidence || 0) * 100)}%
            </Chip>
          </div>
          <pre style={{
            margin: 0,
            fontFamily: 'Consolas, "Courier New", monospace',
            fontSize: '12.5px',
            color: '#241f4d',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}>
            {equation.latex || equation.text}
          </pre>
          {equation.text && equation.latex !== equation.text && (
            <div style={{ fontSize: '11.5px', color: '#a5a1bb', marginTop: '6px' }}>
              原始文本：{equation.text}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function ReferencesView({ references, citations, activeRef, onJumpToBlock }) {
  const entries = references?.entries || []
  if (!entries.length) return <Empty text="未检测到参考文献。" />

  const citedCount = entries.filter(
    (_, index) => (citations?.get(index + 1) || []).length > 0
  ).length

  return (
    <div>
      <div style={{ fontSize: '12.5px', color: '#8b87a8', marginBottom: '10px' }}>
        共 {entries.length} 条，其中 {citedCount} 条能在正文中找到引用标记（点击页码跳回正文）
      </div>

      <ol style={{ margin: 0, paddingLeft: '20px', fontSize: '13px', lineHeight: 1.8, color: '#2c274d' }}>
        {entries.map((entry, index) => {
          const number = index + 1
          const used = citations?.get(number) || []
          const pages = [...new Set(used.map(item => item.page))].slice(0, 6)
          const active = activeRef === number

          return (
            <li
              key={entry.id || number}
              id={`refitem-${number}`}
              style={{
                marginBottom: '8px',
                padding: '4px 8px',
                borderRadius: '8px',
                background: active ? '#f0edff' : 'transparent',
                outline: active ? '1px solid #cfc8ff' : 'none',
              }}
            >
              {entry.year && <Chip tone="gray">{entry.year}</Chip>}{' '}
              {entry.raw}

              <div style={{ marginTop: '3px', fontSize: '11.5px', color: '#8b87a8' }}>
                {used.length > 0 ? (
                  <>
                    被引 {used.length} 次 · 跳回：
                    {pages.map(page => (
                      <button
                        key={`${number}-${page}`}
                        type="button"
                        onClick={() => {
                          const target = used.find(item => item.page === page)
                          if (target) onJumpToBlock?.(target.sectionId, target.index)
                        }}
                        title={used.find(item => item.page === page)?.sectionTitle || ''}
                        style={{
                          border: 'none',
                          background: '#f0edff',
                          color: '#4c3fd6',
                          borderRadius: '5px',
                          padding: '0 5px',
                          margin: '0 3px',
                          fontSize: '11.5px',
                          cursor: 'pointer',
                        }}
                      >
                        第 {page} 页
                      </button>
                    ))}
                  </>
                ) : (
                  '正文未检出引用标记'
                )}
              </div>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

/**
 * 中英对照双栏：左右各自滚动，按滚动比例双向同步。
 */
function BilingualPane({
  data, translation, loading, error,
  onCitation, onSelectText, onSelectSection,
  sectionsDone, sectionsTotal,
}) {
  const leftRef = useRef(null)
  const rightRef = useRef(null)
  const syncing = useRef(false)

  const sections = useMemo(() => data?.sections?.items || [], [data])

  const zhMap = useMemo(() => {
    const map = new Map()
    for (const item of translation?.blocks || []) {
      map.set(`${item.section_id}:${item.block_index}`, item.zh)
    }
    return map
  }, [translation])

  const rows = useMemo(() => {
    const list = []
    let count = 0

    for (const section of sections) {
      if (count >= 1200) break
      list.push({ type: 'heading', id: section.id, title: section.title })

      for (const [index, block] of (section.blocks || []).entries()) {
        if (count >= 1200) break
        const key = `${section.id}:${index}`
        list.push({
          type: 'block',
          key,
          sectionId: section.id,
          index,
          page: block.page,
          text: block.text || '',
          zh: zhMap.get(key) || '',
        })
        count += 1
      }
    }
    return list
  }, [sections, zhMap])

  function handleScroll(which) {
    if (syncing.current) return

    const source = which === 'left' ? leftRef.current : rightRef.current
    const target = which === 'left' ? rightRef.current : leftRef.current
    if (!source || !target) return

    syncing.current = true
    const ratio = source.scrollTop / Math.max(1, source.scrollHeight - source.clientHeight)
    target.scrollTop = ratio * Math.max(0, target.scrollHeight - target.clientHeight)
    requestAnimationFrame(() => {
      syncing.current = false
    })
  }

  return (
    <div style={{ flex: 1, display: 'flex', minHeight: 0, gap: '1px', background: '#eceaf8' }}>
      <div
        ref={leftRef}
        onScroll={() => handleScroll('left')}
        onMouseUp={event => onSelectText?.(event)}
        style={PANE_STYLE}
      >
        {rows.map(row => (
          row.type === 'heading' ? (
            <h4
              key={`h-${row.id}`}
              id={`sec-${row.id}`}
              onClick={() => onSelectSection?.(row.id)}
              style={{ ...HEADING_STYLE, cursor: 'pointer' }}
              title="跳到该节（会自动翻译这一节）"
            >
              {row.title}
            </h4>
          ) : (
            <div
              key={row.key}
              id={`blk-${row.sectionId}-${row.index}`}
              style={{ marginBottom: '10px', fontSize: '13.5px', lineHeight: 1.75, color: '#2c274d' }}
            >
              <span style={{ fontSize: '11px', color: '#a5a1bb', marginRight: '6px' }}>p{row.page}</span>
              <CitationText text={row.text} onCitation={onCitation} />
            </div>
          )
        ))}
      </div>

      <div
        ref={rightRef}
        onScroll={() => handleScroll('right')}
        style={PANE_STYLE}
      >
        {loading && (
          <div style={{ fontSize: '12.5px', color: '#5c5878', marginBottom: '10px' }}>
            正在翻译当前章节（首次调用模型，之后走缓存）…
          </div>
        )}
        {error && (
          <div style={{ fontSize: '12.5px', color: '#a12b2b', marginBottom: '10px' }}>
            翻译失败：{error}
          </div>
        )}
        {translation && (
          <div style={{ fontSize: '11.5px', color: '#8b87a8', marginBottom: '8px' }}>
            已译 {sectionsDone}/{sectionsTotal} 节 · {translation.translated_blocks}/
            {translation.available_blocks ?? translation.translated_blocks} 段
          </div>
        )}

        {rows.map(row => (
          row.type === 'heading' ? (
            <h4 key={`h-zh-${row.id}`} style={HEADING_STYLE}>{row.title}</h4>
          ) : (
            <div
              key={`zh-${row.key}`}
              style={{ marginBottom: '10px', fontSize: '13.5px', lineHeight: 1.85, color: '#2c274d' }}
            >
              <span style={{ fontSize: '11px', color: '#a5a1bb', marginRight: '6px' }}>p{row.page}</span>
              {row.zh ? (
                <CitationText text={row.zh} onCitation={onCitation} />
              ) : (
                <span style={{ color: '#a5a1bb' }}>（未翻译）</span>
              )}
            </div>
          )
        ))}
      </div>
    </div>
  )
}

/** 划词翻译浮层：挂载即发起请求，后端有缓存秒回。 */
function TranslateCard({ text, position, onClose }) {
  const [state, setState] = useState({ loading: true, translation: '', error: '' })

  useEffect(() => {
    let alive = true

    async function run() {
      try {
        const res = await fetch(`${API}/translate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        })
        const payload = await res.json()
        if (!alive) return
        if (!res.ok) throw new Error(payload.detail || '翻译失败')
        setState({ loading: false, translation: payload.translation, error: '' })
      } catch (err) {
        if (alive) setState({ loading: false, translation: '', error: err.message })
      }
    }

    run()
    return () => {
      alive = false
    }
  }, [text])

  const top = Math.min(Math.max(12, (position?.y || 120) + 12), Math.max(12, window.innerHeight - 240))
  const left = Math.min(Math.max(12, (position?.x || 240) - 150), Math.max(12, window.innerWidth - 340))

  return (
    <div style={{
      position: 'fixed',
      top,
      left,
      width: '320px',
      background: '#fff',
      border: '1px solid #ddd8f8',
      borderRadius: '12px',
      boxShadow: '0 12px 32px rgba(24, 20, 60, 0.18)',
      padding: '12px 14px',
      zIndex: 100000,
      fontSize: '12.8px',
      lineHeight: 1.7,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px' }}>
        <Languages size={14} style={{ color: '#4c3fd6' }} />
        <strong style={{ color: '#241f4d', fontSize: '12.5px' }}>划词翻译</strong>
        <span style={{ flex: 1 }} />
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭翻译"
          style={{ border: 'none', background: 'transparent', cursor: 'pointer', color: '#8b87a8' }}
        >
          <X size={14} />
        </button>
      </div>

      <div style={{ color: '#8b87a8', fontSize: '11.5px', marginBottom: '6px' }}>
        {text.slice(0, 90)}{text.length > 90 ? '…' : ''}
      </div>

      {state.loading && <div style={{ color: '#5c5878' }}>翻译中…</div>}
      {state.error && <div style={{ color: '#a12b2b' }}>{state.error}</div>}
      {!state.loading && !state.error && (
        <div style={{ color: '#241f4d' }}>{state.translation}</div>
      )}
    </div>
  )
}

/** 请求指定章节的段落译文；后端按「文档 + 模型」做磁盘缓存。 */
async function requestTranslation(sha256, sectionIds, maxBlocks) {
  const res = await fetch(
    `${API}/papers/structure/${sha256}/translate`
    + `?model=auto&concurrency=4&max_blocks=${maxBlocks}`
    + `&sections=${sectionIds.join(',')}`,
    { method: 'POST' }
  )
  const payload = await res.json()
  if (!res.ok) throw new Error(payload.detail || '翻译失败')
  return payload
}

/**
 * 结构化阅读面板：标题树导航、表格/图表/公式/文献、引用双向跳转、中英对照。
 */
export default function StructurePanel({ open, loading, data, error, onClose }) {
  const [tab, setTab] = useState('sections')
  const [activeSectionId, setActiveSectionId] = useState(null)
  const [expandedGroups, setExpandedGroups] = useState({})
  const [bilingual, setBilingual] = useState(false)
  const [translation, setTranslation] = useState(null)
  const [translationLoading, setTranslationLoading] = useState(false)
  const [translationError, setTranslationError] = useState('')
  const [snippet, setSnippet] = useState(null)
  const [activeRef, setActiveRef] = useState(null)
  const [doneSections, setDoneSections] = useState(() => new Set())
  const [bulkRunning, setBulkRunning] = useState(false)

  const zhStore = useRef(new Map())

  const sections = useMemo(() => data?.sections?.items || [], [data])
  const groups = useMemo(() => groupOutline(data?.outline), [data])

  const activeSection = useMemo(
    () => sections.find(item => item.id === activeSectionId) || sections[0] || null,
    [sections, activeSectionId]
  )

  const translatableSections = useMemo(
    () => sections.filter(
      section => !REFERENCES_TITLE_RE.test((section.title || '').trim())
    ),
    [sections]
  )

  /** 正文 [n] → 参考文献条目；同时记录每条文献被哪些正文块引用。 */
  const citationIndex = useMemo(() => {
    const map = new Map()

    for (const section of sections) {
      for (const [index, block] of (section.blocks || []).entries()) {
        scanCitations(block.text, match => {
          for (const number of parseCitationNumbers(match[1])) {
            if (!map.has(number)) map.set(number, [])
            map.get(number).push({
              sectionId: section.id,
              index,
              page: block.page,
              sectionTitle: section.title,
            })
          }
        })
      }
    }

    return map
  }, [sections])

  const sectionsDone = translatableSections.filter(
    section => doneSections.has(section.id)
  ).length
  const nextSection = translatableSections.find(
    section => !doneSections.has(section.id)
  ) || null

  // 换文档时清空译文缓存；放在微任务里，避免 effect 同步阶段 setState
  useEffect(() => {
    let alive = true
    Promise.resolve().then(() => {
      if (!alive) return
      zhStore.current = new Map()
      setTranslation(null)
      setDoneSections(new Set())
      setTranslationError('')
    })
    return () => {
      alive = false
    }
  }, [data?.sha256])

  /** 合并增量译文：按 sectionId:blockIndex 累积，避免各章节互相覆盖。 */
  function mergeTranslation(payload) {
    for (const block of payload.blocks || []) {
      if (!block.zh) continue
      zhStore.current.set(`${block.section_id}:${block.block_index}`, block.zh)
    }

    const merged = []
    for (const [key, zh] of zhStore.current.entries()) {
      const separator = key.indexOf(':')
      merged.push({
        section_id: key.slice(0, separator),
        block_index: Number(key.slice(separator + 1)),
        zh,
      })
    }

    setTranslation({
      blocks: merged,
      translated_blocks: merged.length,
      available_blocks: payload.available_blocks ?? merged.length,
      model: payload.model,
    })
  }

  // 打开对照或切换章节时，只翻译当前这一节
  useEffect(() => {
    if (!bilingual || !data?.sha256 || !activeSection?.id) return
    if (doneSections.has(activeSection.id)) return

    const sectionId = activeSection.id
    let alive = true

    Promise.resolve().then(() => {
      if (alive) {
        setTranslationLoading(true)
        setTranslationError('')
      }
    })

    requestTranslation(data.sha256, [sectionId], 20)
      .then(payload => {
        if (!alive) return
        mergeTranslation(payload)
        setDoneSections(prev => new Set(prev).add(sectionId))
      })
      .catch(err => {
        if (alive) setTranslationError(err.message)
      })
      .finally(() => {
        if (alive) setTranslationLoading(false)
      })

    return () => {
      alive = false
    }
  }, [bilingual, data?.sha256, activeSection?.id, doneSections])

  /** 按块数预算切组，让每次请求的等待时间可控。 */
  function buildGroups(limit = 18) {
    const groups = []
    let current = []
    let count = 0

    for (const section of translatableSections) {
      const size = Math.max(1, (section.blocks || []).length)
      if (current.length && count + size > limit) {
        groups.push(current)
        current = []
        count = 0
      }
      current.push(section.id)
      count += size
    }

    if (current.length) groups.push(current)
    return groups
  }

  async function translateNext() {
    if (!data?.sha256 || bulkRunning || !nextSection) return

    const target = nextSection
    setBulkRunning(true)
    setTranslationLoading(true)
    setTranslationError('')

    try {
      const payload = await requestTranslation(data.sha256, [target.id], 20)
      mergeTranslation(payload)
      setDoneSections(prev => new Set(prev).add(target.id))
      setActiveSectionId(target.id)

      setTimeout(() => {
        document
          .getElementById(`sec-${target.id}`)
          ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      }, 80)
    } catch (err) {
      setTranslationError(err.message)
    } finally {
      setTranslationLoading(false)
      setBulkRunning(false)
    }
  }

  async function translateAll() {
    if (!data?.sha256 || bulkRunning) return

    const pending = buildGroups().filter(
      group => group.some(id => !doneSections.has(id))
    )
    if (!pending.length) return

    setBulkRunning(true)
    setTranslationError('')

    for (const [index, group] of pending.entries()) {
      if (index === 0) setTranslationLoading(true)

      try {
        const payload = await requestTranslation(data.sha256, group, 24)
        mergeTranslation(payload)
        setDoneSections(prev => {
          const next = new Set(prev)
          for (const id of group) next.add(id)
          return next
        })
      } catch (err) {
        setTranslationError(err.message)
        break
      }
    }

    setTranslationLoading(false)
    setBulkRunning(false)
  }

  function focusSection(sectionId) {
    setActiveSectionId(sectionId)
    setTimeout(() => {
      document
        .getElementById(`sec-${sectionId}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }, 60)
  }

  function jumpToReference(number) {
    setActiveRef(number)
    setBilingual(false)
    setTab('references')
    setTimeout(() => {
      document
        .getElementById(`refitem-${number}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 80)
  }

  function jumpToBlock(sectionId, index) {
    setBilingual(false)
    setTab('sections')
    setActiveSectionId(sectionId)
    setTimeout(() => {
      document
        .getElementById(`blk-${sectionId}-${index}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 80)
  }

  /** 鼠标选中文本后弹出划词翻译，长度做了上下限保护。 */
  function handleSelectText() {
    const selection = window.getSelection()
    const text = (selection?.toString() || '').trim()
    if (!text || text.length < 2 || text.length > 1500) {
      setSnippet(null)
      return
    }

    const range = selection.getRangeAt(0)
    const rect = range.getBoundingClientRect()
    setSnippet({ text, x: rect.left + rect.width / 2, y: rect.bottom })
  }

  function downloadMarkdown() {
    const blob = new Blob([data?.markdown || ''], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${(data?.meta?.title || data?.filename || 'paper').slice(0, 60)}.md`
    link.click()
    URL.revokeObjectURL(url)
  }

  if (!open) return null

  const stats = data?.stats || {}
  const meta = data?.meta || {}

  return (
    <div style={{
      position: 'fixed',
      inset: 0,
      background: 'rgba(24, 20, 60, 0.45)',
      zIndex: 99990,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '24px',
      overflowY: 'auto',
    }}>
      <div style={{
        background: '#fff',
        width: 'min(1180px, 96vw)',
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
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: '12px', color: '#7c6cff', fontWeight: 600 }}>结构化解析</div>
            <div style={{
              fontSize: '16px',
              fontWeight: 700,
              color: '#241f4d',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}>
              {meta.title || data?.filename || '正在解析…'}
            </div>
          </div>

          {data && (
            <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
              <Chip>{stats.page_count ?? 0} 页</Chip>
              <Chip tone="green">标题 {stats.headings ?? 0}</Chip>
              <Chip tone="gray">表格 {stats.tables ?? 0}</Chip>
              <Chip tone="gray">图 {stats.figures ?? 0}</Chip>
              <Chip tone="gray">公式 {stats.equations ?? 0}</Chip>
              {stats.ocr_pages > 0 && <Chip tone="amber">OCR {stats.ocr_pages} 页</Chip>}
            </div>
          )}

          <button
            type="button"
            onClick={downloadMarkdown}
            disabled={!data}
            title="下载 Markdown"
            style={GHOST_BUTTON}
          >
            <Download size={15} /> Markdown
          </button>

          <button
            type="button"
            onClick={() => {
              const next = !bilingual
              setBilingual(next)
              if (next) setTab('sections')
            }}
            disabled={!data}
            title="中英对照阅读：公式、表格与参考文献保持原文"
            style={{
              ...GHOST_BUTTON,
              border: `1px solid ${bilingual ? '#7c6cff' : '#ddd8f8'}`,
              background: bilingual ? '#f0edff' : '#fff',
            }}
          >
            <Languages size={15} /> {bilingual ? '退出对照' : '中英对照'}
          </button>

          {bilingual && (
            <>
              <button
                type="button"
                onClick={translateNext}
                disabled={bulkRunning || !nextSection}
                title={nextSection ? `翻译下一节：${nextSection.title}` : '全文已翻译完毕'}
                style={{
                  ...PRIMARY_BUTTON,
                  background: nextSection ? '#5b4bd6' : '#f0edff',
                  color: nextSection ? '#fff' : '#8b87a8',
                  cursor: bulkRunning || !nextSection ? 'default' : 'pointer',
                }}
              >
                <ChevronRight size={15} />
                {bulkRunning
                  ? '正在翻译这一节…'
                  : nextSection
                    ? `继续翻译（${sectionsDone + 1}/${translatableSections.length}）`
                    : '已全部翻译'}
              </button>

              <button
                type="button"
                onClick={translateAll}
                disabled={bulkRunning || !nextSection}
                title="一次译完剩余章节（较慢，会逐节刷新）"
                style={{ ...GHOST_BUTTON, fontSize: '12.5px' }}
              >
                一次译完
              </button>
            </>
          )}

          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            style={{
              border: 'none',
              background: '#f4f2ff',
              borderRadius: '10px',
              width: '34px',
              height: '34px',
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#4c3fd6',
            }}
          >
            <X size={18} />
          </button>
        </header>

        {snippet && (
          <TranslateCard
            text={snippet.text}
            position={snippet}
            onClose={() => setSnippet(null)}
          />
        )}

        {loading && (
          <div style={{ padding: '48px', textAlign: 'center', color: '#5c5878' }}>
            <div className="typing-indicator"><i /><i /><i /> 正在解析版面、表格、图表与公式…</div>
            <p style={{ fontSize: '13px', marginTop: '12px', color: '#8b87a8' }}>
              扫描页会逐页 OCR，页数较多时需要一段时间。
            </p>
          </div>
        )}

        {!loading && error && (
          <div style={{
            margin: '24px',
            padding: '16px',
            borderRadius: '12px',
            background: '#fff4f4',
            color: '#a12b2b',
            display: 'flex',
            gap: '10px',
            alignItems: 'flex-start',
          }}>
            <AlertTriangle size={18} />
            <div>解析失败：{error}</div>
          </div>
        )}

        {!loading && !error && data && (
          <>
            <nav style={{
              display: 'flex',
              gap: '4px',
              padding: '10px 18px 0',
              borderBottom: '1px solid #eceaf8',
              flexWrap: 'wrap',
              flexShrink: 0,
            }}>
              {TABS.map(item => {
                const Icon = item.icon
                const active = tab === item.id
                const count = {
                  sections: sections.length,
                  tables: data.tables?.length,
                  figures: data.figures?.length,
                  equations: data.equations?.length,
                  references: data.references?.count,
                }[item.id]

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
                    {count != null && (
                      <span style={{ fontSize: '11.5px', color: '#8b87a8' }}>{count}</span>
                    )}
                  </button>
                )
              })}
            </nav>

            <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
              {tab === 'sections' && (
                <aside style={{
                  width: '300px',
                  borderRight: '1px solid #eceaf8',
                  overflowY: 'auto',
                  minHeight: 0,
                  padding: '14px',
                  flexShrink: 0,
                }}>
                  <div style={{ fontSize: '12px', color: '#8b87a8', marginBottom: '8px' }}>
                    标题树（一 / 二 / 三级）
                  </div>

                  {groups.length === 0 && <Empty text="未识别到标题层级。" />}

                  {groups.map((group, index) => {
                    const expanded = expandedGroups[group.root.id] ?? true
                    const hasChildren = group.children.length > 0

                    return (
                      <div key={group.root.id || index} style={{ marginBottom: '2px' }}>
                        <button
                          type="button"
                          onClick={() => {
                            setActiveSectionId(group.root.id)
                            if (hasChildren) {
                              setExpandedGroups(prev => ({
                                ...prev,
                                [group.root.id]: !expanded,
                              }))
                            }
                          }}
                          style={{
                            width: '100%',
                            border: 'none',
                            background: activeSection?.id === group.root.id ? '#f0edff' : 'transparent',
                            color: '#241f4d',
                            textAlign: 'left',
                            padding: '7px 8px',
                            borderRadius: '8px',
                            cursor: 'pointer',
                            display: 'flex',
                            alignItems: 'center',
                            gap: '6px',
                            fontSize: '13.5px',
                            fontWeight: 600,
                          }}
                        >
                          <ChevronRight
                            size={14}
                            style={{
                              transform: expanded ? 'rotate(90deg)' : 'none',
                              opacity: hasChildren ? 0.7 : 0.25,
                              transition: 'transform .15s',
                            }}
                          />
                          <span style={{ flex: 1 }}>{group.root.title}</span>
                          <span style={{ fontSize: '11px', color: '#a5a1bb' }}>
                            p{group.root.page}
                          </span>
                        </button>

                        {expanded && group.children.map(child => (
                          <button
                            key={child.id}
                            type="button"
                            onClick={() => setActiveSectionId(child.id)}
                            style={{
                              width: '100%',
                              border: 'none',
                              background: activeSection?.id === child.id ? '#f0edff' : 'transparent',
                              color: child.level === 2 ? '#3f3a63' : '#5c5878',
                              textAlign: 'left',
                              padding: '5px 8px 5px ' + (child.level === 2 ? '22px' : '34px'),
                              borderRadius: '8px',
                              cursor: 'pointer',
                              fontSize: '13px',
                            }}
                          >
                            {child.title}
                          </button>
                        ))}
                      </div>
                    )
                  })}
                </aside>
              )}

              <main
                onMouseUp={handleSelectText}
                style={bilingual
                  ? {
                      flex: 1,
                      minHeight: 0,
                      maxHeight: '100%',
                      display: 'flex',
                      flexDirection: 'column',
                      padding: 0,
                      overflow: 'hidden',
                    }
                  : {
                      flex: 1,
                      minHeight: 0,
                      maxHeight: '100%',
                      overflowY: 'auto',
                      overflowX: 'hidden',
                      padding: '18px 22px',
                    }}
              >
                {tab === 'sections' && bilingual && (
                  <BilingualPane
                    data={data}
                    translation={translation}
                    loading={translationLoading}
                    error={translationError}
                    onCitation={jumpToReference}
                    onSelectText={handleSelectText}
                    onSelectSection={focusSection}
                    sectionsDone={sectionsDone}
                    sectionsTotal={translatableSections.length}
                  />
                )}

                {tab === 'sections' && !bilingual && (
                  <>
                    <MetaCard meta={meta} stats={stats} />

                    {activeSection && (
                      <section style={{ marginTop: '18px' }}>
                        <h3 style={{
                          margin: '0 0 8px',
                          fontSize: '15px',
                          color: '#241f4d',
                          display: 'flex',
                          alignItems: 'center',
                          gap: '8px',
                        }}>
                          <FileText size={16} />
                          {activeSection.title}
                          <Chip tone="gray">第 {activeSection.page_start} 页</Chip>
                        </h3>

                        {(activeSection.blocks || []).length === 0 && (
                          <Empty text="本节未抽取到正文块。" />
                        )}

                        {(activeSection.blocks || []).map((block, index) => (
                          <div key={index} style={{ marginBottom: '10px' }}>
                            <div style={{ display: 'flex', gap: '8px', alignItems: 'flex-start' }}>
                              <span style={{
                                fontSize: '11px',
                                color: '#a5a1bb',
                                paddingTop: '3px',
                                minWidth: '30px',
                              }}>
                                p{block.page}
                              </span>
                              <p style={{
                                margin: 0,
                                fontSize: '13.5px',
                                lineHeight: 1.75,
                                color: '#2c274d',
                              }}>
                                <CitationText text={block.text} onCitation={jumpToReference} />
                              </p>
                            </div>
                          </div>
                        ))}
                      </section>
                    )}
                  </>
                )}

                {tab === 'tables' && <TablesView tables={data.tables} />}
                {tab === 'figures' && <FiguresView figures={data.figures} />}
                {tab === 'equations' && <EquationsView equations={data.equations} />}
                {tab === 'references' && (
                  <ReferencesView
                    references={data.references}
                    citations={citationIndex}
                    activeRef={activeRef}
                    onJumpToBlock={jumpToBlock}
                  />
                )}

                {(data.limitations || []).length > 0 && (
                  <div style={{
                    marginTop: '22px',
                    padding: '12px 14px',
                    background: '#fffaf0',
                    border: '1px solid #ffe3ae',
                    borderRadius: '10px',
                    color: '#8a6100',
                    fontSize: '12.5px',
                    lineHeight: 1.7,
                  }}>
                    <strong style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
                      <AlertTriangle size={14} /> 解析说明与限制
                    </strong>
                    <ul style={{ margin: '8px 0 0 18px', padding: 0 }}>
                      {data.limitations.map((item, index) => (
                        <li key={index}>{item}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </main>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
