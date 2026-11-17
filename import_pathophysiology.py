#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import pathophysiology docx banks into the site data."""

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
SRC_DIR = BASE / "病理生理学"
OUT_JSON = BASE / "pathophysiology.json"
DATA_JS = BASE / "data.js"
INDEX_HTML = BASE / "index.html"

BANK_ID = "pathophysiology"
BANK_NAME = "病理生理学"

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
QUESTION_RE = re.compile(r"^\s*(\d+)\s*[.、．]\s*(.*)$")
ANSWER_RE = re.compile(r"^\s*答案\s*[：:]\s*([A-Ea-e]+)\s*$")
TYPE_RE = re.compile(r"^(单选题|多选题|判断题|填空题|简答题)")
OPTION_START_RE = re.compile(r"^([A-E])\s*(?:[.、．:：]\s*)?(.*)$")
COUNT_RE = re.compile(r"共\s*(\d+)\s*题")
TABLE_ANSWER_RE = re.compile(r"^\s*(\d+)\s*[.、．]\s*([A-Ea-e]+)\s*$")


def paragraph_text(paragraph):
    return "".join(node.text or "" for node in paragraph.iter(W + "t"))


def extract_document(path):
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(W + "body")
    paragraphs = []
    tables = []
    for child in list(body):
        if child.tag == W + "p":
            text = paragraph_text(child).strip()
            if text:
                paragraphs.append(text)
        elif child.tag == W + "tbl":
            rows = []
            for tr in child.findall(W + "tr"):
                cells = []
                for tc in tr.findall(W + "tc"):
                    cell_text = " ".join(
                        paragraph_text(p).strip() for p in tc.findall(W + "p")
                        if paragraph_text(p).strip()
                    )
                    cells.append(cell_text)
                rows.append(cells)
            tables.append(rows)
    return paragraphs, tables


def parse_answer_table(tables):
    answers = {}
    for table in tables:
        for row in table:
            for cell in row:
                match = TABLE_ANSWER_RE.match(cell)
                if match:
                    answers[int(match.group(1))] = match.group(2).upper()
    return answers


def split_type_prefix(rest):
    match = TYPE_RE.match(rest)
    if not match:
        return None, rest
    remainder = rest[match.end():].strip()
    remainder = re.sub(r"^[\(（]\s*\d+\s*分\s*[\)）]\s*", "", remainder)
    return match.group(1), remainder


def parse_options_from(lines, start, end):
    first = OPTION_START_RE.match(lines[start])
    if not first or first.group(1) != "A":
        return []
    text = first.group(2).strip()
    if not text:
        return []
    options = [{"key": "A", "text": text}]
    expected = "B"
    for i in range(start + 1, end):
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


def parse_block(lines, table_answer, chapter_id, chapter_name, index):
    first = QUESTION_RE.match(lines[0])
    number = int(first.group(1))
    rest = first.group(2).strip()
    type_hint, remainder = split_type_prefix(rest)
    stem_parts = []
    if remainder:
        stem_parts.append(remainder)

    answer_index = None
    inline_answer = ""
    for i in range(1, len(lines)):
        match = ANSWER_RE.match(lines[i])
        if match:
            answer_index = i
            inline_answer = match.group(1).upper()
            break

    answer_end = answer_index if answer_index is not None else len(lines)
    options = []
    candidates = []
    for i in range(1, answer_end):
        match = OPTION_START_RE.match(lines[i])
        if not match or match.group(1) != "A":
            continue
        parsed = parse_options_from(lines, i, answer_end)
        if len(parsed) >= 3:
            candidates.append((i, parsed))
    if candidates:
        option_start, options = candidates[-1]
        stem_parts.extend(line.strip() for line in lines[1:option_start] if line.strip())
    else:
        stem_parts.extend(line.strip() for line in lines[1:answer_end] if line.strip())

    answer = table_answer or inline_answer
    warnings = []
    if table_answer and inline_answer and table_answer != inline_answer:
        warnings.append("answer mismatch inline=%s table=%s" % (inline_answer, table_answer))
    stem = " ".join(part for part in stem_parts if part).strip()
    answer_keys = [ch for ch in answer]
    valid_keys = {option["key"] for option in options}
    errors = []
    if not stem:
        errors.append("empty stem")
    if not options:
        errors.append("no options")
    if not answer:
        errors.append("missing answer")
    if answer and any(key not in valid_keys for key in answer_keys):
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
    question = {
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
    }
    return question, errors, warnings


