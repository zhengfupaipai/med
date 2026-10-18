#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate built-in pathology explanations for selected chapters.

Default target: pathology-2a, chapters 3 (局部血液循环障碍), 9 (心血管系统疾病),
6 (肿瘤).

Usage:
    python generate_analysis.py
    python generate_analysis.py --status
    python generate_analysis.py --apply
    python generate_analysis.py --limit 2
    python generate_analysis.py --reset

Progress is saved after every question, so re-running resumes. Timeouts and
transient API errors are retried. --apply writes analyses into data.js and the
embedded data inside index.html.
"""

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parent
API_FILE = BASE / "api.txt"
DATA_JS = BASE / "data.js"
INDEX_HTML = BASE / "index.html"
PROGRESS_FILE = BASE / "analysis_progress.json"

BANK_ID = "pathology-2a"
CHAPTERS = [3, 6, 9]
CHAPTER_NAMES = {
    3: "局部血液循环障碍",
    6: "肿瘤",
    9: "心血管系统疾病",
}

API_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
API_URL = API_BASE + "/chat/completions"
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

SYSTEM_PROMPT = "\n".join([
    "你是一名病理学教师，正在为准备考试的中国医学生讲解病理学选择题。请根据题干、选项和标准答案，输出可直接用于复习的详细解析。",
    "",
    "输出格式（纯文本，不要 Markdown，不要表格）：",
    "答案：<正确选项字母>",
    "核心考点：<一句话点明本题考查的病理学概念>",
    "解析：<2-4 句，说明正确答案的病理机制、形态学或临床联系，并点出本题题眼>",
    "选项分析：",
    "A. <正确/错误>：<理由>",
    "B. <正确/错误>：<理由>",
    "（每个选项单独一行，必须全部分析，不得省略）",
    "易混点：<1-3 句，辨析最易混淆的相近病变或概念，给出记忆要点>",
    "",
    "要求：",
    "- 以标准答案为准，不得质疑或修改答案。",
    "- 错误选项要说明它对应什么疾病或病变、错在哪个概念，不能只写“不符合”。",
    "- 正确选项要讲清病理学依据，优先覆盖考试常考机制、肉眼/镜下形态、好发部位和临床联系。",
    "- 题干提到图片但未提供时，注明“题目图片未提供”，仅依据文字信息分析，不编造图像细节。",
    "- 语言准确、条理清楚，避免空话和重复题干，总长度约 500-900 字。",
])


class FatalApiError(Exception):
    pass


def read_key():
    if not API_FILE.exists():
        raise SystemExit("api.txt not found in %s" % BASE)
    key = API_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit("api.txt is empty")
    return key


def load_bank():
    raw = DATA_JS.read_text(encoding="utf-8")
    eq = raw.find("=")
    semi = raw.rfind(";")
    if eq == -1 or semi == -1 or semi <= eq:
        raise SystemExit("cannot locate JSON inside data.js")
    banks = json.loads(raw[eq + 1:semi].strip())
    for bank in banks:
        if bank.get("id") == BANK_ID:
            return bank
    raise SystemExit("bank %s not found" % BANK_ID)


def selected_questions(bank, all_questions=False):
    questions = bank.get("questions", [])
    if all_questions:
        return questions
    return [q for q in questions if q.get("chapter") in CHAPTERS]


def load_progress():
    if PROGRESS_FILE.exists():
        try:
            data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("results", {})
                data.setdefault("model", DEFAULT_MODEL)
                return data
        except Exception as exc:
            print("warning: cannot read progress: %s" % exc, flush=True)
    return {
        "bank": BANK_ID,
        "chapters": CHAPTERS,
        "model": DEFAULT_MODEL,
        "updated_at": None,
        "results": {},
    }


def save_progress(progress):
    progress["updated_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    tmp = PROGRESS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, PROGRESS_FILE)


def analysis_messages(question):
    options = "\n".join(
        "%s. %s" % (o.get("key", ""), o.get("text", ""))
        for o in question.get("options", []) or []
    )
    answer = "、".join(question.get("answerKeys", []) or [])
    answer_text = question.get("answerText", []) or []
    if answer_text:
        answer += "（" + "；".join(answer_text) + "）"
    chapter = question.get("chapterTitle") or (
        "第%d章 %s" % (question.get("chapter"), CHAPTER_NAMES.get(question.get("chapter"), ""))
        if question.get("chapter") else ""
    )
    content = "\n".join(part for part in [
        "章节：" + chapter if chapter else "",
        "题目：" + question.get("question", ""),
        "选项：",
        options,
        "标准答案：" + answer,
    ] if part)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def call_api(key, model, messages, timeout, max_tokens):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": False,
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("empty choices")
    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    reasoning = (message.get("reasoning_content") or "").strip()
    return content, reasoning, choices[0].get("finish_reason"), body.get("usage") or {}


def clean_text(text):
    text = re.sub(r"```[A-Za-z]*", "", text)
    text = text.replace("```", "")
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    return text.strip()


def validate_analysis(text, question):
    text = clean_text(text)
    if len(text) < 250:
        return False, "too short (%d chars)" % len(text)
    if len(text) > 3000:
        return False, "too long (%d chars)" % len(text)
    for section in ("答案", "核心考点", "解析", "选项分析", "易混点"):
        if section not in text:
            return False, "missing section: %s" % section

    answer_match = re.search(r"答案\s*[:：]\s*([A-Ea-e][A-Ea-e、,，\s]*)", text)
    if not answer_match:
        return False, "missing answer line"
    found_answers = set(re.findall(r"[A-E]", answer_match.group(1).upper()))
    expected = set(str(k).upper() for k in question.get("answerKeys", []) or [])
    if expected and found_answers != expected:
        return False, "answer mismatch: %s != %s" % (
            "".join(sorted(found_answers)), "".join(sorted(expected)))

    option_keys = [str(o.get("key", "")).upper() for o in question.get("options", []) or []]
    analyzed = {}
    for line in text.splitlines():
        match = re.match(r"\s*([A-E])\s*[.、．:：]?\s*(正确|错误)", line)
        if match and match.group(1) not in analyzed:
            analyzed[match.group(1)] = match.group(2)
    missing_options = [key for key in option_keys if key not in analyzed]
    if missing_options:
        return False, "missing option analysis: %s" % ",".join(missing_options)
    if option_keys and not any(analyzed.get(key) == "正确" for key in option_keys):
        return False, "no option marked correct"
    if len(option_keys) > 1 and not any(analyzed.get(key) == "错误" for key in option_keys):
        return False, "no option marked wrong"
    return True, ""


def generate_one(question, key, args):
    base_messages = analysis_messages(question)
    max_tokens = args.max_tokens
    errors = []
    quality_reason = ""
    for attempt in range(args.retries + 1):
        messages = base_messages
        if quality_reason:
            messages = base_messages + [{
                "role": "user",
                "content": "上一次输出未通过质量检查：%s。请重新输出完整解析，严格包含答案、核心考点、解析、逐个选项的正确/错误分析以及易混点。" % quality_reason,
            }]
        try:
            content, reasoning, finish, usage = call_api(
                key, args.model, messages, args.timeout, max_tokens)
            text = clean_text(content or reasoning or "")
            if text:
                ok, reason = validate_analysis(text, question)
                if ok:
                    return text, errors, usage
                quality_reason = reason
                errors.append("quality: " + reason)
                print("[quality] %s %s" % (question.get("id"), reason), flush=True)
                if finish == "length":
                    max_tokens = min(max_tokens * 2, 16384)
            else:
                errors.append("empty content (finish=%s)" % finish)
                if finish == "length":
                    max_tokens = min(max_tokens * 2, 16384)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if exc.code in (401, 402, 403):
                raise FatalApiError("HTTP %d: %s" % (exc.code, detail))
            errors.append("HTTP %d: %s" % (exc.code, detail))
        except (socket.timeout, TimeoutError):
            errors.append("timeout after %ss" % args.timeout)
        except urllib.error.URLError as exc:
            errors.append("network: %r" % (exc.reason,))
        except Exception as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))
        if attempt < args.retries:
            delay = min(args.backoff * (2 ** attempt), 60)
            print("[retry] %s in %ds (%s)" % (question.get("id"), delay, errors[-1]), flush=True)
            time.sleep(delay)
    return None, errors, {}


def run_generate(args):
    key = read_key()
    bank = load_bank()
    questions = selected_questions(bank, args.all)
    progress = load_progress()
    if progress.get("model") != args.model and progress.get("results"):
        print("warning: progress model is %s, now using %s" %
              (progress.get("model"), args.model), flush=True)
    progress["model"] = args.model
    progress["chapters"] = "all" if args.all else CHAPTERS
    results = progress["results"]
    invalid = []
    for question in questions:
        text = results.get(question["id"])
        if not text:
            continue
        ok, reason = validate_analysis(text, question)
        if not ok:
            invalid.append((question["id"], reason))
            del results[question["id"]]
    if invalid:
        save_progress(progress)
        print("removed %d existing result(s) failing quality" % len(invalid), flush=True)
        for qid, reason in invalid[:10]:
            print("  %s %s" % (qid, reason), flush=True)
    missing = [q for q in questions if not results.get(q["id"])]
    if args.limit:
        missing = missing[:args.limit]
    if not missing:
        print("nothing to do: %d/%d selected questions already have valid analyses" %
              (sum(1 for q in questions if results.get(q["id"])), len(questions)), flush=True)
        return 0

    done_count = sum(1 for q in questions if results.get(q["id"]))
    label = "all" if args.all else str(CHAPTERS)
    print("bank=%s chapters=%s selected=%d done=%d missing=%d workers=%d model=%s" % (
        BANK_ID, label, len(questions), done_count, len(missing),
        args.workers, args.model), flush=True)

    lock = threading.Lock()
    started = time.time()
    failures = []

    def handle(question):
        text, errors, usage = generate_one(question, key, args)
        if not text:
            with lock:
                failures.append((question.get("id"), errors))
            print("[fail] %s %s" % (question.get("id"), (errors or ["unknown"])[-1]), flush=True)
            return
        with lock:
            results[question.get("id")] = text
            save_progress(progress)
            done = sum(1 for q in questions if results.get(q["id"]))
            elapsed = time.time() - started
            print("[progress] %d/%d (%.1f%%) %s ch%s %d chars %.0fs" % (
                done, len(questions), done * 100.0 / len(questions),
                question.get("id"), question.get("chapter"), len(text), elapsed), flush=True)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(handle, question) for question in missing]
        try:
            for future in as_completed(futures):
                future.result()
        except FatalApiError as exc:
            for future in futures:
                future.cancel()
            print("fatal: %s" % exc, flush=True)
            return 2
        except KeyboardInterrupt:
            print("interrupted; progress saved", flush=True)
            return 130

    save_progress(progress)
    done = sum(1 for q in questions if results.get(q["id"]))
    print("finished: %d/%d" % (done, len(questions)), flush=True)
    if failures:
        print("%d question(s) failed; re-run to retry" % len(failures), flush=True)
        return 1
    return 0


def show_status(args):
    bank = load_bank()
    questions = selected_questions(bank, args.all)
    progress = load_progress()
    results = progress.get("results", {})
    done = [q for q in questions if results.get(q["id"])]
    valid = 0
    invalid = []
    for q in done:
        ok, reason = validate_analysis(results[q["id"]], q)
        if ok:
            valid += 1
        else:
            invalid.append((q["id"], reason))
    names = {}
    for q in questions:
        chapter = q.get("chapter")
        if chapter and chapter not in names:
            names[chapter] = q.get("chapterName") or CHAPTER_NAMES.get(chapter, "")
    chapters = sorted(names)
    print("bank: %s" % BANK_ID)
    print("chapters: %s" % ("all" if args.all else ", ".join(
        "%d %s" % (n, CHAPTER_NAMES.get(n, "")) for n in CHAPTERS)))
    print("selected: %d" % len(questions))
    print("generated: %d" % len(done))
    print("valid: %d" % valid)
    print("invalid: %d" % len(invalid))
    print("missing: %d" % (len(questions) - len(done)))
    by_chapter = {n: [0, 0] for n in chapters}
    for q in questions:
        chapter = q.get("chapter")
        if chapter not in by_chapter:
            continue
        by_chapter[chapter][0] += 1
        if results.get(q.get("id")):
            by_chapter[chapter][1] += 1
    for n in chapters:
        total, generated = by_chapter[n]
        print("  ch%-2d %-18s %d/%d" % (n, names.get(n, ""), generated, total))
    if invalid:
        print("invalid results:")
        for qid, reason in invalid[:15]:
            print("  %s %s" % (qid, reason))
    if PROGRESS_FILE.exists():
        print("progress: %s" % PROGRESS_FILE.name)
    return 0


def embed_into_index(json_text):
    if not INDEX_HTML.exists():
        return False
    html = INDEX_HTML.read_text(encoding="utf-8")
    marker = "window.QUESTION_BANKS = "
    start = html.find(marker)
    if start == -1:
        return False
    end = html.find("</script>", start)
    if end == -1:
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


def apply_results(args):
    progress = load_progress()
    results = progress.get("results", {})
    if not results:
        raise SystemExit("no generated analyses yet; run without --apply first")
    raw = DATA_JS.read_text(encoding="utf-8")
    eq = raw.find("=")
    semi = raw.rfind(";")
    banks = json.loads(raw[eq + 1:semi].strip())
    total = 0
    applied = 0
    skipped = []
    counts = {}
    names = {}
    for bank in banks:
        if bank.get("id") != BANK_ID:
            continue
        for question in bank.get("questions", []):
            total += 1
            text = results.get(question.get("id"))
            if not text:
                continue
            ok, reason = validate_analysis(text, question)
            if not ok:
                skipped.append((question.get("id"), reason))
                continue
            question["analysis"] = text
            applied += 1
            chapter = question.get("chapter")
            counts[chapter] = counts.get(chapter, 0) + 1
            names[chapter] = question.get("chapterName") or CHAPTER_NAMES.get(chapter, "")
    json_text = json.dumps(banks, ensure_ascii=False, separators=(",", ":"))
    output = "window.QUESTION_BANKS = " + json_text + ";\n"
    tmp = DATA_JS.with_suffix(".js.tmp")
    tmp.write_text(output, encoding="utf-8")
    os.replace(tmp, DATA_JS)
    print("updated %s: %d/%d questions have valid analyses" % (DATA_JS.name, applied, total))
    if embed_into_index(json_text):
        print("updated %s with embedded analyses" % INDEX_HTML.name)
    else:
        print("warning: could not update embedded data in %s" % INDEX_HTML.name)
    for chapter in sorted(counts, key=lambda value: (value is None, value)):
        print("  ch%-2s %-18s %d" % (chapter, names.get(chapter, ""), counts[chapter]))
    if skipped:
        print("skipped %d invalid result(s):" % len(skipped))
        for qid, reason in skipped[:15]:
            print("  %s %s" % (qid, reason))
    return 0


def run_reset(args):
    if PROGRESS_FILE.exists():
        if not args.yes:
            answer = input("delete %s? [y/N] " % PROGRESS_FILE.name).strip().lower()
            if answer not in ("y", "yes"):
                print("cancelled")
                return 0
        PROGRESS_FILE.unlink()
        print("deleted %s" % PROGRESS_FILE.name)
    else:
        print("no progress file")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Generate built-in pathology analyses")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--backoff", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser


def main():
    args = build_parser().parse_args()
    if args.status:
        return show_status(args)
    if args.apply:
        return apply_results(args)
    if args.reset:
        return run_reset(args)
    return run_generate(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FatalApiError as exc:
        print("fatal: %s" % exc, flush=True)
        sys.exit(2)
    except KeyboardInterrupt:
        print("interrupted", flush=True)
        sys.exit(130)
