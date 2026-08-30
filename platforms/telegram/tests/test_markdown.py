#!/usr/bin/env python3
"""
Unit tests for the markdown -> Telegram HTML converter in platform.py
(markdown_to_html / strip_markdown). Pure stdlib, no network, no subprocess.

Usage:
    python3 tests/test_markdown.py
Exit code 0 on success, 1 on failure.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from platform import markdown_to_html, strip_markdown  # noqa: E402

FAILURES = []


def check(cond, label):
    if cond:
        print("PASS: " + label)
    else:
        FAILURES.append(label)
        print("FAIL: " + label)


# -- inline ----------------------------------------------------------------
check(markdown_to_html("**bold**") == "<b>bold</b>", "bold **x** -> <b>x</b>")
check(markdown_to_html("*italic*") == "<i>italic</i>", "italic *x* -> <i>x</i>")
check(markdown_to_html("_italic_") == "<i>italic</i>", "italic _x_ -> <i>x</i>")
check(markdown_to_html("`code`") == "<code>code</code>",
      "code `x` -> <code>x</code>")
check(markdown_to_html("[text](https://example.com)")
      == '<a href="https://example.com">text</a>', "link -> <a href>")
check(markdown_to_html("~~gone~~") == "<s>gone</s>", "strike ~~x~~ -> <s>x</s>")
check(markdown_to_html("__bold__") == "<b>bold</b>", "bold __x__ -> <b>x</b>")
check(markdown_to_html("**b _i_**") == "<b>b <i>i</i></b>",
      "nested bold+italic")
check(markdown_to_html("a **b** c") == "a <b>b</b> c",
      "bold inline in sentence")
check(markdown_to_html("a *b* c") == "a <i>b</i> c",
      "italic inline in sentence")

# -- code protects content -------------------------------------------------
check(markdown_to_html("`**x**`") == "<code>**x**</code>",
      "code span protects ** from formatting")
check(markdown_to_html("`_x_`") == "<code>_x_</code>",
      "code span protects _ from formatting")
check(markdown_to_html("[**bold**](https://x.com)")
      == '<a href="https://x.com">bold</a>',
      "link label with markers stripped (Telegram forbids tags in <a>)")

# -- block level -----------------------------------------------------------
check(markdown_to_html("# Heading") == "<b>Heading</b>", "heading -> bold")
check(markdown_to_html("## Sub") == "<b>Sub</b>", "subheading -> bold")
check(markdown_to_html("> quote") == "<blockquote>quote</blockquote>",
      "blockquote -> <blockquote>")
check(markdown_to_html("```\nfenced\n```") == "<pre>fenced</pre>",
      "fenced block -> <pre>")
check(markdown_to_html("```py\nx = 1\n```") == "<pre>x = 1</pre>",
      "fenced block with language tag")
check(markdown_to_html("- item") == "\u2022 item",
      "unordered list item -> bullet")
check(markdown_to_html("1. item") == "1. item",
      "ordered list item keeps number")
check(markdown_to_html("---") == "\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015",
      "hr -> separator")
check(markdown_to_html("**b**\n\nplain") == "<b>b</b>\n\nplain",
      "multi-line keeps line breaks")

# -- escaping --------------------------------------------------------------
check(markdown_to_html("R&D <tag>") == "R&amp;D &lt;tag&gt;",
      "html special chars escaped")
check(markdown_to_html("a & b") == "a &amp; b", "ampersand escaped")
check(markdown_to_html("**b** & <x>") == "<b>b</b> &amp; &lt;x&gt;",
      "escaped text around converted span")
check(markdown_to_html("[t](https://x.com?a=1&b=2)")
      == '<a href="https://x.com?a=1&amp;b=2">t</a>',
      "link url escaped")

# -- degenerate / unbalanced -----------------------------------------------
check(markdown_to_html("unclosed **bold") == "unclosed bold",
      "unbalanced ** stripped")
check(markdown_to_html("unclosed `tick") == "unclosed tick",
      "unbalanced backtick stripped")
check(markdown_to_html("") == "", "empty input")
check("**" not in markdown_to_html("**a** and *b* and `c`"),
      "no raw ** anywhere after conversion")
check("```" not in markdown_to_html("```\nx\n```"), "no raw fence markers")
check(markdown_to_html("some_file.txt") == "some_file.txt",
      "single underscore (filename) untouched")
check(markdown_to_html("2 * 3") == "2 * 3",
      "lone asterisk (multiplication) untouched")

# -- strip_markdown (plain fallback) ---------------------------------------
check(strip_markdown("**bold** and `code`") == "bold and code",
      "strip: removes ** and backticks")
check(strip_markdown("# Head") == "Head", "strip: heading marker removed")
check(strip_markdown("> quote") == "quote", "strip: blockquote marker removed")
check(strip_markdown("- item") == "\u2022 item", "strip: list marker -> bullet")
check(strip_markdown("```\ncode\n```") == "code", "strip: fence lines dropped")
check(strip_markdown("[t](https://x.com)") == "t (https://x.com)",
      "strip: link -> label (url)")

print("")
if FAILURES:
    print("UNIT TEST FAILED: {} failure(s)".format(len(FAILURES)))
    sys.exit(1)
print("UNIT TEST PASSED - markdown_to_html/strip_markdown behave correctly")
sys.exit(0)
