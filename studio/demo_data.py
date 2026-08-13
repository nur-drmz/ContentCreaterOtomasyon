"""Shared design-preview demo data.

This module is the single source of truth for the illustrative moment
content used by:

- the global `/moments/` design-preview route (studio.views.moment_explorer);
- the `seed_demo_moments` management command, which persists the same
  content as real `HotMoment` rows tagged `is_demo=True` for interface
  testing of project-specific pages.

None of this represents real analysis output. Values are illustrative only.
"""

from .pipeline.scoring import SCORE_BREAKDOWN_LABELS  # noqa: F401 - re-exported for callers


DEMO_MOMENTS = [
    {
        "id": "m01",
        "rank": 1,
        "start": "02:14",
        "end": "02:27",
        "duration": 13,
        "score": 94,
        "character": "Daemon",
        "spoiler": "Low",
        "reason": "Character reveal",
        "title": "A choice made before the words arrive",
        "summary": (
            "A restrained reaction shifts the balance of the scene and creates "
            "a clean opening for a character-motivation theory."
        ),
        "transcript": "The pause carries more meaning than the answer that follows.",
        "signals": ["Reaction", "Dialogue pause", "Theory"],
        "score_breakdown": [96, 90, 70, 65, 92, 88, 80, 91],
    },
    {
        "id": "m02",
        "rank": 2,
        "start": "04:39",
        "end": "04:55",
        "duration": 16,
        "score": 91,
        "character": "Daemon",
        "spoiler": "Medium",
        "reason": "Power shift",
        "title": "The room changes sides",
        "summary": (
            "Blocking and eyelines reveal a power shift before the scene states "
            "it directly."
        ),
        "transcript": "Everyone is watching the same person, but not for the same reason.",
        "signals": ["Composition", "Tension", "Narrative"],
        "score_breakdown": [93, 82, 60, 74, 85, 80, 70, 84],
    },
    {
        "id": "m03",
        "rank": 3,
        "start": "07:08",
        "end": "07:20",
        "duration": 12,
        "score": 88,
        "character": "Rhaenyra",
        "spoiler": "Low",
        "reason": "Emotional turn",
        "title": "The reaction the scene almost hides",
        "summary": (
            "A brief reaction shot provides evidence for an emotional reading "
            "without requiring plot-heavy context."
        ),
        "transcript": "No dialogue — the expression supplies the counterpoint.",
        "signals": ["Reaction", "Low context", "Emotion"],
        "score_breakdown": [78, 95, 40, 55, 80, 72, 65, 96],
    },
    {
        "id": "m04",
        "rank": 4,
        "start": "09:51",
        "end": "10:09",
        "duration": 18,
        "score": 85,
        "character": "Viserys",
        "spoiler": "High",
        "reason": "Foreshadowing",
        "title": "A warning disguised as reassurance",
        "summary": (
            "The spoken reassurance conflicts with the visual framing, making "
            "the beat useful for a foreshadowing explanation."
        ),
        "transcript": "The promise sounds certain; the staging makes it feel temporary.",
        "signals": ["Foreshadowing", "Dialogue", "Visual contrast"],
        "score_breakdown": [90, 76, 35, 40, 70, 91, 60, 70],
    },
    {
        "id": "m05",
        "rank": 5,
        "start": "12:26",
        "end": "12:41",
        "duration": 15,
        "score": 82,
        "character": "Daemon",
        "spoiler": "Medium",
        "reason": "Visual motif",
        "title": "The image that repeats the argument",
        "summary": (
            "A recurring visual motif turns the scene into a compact explanation "
            "of ambition and consequence."
        ),
        "transcript": "The same symbol returns, now framed as a decision.",
        "signals": ["Motif", "Cinematic", "Theory"],
        "score_breakdown": [82, 70, 55, 60, 78, 85, 68, 66],
    },
    {
        "id": "m06",
        "rank": 6,
        "start": "15:03",
        "end": "15:14",
        "duration": 11,
        "score": 79,
        "character": "Rhaenyra",
        "spoiler": "Low",
        "reason": "Hook line",
        "title": "A line that can open the Short",
        "summary": (
            "A concise line and immediate reaction form a strong cold open with "
            "minimal setup."
        ),
        "transcript": "One sentence creates the question the rest of the cut can answer.",
        "signals": ["Hook", "Dialogue", "Fast context"],
        "score_breakdown": [65, 60, 50, 45, 60, 62, 94, 88],
    },
]

for _moment in DEMO_MOMENTS:
    _moment["score_components"] = [
        {"key": key, "label": label, "weight": weight, "value": value}
        for (key, label, weight), value in zip(
            SCORE_BREAKDOWN_LABELS, _moment["score_breakdown"], strict=True
        )
    ]


def parse_timestamp(value: str) -> float:
    """Parse an ``MM:SS`` demo timestamp into whole seconds."""
    minutes, seconds = value.split(":")
    return float(int(minutes) * 60 + int(seconds))


DEMO_PIPELINE_VERSION = "demo-preview"
