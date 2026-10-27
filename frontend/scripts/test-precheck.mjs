/**
 * 批次内查重的回归测试（无需浏览器、无需网络）。
 * 运行：npm run test:precheck
 */
import { detectBatchConflicts, extractArxivIdentity } from '../src/precheck.js'

const cases = [
  {
    name: 'v1 + v7 同批（库为空时的真实场景）',
    files: ['1706.03762v1.pdf', '1706.03762v7.pdf'],
    expect: 1,
    contain: '1706.03762v7.pdf',
  },
  { name: '只选一个文件', files: ['1706.03762v7.pdf'], expect: 0 },
  {
    name: '无版本号 + v7（无法判断新旧）',
    files: ['1706.03762.pdf', '1706.03762v7.pdf'],
    expect: 1,
    contain: '没带版本号',
  },
  {
    name: '同名重复选择',
    files: ['1706.03762v7.pdf', '1706.03762v7.pdf'],
    expect: 1,
    contain: '被重复选择',
  },
  { name: '两篇不同论文', files: ['2103.00020v1.pdf', '1706.03762v7.pdf'], expect: 0 },
  { name: '无关文件名', files: ['draft.pdf', 'slides final.pdf'], expect: 0 },
  {
    name: '编号嵌在长文件名里',
    files: ['Attention Is All You Need 1706.03762v1.pdf', '注意力 1706.03762v7.pdf'],
    expect: 1,
  },
  {
    name: 'v2 与 v10 按数值比较版本',
    files: ['1706.03762v2.pdf', '1706.03762v10.pdf'],
    expect: 1,
    contain: '1706.03762v10.pdf',
  },
]

let failed = 0
for (const item of cases) {
  const warnings = detectBatchConflicts(item.files.map(name => ({ name })))
  const ok =
    warnings.length === item.expect &&
    (!item.contain || warnings.join('').includes(item.contain))
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${item.name} → ${warnings.length} 条（期望 ${item.expect}）`)
  if (!ok) failed += 1
}

const identity = extractArxivIdentity('1706.03762v7.pdf')
const identityOk = identity.arxivId === '1706.03762' && identity.version === 7
console.log(`${identityOk ? 'PASS' : 'FAIL'}  编号与版本解析 → ${JSON.stringify(identity)}`)
if (!identityOk) failed += 1

const total = cases.length + 1
console.log(`\n${total - failed}/${total} 通过`)
process.exit(failed === 0 ? 0 : 1)
