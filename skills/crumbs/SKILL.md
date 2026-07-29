---
name: crumbs
description: 核查当前 Claude Code 或 Codex 对话还有什么没被接住。用户说 /crumbs、还有什么没说完、AI 有没有漏答漏做、有哪些待确认选择或未经确认的前提时触发；用户在上一题后回答“处理”“已处理”或“忽略”时也继续本流程。先用结构规则定位候选，再让独立 evaluator 封闭核验；逐项请用户裁决，全部回答后生成对账总结和一个下一步行动。不生成 HTML，不做完整思路复盘。
---

# Crumbs · 对话对账

只处理当前窗口。目标不是总结对话，而是找出双方可能漏接的开放回路，让用户
逐项决定：

- **处理**：加入本轮行动队列；
- **已处理**：确认此前已经闭环；
- **忽略**：主动关闭，后续不再提醒。

事实由证据核验，意图交给用户。不能把沉默解释成“用户放弃了”。

## 输出契约

1. 一次只展示一个问题，但保留本轮其余问题；
2. 使用宿主原生提问工具时，固定提供 `处理 / 已处理 / 忽略` 三个选项；
3. 用户没有回答时将问题保持为 `pending`，不得因展示过而隐藏；
4. 用户回答后立即记账并展示下一题；
5. 本轮全部问题得到裁决后，输出一次简短总结和一个下一步；
6. 若有“处理”项，安全且无需新增权限时，输出总结后直接执行第一项；
7. 若全部为“已处理”或“忽略”，明确说明没有后续行动。

逐题阶段不要添加标题、问题清单、候选数量或对话摘要。结算阶段只总结本轮裁决，
不要重新总结整段对话。

## 边界

- 默认每轮最多准备 3 个问题，按影响、证据强度和结构分排序；
- `open` 是否定性断言，必须核查到当前窗口末尾并说明缺少什么；
- `uncertain` 只要能定位具体事项且用户回答会改变行动，就用疑问语气展示；
- 无法形成具体、可裁决问题的低可信候选不展示，也不进入总结；
- evaluator 已核实闭环的事项不打扰用户，结算时只计数；
- 状态只保存候选哈希和 `pending / queued / resolved / ignored`，不保存原话；
- `/crumbs` 自己的调用和追问不进入候选；
- 完整复盘、来路图和 HTML 属于 `bread`。

## 1. 生成有限证据包

Claude Code：

```bash
python3 ~/.claude/skills/crumbs/scripts/crumbs.py prepare \
  --last -o /tmp/breadcrumbs/crumbs.json --agent-view
```

Codex：

```bash
python3 ~/.codex/skills/crumbs/scripts/crumbs.py prepare \
  --source codex --last -o /tmp/breadcrumbs/crumbs.json --agent-view
```

脚本只定位显式言语行为：多项要求、被澄清顶掉的问题、等待确认或外部动作、
AI 承诺、显式假设、完成声明、主动搁置和否定后没有替代。默认最多给 evaluator
6 个候选，每类最多 1 个；不把完整会话交给开放式总结。

若状态中有 `pending`，继续保留；只排除已经 `queued / resolved / ignored` 的候选。

## 2. 独立小模型封闭核验

不要由当前执行 Agent 自己判卷。交互式调用使用当前宿主的原生子 Agent，复用当前
认证；不要在 Claude Code 中嵌套 `claude -p`，也不要在 Codex 中嵌套
`codex exec`。

Claude Code：

- 调用已安装的 `crumbs-evaluator`，只给它
  `references/crumbs-evaluator.md` 和
  `/tmp/breadcrumbs/crumbs.agent.json`；
- 默认 `haiku`，结果校验失败时仅回退一次 `sonnet`。

Codex：

- 调用已安装的 `crumbs_evaluator`，同样只给契约和证据包路径；
- 默认 `gpt-5.6-luna`、low；校验失败时仅回退一次
  `gpt-5.6-terra`、low。

把子 Agent 的最终输出写入 `/tmp/breadcrumbs/verdicts.raw.txt`，再运行：

```bash
python3 ~/.codex/skills/crumbs/scripts/crumbs.py normalize \
  /tmp/breadcrumbs/crumbs.agent.json \
  /tmp/breadcrumbs/verdicts.raw.txt \
  -o /tmp/breadcrumbs/verdicts.json
```

Claude Code 把脚本根目录改为 `~/.claude/skills/crumbs/`。

evaluator 只能逐项判断 `closed / open / uncertain`，不得发现新事项或写总结。
AI 的“已完成”不能证明自身完成；用户沉默不能解释成放弃。两个模型都失败时停止，
不要改由当前主模型凭印象作答。

## 3. 建立本轮问题队列

```bash
python3 ~/.codex/skills/crumbs/scripts/crumbs.py present \
  /tmp/breadcrumbs/crumbs.agent.json \
  /tmp/breadcrumbs/verdicts.json \
  -o /tmp/breadcrumbs/interaction.json \
  --remember --limit 3
```

Claude Code 同样替换脚本根目录。

读取 `interaction.json` 的 `questions[0]`。有原生提问工具时，问题使用 `prompt`，
只传入其中的三个 options；没有时运行同一命令并加 `--ask`，逐字转交输出。

`--remember` 将本轮问题登记为 `pending`，不是“已经处理”。重复调用时，未回答的
问题仍会出现。

## 4. 记录回答并继续

把用户回答映射为：

```text
处理   → process
已处理 → resolved
忽略   → ignore
```

然后运行：

```bash
python3 ~/.codex/skills/crumbs/scripts/crumbs.py decide \
  /tmp/breadcrumbs/interaction.json \
  --decision process \
  -o /tmp/breadcrumbs/decision.json \
  --ask
```

按用户回答替换 `--decision`；Claude Code 替换脚本根目录。

- `decision.json.mode == "crumbs-question"`：逐字展示下一题并等待；
- `decision.json.mode == "crumbs-settlement"`：逐字展示总结与下一步。

不要在问题之间提前总结。用户纠正检测结果时接受纠正，不为检测器辩护。

## 5. 结算后进入行动

结算只包含：

- 准备处理的事项；
- 已经处理的事项；
- 主动忽略的事项；
- evaluator 自动核实闭环的数量；
- 排名最高的一项下一步。

若 `next_action` 存在，输出总结后立即执行它；需要用户材料、选择或外部权限时，
把下一步收窄成一个具体请求。若不存在，结束本轮对账。

## 已知限制

第一版对显式言语行为最可靠。隐含假设、长距离命题改口、没有明确范围的静默降级，
以及“回答了相邻但不同的问题”通常进入 `uncertain` 或不展示。精度优先于召回。
