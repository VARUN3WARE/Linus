"""Points for a verified PR. JEV supplies levels on a rubric; the table and the
arithmetic live here. Uncertainty never inflates a payout: when JEV's
confidence is low we take the lower of its two most likely levels.
"""

from dataclasses import dataclass, field

from linus.diffcheck import DiffSignals
from linus.policy import Policy

LOW_CONFIDENCE = 0.5


@dataclass
class Points:
    points: float
    explanation: str
    uncertain: list[str] = field(default_factory=list)


def _levels(mapping: dict) -> dict[int, object]:
    """Score keys come back as strings after a JSON round-trip through the store."""
    return {int(k): v for k, v in mapping.items()}


def conservative_level(answer: dict) -> tuple[int, bool]:
    probs = _levels(answer["probabilities"])
    ranked = sorted(probs, key=lambda k: probs[k], reverse=True)
    if answer["confidence"] < LOW_CONFIDENCE and len(ranked) > 1:
        return min(ranked[0], ranked[1]), True
    return ranked[0], False


def difficulty_cap(signals: DiffSignals) -> int:
    """A tiny diff can't be 'hard', whatever the model or the PR text says."""
    lines = signals.code_lines_changed
    if lines <= 2:
        return 1
    if lines <= 20:
        return 2
    return 3


def score(policy: Policy, answers: dict, signals: DiffSignals) -> Points:
    uncertain = []
    if signals.docs_only:
        level, unsure = conservative_level(answers["docs_value"])
        if unsure:
            uncertain.append("docs_value")
        pts = policy.docs_points[level]
        legend = _levels(answers["docs_value"]["legend"])[level]
        return Points(pts, f"docs: {legend.split(':')[0].lower()} = {pts:g} pts", uncertain)

    difficulty, unsure_d = conservative_level(answers["difficulty"])
    impact, unsure_i = conservative_level(answers["impact"])
    if unsure_d:
        uncertain.append("difficulty")
    if unsure_i:
        uncertain.append("impact")
    capped = min(difficulty, difficulty_cap(signals))
    d_pts = policy.difficulty_points[capped]
    mult = policy.impact_multiplier[impact]
    pts = round((policy.base_points + d_pts) * mult, 2)
    d_name = _levels(answers["difficulty"]["legend"])[capped].split(":")[0].lower()
    i_name = _levels(answers["impact"]["legend"])[impact].split(":")[0].lower()
    cap_note = f" (capped from level {difficulty})" if capped < difficulty else ""
    explanation = (f"({policy.base_points:g} base + {d_pts:g} {d_name}{cap_note}) x {mult:g} {i_name} impact"
                   f" = {pts:g} pts")
    return Points(pts, explanation, uncertain)
