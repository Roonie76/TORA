"""
Light, conservative text normalisation used before rule-based extraction and
intent classification (Phase 5B): common typos, everyday Hinglish and Hindi
written in Devanagari.

It only rewrites whole words/phrases from fixed tables, so English input is
left untouched. The original message is still what gets stored and shown.
"""

import re
from typing import List, Tuple

# Hindi written in Devanagari: digits and the words that carry money facts.
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_HINDI: List[Tuple[str, str]] = [
    ("हज़ार", "thousand"),      # hazaar
    ("हजार", "thousand"),
    ("लाख", "lakh"),
    ("करोड़", "crore"),
    ("करोड", "crore"),
    ("सैलरी", "salary"),        # salary
    ("वेतन", "salary"),              # vetan
    ("तनख्वाह", "salary"),
    ("आमदनी", "income"),
    ("किराया", "rent"),
    ("बचत", "savings"),
    ("खर्च", "expenses"),
    ("कर्ज़", "loan"),
    ("कर्ज", "loan"),
    ("किस्त", "emi"),
    ("मेरी", "my"),
    ("मेरा", "my"),
    ("मुझे", "me"),
    ("भूल जाओ", "forget"),
    ("कितना", "how much"),
    ("कितनी", "how much"),
    ("हैं", " "),
    ("है", " "),
]

_PHRASES: List[Tuple[str, str]] = [
    # Hinglish phrases (longest first)
    (r"\b(?:bhool|bhul)\s+ja(?:o|na|iye)\b", "forget"),
    (r"\b(?:kitna|kitni|kitne)\b", "how much"),
    (r"\b(?:kamata|kamati|kamate)\s+(?:hoon|hu|hun|hai|hain)\b", "i earn"),
    (r"\b(?:badh|badhkar|badha)\s+(?:gayi|gaya|gai|ke)\b", "increased"),
    (r"\bpichh?l[aie]\b", "previous"),
    (r"\bagar\b", "if"),
    (r"\babhi\b", "now"),
    (r"\bmer[aie]\b", "my"),
    (r"\bmujhe\b", "me"),
    (r"^\s*(?:main|mai)\b(?=\s+(?:\d|₹|rs\b|har\b|abhi\b|ek\b))", "i"),
    (r"\b(?:tankhwah|tankhwa|tankha|pagaar|pagar)\b", "salary"),
    (r"\b(?:kiraya|kiraaya)\b", "rent"),
    (r"\bbachat\s+(?:karta|karti|karte)\b", "save"),
    (r"\bbachat\b", "savings"),
    (r"\bkharch[ae]?\b", "expenses"),
    (r"\b(?:lagega|lagegi|dena\s+hoga|bharna\s+hoga)\b", "payable"),
    (r"\b(?:pe|par)\b(?=\s*[?.!]?\s*$)", "on"),
    # typos
    (r"\b(?:salry|slary|salery|sallary|saalry|salarry)\b", "salary"),
    (r"\b(?:incom|imcome|incme|icome)\b", "income"),
    (r"\b(?:rnet|rentt)\b", "rent"),
    (r"\b(?:savngs|savigns|saving's)\b", "savings"),
    (r"\b(?:credt|creidt|cedit)\s+card\b", "credit card"),
    (r"\b(?:ballance|balace|balnce)\b", "balance"),
    (r"\b(?:intrest|interst|intreset)\b", "interest"),
    (r"\b(?:morgage|mortage)\b", "mortgage"),
    (r"\b(?:forgt|froget)\b", "forget"),
    (r"\b(?:prevous|previos|previuos)\b", "previous"),
    (r"\bhazz?aa?r\b", "thousand"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), r) for p, r in _PHRASES]
_TRAILING_HAI = re.compile(r"\s+(?:hai|hain|hoon|hu|tha|thi)\b", re.IGNORECASE)


def normalize_message(text: str) -> str:
    if not text:
        return text
    out = text.translate(_DEVANAGARI_DIGITS)
    for hindi, english in _HINDI:
        if hindi in out:
            out = out.replace(hindi, english)
    out = re.sub(r"\s{2,}", " ", out).strip()
    for rx, repl in _COMPILED:
        out = rx.sub(repl, out)
    out = _TRAILING_HAI.sub("", out)
    # "rent forget" (Hinglish order) -> "forget my rent"
    m = re.match(r"^\s*(?:please\s+)?(?:my\s+)?(.+?)\s+forget\s*[.!]?\s*$", out, re.IGNORECASE)
    if m and len(m.group(1).split()) <= 3:
        out = f"forget my {m.group(1)}"
    # "salary how much?" (Hinglish order) -> "how much is my salary?"
    m = re.match(r"^\s*(?:my\s+)?([a-z ]{3,30}?)\s+how much\s*(\?)?\s*$", out, re.IGNORECASE)
    if m and not re.search(r"\d", out):
        out = f"how much is my {m.group(1)}?"
    return out
