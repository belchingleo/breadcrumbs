---
name: breadcrumbs
description: 复盘一段较长的 Claude Code 或 Codex 对话，把用户的提问变成可返回的原话锚点，重建讨论路径，核实每条派生思路留下了什么、怎样影响主问题、哪些仍然开放，并生成本地 HTML 复盘稿。当用户说 /crumbs、breadcrumbs、复盘这次 AI 对话、我刚才的思路是怎么走的、有哪些问题提了但没收束、帮我回到某个岔开的想法时触发。不用于即时提醒用户保持专注，也不替代常规内容摘要或项目 retrospective。
---

# Breadcrumbs · 沿原话返回

Breadcrumbs 的隐喻不是“把分支画漂亮”，而是：当用户在一段长对话里走散，
留下足够具体的面包屑，让他能找到来时的路，并从某个问题重新进入工作。

它把一次会话中**用户自己输入的 prompt**重建成三层复盘：

1. **讨论路径**：从哪里开始、转去了哪里、后来又回到哪里；
2. **复盘正文**：这次改变了什么、建议优先继续什么、每条思路留下了什么；
3. **原话证据**：每个语义判断都能打开当时的用户输入，工程定位只在诊断层出现。

## 产品边界

- 不评判“跑偏”好坏。注意力漂移是可能产生新问题的材料，不是需要被纠正的错误。
- 可以给出一条**可推翻的编辑建议**，但必须说明依据，并提供原话入口；不要生成
  “漂移质量分”或伪装成客观价值排名。
- 不是普通 AI 对话摘要。摘要压缩“说了什么”；Breadcrumbs 保存“思路怎样移动、
  哪些东西没兑现、从哪句原话能回去”。
- AI 回复**按需回读，不全量进入结构分析**。这同时控制 token、隐私和幻觉风险。
- 页面中的实心节点表示语义名称已由 Agent 回读核实；路径连接始终保留虚线，
  因为现阶段 Agent 没有逐段确认算法画出的每一次转向。
- 用户第一层语言不得出现“线 3”“第 18 轮”“断了 34 轮”之类工程标识。
  对用户说思路名和原话；记录号只留在“方法与诊断”里。

**运行前提：Python 3 标准库。** 不需要 numpy、scikit-learn、虚拟环境或联网。
读取 Codex 任务时还需要本机 `codex` 命令；脚本通过官方 App Server
`thread/list` / `thread/read` 读取，不解析不稳定的原始 transcript 格式。

## 安装与更新

在源码目录运行同一个命令即可安装或更新：

```bash
python3 scripts/install.py --target claude
python3 scripts/install.py --target codex
# 两边都使用
python3 scripts/install.py --target both
```

更新不是原地覆盖：旧版本会保留为带时间戳的 `breadcrumbs.backup-*`，确认新版本
正常后再由用户自行清理。安装包只包含 `SKILL.md`、`scripts/` 与 `assets/`。

## 工作流

脚本负责便宜、可复现的结构；Agent 负责必须读上下文才能完成的语义核实。

### 1. 定位会话

Claude Code：

```bash
python3 ~/.claude/skills/breadcrumbs/scripts/extract_prompts.py --list --min-prompts 6
```

Codex：

```bash
python3 ~/.codex/skills/breadcrumbs/scripts/extract_prompts.py \
  --source codex --list --min-prompts 6
```

Codex 候选会显示为 `codex:<task-id>`。也可直接把路径或 id 传给分析脚本；
`--last` 使用该来源最近活动的会话。Codex 沙箱首次读取本地任务状态时可能要求
一次只读权限；这是本地会话访问，不是把对话上传到网络。
会话真实工作目录以来源返回的 `cwd` 为准，不要从项目文件夹名猜。

### 2. 结构分析（0 token）

```bash
python3 ~/.claude/skills/breadcrumbs/scripts/analyze.py \
  <session.jsonl> -o /tmp/breadcrumbs/analysis.json --agent-view
```

Codex 使用同一分析流程，只替换输入适配器：

```bash
python3 ~/.codex/skills/breadcrumbs/scripts/analyze.py \
  --source codex <task-id> \
  -o /tmp/breadcrumbs/analysis.json --agent-view
```