def parse_chapter(path, chapter_id, chapter_name):
    paragraphs, tables = extract_document(path)
    table_answers = parse_answer_table(tables)
    expected = None
    for line in paragraphs[:5]:
        match = COUNT_RE.search(line)
        if match:
            expected = int(match.group(1))
            break
    starts = [index for index, line in enumerate(paragraphs) if QUESTION_RE.match(line)]
    questions = []
    errors = []
    warnings = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(paragraphs)
        block = paragraphs[start:end]
        number_match = QUESTION_RE.match(block[0])
        number = int(number_match.group(1))
        question, question_errors, question_warnings = parse_block(
            block, table_answers.get(number), chapter_id, chapter_name, position + 1)
        if question_errors:
            errors.append((question["id"], number, question_errors, block[:3]))
        if question_warnings:
            warnings.append((question["id"], number, question_warnings))
        questions.append(question)
    if expected is not None and expected != len(questions):
        warnings.append(("chapter-count", chapter_id, ["expected %d, parsed %d" % (expected, len(questions))]))
    if table_answers and len(table_answers) != len(questions):
        warnings.append(("table-count", chapter_id, ["answers %d, parsed %d" % (len(table_answers), len(questions))]))
    return questions, errors, warnings, expected, len(table_answers)


def find_files():
    if not SRC_DIR.exists():
        raise SystemExit("missing folder: %s" % SRC_DIR)
    files = [name for name in os.listdir(SRC_DIR) if name.lower().endswith(".docx")]
    infos = []
    for name in files:
        path = SRC_DIR / name
        chapter_name = name.split("-")[0].strip()
        infos.append({
            "name": name,
            "path": path,
            "chapterName": chapter_name,
            "mtime": os.path.getmtime(path),
            "ctime": os.path.getctime(path),
        })
    infos.sort(key=lambda item: (item["mtime"], item["name"]))
    return infos


def parse_all():
    infos = find_files()
    chapters = []
    all_questions = []
    all_errors = []
    all_warnings = []
    for position, info in enumerate(infos):
        chapter_id = position + 1
        questions, errors, warnings, expected, table_count = parse_chapter(
            info["path"], chapter_id, info["chapterName"])
        chapters.append({
            "id": chapter_id,
            "name": info["chapterName"],
            "title": "第%d章 %s" % (chapter_id, info["chapterName"]),
            "source": info["name"],
            "mtime": info["mtime"],
            "expected": expected,
            "tableAnswers": table_count,
            "questions": questions,
        })
        all_questions.extend(questions)
        all_errors.extend(errors)
        all_warnings.extend(warnings)
    return chapters, all_questions, all_errors, all_warnings


def show_status(chapters, questions, errors, warnings, valid):
    print("pathophysiology import status")
    print("parsed: %d  valid: %d  dropped: %d" % (
        len(questions), len(valid), len(questions) - len(valid)))
    for chapter in chapters:
        print("  ch%d %-10s %4d  table=%d expected=%s  %s" % (
            chapter["id"], chapter["name"], len(chapter["questions"]),
            chapter["tableAnswers"], chapter["expected"], chapter["source"]))
    for chapter in chapters:
        chapter_questions = [q for q in questions if q["chapter"] == chapter["id"]]
        option_counts = {}
        type_counts = {}
        for question in chapter_questions:
            option_counts[len(question["options"])] = option_counts.get(len(question["options"]), 0) + 1
            type_counts[question["typeLabel"]] = type_counts.get(question["typeLabel"], 0) + 1
        print("  ch%d %-10s questions=%d options=%s types=%s" % (
            chapter["id"], chapter["name"], len(chapter_questions),
            dict(sorted(option_counts.items())), type_counts))
    multi = [q for q in questions if q["type"] == "multiple"]
    print("multiple-choice questions: %d" % len(multi))
    print("parse errors: %d" % len(errors))
    for item in errors[:20]:
        print("  %s number=%s errors=%s first=%s" % (item[0], item[1], item[2], item[3]))
    print("warnings: %d" % len(warnings))
    for item in warnings[:30]:
        print("  %s number=%s warnings=%s" % (item[0], item[1], item[2]))


def build_bank(questions, chapters):
    return {
        "id": BANK_ID,
        "name": BANK_NAME,
        "count": len(questions),
        "chapters": [
            {"id": chapter["id"], "name": chapter["name"], "title": chapter["title"]}
            for chapter in chapters
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


def write_json(questions, chapters):
    bank = build_bank(questions, chapters)
    OUT_JSON.write_text(json.dumps({
        "meta": {
            "title": BANK_NAME,
            "questionCount": len(questions),
            "chapters": bank["chapters"],
        },
        "bank": bank,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote %s (%d questions)" % (OUT_JSON.name, len(questions)))
    return bank


def apply_bank(questions, chapters):
    bank = write_json(questions, chapters)
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
    parser = argparse.ArgumentParser(description="Import pathophysiology docx banks")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    chapters, questions, errors, warnings = parse_all()
    invalid_ids = {item[0] for item in errors}
    valid = [question for question in questions if question["id"] not in invalid_ids]
    show_status(chapters, questions, errors, warnings, valid)
    if args.apply:
        apply_bank(valid, chapters)
    elif args.write:
        write_json(valid, chapters)
    if errors:
        print("import completed: %d question(s) dropped due to parse errors" % (len(questions) - len(valid)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
