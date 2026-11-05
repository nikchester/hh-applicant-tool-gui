"""Conservative title-only exclusion for explicitly senior/leadership vacancies."""

import re


SENIOR_TITLE = re.compile(
    r"(?<!\w)(?:lead|senior|sr\.?|principal|staff|head|chief|architect|"
    r"team[ -]?lead|tech[ -]?lead|"
    r"ведущ\w*|главн\w*|старш\w*|руководител\w*|начальник\w*|"
    r"директор\w*|архитектор\w*|тимлид\w*|техлид\w*|лид)(?!\w)",
    re.IGNORECASE,
)


def senior_title_reason(vacancy):
    name = vacancy.get("name") or ""
    match = SENIOR_TITLE.search(name)
    return f"уровень выше текущего опыта ({match.group()})" if match else None