只读取 `analysis.agent.json`。不要整份读取原始 JSONL；其中大部分是工具结果。

分析结果包含：

- `analysis_id`、每条思路的 `thread_signature` 与起点原话，用来阻止旧标注错位；
- 每条用户输入的 `prompt_anchor`，重新划线后仍可沿原话返回；
- 每条候选思路的起止位置、触发事件、休眠后回访、结构性悬空判断；
- `corpus.degraded`。如果历史语料不足，页面必须明确显示低置信度，不能装作正常。

### 3. 按需回读并做最小语义标注

不要把整场 AI 回复灌进上下文。对每条候选思路，通常只需读两个位置：

```bash
# 判断这条思路由谁引出：读起点之前的 AI 回复
python3 ~/.claude/skills/breadcrumbs/scripts/fetch_reply.py \
  <session.jsonl> --reply <起点记录号> --before

# 判断留下了什么、是否收束：读末次提问之后的 AI 回复
python3 ~/.claude/skills/breadcrumbs/scripts/fetch_reply.py \
  <session.jsonl> --reply <末次记录号>

# prompt 预览被截断时才取全文
python3 ~/.claude/skills/breadcrumbs/scripts/fetch_reply.py \
  <session.jsonl> --prompt <记录号>
```

Codex 回读在同一个命令中增加来源即可：

```bash
python3 ~/.codex/skills/breadcrumbs/scripts/fetch_reply.py \
  --source codex <task-id> --reply <末次记录号>
```

写入 `annotations.json`。顶层 `analysis_id`、每条的 `thread_signature` 和
`anchor_quote` 必须从当前分析原样复制，不能凭顺序套用旧文件：

```json
{
  "analysis_id": "sha256:从当前分析复制",
  "report": {
    "title": "样本量是否支撑当前结论",
    "subtitle": "这次讨论从调整写法，转回核查案例是否真的支撑核心概念"
  },
  "threads": [
    {
      "id": 3,
      "thread_signature": "sha256:从当前分析复制",
      "anchor_quote": "现在这些样本是不是只说明了相关，没说明因果……",
      "name": "样本量是否支撑当前结论",
      "topic": "核查案例与核心概念之间是否存在偷换",
      "yield": "确认了概念偷换风险，但还没有找到能排除它的案例",
      "outcome": "unresolved",
      "resolved": false,
      "spawned_by": "user",
      "relation_to_trunk": "blocked",
      "relation_note": "案例选择在等这个前提被澄清",
      "evidence_turn": 18,
      "resolution_evidence": "已读该思路末次提问后的 AI 回复；回复承认风险但没有给出排除标准",
      "agent_note": null
    }
  ]
}
```

报告标题不是 Claude/Codex 的会话导航标题，也不负责复述整条讨论路径：

- `report.title` 说明这份复盘在处理什么明确问题，优先写成用户能辨认的
  “如何 / 为什么 / 是否”问题，建议 8–28 字；
- `report.subtitle` 只概括这次讨论发生的关键变化，建议不超过 80 字；
- 不要把“从 A 到 B”写进主标题；变化过程属于副标题和讨论路径；
- 不使用只出现一次、尚未稳定的内部术语，不宣称仍开放的问题已经解决；
- 标题、思路名和副标题都应能回到已有原话或已核实的语义标注，不另做全量总结。

旧标注没有 `report` 时仍可渲染：页面先降级使用已核实的主问题名称，最后才使用
Claude/Codex 的宿主会话标题。

只维护这些字段，避免为了页面文案继续膨胀标注 token：

| 字段 | 填写规则 |
|---|---|
| `name` | 让用户能认出的动词或名词短语；不得写“线 3”“支路 A” |
| `topic` | 具体在核查、生成或决定什么 |
| `yield` | 已留下的结论、假设、材料或新问题；没有就明确写没有 |
| `outcome` | `conclusion` / `assumption` / `unresolved` |
| `resolved` | `true` / `false`；可推翻结构算法的猜测 |
| `spawned_by` | `user` / `assistant` / `both` / `unknown` |
| `relation_to_trunk` | `redirected` / `supplied` / `blocked` / `tangent` / `dropped` |
| `relation_note` | 一句话说明它对原问题做了什么 |
| `evidence_turn` | 可选；语义判断应打开哪条用户原话。算法假合并、起点误导时必须填 |
| `resolution_evidence` | 声称未解决时必填：读了哪段回复、看到了什么 |
| `agent_note` | 仅在推翻算法判断或记录误判时使用 |

