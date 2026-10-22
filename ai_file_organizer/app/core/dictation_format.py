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


def _num_value(phrase: str):
    """Int value if `phrase` is a WELL-FORMED cardinal; None if it's an ambiguous run that
    must NOT be summed — a spoken digit sequence ("five five five one two three four", a
    phone number / code) or a year said in groups ("nineteen eighty four"). Summing those
    was corrupting dictations ("five five five..." -> 25), so we leave them as words."""
    words = [w for w in re.split(r"[\s-]+", phrase.lower()) if w and w != "and"]
    if not words:
        return None
    if len(words) == 1:                        # a single number word
        return _parse_cardinal(words[0])
    if any(w in _SCALES for w in words):       # has hundred/thousand/... -> structured cardinal
        return _parse_cardinal(phrase)
    if words[0] in _TENS and len(words) == 2 and words[1] in _ONES:  # "twenty five"
        return _parse_cardinal(phrase)
    return None                                # digit sequence / ambiguous -> leave as words


_ONEW = r"(?:%s)" % "|".join(sorted(_ONES, key=len, reverse=True))


def format_spoken(text: str) -> str:
    """Format spelled-out numbers/currency/percent in a dictation transcript.
    Returns text unchanged on any internal error (formatting must never eat a dictation)."""
    if not text or not text.strip():
        return text
    try:
        s = text
        # 0) Decimals: "<n> point <d> <d> ..." -> "<n>.<dd>"  (three point five -> 3.5)
        def _dec(m):
            whole = _num_value(m.group(1))
            if whole is None:
                return m.group(0)
            digits = "".join(str(_ONES[w]) for w in re.split(r"[\s-]+", m.group(2).lower()) if w in _ONES)
            return f"{whole:,}.{digits}" if digits else m.group(0)
        s = re.sub(r"\b(%s)\s+point\s+((?:%s)(?:[\s-]+(?:%s))*)" % (_SEQ, _ONEW, _ONEW),
                   _dec, s, flags=re.IGNORECASE)
        # 1) Currency with cents: "<n> dollars and <m> cents" -> "$n.mm"
        def _cc(m):
            d, c = _num_value(m.group(1)), _num_value(m.group(2))
            return "$%s.%02d" % (f"{d:,}", c) if (d is not None and c is not None) else m.group(0)
        s = re.sub(r"\b(%s)\s+dollars\s+and\s+(%s)\s+cents\b" % (_SEQ, _SEQ), _cc, s, flags=re.IGNORECASE)
        # 2) Whole-dollar: "<n> dollars" -> "$n"
        s = re.sub(r"\b(%s)\s+dollars\b" % _SEQ,
                   lambda m: ("$%s" % f"{_num_value(m.group(1)):,}") if _num_value(m.group(1)) is not None else m.group(0),
                   s, flags=re.IGNORECASE)
        # 3) Percent: "<n> percent" -> "n%"
        s = re.sub(r"\b(%s)\s+percent\b" % _SEQ,
                   lambda m: ("%s%%" % f"{_num_value(m.group(1)):,}") if _num_value(m.group(1)) is not None else m.group(0),
                   s, flags=re.IGNORECASE)
        # 4) Bare numbers — only MULTI-word, WELL-FORMED cardinals, so lone "one"/"two" in
        #    prose AND digit sequences ("five five five") are left untouched.
        s = re.sub(r"\b(%s)\b" % _SEQ,
                   lambda m: f"{_num_value(m.group(1)):,}" if (_is_multiword(m.group(1)) and _num_value(m.group(1)) is not None) else m.group(0),
                   s, flags=re.IGNORECASE)
        # 5) Catch DIGIT-form currency/percent an upstream LLM cleanup may have left
        #    half-done ("10,000 dollars" -> "$10,000", "50 percent" -> "50%").
        s = re.sub(r"\$?(\d[\d,]*(?:\.\d+)?)\s+dollars\b", r"$\1", s, flags=re.IGNORECASE)
        s = re.sub(r"(\d[\d,]*(?:\.\d+)?)\s+percent\b", r"\1%", s, flags=re.IGNORECASE)
        return s
    except Exception:
        return text


def tidy_text(text: str) -> str:
    """Deterministic formatting polish that runs regardless of the AI Cleanup toggle:
      * collapse accidental runs of spaces/tabs into one
      * remove a stray space before , . ; : ! ?
      * capitalize the standalone pronoun 'i' (covers i'm / i'll / i've / i'd)
      * capitalize the first letter of a NEW sentence after . ! ? inside the text
    Lossless on everything else; never raises (returns the input on any error)."""
    if not text or not text.strip():
        return text
    try:
        s = re.sub(r"[ \t]{2,}", " ", text)                  # collapse multi-space
        s = re.sub(r"[ \t]+([,.;:!?])", r"\1", s)            # no space before punctuation
        s = re.sub(r"\bi\b", "I", s)                          # pronoun I (also i'm, i'll, ...)
        s = re.sub(r"(?<![.!?])([.!?])(\s+)([a-z])",          # capitalize new-sentence starts
                   lambda m: m.group(1) + m.group(2) + m.group(3).upper(), s)  # (not after '...')
        return s
    except Exception:
        return text


def apply_sentence_case(text: str, before_caret, custom_terms=None) -> str:
    """Fix the FIRST letter's case based on what's right before the insertion point, the way
    a good dictation app (Whispr Flow) does. This is deterministic — it runs whether or not
    the AI Cleanup polish is on.

    `before_caret` is the text immediately before the caret in the target field, or None when
    it couldn't be read (then we leave the text unchanged — no regression). Rule:
      * start of field, or after sentence-ending punctuation (. ! ? or a newline) -> capitalize
      * anywhere mid-sentence -> lowercase a wrongly-capitalized first word, EXCEPT an acronym
        (all-caps, e.g. GPT/API) or one of the user's Custom Words (a proper noun like Filect).
    """
    if not text or before_caret is None:
        return text
    # first alphabetic character of the dictation
    i = 0
    while i < len(text) and not text[i].isalpha():
        i += 1
    if i >= len(text):
        return text
    trimmed = before_caret.rstrip(" \t")
    at_sentence_start = (not trimmed) or trimmed[-1] in ".!?\n"
    first = text[i]
    # the first word (for acronym / custom-term checks)
    j = i
    while j < len(text) and (text[j].isalnum() or text[j] in "_-"):
        j += 1
    word = text[i:j]

    if at_sentence_start:
        return text[:i] + first.upper() + text[i + 1:] if first.islower() else text

    # mid-sentence: drop the stray capital, but protect acronyms + the user's proper nouns
    if first.isupper():
        if len(word) > 1 and word.isupper():
            return text  # acronym (GPT, API, JSON)
        terms = {str(t).strip().lower() for t in (custom_terms or []) if str(t).strip()}
        if word.lower() in terms:
            return text  # user's Custom Word (proper noun)
        return text[:i] + first.lower() + text[i + 1:]
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
