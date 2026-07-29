---
name: breadcrumbs
description: 复盘一段较长的 Claude Code 或 Codex 对话，把用户的提问变成可返回的原话锚点，重建讨论路径，核实每条派生思路留下了什么、怎样影响主问题、哪些仍然开放，并生成本地 HTML 复盘稿。当用户说 /crumbs、breadcrumbs、复盘这次 AI 对话、我刚才的思路是怎么走的、有哪些问题提了但没收束、帮我回到某个岔开的想法时触发。不用于即时提醒用户保持专注，也不替代常规内容摘要或项目 retrospective。
---

# Breadcrumbs · 思路

Breadcrumbs 的隐喻不是“把分支画漂亮”，而是：当用户在一段长对话里走散，
留下足够具体的面包屑，让他能找到来时的路，并从某个问题重新进入工作。

它把一次会话中**用户自己输入的 prompt**重建成三层复盘：

1. **一、来路图**：从哪里开始、在哪里分叉、后来又回到哪里；正文滚动时，
   侧栏小地图继续保留这份空间关系；
2. **二、复盘总览**：先认出来路，再呈现这次改变了什么、建议优先继续什么；
3. **三、思路复盘**：逐条说明每条思路留下了什么；
   对仍开放的思路提供可复制的返回提示词；
3. **原话与完整结构**：每个语义判断都能打开当时的用户输入，完整树逐条显示
   prompt 之间的连接，工程定位只在诊断层出现。

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
  对用户说思路名和原话；记录号只留在“方法与诊断”里。这条**对标注字段同样成立**：
  `yield`、`relation_note`、`resolution_evidence` 会直接渲染进正文，里面不要写轮号；
  要指某一轮就填 `evidence_turn`，页面会把它变成可点开的原话。

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

不要把整场 AI 回复灌进上下文。对每条候选思路只需读两个位置：起点之前
（判断由谁引出）、末次提问之后（判断留下了什么、是否收束）。

**默认用一条命令取回全部**，不要逐条调用：

```bash
python3 ~/.claude/skills/breadcrumbs/scripts/fetch_reply.py \
  <session.jsonl> --auto /tmp/breadcrumbs/analysis.agent.json
```

逐条回读会让每条线产生两次工具往返，而每次往返都要重新计费已累积的上下文。
真正的 token 成本在**往返次数**，不在单段回复长度——一场 8 条线的会话，
逐条是 16 次往返，`--auto` 是 1 次。

只有 `--auto` 里某段被截断、或需要 prompt 全文时，才单点补取：

```bash
# 单点补取（可一次给多个轮号，也可两种位置合并成一次调用）
python3 ~/.claude/skills/breadcrumbs/scripts/fetch_reply.py \
  <session.jsonl> --before-reply 3,7 --reply 18,25

# prompt 预览被截断时才取全文
python3 ~/.claude/skills/breadcrumbs/scripts/fetch_reply.py \
  <session.jsonl> --prompt <记录号>
```

Codex 回读在同一个命令中增加来源即可：

```bash
python3 ~/.codex/skills/breadcrumbs/scripts/fetch_reply.py \
  --source codex <task-id> --auto /tmp/breadcrumbs/analysis.agent.json
```

`analysis.agent.json` 里每条线的中段轮次会被折叠，并写明 `turns_elided` 有多少条。
折叠时优先保留首尾、转向证据和沿时间均匀分布的代表输入，不以“文字最长”
代替“最重要”。**折叠不等于没发生**：需要中间某一轮的原文时用
`--prompt <轮号>` 取。批量回读超长回复时同时保留开头与结尾；只有确实缺少
判断依据才单点补取全文。

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
| `agent_note` | 仅在推翻算法判断或记录误判时使用。**这一条渲染在「方法与诊断」里**，是唯一允许出现「轮 3」「线 4」这类记录号的字段 |

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

重新分析导致线路重划时，不要从头重标。先救回可复用的语义判断：

```bash
python3 ~/.claude/skills/breadcrumbs/scripts/realign.py \
  新的analysis.json 旧的annotations.json -o 新的annotations.json
```

按起点内容而非顺序编号配对；轮次构成变过的会自动写入待复核提示，
配不上的会明确列出，不静默丢弃。

标注身份、数量或起点不匹配时必须拒绝渲染。`--force` 只用于排错；强行生成的
页面会显示红色风险说明，不能作为正式复盘交付。

讨论路径的阶段名由已有思路名称、首次转向和休眠后回访生成，不再追加一次
LLM 总结。这样既保留“从哪里到哪里”的导航，也不为长会话重复支付总结 token。

报告必须通过本 skill 自带的 `report.py`、`render.py` 和 `assets/` 生成。Claude
与 Codex 都不得另写一份独立 HTML、内联一套临时 CSS，或根据自己的审美改动
模块顺序；否则同一份分析会因执行 Agent 不同而产生两套产品。AI 只负责填写
标注字段，页面结构、交互、字号、颜色和响应式统一由生成器决定。

来路图的交互不能靠用户猜：

- 图上直接写明“点思路名称跳到复盘、点原话查看提问、点返回复制提示词”；
- 全宽来路图负责第一次定向；侧栏“思路的面包屑”只保留小地图，并在进入
  具体思路时点亮对应节点；
- 首次分叉可在右侧思路标题的“已收束 / 未收束”旁显示“注意力漂移
  低 / 中 / 高”，左侧面包屑树不显示；必须明确它只是算法估计的语义距离，
  不是质量分、价值判断或是否应该拉回注意力的结论；
- “这次改变了什么”与“建议优先继续”的实际内容直接进入复盘总览，
  不生成跳转按钮，也不在正文重复；正文从思路账本开始；
- “未尽事宜”同时检查主问题和派生思路；主问题尚未收束时不能因为它不是
  支线而显示“没有明显悬空”；
- “完整讨论结构”必须保留为正文一级章节，章节内可展开逐条原话，
  作为来路图下面的第二级可视化；
- 返回提示词复用已有思路名、原话、关系和产出，以固定模板在本地生成，不追加
  一次长对话总结，也不把全文发送给新的模型调用。

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
