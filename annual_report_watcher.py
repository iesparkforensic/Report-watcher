KEYWORDS = [
    "annual report",
    "integrated annual report",
    "integrated report",
    "annual report and notice of agm",
    "annual report alongwith notice of agm",
    "annual report along with notice of agm",
    "annual report along with the notice of agm",
    "notice of annual general meeting",
    "business responsibility and sustainability report",
    "brsr",
    "sustainability report",
    "esg report",
]

EXCLUDE_KEYWORDS = [
    "annual secretarial compliance report",
    "secretarial audit report",
    "annual performance review",
    "annual information memorandum",
    "proceedings of the annual general meeting",
    "proceedings of annual general meeting",
    "proceedings of agm",
    "voting results",
    "scrutinizer report",
    "scrutinizer's report",
    "outcome of agm",
    "outcome of the agm",
]


def matches_keyword(text: str) -> bool:
    t = (text or "").lower()
    if any(neg in t for neg in EXCLUDE_KEYWORDS):
        return False
    return any(k in t for k in KEYWORDS)
