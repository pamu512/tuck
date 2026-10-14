"""Scripted family for the judge demo, the CLI, and GET /sample-family.

The anecdote is sample copy for the family record. It is not a biography
and it is not a Qloo signal.
"""

SAMPLE: dict = {
    "caregiver_label": "Asha (daughter)",
    "photo_path": "family/sunday-records-1974.jpg",
    "anecdote": (
        "Every Sunday after church, she played Nat King Cole records while "
        "making lunch for the whole family."
    ),
    "interests": ["Nat King Cole", "jazz"],
    "exclusions": [
        {
            "label": "war documentaries",
            "kind": "topic",
            "reason": "Family asked to avoid war themes.",
        }
    ],
}
