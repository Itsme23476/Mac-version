"""Deterministic spoken-number → written-form formatter for dictation output.

Grok Voice Transcribe returns numbers/currency/percent fully spelled out
("ten thousand dollars", "fifty percent") — no inverse text normalization. This
converts the high-confidence, context-free cases to their written forms the way a
dictation app (Whispr Flow) does:

    "ten thousand dollars"                        -> "$10,000"
    "fifty percent"                               -> "50%"
    "twenty five dollars and ninety nine cents"   -> "$25.99"
    "two hundred fifty megabytes"                 -> "250 megabytes"
    "I need ten thousand"                         -> "I need 10,000"

Safety first — it is LOSSLESS on anything it doesn't recognize, and deliberately:
  * converts a bare number only when it's MULTI-word (a real quantity like "twenty
    five" / "ten thousand"); a lone small word ("I have one question") is LEFT alone
    so prose isn't mangled into "1 question".
  * does NOT touch context-dependent symbols — email "at"->@, "dot"->., math
    "plus"/"equals", dates, times, phone numbers. Those are ambiguous without
    context and belong to the optional LLM polish pass, not here.

# ponytail: deterministic cardinals + currency + percent only. Full context-aware
# formatting (dates/times/phones/emails/math symbols) is the LLM-polish job, not this.
"""
import re

_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000, "billion": 1000000000}
_NUMWORD = set(_ONES) | set(_TENS) | set(_SCALES)

# One number word, then more number words separated by space/hyphen, with an optional
# "and" between them ("one hundred and five"). Case-insensitive at match time.
_W = r"(?:%s)" % "|".join(sorted(_NUMWORD, key=len, reverse=True))
_SEQ = r"%s(?:[\s-]+(?:and[\s-]+)?%s)*" % (_W, _W)


def _parse_cardinal(phrase: str) -> int:
    """'two hundred fifty' -> 250, 'ten thousand' -> 10000, 'twenty five' -> 25."""
    total = current = 0
    for w in re.split(r"[\s-]+", phrase.lower()):
        if w in ("", "and"):
            continue
        if w in _ONES:
            current += _ONES[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred":
            current = (current or 1) * 100
        elif w in _SCALES:          # thousand / million / billion
            total += (current or 1) * _SCALES[w]
            current = 0
    return total + current


def _is_multiword(phrase: str) -> bool:
    return bool(re.search(r"[\s-]", phrase.strip()))


def format_spoken(text: str) -> str:
    """Format spelled-out numbers/currency/percent in a dictation transcript.
    Returns text unchanged on any internal error (formatting must never eat a dictation)."""
    if not text or not text.strip():
        return text
    try:
        s = text
        # 1) Currency with cents: "<n> dollars and <m> cents" -> "$n.mm"
        s = re.sub(
            r"\b(%s)\s+dollars\s+and\s+(%s)\s+cents\b" % (_SEQ, _SEQ),
            lambda m: "$%s.%02d" % (f"{_parse_cardinal(m.group(1)):,}", _parse_cardinal(m.group(2))),
            s, flags=re.IGNORECASE)
        # 2) Whole-dollar: "<n> dollars" -> "$n"
        s = re.sub(r"\b(%s)\s+dollars\b" % _SEQ,
                   lambda m: "$%s" % f"{_parse_cardinal(m.group(1)):,}",
                   s, flags=re.IGNORECASE)
        # 3) Percent: "<n> percent" -> "n%"
        s = re.sub(r"\b(%s)\s+percent\b" % _SEQ,
                   lambda m: "%s%%" % f"{_parse_cardinal(m.group(1)):,}",
                   s, flags=re.IGNORECASE)
        # 4) Bare numbers — only MULTI-word spans (real quantities), so lone "one"/"two"
        #    in prose are left untouched.
        s = re.sub(r"\b(%s)\b" % _SEQ,
                   lambda m: f"{_parse_cardinal(m.group(1)):,}" if _is_multiword(m.group(1)) else m.group(0),
                   s, flags=re.IGNORECASE)
        return s
    except Exception:
        return text


if __name__ == "__main__":
    cases = [
        # (input, expected)
        ("I need ten thousand dollars, which is fifty percent of the budget.",
         "I need $10,000, which is 50% of the budget."),
        ("It costs twenty five dollars and ninety nine cents.",
         "It costs $25.99."),
        ("The file is over two hundred fifty megabytes.",
         "The file is over 250 megabytes."),
        ("Transfer ten thousand to savings.",
         "Transfer 10,000 to savings."),
        ("one hundred and five people showed up",
         "105 people showed up"),
        ("two million dollars", "$2,000,000"),
        ("ninety nine percent sure", "99% sure"),
        # safety: lone small number words in prose are NOT converted
        ("I have one question for you", "I have one question for you"),
        ("give me a second", "give me a second"),
        # safety: plain text with no numbers is untouched
        ("The quick brown fox.", "The quick brown fox."),
        # cents-only edge stays sane (no dollars) — left as spoken (not a currency unit here)
    ]
    ok = True
    for src, exp in cases:
        got = format_spoken(src)
        flag = "OK " if got == exp else "FAIL"
        if got != exp:
            ok = False
        print(f"[{flag}] {src!r}\n       -> {got!r}" + ("" if got == exp else f"\n   WANT: {exp!r}"))
    # parser unit checks
    assert _parse_cardinal("twenty five") == 25
    assert _parse_cardinal("two hundred fifty") == 250
    assert _parse_cardinal("ten thousand") == 10000
    assert _parse_cardinal("one hundred and five") == 105
    assert _parse_cardinal("two thousand twenty four") == 2024
    print("\nparser checks passed ✓" if ok else "\nSOME CASES FAILED ✗")
