"""Classify one failed job with the rule table."""

from __future__ import annotations

from dataclasses import dataclass

from triage.rules import RULES, UNKNOWN_CATEGORY, UNKNOWN_HINT, UNKNOWN_OWNER, Rule

MAX_EVIDENCE = 500


@dataclass(frozen=True)
class Classification:
    category: str
    owner: str
    rule: str
    evidence: str | None  # the line that matched, so a person can check the decision
    hint: str


def _line_at(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start : end if end != -1 else len(text)].strip()[:MAX_EVIDENCE]


def classify(
    state: str,
    log_text: str | None,
    node_text: str | None = None,
    rules: tuple[Rule, ...] = RULES,
) -> Classification:
    """First matching rule wins; nothing matched means unknown / human_review."""
    for rule in rules:
        if rule.source == "state":
            if rule.pattern.fullmatch(state):
                return Classification(
                    rule.category, rule.owner, rule.name, f"sacct State={state}", rule.hint
                )
            continue
        text = log_text if rule.source == "log" else node_text
        if not text:
            continue
        m = rule.pattern.search(text)
        if m:
            return Classification(
                rule.category, rule.owner, rule.name, _line_at(text, m.start()), rule.hint
            )
    return Classification(UNKNOWN_CATEGORY, UNKNOWN_OWNER, "no_rule_matched", None, UNKNOWN_HINT)
