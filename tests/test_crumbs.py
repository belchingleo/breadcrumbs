from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from conversation_sources import Conversation, finalize_prompts  # noqa: E402
from crumbs import (agent_view, detect_candidates, evaluator_prompt,  # noqa: E402
                    candidate_statuses, excluded_candidate_ids,
                    interaction_plan, load_state, pending_candidate_ids,
                    record_decision, register_questions,
                    render_decision_result, render_first_question,
                    render_settlement, rounds_from_conversation,
                    run_evaluator, settlement_plan,
                    remember_verified_closures, validate_verdicts)


def conversation(turns: list[tuple[str, str]]) -> Conversation:
    raw = [
        {
            "line": index,
            "source_id": f"u{index}",
            "timestamp": f"2026-01-01T00:{index:02d}:00Z",
            "text": user,
        }
        for index, (user, _) in enumerate(turns, 1)
    ]
    prompts = finalize_prompts(raw)
    stream = []
    for index, (user, assistant) in enumerate(turns, 1):
        stream.append(("human", user, f"u{index}"))
        if assistant:
            stream.append(("assistant", assistant, f"a{index}"))
    return Conversation(
        source="codex",
        source_id="synthetic-session",
        locator="codex:synthetic-session",
        title="完全虚构的闭环核查样例",
        cwd="/tmp/synthetic",
        prompts=prompts,
        stream=stream,
    )


def by_type(packet: dict, kind: str) -> list[dict]:
    return [item for item in packet["candidates"] if item["type"] == kind]


