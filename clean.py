"""Turn a markdown assistant reply into something worth hearing out loud.

Read verbatim, a Claude response is unlistenable: fenced code, file paths,
bullet syntax, backticks. This strips it down to the prose and caps the length
so you get the gist, not a recital.
"""

import re

_MAX_INLINE_CODE = 40


def clean(text, max_chars=700, speak_code_blocks=False):
    if not text:
        return ""

    text = text.replace("﻿", "")

    # Fenced code -- summarise rather than read.
    def _fence(match):
        if speak_code_blocks:
            return match.group(2)
        lang = (match.group(1) or "").strip()
        lines = match.group(2).count("\n") + 1
        return " (%s code block, %d lines) " % (lang or "a", lines)

    text = re.sub(r"```([^\n]*)\n(.*?)```", _fence, text, flags=re.DOTALL)
    text = re.sub(r"```.*", " (code block) ", text, flags=re.DOTALL)

    # Long inline code is usually a path or a symbol -- skip it. Short is fine.
    text = re.sub(
        r"`([^`\n]+)`",
        lambda m: m.group(1) if len(m.group(1)) <= _MAX_INLINE_CODE else " that ",
        text,
    )

    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)          # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)        # links -> label
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)   # headings
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)        # bullets
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.M)      # numbered lists
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)            # quotes
    text = re.sub(r"^\s*\|.*\|\s*$", " ", text, flags=re.M)     # table rows
    text = re.sub(r"^\s*[-=_*]{3,}\s*$", " ", text, flags=re.M) # rules
    text = re.sub(r"(\*\*|__|~~)(.+?)\1", r"\2", text)          # emphasis
    text = re.sub(r"(?<!\w)[*_](\S(?:.*?\S)?)[*_](?!\w)", r"\1", text)

    # Windows paths and URLs read terribly; name them instead.
    text = re.sub(r"[A-Za-z]:\\[^\s\"'<>|]+", " that file ", text)
    text = re.sub(r"https?://\S+", " a link ", text)

    # Emoji and box-drawing leftovers.
    text = re.sub(r"[─-╿←-⇿☀-➿\U0001f000-\U0001faff]", " ", text)

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    text = text.strip()

    if max_chars and len(text) > max_chars:
        cut = text[:max_chars]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
        text = (cut[: stop + 1] if stop > max_chars * 0.5 else cut).strip()
        text += " ... see the terminal for the rest."

    return text
