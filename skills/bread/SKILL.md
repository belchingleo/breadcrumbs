---
name: bread
description: 完整复盘一段较长的 Claude Code 或 Codex 对话。用户说 /bread、完整复盘、我刚才的思路怎么走到这里、生成来路图或 HTML 复盘稿时触发。把用户原话变成稳定锚点，重建讨论路径，按需回读 AI 回复，核实每条思路留下什么、怎样影响原问题、哪些仍未收束，并生成本地交互式 HTML。不用于当前窗口的轻量欠账核查。
---

# Bread · 完整思路复盘

将线性对话还原成一份可以重新进入工作的复盘稿：来路图、复盘总览、思路复盘、
原话证据与返回提示词。不要把它做成普通 AI 总结或分析仪表盘。

## 原则

- 用户提示词是稳定锚点；AI 回复只按需回读，用来核验履约与收束状态。
- 不评判注意力漂移好坏，不生成“漂移质量分”。
- 用户可见语言只用思路名和原话，不出现“线 3”“第 18 轮”等工程编号。
- 路径连接是算法推断，保持虚线；语义名称经 Agent 回读核实后才用实心节点。
- 报告里的每个判断必须能打开原话或对应回复证据。
- 只使用随 skill 分发的报告生成器；不要临时另写 HTML/CSS。
- 完整报告与当前窗口对账相互独立；不要先自动运行 `crumbs`。

## 1. 定位会话

Claude Code：

```bash
python3 ~/.claude/skills/bread/scripts/extract_prompts.py --list --min-prompts 6
```

Codex：

```bash
python3 ~/.codex/skills/bread/scripts/extract_prompts.py \
  --source codex --list --min-prompts 6
```

Codex 候选形如 `codex:<task-id>`。也可把路径或 id 直接传给后续脚本，`--last`
使用该来源最近活动的会话。不要从文件夹名猜真实工作目录。

## 2. 零 Token 结构分析

Claude Code：

```bash
python3 ~/.claude/skills/bread/scripts/analyze.py \
  <session.jsonl> -o /tmp/breadcrumbs/analysis.json --agent-view
```

Codex：

```bash
python3 ~/.codex/skills/bread/scripts/analyze.py \
  --source codex <task-id> \
  -o /tmp/breadcrumbs/analysis.json --agent-view
```

只读取 `analysis.agent.json`，不要把整份原始 transcript 灌进上下文。分析结果中的
`analysis_id`、`thread_signature`、`anchor_quote` 和 `prompt_anchor` 是防止旧标注
错位的身份字段；原样保留。`corpus.degraded=true` 时不得假装路径高可信。

## 3. 一次性按需回读

每条候选思路只读取起点之前与末次提问之后的必要回复。默认使用一次批量调用：

```bash
python3 ~/.claude/skills/bread/scripts/fetch_reply.py \
  <session.jsonl> --auto /tmp/breadcrumbs/analysis.agent.json
```

Codex：

```bash
python3 ~/.codex/skills/bread/scripts/fetch_reply.py \
  --source codex <task-id> --auto /tmp/breadcrumbs/analysis.agent.json
```

只有片段被截断或证据确实不足时才单点补取：

```bash
python3 ~/.claude/skills/bread/scripts/fetch_reply.py \
  <session.jsonl> --before-reply 3,7 --reply 18,25
```

## 4. 最小语义标注

将标注写入 `/tmp/breadcrumbs/annotations.json`。不要增加同义字段：

```json
{
  "analysis_id": "sha256:从当前分析复制",
  "report": {
    "title": "样本是否支撑当前结论",
    "subtitle": "讨论从调整写法，转回核查案例是否支撑核心概念"
  },
  "threads": [
    {
      "id": 3,
      "thread_signature": "sha256:从当前分析复制",
      "anchor_quote": "现在这些样本是不是只说明了相关……",
      "name": "样本是否支撑当前结论",
      "topic": "核查案例与核心概念之间是否存在偷换",
      "yield": "确认了概念偷换风险，但还没有排除标准",
      "outcome": "unresolved",
      "resolved": false,
      "spawned_by": "user",
      "relation_to_trunk": "blocked",
      "relation_note": "案例选择在等这个前提被澄清",
      "evidence_turn": 18,
      "resolution_evidence": "已读末次提问后的回复；回复承认风险但没有给出标准",
      "agent_note": null
    }
  ]
}
```

字段约束：

| 字段 | 规则 |
|---|---|
| `name` | 用户能认出的短语，不写线路编号 |
| `topic` | 具体在核查、生成或决定什么 |
| `yield` | 已留下的结论、假设、材料或新问题 |
| `outcome` | `conclusion` / `assumption` / `unresolved` |
| `resolved` | 是否真正收束；可推翻算法猜测 |
| `spawned_by` | `user` / `assistant` / `both` / `unknown` |
| `relation_to_trunk` | `redirected` / `supplied` / `blocked` / `tangent` / `dropped` |
| `relation_note` | 一句话说明对原问题的影响 |
| `evidence_turn` | 语义判断应打开哪条用户原话 |
| `resolution_evidence` | 声称未解决时必填 |
| `agent_note` | 仅供方法与诊断，允许写工程编号 |

不许只看 prompt 就填 `yield`、`outcome`、`resolved`。未解决是高风险否定性断言，
必须有末次回复的回读证据。读取起点之前的回复后再判断 `spawned_by`。

报告标题说明这份复盘在处理什么明确问题，优先用“如何 / 为什么 / 是否”，建议
8–28 字；副标题只说明关键变化，不把完整路径塞进主标题。

## 5. 校验并渲染

```bash
python3 ~/.claude/skills/bread/scripts/report.py \
  /tmp/breadcrumbs/analysis.json \
  -a /tmp/breadcrumbs/annotations.json \
  -o ./breadcrumbs-<会话简称>.html
```

Codex 只替换脚本根目录。身份、数量或起点不匹配时必须拒绝渲染；不要用 `--force`
交付正式报告。重新分析导致线路变化时先恢复可复用标注：

```bash
python3 ~/.claude/skills/bread/scripts/realign.py \
  新analysis.json 旧annotations.json -o 新annotations.json
```

报告顺序固定为：

1. **来路图**：从哪里开始、在哪里分叉、最后停在哪里；
2. **复盘总览**：最初在问、最后停在、未尽事宜、改变与建议；
3. **思路复盘**：每条思路从哪里来、留下什么、怎样影响原问题；
4. **完整讨论结构**：逐条原话与第二级结构。

返回提示词复用既有思路名、原话、关系和产出在本地生成，不额外总结全文。

## 6. 交付

用三段人话汇报：

1. 哪条思路改变、补充或卡住了原问题；
2. 建议优先继续哪一条，以及可推翻的理由；
3. 其余未尽事宜与本地 HTML 路径。

不要汇报线路数和轮次。只有证据真的无法判断时才追问用户。

## 已知限制

当前适配 Claude Code JSONL 与 Codex 本地任务。字符 n-gram TF-IDF 只提供候选
结构，不理解同义改写；路径与语义名称仍需回读核实。跨会话的同主题连接尚未实现。
