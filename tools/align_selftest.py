#!/usr/bin/env python3
"""Word-alignment self-test: spoken number words must light the written
number, never a look-alike word further on. Run: python tools/align_selftest.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cedar.tts import _align_words  # noqa: E402


def raw(*words):
    return [{"word": w, "start_time": i * 0.3, "end_time": i * 0.3 + 0.25} for i, w in enumerate(words)]


CASES = [
    # (text, spoken tokens, expected lit text per spoken token; None = unmatched)
    ("(1975, pp. 100–105), these actions were considered laudable since murdering one’s children and sick wife",
     raw("(", "nineteen", "seventy-five", ",", "pp", ".", "one", "hundred-", "one", "hundred", "and", "five", ")", ",",
         "these", "actions", "were", "considered", "laudable", "since", "murdering", "one's", "children", "and", "sick", "wife"),
     ["1975", "1975", "pp", "100–105", "100–105", "100–105", "100–105", "100–105", "100–105",
      "these", "actions", "were", "considered", "laudable", "since", "murdering", "one’s", "children", "and", "sick", "wife"]),
    ("with those of Korea (1981, p. 74): “In greetings they say that they understand one another",
     raw("with", "those", "of", "Korea", "(", "nineteen", "eighty-one", ",", "p", ".", "seventy-four", ")", ":", '"',
         "In", "greetings", "they", "say", "that", "they", "understand", "one", "another"),
     ["with", "those", "of", "Korea", "1981", "1981", "p", "74", "In", "greetings", "they", "say", "that", "they",
      "understand", "one", "another"]),
    ("Green now measures 7.22:1 on night",
     raw("Green", "now", "measures", "seven", "point", "two", "two", ":", "one", "on", "night"),
     ["Green", "now", "measures", "7.22:1", "7.22:1", "7.22:1", "7.22:1", "7.22:1", "on", "night"]),
    ("5 and 6 apples", raw("five", "and", "six", "apples"), ["5", "and", "6", "apples"]),
    ("one of the 3 kings", raw("one", "of", "the", "three", "kings"), ["one", "of", "the", "3", "kings"]),
    ("It cost $4.50 then.", raw("It", "cost", "four", "dollars", "and", "fifty", "cents", "then", "."),
     ["It", "cost", "$4.50", "$4.50", "$4.50", "$4.50", "$4.50", "then"]),
    ("the 19th century", raw("the", "nineteenth", "century"), ["the", "19th", "century"]),
    ("In 1975, 1986 came later", raw("In", "nineteen", "seventy-five", ",", "nineteen", "eighty-six", "came", "later"),
     ["In", "1975", "1975", "1986", "1986", "came", "later"]),
    ("Chapter 2. The 1990s were loud", raw("Chapter", "two", ".", "The", "nineteen", "nineties", "were", "loud"),
     ["Chapter", "2", "The", "1990s", "1990s", "were", "loud"]),
    # no digits in the text: number words stay literal, look-alikes still barred by the window
    ("We are one people, one nation", raw("We", "are", "one", "people", ",", "one", "nation"),
     ["We", "are", "one", "people", "one", "nation"]),
]

fails = 0
for text, words, expected in CASES:
    got = [(text[w["cs"]:w["ce"]] if w["cs"] >= 0 else None) for w in _align_words(text, words)]
    if got != expected:
        fails += 1
        print(f"FAIL: {text[:60]!r}\n  expected {expected}\n  got      {got}")
print(f"{len(CASES) - fails}/{len(CASES)} alignment cases pass")
sys.exit(1 if fails else 0)
