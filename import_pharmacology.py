#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import pharmacology docx banks into the site data."""

import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parent
SRC_DIR = BASE / "药理学"
OUT_JSON = BASE / "pharmacology.json"
DATA_JS = BASE / "data.js"
INDEX_HTML = BASE / "index.html"

BANK_ID = "pharmacology"
BANK_NAME = "药理学"
CHAPTERS = [
    (1, "总论", "总论"),
    (2, "循环", "循环"),
    (3, "传出", "传出"),
    (4, "内分泌", "内分泌"),
    (5, "中枢", "中枢"),
]
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
QUESTION_RE = re.compile(r"^\s*(\d+)\s*[.、．]\s*(.*)$")
ANSWER_RE = re.compile(r"^\s*(?:正确答案|答案)\s*[：:]\s*([A-Ea-e]+)\s*$")
TYPE_RE = re.compile(r"^(单选题|多选题|判断题|填空题|简答题)")
OPTION_RE = re.compile(r"^([A-E])(?:[.、．:：]\s*|\s+|(?=[^\x00-\x7F]))(.*)$")
OPTION_START_RE = re.compile(r"^([A-E])(.*)$")


def paragraph_text(p):
    return "".join(t.text or "" for t in p.iter(W + "t"))


def extract_paragraphs(path):
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(W + "body")
    paragraphs = []
    for child in list(body):
        if child.tag == W + "p":
            text = paragraph_text(child).strip()
            if text:
                paragraphs.append(text)
    return paragraphs


def find_source_files():
    if not SRC_DIR.exists():
        raise SystemExit("missing folder: %s" % SRC_DIR)
    files = [name for name in os.listdir(SRC_DIR) if name.lower().endswith(".docx")]
    result = {}
    for chapter_id, chapter_name, keyword in CHAPTERS:
        matches = [name for name in files if keyword in name]
        if not matches:
            raise SystemExit("no docx found for %s in %s" % (keyword, SRC_DIR))
        if len(matches) > 1:
            matches.sort(key=len)
        result[chapter_id] = SRC_DIR / matches[0]
    return result


def split_type_prefix(rest):
    match = TYPE_RE.match(rest)
    if not match:
        return None, rest
    remainder = rest[match.end():].strip()
    remainder = re.sub(r"^[\(（]\s*\d+\s*分\s*[\)）]\s*", "", remainder)
    return match.group(1), remainder


def parse_options_from(lines, start, answer_index):
    first = OPTION_START_RE.match(lines[start])
    if not first or first.group(1) != "A":
        return []
    text = first.group(2).strip()
    if not text:
        return []
    options = [{"key": "A", "text": text}]
    expected = "B"
    for i in range(start + 1, answer_index):
        match = OPTION_START_RE.match(lines[i])
        if match and match.group(1) == expected:
            text = match.group(2).strip()
            if not text:
                continue
            options.append({"key": expected, "text": text})
            expected = chr(ord(expected) + 1)
        else:
            options[-1]["text"] = (options[-1]["text"] + " " + lines[i].strip()).strip()
    return options


def parse_block(lines, chapter_id, chapter_name, index):
    first = QUESTION_RE.match(lines[0])
    number = int(first.group(1))
    rest = first.group(2).strip()
    type_hint, remainder = split_type_prefix(rest)
    stem_parts = []
    if remainder:
        stem_parts.append(remainder)

    answer_index = None
    answer = ""
    for i in range(1, len(lines)):
        match = ANSWER_RE.match(lines[i])
        if match:
            answer_index = i
            answer = match.group(1).upper()
            break

    options = []
    if answer_index is not None:
        candidates = []
        for i in range(1, answer_index):
            match = OPTION_START_RE.match(lines[i])
            if not match or match.group(1) != "A":
                continue
            parsed = parse_options_from(lines, i, answer_index)
            if len(parsed) >= 3:
                candidates.append((i, parsed))
        if candidates:
            option_start, options = candidates[-1]
            stem_parts.extend(line.strip() for line in lines[1:option_start] if line.strip())
        else:
            stem_parts.extend(line.strip() for line in lines[1:answer_index] if line.strip())
    else:
        stem_parts.extend(line.strip() for line in lines[1:] if line.strip())

    stem = " ".join(part for part in stem_parts if part).strip()
    answer_keys = [ch for ch in answer]
    errors = []
    if not stem:
        errors.append("empty stem")
    if not options:
        errors.append("no options")
    if not answer:
        errors.append("missing answer")
    valid_keys = {option["key"] for option in options}
    if answer_index is not None and any(key not in valid_keys for key in answer_keys):
        errors.append("answer key not in options")
    question_type = "multiple" if (type_hint == "多选题" or len(answer_keys) > 1) else "single"
    type_label = "多选题" if question_type == "multiple" else "单选题"
    answer_text = [
        next((option["text"] for option in options if option["key"] == key), "")
        for key in answer_keys
    ]
    analysis_parts = lines[answer_index + 1:] if answer_index is not None else []
    analysis = " ".join(part.strip() for part in analysis_parts if part.strip()).strip()
    analysis = re.sub(r"^解析\s*[：:]\s*", "", analysis)
    return {
        "id": "%s-ch%d-%04d" % (BANK_ID, chapter_id, index),
        "volume": 1,
        "type": question_type,
        "typeLabel": type_label,
        "number": number,
        "question": stem,
        "options": options,
        "answerKeys": answer_keys,
        "answerText": answer_text,
        "topic": BANK_NAME,
        "category": "",
        "chapter": chapter_id,
        "chapterName": chapter_name,
        "chapterTitle": "第%d章 %s" % (chapter_id, chapter_name),
        "score": 1,
        "analysis": analysis,
    }, errors


