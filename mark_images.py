#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mark questions that depend on images missing from the question bank."""

import argparse
import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parent
DATA_JS = BASE / "data.js"
INDEX_HTML = BASE / "index.html"

INDICATORS = [
    "\u5982\u56fe\u6240\u793a",
    "\u5982\u56fe",
    "\u56fe\u793a",
    "\u56fe\u4e3a",
    "\u56fe\u4e2d",
    "\u4e0b\u56fe",
    "\u4e0a\u56fe",
    "\u9644\u56fe",
    "\u7bad\u5934\u6240\u6307",
    "\u56fe1",
    "\u56fe2",
]


def needs_image(question):
    text = question.get("question", "") + " " + " ".join(
        option.get("text", "") for option in question.get("options", []) or []
    )
    return any(indicator in text for indicator in INDICATORS)


def load_banks():
    raw = DATA_JS.read_text(encoding="utf-8")
    eq = raw.find("=")
    semi = raw.rfind(";")
    if eq == -1 or semi == -1 or semi <= eq:
        raise SystemExit("cannot locate JSON inside data.js")
    return json.loads(raw[eq + 1:semi].strip())


def embed_into_index(json_text):
    html = INDEX_HTML.read_text(encoding="utf-8")
    marker = "window.QUESTION_BANKS = "
    start = html.find(marker)
    end = html.find("</script>", start)
    if start == -1 or end == -1:
        return False
    semi = html.rfind(";", start, end)
    if semi == -1:
        return False
    safe = json_text.replace("</", "<\\/")
    new_html = html[:start + len(marker)] + safe + ";" + html[semi + 1:]
    tmp = INDEX_HTML.with_suffix(".html.tmp")
    tmp.write_text(new_html, encoding="utf-8")
    os.replace(tmp, INDEX_HTML)
    return True


def scan(banks):
    counts = {}
    marked = {}
    for bank in banks:
        bank_id = bank.get("id")
        counts[bank_id] = [0, 0]
        marked[bank_id] = []
        for question in bank.get("questions", []):
            counts[bank_id][0] += 1
            if needs_image(question):
                counts[bank_id][1] += 1
                marked[bank_id].append(question.get("id"))
    return counts, marked


def main():
    parser = argparse.ArgumentParser(description="Mark image-dependent questions")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    banks = load_banks()
    counts, marked = scan(banks)

    print("image-dependent questions:")
    total = 0
    for bank in banks:
        bank_id = bank.get("id")
        total_questions, image_questions = counts[bank_id]
        total += image_questions
        print("  %-22s %d/%d" % (bank_id, image_questions, total_questions))
    print("  total                  %d" % total)

    if not args.apply:
        print("dry run: add --apply to write data.js and index.html")
        return 0

    for bank in banks:
        bank_id = bank.get("id")
        wanted = set(marked[bank_id])
        for question in bank.get("questions", []):
            if question.get("id") in wanted:
                question["needsImage"] = True
            else:
                question.pop("needsImage", None)

    json_text = json.dumps(banks, ensure_ascii=False, separators=(",", ":"))
    output = "window.QUESTION_BANKS = " + json_text + ";\n"
    tmp = DATA_JS.with_suffix(".js.tmp")
    tmp.write_text(output, encoding="utf-8")
    os.replace(tmp, DATA_JS)
    print("updated %s" % DATA_JS.name)
    if embed_into_index(json_text):
        print("updated %s" % INDEX_HTML.name)
    else:
        print("warning: could not update embedded data in %s" % INDEX_HTML.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