`relation_to_trunk` 的统一含义：

| 值 | 用户可见语言 |
|---|---|
| `redirected` | 改变了主问题的走向 |
| `supplied` | 为主问题补进了材料 |
| `blocked` | 暴露了主问题的卡点 |
| `tangent` | 形成了独立问题 |
| `dropped` | 暂未回到主问题 |

**不许只看 prompt 就填 `yield`、`outcome` 或 `resolved`。** 用户问了什么，
与 AI 是否真正回答，是两件事。未解决判断必须有回读证据。

`spawned_by` 读取起点之前的回复后再判断：

- AI 刚提出了某概念或建议，用户顺着追问 → `assistant`
- 用户带入 AI 没提过的新角度 → `user`
- 双方连续共同塑形 → `both`
- 回复缺失、重试或证据不足 → `unknown`

特别核查四类误判：

1. 用户引用“不是……而是……”作为材料，可能被误判成框架修正；
2. 一条思路表面消失，但结论已经换种说法回到主问题；
3. 导出、安装等工具性问题是独立事务，不要拔高成思维突破；
4. 用户在调整人机协作方式，词汇虽远离正题，却可能真实改变后续路线。

### 4. 校验并渲染

```bash
python3 ~/.claude/skills/breadcrumbs/scripts/report.py \
  /tmp/breadcrumbs/analysis.json \
  -a /tmp/breadcrumbs/annotations.json \
  -o ./breadcrumbs-<会话简称>.html
```

标注身份、数量或起点不匹配时必须拒绝渲染。`--force` 只用于排错；强行生成的
页面会显示红色风险说明，不能作为正式复盘交付。

讨论路径的阶段名由已有思路名称、首次转向和休眠后回访生成，不再追加一次
LLM 总结。这样既保留“从哪里到哪里”的导航，也不为长会话重复支付总结 token。

### 5. 对话中汇报

不要再汇报“92 轮、10 条线、线 3 悬空”。这些数字只对工程排错有用。

用三段人话交付：

1. **改变**：点名哪条思路改变、补充或卡住了原问题；
2. **建议优先继续**：给出一个可推翻的建议及理由；
3. **仍然开放**：按语义名称列出其余问题，并给报告路径。

例：

> 这次真正改变方向的是“样本量是否支撑当前结论”：它把问题从写法调整
> 推回了概念有效性。建议先继续这条，因为案例选择仍在等它被澄清，而且你后来
> 又主动回到过它。另有“标题是否提前承诺结论”仍然开放；报告里可以直接打开
> 当时的原话。

只在 Agent 真正无法判断时追问用户，例如：“连续打磨过渡句对你来说是形成了
判断，还是只消耗了时间？”不要问“这次复盘有用吗”。

## 已知限制

- 当前适配 Claude Code JSONL 与 Codex 本地任务；claude.ai 导出格式尚未接入。
- Codex 来源依赖本机 App Server。若 `codex` 命令版本过旧、不在 PATH，或本地
  会话权限被沙箱拒绝，脚本会停止并给出具体原因，不会退回解析原始 session 文件。
- Codex 附件列表由客户端包在首条消息外层时，原话锚点只保留
  `My request for Codex` 后的实际请求，避免工程外壳污染讨论路径。
- 字符 n-gram TF-IDF 擅长词汇重叠，不理解同义改写；所以算法只提供候选结构。
- 历史语料少于阈值时会退化为本会话内拟合，页面会显式降低路径可信度。
- 短输入通常挂到相邻思路；高度重复的重发会折叠，并记入诊断信息。
- 跨会话内容签名已经预留，但“A 会话的问题是否在 C 会话解决”尚未实现。