def parse_chapter(path, chapter_id, chapter_name):
    paragraphs = extract_paragraphs(path)
    starts = [index for index, line in enumerate(paragraphs) if QUESTION_RE.match(line)]
    questions = []
    parse_errors = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(paragraphs)
        block = paragraphs[start:end]
        question, errors = parse_block(block, chapter_id, chapter_name, position + 1)
        if errors:
            parse_errors.append((question["id"], question["number"], errors, block[:3]))
        questions.append(question)
    return questions, parse_errors


def parse_all():
    files = find_source_files()
    banks = []
    all_questions = []
    all_errors = []
    for chapter_id, chapter_name, keyword in CHAPTERS:
        path = files[chapter_id]
        questions, errors = parse_chapter(path, chapter_id, chapter_name)
        banks.append({
            "id": BANK_ID,
            "chapterId": chapter_id,
            "chapterName": chapter_name,
            "name": "%s %s" % (BANK_NAME, chapter_name),
            "source": path.name,
            "questionCount": len(questions),
            "questions": questions,
        })
        all_questions.extend(questions)
        all_errors.extend(errors)
    return banks, all_questions, all_errors


def show_status(banks, questions, errors, valid):
    chapter_names = {chapter_id: chapter_name for chapter_id, chapter_name, keyword in CHAPTERS}
    print("pharmacology import status")
    print("parsed: %d  valid: %d  dropped: %d" % (
        len(questions), len(valid), len(questions) - len(valid)))
    for bank in banks:
        chapter_id = bank["chapterId"]
        print("  ch%d %-6s %4d  %s" % (
            chapter_id, chapter_names[chapter_id], bank["questionCount"], bank["source"]))
    for chapter_id, chapter_name, keyword in CHAPTERS:
        chapter_questions = [q for q in questions if q["chapter"] == chapter_id]
        option_counts = {}
        type_counts = {}
        for question in chapter_questions:
            option_counts[len(question["options"])] = option_counts.get(len(question["options"]), 0) + 1
            type_counts[question["typeLabel"]] = type_counts.get(question["typeLabel"], 0) + 1
        print("  ch%d %-6s questions=%d options=%s types=%s" % (
            chapter_id, chapter_name, len(chapter_questions),
            dict(sorted(option_counts.items())), type_counts))
    multi = [q for q in questions if q["type"] == "multiple"]
    print("multiple-choice questions: %d" % len(multi))
    print("parse errors: %d" % len(errors))
    for item in errors[:20]:
        print("  %s number=%s errors=%s first=%s" % (item[0], item[1], item[2], item[3]))


def build_bank(questions):
    return {
        "id": BANK_ID,
        "name": BANK_NAME,
        "count": len(questions),
        "chapters": [
            {"id": chapter_id, "name": chapter_name, "title": "第%d章 %s" % (chapter_id, chapter_name)}
            for chapter_id, chapter_name, keyword in CHAPTERS
        ],
        "questions": questions,
    }


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


def write_json(questions):
    bank = build_bank(questions)
    OUT_JSON.write_text(json.dumps({"meta": {"title": BANK_NAME, "questionCount": len(questions), "chapters": bank["chapters"]}, "bank": bank}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote %s (%d questions)" % (OUT_JSON.name, len(questions)))
    return bank


def apply_bank(questions):
    bank = write_json(questions)
    raw = DATA_JS.read_text(encoding="utf-8")
    eq = raw.find("=")
    semi = raw.rfind(";")
    banks = json.loads(raw[eq + 1:semi].strip())
    banks = [item for item in banks if item.get("id") != BANK_ID]
    banks.append(bank)
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


def main():
    parser = argparse.ArgumentParser(description="Import pharmacology docx banks")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    banks, questions, errors = parse_all()
    invalid_ids = {item[0] for item in errors}
    valid = [question for question in questions if question["id"] not in invalid_ids]
    show_status(banks, questions, errors, valid)
    if args.apply:
        apply_bank(valid)
    elif args.write:
        write_json(valid)
    if errors:
        print("import completed: %d question(s) dropped due to parse errors" % (len(questions) - len(valid)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