class CrumbsDetectionTests(unittest.TestCase):
    def test_round_contract_reuses_both_source_ids(self) -> None:
        convo = conversation([("请检查 README", "我会检查。")])
        rounds = rounds_from_conversation(convo)
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0].user, "请检查 README")
        self.assertIn("我会检查", rounds[0].assistant)

    def test_detects_clarification_handoff(self) -> None:
        convo = conversation([
            (
                "为什么部署总是失败？",
                "在继续之前，请你确认是在本地还是云端运行？",
            ),
            ("是在本地运行。", "好，我先调整配置。"),
            ("现在颜色也需要修改。", "我会修改颜色。"),
        ])
        packet = detect_candidates(convo, max_candidates=None)
        handoffs = by_type(packet, "clarification_handoff")
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]["source_turn"], 1)
        self.assertTrue(handoffs[0]["evidence"])

    def test_detects_high_reliability_speech_acts(self) -> None:
        convo = conversation([
            (
                "请完成三件事：\n1. 修改标题\n2. 检查链接\n3. 更新说明",
                "我假设说明只需要中文。我会先修改标题，之后再检查链接。",
            ),
            (
                "说明先放着。另外这个蓝色方案不行。",
                "请你确认要换成灰色还是黑色。",
            ),
            ("换成灰色。", "已完成全部修改。"),
        ])
        packet = detect_candidates(convo, max_candidates=None)
        kinds = {item["type"] for item in packet["candidates"]}
        self.assertTrue({
            "multi_request",
            "explicit_assumption",
            "assistant_promise",
            "user_deferred",
            "user_rejection",
            "assistant_request",
            "completion_claim",
        }.issubset(kinds))

    def test_packet_is_bounded_and_agent_view_has_contract(self) -> None:
        turns = [
            (f"请检查第 {index} 项是否完成？", "我会检查，稍后告诉你。")
            for index in range(1, 20)
        ]
        packet = detect_candidates(
            conversation(turns), max_candidates=5, max_evidence=2,
        )
        self.assertLessEqual(len(packet["candidates"]), 5)
        counts = {}
        for item in packet["candidates"]:
            counts[item["type"]] = counts.get(item["type"], 0) + 1
        self.assertTrue(all(count <= 1 for count in counts.values()))
        self.assertGreater(packet["budget"]["omitted_candidates"], 0)
        self.assertTrue(all(
            len(item["evidence"]) <= 2 for item in packet["candidates"]
        ))
        view = agent_view(packet)
        self.assertEqual(
            view["evaluator_contract"]["task"],
            "逐项判断 closed / open / uncertain，不发现新事项",
        )

    def test_pending_candidate_is_prioritized_over_new_candidates(self) -> None:
        convo = conversation([
            (
                "请完成三件事：\n1. 改标题\n2. 查链接\n3. 补说明",
                "我会先改标题。",
            ),
            ("说明先放着。", "好。"),
            ("请检查是否全部完成？", "我会检查。"),
        ])
        all_candidates = detect_candidates(convo, max_candidates=None)
        pending = min(
            all_candidates["candidates"],
            key=lambda item: item["score"],
        )
        prioritized = detect_candidates(
            convo,
            max_candidates=1,
            priority_ids={pending["id"]},
        )
        self.assertEqual(
            prioritized["candidates"][0]["id"], pending["id"],
        )
        self.assertEqual(
            prioritized["budget"]["pending_candidates"], 1,
        )

    @patch("crumbs._run_process")
    def test_no_candidates_skips_model_call(self, run_process) -> None:
        packet = detect_candidates(
            conversation([("谢谢", "不客气。")]),
            max_candidates=6,
        )
        verdicts, model = run_evaluator(packet, host="codex")
        self.assertEqual(verdicts["items"], [])
        self.assertIn("未调用模型", model)
        run_process.assert_not_called()

    def test_crumbs_does_not_turn_its_own_question_into_debt(self) -> None:
        for invocation in ("$crumbs", "/crumbs", "还有什么没说完？"):
            with self.subTest(invocation=invocation):
                packet = detect_candidates(
                    conversation([
                        (
                            invocation,
                            "原来的问题现在要处理、已处理，还是忽略？",
                        ),
                    ]),
                    max_candidates=None,
                )
                self.assertEqual(packet["candidates"], [])
                self.assertEqual(packet["conversation_index"], [])

    def test_candidate_id_survives_later_crumbs_invocation(self) -> None:
        original = conversation([
            ("为什么导出失败？", "我会检查原因。"),
        ])
        after_call = conversation([
            ("为什么导出失败？", "我会检查原因。"),
            ("$crumbs", "原来的问题现在要处理、已处理，还是忽略？"),
        ])
        before = detect_candidates(original, max_candidates=None)
        after = detect_candidates(after_call, max_candidates=None)
        before_ids = {item["id"] for item in before["candidates"]}
        after_ids = {item["id"] for item in after["candidates"]}
        self.assertEqual(before_ids, after_ids)
        self.assertEqual(after["session"]["source_human_turns"], 2)
        self.assertEqual(after["session"]["human_turns"], 1)

    def test_prepare_cli_reads_anonymized_claude_jsonl(self) -> None:
        rows = [
            {
                "type": "user", "uuid": "user-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": "为什么虚构的导出流程失败？",
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": "请你确认要导出 PDF 还是 HTML。",
                },
            },
            {
                "type": "user", "uuid": "user-2",
                "timestamp": "2026-01-01T00:01:00Z",
                "message": {"role": "user", "content": "HTML。"},
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": "我会重新检查导出流程。",
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "synthetic.jsonl"
            packet_path = Path(tmp) / "crumbs.json"
            source.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "crumbs.py"),
                    "prepare", str(source), "-o", str(packet_path),
                    "--agent-view",
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            self.assertTrue(by_type(packet, "clarification_handoff"))
            self.assertTrue(
                packet_path.with_name("crumbs.agent.json").exists()
            )


class CrumbsVerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.packet = detect_candidates(
            conversation([
                ("为什么导出失败？", "请你确认文件格式是 PDF 还是 HTML。"),
                ("是 HTML。", "我会重新检查导出流程。"),
                ("现在先处理配色。", "已修改配色。"),
            ]),
            max_candidates=None,
        )
        self.candidate = by_type(
            self.packet, "clarification_handoff",
        )[0]

    def verdict(self, **overrides) -> dict:
        target = {
            "candidate_id": self.candidate["id"],
            "status": "open",
            "confidence": "high",
            "owner": "assistant",
            "fact": "原问题在澄清之后没有找到对应回答。",
            "checked_range": {
                "from_turn": self.candidate["source_turn"],
                "through_turn": self.packet["session"]["human_turns"],
            },
            "support_turns": [],
            "missing": "缺少对导出失败原因的回答。",
            "impact": "medium",
            "impact_reason": None,
            "intent": "unknown",
            "intent_turn": None,
            "intent_question": "原来的原因现在要处理、已处理，还是忽略？",
            "reason": "后续只处理了澄清和配色。",
        }
        target.update(overrides)
        items = [target]
        for candidate in self.packet["candidates"]:
            if candidate["id"] == self.candidate["id"]:
                continue
            items.append({
                "candidate_id": candidate["id"],
                "status": "closed",
                "confidence": "high",
                "owner": candidate["owner_hint"],
                "fact": "",
                "checked_range": {
                    "from_turn": candidate["source_turn"],
                    "through_turn": self.packet["session"]["human_turns"],
                },
                "support_turns": [candidate["source_turn"]],
                "missing": None,
                "impact": "low",
                "impact_reason": None,
                "intent": "unknown",
                "intent_turn": None,
                "intent_question": "",
                "reason": "虚构测试中视为已有闭环证据。",
            })
        return {
            "schema_version": 1,
            "session_fingerprint": self.packet["session"]["session_fingerprint"],
            "items": items,
        }

    def test_rejects_foreign_session_and_evidence(self) -> None:
        foreign = self.verdict()
        foreign["session_fingerprint"] = "sha256:foreign"
        errors = validate_verdicts(self.packet, foreign)
        self.assertTrue(any("session_fingerprint" in error for error in errors))

        bad_turn = self.verdict(checked_range={
            "from_turn": self.candidate["source_turn"],
            "through_turn": 999,
        })
        errors = validate_verdicts(self.packet, bad_turn)
        self.assertTrue(any("不在会话索引" in error for error in errors))

    def test_open_requires_missing_and_question(self) -> None:
        errors = validate_verdicts(
            self.packet,
            self.verdict(missing="", fact="", intent_question=""),
        )
        self.assertTrue(any(".open 必须说明" in error for error in errors))
        self.assertTrue(any("事实陈述" in error for error in errors))
        self.assertTrue(any("intent_question" in error for error in errors))

    def test_intent_cannot_be_inferred_without_turn(self) -> None:
        errors = validate_verdicts(
            self.packet,
            self.verdict(intent="explicit_drop", intent_turn=None),
        )
        self.assertTrue(any("intent_turn" in error for error in errors))

    def test_low_confidence_cannot_make_open_assertion(self) -> None:
        errors = validate_verdicts(
            self.packet,
            self.verdict(confidence="medium"),
        )
        self.assertTrue(any("high confidence" in error for error in errors))

    def test_evaluator_prompt_contains_only_contract_and_packet(self) -> None:
        prompt = evaluator_prompt(self.packet)
        self.assertIn("# `/crumbs` evaluator contract", prompt)
        self.assertIn(self.candidate["id"], prompt)
        self.assertNotIn("/tmp/synthetic", prompt)

    @patch("crumbs.shutil.which", return_value="/usr/bin/claude")
    @patch("crumbs._run_process")
    def test_claude_evaluator_uses_haiku(
        self, run_process, _which,
    ) -> None:
        run_process.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps(self.verdict(), ensure_ascii=False),
            stderr="",
        )
        verdicts, model = run_evaluator(self.packet, host="claude")
        command = run_process.call_args.args[0]
        self.assertEqual(command[command.index("--model") + 1], "haiku")
        self.assertEqual(command[command.index("--effort") + 1], "low")
        self.assertIn("--no-session-persistence", command)
        self.assertIn("haiku", model)
        self.assertEqual(len(verdicts["items"]), len(self.packet["candidates"]))

    @patch("crumbs.shutil.which", return_value="/usr/bin/codex")
    @patch("crumbs._run_process")
    def test_codex_evaluator_falls_back_from_luna_to_terra(
        self, run_process, _which,
    ) -> None:
        verdict_text = json.dumps(self.verdict(), ensure_ascii=False)

        def fake_run(command, _prompt, *, timeout):
            model = command[command.index("--model") + 1]
            if model == "gpt-5.6-luna":
                return subprocess.CompletedProcess(
                    args=command, returncode=1,
                    stdout="", stderr="model unavailable",
                )
            output_path = Path(
                command[command.index("--output-last-message") + 1]
            )
            output_path.write_text(verdict_text, encoding="utf-8")
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout="", stderr="",
            )

        run_process.side_effect = fake_run
        verdicts, model = run_evaluator(self.packet, host="codex")
        self.assertEqual(model, "gpt-5.6-terra")
        self.assertEqual(run_process.call_count, 2)
        second_command = run_process.call_args.args[0]
        self.assertIn('model_reasoning_effort="low"', second_command)
        self.assertEqual(len(verdicts["items"]), len(self.packet["candidates"]))

    def test_interaction_plan_states_fact_then_asks_intent(self) -> None:
        plan = interaction_plan(self.packet, self.verdict(), limit=3)
        self.assertEqual(len(plan["questions"]), 1)
        question = plan["questions"][0]
        self.assertEqual(question["certainty"], "verified_open")
        self.assertIn("没有找到", question["fact"])
        self.assertIn("处理", question["question"])
        self.assertEqual(
            {option["action"] for option in question["options"]},
            {"process", "resolved", "ignore"},
        )
        self.assertEqual(
            [option["label"] for option in question["options"]],
            ["处理", "已处理", "忽略"],
        )
        self.assertTrue(question["prompt"].endswith("处理 / 已处理 / 忽略"))
        self.assertNotIn("接回来 / 放下", question["prompt"])

    def test_user_facing_output_leads_with_exactly_one_question(self) -> None:
        plan = interaction_plan(self.packet, self.verdict(), limit=3)
        rendered = render_first_question(plan)
        first_block = rendered.split("\n\n", 1)[0]
        self.assertTrue(first_block.endswith("？"))
        self.assertEqual(rendered.count("？") + rendered.count("?"), 1)
        self.assertNotIn("核查结果", rendered)
        self.assertNotIn("候选", rendered)
        self.assertNotIn("问题如下", rendered)
        self.assertIn("我这样问是因为：", rendered)
        self.assertIn("你可以直接回答：", rendered)

    def test_uncertain_uses_question_without_fact_assertion(self) -> None:
        verdict = self.verdict(
            status="uncertain",
            confidence="medium",
            fact="",
            missing=None,
            intent_question="原来的原因是不是还没有得到回答？",
        )
        plan = interaction_plan(self.packet, verdict)
        item = plan["questions"][0]
        self.assertEqual(item["certainty"], "suspected")
        self.assertIsNone(item["fact"])
        self.assertTrue(item["question"].endswith("？"))

    def test_closed_and_explicitly_dropped_items_are_suppressed(self) -> None:
        closed = self.verdict(
            status="closed",
            missing=None,
            intent_question="",
            support_turns=[self.candidate["source_turn"]],
        )
        self.assertEqual(interaction_plan(self.packet, closed)["questions"], [])

        intent_turn = self.candidate["evidence"][0]["turn"]
        dropped = self.verdict(
            intent="explicit_drop",
            intent_turn=intent_turn,
        )
        self.assertEqual(interaction_plan(self.packet, dropped)["questions"], [])

    def test_unanswered_question_stays_pending_and_is_not_excluded(self) -> None:
        plan = interaction_plan(self.packet, self.verdict(), limit=1)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            self.assertTrue(register_questions(plan, state_path))
            state = load_state(state_path)
            statuses = candidate_statuses(
                state, self.packet["session"]["session_key"],
            )
            self.assertEqual(
                statuses,
                {plan["questions"][0]["candidate_id"]: "pending"},
            )
            self.assertEqual(
                pending_candidate_ids(
                    state, self.packet["session"]["session_key"],
                ),
                {plan["questions"][0]["candidate_id"]},
            )
            self.assertEqual(
                excluded_candidate_ids(
                    state, self.packet["session"]["session_key"],
                ),
                set(),
            )

    def test_decisions_create_ledger_and_terminal_exclusions(self) -> None:
        plan = interaction_plan(self.packet, self.verdict(), limit=1)
        candidate_id = plan["questions"][0]["candidate_id"]
        for decision, expected in (
            ("process", "queued"),
            ("resolved", "resolved"),
            ("ignore", "ignored"),
        ):
            with self.subTest(decision=decision):
                with tempfile.TemporaryDirectory() as tmp:
                    state_path = Path(tmp) / "state.json"
                    register_questions(plan, state_path)
                    selected = record_decision(
                        plan, decision, state_path,
                    )
                    self.assertEqual(selected, candidate_id)
                    state = load_state(state_path)
                    self.assertEqual(
                        candidate_statuses(
                            state,
                            self.packet["session"]["session_key"],
                        )[candidate_id],
                        expected,
                    )
                    self.assertEqual(
                        excluded_candidate_ids(
                            state,
                            self.packet["session"]["session_key"],
                        ),
                        {candidate_id},
                    )

    def test_verified_closure_resolves_previous_pending_item(self) -> None:
        plan = interaction_plan(self.packet, self.verdict(), limit=1)
        candidate_id = plan["questions"][0]["candidate_id"]
        closed = self.verdict(
            status="closed",
            missing=None,
            intent_question="",
            support_turns=[self.candidate["source_turn"]],
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            register_questions(plan, state_path)
            closed_plan = interaction_plan(self.packet, closed, limit=1)
            changed = remember_verified_closures(
                closed_plan, closed, state_path,
            )
            self.assertGreaterEqual(changed, 1)
            self.assertEqual(
                candidate_statuses(
                    load_state(state_path),
                    self.packet["session"]["session_key"],
                )[candidate_id],
                "resolved",
            )

    def test_v1_seen_state_migrates_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({
                "schema_version": 1,
                "sessions": {
                    "session-old": {"seen": ["crumb-old"]},
                },
            }), encoding="utf-8")
            migrated = load_state(state_path)
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(
                candidate_statuses(migrated, "session-old"),
                {"crumb-old": "pending"},
            )

    def test_all_decisions_produce_summary_and_next_action(self) -> None:
        plan = interaction_plan(self.packet, self.verdict(), limit=1)
        first = plan["questions"][0]
        second = json.loads(json.dumps(first, ensure_ascii=False))
        second["candidate_id"] = "crumb-synthetic-resolved"
        second["source_quote"] = "请核对虚构的第二项。"
        second["missing"] = None
        second["prompt"] = "第二项如何处理？"
        third = json.loads(json.dumps(first, ensure_ascii=False))
        third["candidate_id"] = "crumb-synthetic-ignored"
        third["source_quote"] = "请核对虚构的第三项。"
        third["missing"] = None
        third["prompt"] = "第三项如何处理？"
        plan["questions"] = [first, second, third]
        plan["summary"]["questions_selected"] = 3

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            register_questions(plan, state_path)
            record_decision(plan, "process", state_path)
            middle = settlement_plan(plan, load_state(state_path))
            self.assertEqual(middle["mode"], "crumbs-question")
            self.assertEqual(middle["progress"], {"answered": 1, "total": 3})

            record_decision(plan, "resolved", state_path)
            record_decision(plan, "ignore", state_path)
            settled = settlement_plan(plan, load_state(state_path))
            self.assertEqual(settled["mode"], "crumbs-settlement")
            self.assertEqual(settled["counts"]["process"], 1)
            self.assertEqual(settled["counts"]["resolved"], 1)
            self.assertEqual(settled["counts"]["ignored"], 1)
            self.assertEqual(
                settled["next_action"]["candidate_id"],
                first["candidate_id"],
            )
            rendered = render_settlement(settled)
            self.assertIn("对账完成：3 项。", rendered)
            self.assertIn("处理：1 项", rendered)
            self.assertIn("已处理：1 项", rendered)
            self.assertIn("忽略：1 项", rendered)
            self.assertIn("下一步：补上对导出失败原因的回答。", rendered)

    def test_all_ignored_has_no_follow_up_action(self) -> None:
        plan = interaction_plan(self.packet, self.verdict(), limit=1)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            register_questions(plan, state_path)
            record_decision(plan, "ignore", state_path)
            settled = settlement_plan(plan, load_state(state_path))
            self.assertIsNone(settled["next_action"])
            self.assertTrue(
                render_decision_result(settled).endswith(
                    "下一步：没有需要继续处理的事项。"
                )
            )

    def test_cli_json_round_trip_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            packet_path = Path(tmp) / "packet.json"
            verdict_path = Path(tmp) / "verdict.json"
            plan_path = Path(tmp) / "interaction.json"
            packet_path.write_text(
                json.dumps(self.packet, ensure_ascii=False), encoding="utf-8",
            )
            verdict_path.write_text(
                json.dumps(self.verdict(), ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertEqual(json.loads(packet_path.read_text())["mode"], "crumbs")
            self.assertEqual(
                json.loads(verdict_path.read_text())["schema_version"], 1,
            )
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "crumbs.py"),
                    "present", str(packet_path), str(verdict_path),
                    "-o", str(plan_path),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(plan_path.read_text(encoding="utf-8"))["mode"],
                "crumbs-interaction",
            )

            state_path = Path(tmp) / "state.json"
            settlement_path = Path(tmp) / "settlement.json"
            decided = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "crumbs.py"),
                    "decide", str(plan_path),
                    "--decision", "process",
                    "--state", str(state_path),
                    "-o", str(settlement_path),
                    "--ask",
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(decided.returncode, 0, decided.stderr)
            settlement = json.loads(
                settlement_path.read_text(encoding="utf-8")
            )
            self.assertEqual(settlement["mode"], "crumbs-settlement")
            self.assertIn("对账完成：1 项。", decided.stdout)
            self.assertIn("下一步：", decided.stdout)

            ask = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "crumbs.py"),
                    "present", str(packet_path), str(verdict_path),
                    "--ask", "--limit", "1",
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(ask.returncode, 0, ask.stderr)
            self.assertTrue(ask.stdout.split("\n\n", 1)[0].endswith("？"))
            self.assertNotIn("交互计划", ask.stdout)
            self.assertNotIn('"summary"', ask.stdout)

            raw_path = Path(tmp) / "raw.txt"
            normalized_path = Path(tmp) / "normalized.json"
            raw_path.write_text(
                "```json\n"
                + json.dumps(self.verdict(), ensure_ascii=False)
                + "\n```\n",
                encoding="utf-8",
            )
            normalized = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "crumbs.py"),
                    "normalize", str(packet_path), str(raw_path),
                    "-o", str(normalized_path),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(normalized.returncode, 0, normalized.stderr)
            self.assertEqual(
                json.loads(normalized_path.read_text())[
                    "session_fingerprint"
                ],
                self.packet["session"]["session_fingerprint"],
            )


class CrumbsProductContractTests(unittest.TestCase):
    def test_skill_and_readme_describe_three_way_settlement(self) -> None:
        skill = (
            ROOT / "skills" / "crumbs" / "SKILL.md"
        ).read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for text in (skill, readme):
            self.assertIn("处理", text)
            self.assertIn("已处理", text)
            self.assertIn("忽略", text)
            self.assertIn("下一步", text)
        self.assertIn(
            "pending / queued / resolved / ignored", skill,
        )
        self.assertIn("全部问题得到裁决后", readme)

    def test_readme_stays_concise_and_contains_no_local_path(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(readme.splitlines()), 140)
        self.assertNotIn("/Users/", readme)
        self.assertNotIn("codex-clipboard", readme)


if __name__ == "__main__":
    unittest.main()
