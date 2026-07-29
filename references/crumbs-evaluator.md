# `/crumbs` evaluator contract

## Purpose

Evaluate only the candidates in `crumbs.agent.json`. Do not discover new
unfinished items and do not summarize the conversation. The detector optimizes
for recall; the evaluator decides whether each supplied speech act is:

- `closed`: the supplied evidence contains a corresponding response or an
  explicit closure;
- `open`: the supplied evidence was checked and the required response is
  absent;
- `uncertain`: the evidence is truncated, semantically ambiguous, or does not
  cover enough of the conversation to support a negative claim.

The executor and evaluator must be separate when the host can route an isolated
small model. Prefer the host's small, fast model. A larger model may evaluate
only high-impact `uncertain` items.

`conversation_index` uses the positional columns declared in
`conversation_index_format`; the first column is always the human turn number.
Return exactly one verdict for every supplied candidate. Do not omit difficult
items; use `uncertain` when the compact evidence cannot support a conclusion.

## Evidence rules

1. Use only the shared `conversation_index` and the candidate's
   `source_quote`, `direct_reply`, and `evidence`.
2. Silence never proves that the user intentionally abandoned an item.
3. `open` is a negative assertion. State what response is missing and set
   `checked_range.through_turn` to the final turn in `conversation_index`.
   Use `open` only with `confidence=high`. If the compact index is too
   truncated or confidence is lower, return `uncertain` instead.
4. `closed` requires a later passage that answers, fulfills, supersedes, or
   explicitly drops the source item.
5. Do not treat an AI completion claim as proof of its own completion.
6. Do not treat a clarification answer as an answer to the original question
   unless the later evidence returns to the original question.
7. Do not infer implicit user intent. Without an explicit user statement, set
   `intent` to `unknown`.
8. Use `impact=high` only when supplied evidence shows later conclusions or
   actions depend on the unconfirmed premise or choice. Explain the dependency.

## Output

Return JSON only:

```json
{
  "schema_version": 1,
  "session_fingerprint": "copy from the packet",
  "items": [
    {
      "candidate_id": "copy from the candidate",
      "status": "closed | open | uncertain",
      "confidence": "high | medium | low",
      "owner": "assistant | user | shared",
      "fact": "Evidence-backed factual clause. Empty when uncertain.",
      "checked_range": {
        "from_turn": 12,
        "through_turn": 20
      },
      "support_turns": [13],
      "missing": "Required when status=open; otherwise null.",
      "impact": "high | medium | low",
      "impact_reason": "Required when impact=high; otherwise optional.",
      "intent": "unknown | explicit_keep | explicit_drop | explicit_done",
      "intent_turn": null,
      "intent_question": "Required for open/uncertain when intent is unknown.",
      "reason": "One short evaluator reason."
    }
  ]
}
```

`intent_question` must change the next action. Phrase it so the executor can
offer exactly three outcomes: process now, confirm it was already processed,
or deliberately ignore it. Do not collapse “already processed” into “ignore”,
and do not ask a status question whose answer changes nothing.

## Assertion language

- `open` + sufficient evidence: state the factual finding, then ask the intent
  question.
- `uncertain`: use a question for the factual uncertainty as well.
- `closed`: do not show the item to the user.
- explicit `drop`, `done`, or `keep`: do not ask again.

Facts belong to the evaluator; intention always belongs to the user.
