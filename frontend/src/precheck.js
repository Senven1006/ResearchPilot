/**
 * 上传前的批次内查重。
 *
 * 后端的 /papers/duplicates/check 只在「已入库」的数据里找同编号论文，
 * 因此一次选中 v1 与 v7 两个文件时，两份都还没入库，谁也发现不了谁。
 * 这里按文件名里的 arXiv 编号先做一次批次内比对，把这种冲突提前暴露出来。
 */

const ARXIV_PATTERN = /(?<!\d)(\d{4}\.\d{4,5})(?:v(\d+))?/i

export function extractArxivIdentity(filename) {
  const match = String(filename || '').match(ARXIV_PATTERN)
  if (!match) return { arxivId: null, version: null }
  return {
    arxivId: match[1],
    version: match[2] ? Number(match[2]) : null,
  }
}

function describe(entry) {
  return entry.version === null
    ? `${entry.name}（未标版本号）`
    : `${entry.name}（v${entry.version}）`
}

/**
 * @param {Array<{name: string}>} files 本次选中的文件
 * @returns {string[]} 需要提示用户注意的冲突说明，每条形如「【本次上传】…」
 */
export function detectBatchConflicts(files) {
  const groups = new Map()

  for (const file of files || []) {
    const { arxivId, version } = extractArxivIdentity(file?.name)
    if (!arxivId) continue
    if (!groups.has(arxivId)) groups.set(arxivId, [])
    groups.get(arxivId).push({ name: file.name, version })
  }

  const warnings = []

  for (const [arxivId, entries] of groups) {
    if (entries.length < 2) continue

    const versioned = entries.filter(entry => entry.version !== null)
    const unversioned = entries.filter(entry => entry.version === null)
    const distinct = Array.from(new Set(versioned.map(entry => entry.version)))
    const listed = entries.map(describe).join('、')

    // 有文件没写版本号时，新旧关系无法从文件名判断，只能提示人工核对
    if (unversioned.length > 0 && distinct.length > 0) {
      warnings.push(
        `【本次上传】同一篇论文（arXiv ${arxivId}）出现多次：${listed}。` +
          '其中至少一份文件名没带版本号，无法判断新旧；' +
          '建议只保留标注了版本号的那份，或先确认内容是否相同。\n'
      )
      continue
    }

    if (distinct.length > 1) {
      const newest = Math.max(...distinct)
      const newestFile = versioned.find(entry => entry.version === newest)
      warnings.push(
        `【本次上传】同一篇论文（arXiv ${arxivId}）选了多个版本：${listed}。` +
          `建议只上传最新版本（${newestFile ? newestFile.name : `v${newest}`}），` +
          '同时入库会让同一篇论文占两份索引，问答与对比时证据会被拆开。\n'
      )
      continue
    }

    warnings.push(
      `【本次上传】同一篇论文（arXiv ${arxivId}）被重复选择：${listed}。` +
        '若两份内容字节不同，入库后会出现同名论文的两份索引，建议先删掉多余的那份。\n'
    )
  }

  return warnings
}
