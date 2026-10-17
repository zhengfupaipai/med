#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Classify all pathology questions into 19 chapters with the DeepSeek API.

Only standard library is used. All files (API key, progress, output) stay in
this folder.

Usage:
    python classify_chapters.py                 # classify, resumable
    python classify_chapters.py --status        # show progress
    python classify_chapters.py --apply         # write chapters into data.js
    python classify_chapters.py --limit 20      # classify only 20 for testing
    python classify_chapters.py --reset         # delete progress file
    python classify_chapters.py --test          # one batch, no progress saved

The run is resumable: progress is saved after every batch. Timeouts and
transient API errors are retried with exponential backoff, and re-running the
same command continues from the last finished batch.
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
PROGRESS_FILE = BASE / "chapter_progress.json"
DATA_JS = BASE / "data.js"
INDEX_HTML = BASE / "index.html"
BANK_FILES = ["pathology-2a.json", "pathology-3a.json", "pathology-a.json"]

API_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
API_URL = API_BASE + "/chat/completions"
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

CHAPTERS = {
    1: "细胞和组织的适应与损伤",
    2: "损伤的修复",
    3: "局部血液循环障碍",
    4: "炎症",
    5: "免疫性疾病",
    6: "肿瘤",
    7: "环境和营养性疾病",
    8: "遗传性疾病和儿童疾病",
    9: "心血管系统疾病",
    10: "呼吸系统疾病",
    11: "消化系统疾病",
    12: "淋巴造血系统疾病",
    13: "泌尿系统疾病",
    14: "生殖系统和乳腺疾病",
    15: "内分泌系统疾病",
    16: "神经系统疾病",
    17: "骨关节和软组织疾病",
    18: "传染病",
    19: "病理学诊断与研究技术",
}

CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def chapter_label(num):
    return "第%d章 %s" % (num, CHAPTERS[num])


def chapter_list_text():
    return "\n".join("%d. %s" % (n, CHAPTERS[n]) for n in sorted(CHAPTERS))


SYSTEM_PROMPT = (
    "你是一名严谨的病理学题库分类助手。任务：把每道题归入下列 19 个章节中的"
    "唯一一个章节。\n\n"
    "章节列表：\n" + chapter_list_text() + "\n\n"
    "分类规则：\n"
    "- 以题干直接考查的核心病理学知识点为准，参考标准病理学教材的章节划分。\n"
    "- 病例题按病例所涉及的器官系统和主要病变归类。\n"
    "- 题干涉及多个知识点时，选择最主要、最直接考查的章节。\n"
    "- 每道题必须且只能选择一个章节，不允许空值，不允许“无法判断”。\n\n"
    "输出要求：\n"
    "- 只输出 JSON，不要输出解释、Markdown 或代码块。\n"
    "- 严格格式：{\"results\":[{\"id\":\"题目id\",\"chapter\":1到19的整数}]}\n"
    "- 必须覆盖输入中的每一道题，id 原样返回，不得遗漏或编造。\n"
)


class FatalApiError(Exception):
    pass


def read_key():
    if not API_FILE.exists():
        raise SystemExit("api.txt not found in %s" % BASE)
    key = API_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit("api.txt is empty")
    return key


def load_questions():
    questions = []
    for filename in BANK_FILES:
        path = BASE / filename
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for q in data.get("questions", []):
            options = []
            for o in q.get("options", []) or []:
                options.append("%s. %s" % (o.get("key", ""), o.get("text", "")))
            questions.append({
                "id": str(q.get("id", "")),
                "question": str(q.get("question", "")),
                "options": options,
                "bank": filename,
            })
    return [q for q in questions if q["id"]]


def batch_payload(batch):
    items = []
    for q in batch:
        items.append({"id": q["id"], "question": q["question"], "options": q["options"]})
    return json.dumps(items, ensure_ascii=False)


def cn_to_int(text):
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + CN_DIGITS.get(text[1:], 0)
    if "十" in text:
        left, right = text.split("十", 1)
        tens = CN_DIGITS.get(left, 0) * 10
        ones = CN_DIGITS.get(right, 0) if right else 0
        return tens + ones
    return CN_DIGITS.get(text)


def normalize_chapter(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        num = value
    elif isinstance(value, float) and value.is_integer():
        num = int(value)
    elif isinstance(value, str):
        text = value.strip()
        match = re.search(r"第?\s*([0-9]+|[一二三四五六七八九十]+)\s*章?", text)
        if match:
            num = cn_to_int(match.group(1))
        else:
            num = None
        if num is None:
            for n, name in CHAPTERS.items():
                if name in text:
                    num = n
                    break
    else:
        num = None
    if num is None or not (1 <= num <= 19):
        return None
    return int(num)


def extract_items(parsed):
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("results", "result", "answers", "data", "items", "分类结果", "结果"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [{"id": k, "chapter": v} for k, v in value.items()]
        values = [v for v in parsed.values() if isinstance(v, list)]
        if len(values) == 1:
            return values[0]
        if all(isinstance(v, (int, str)) for v in parsed.values()):
            return [{"id": k, "chapter": v} for k, v in parsed.items()]
    return []


def parse_results(content, batch):
    text = (content or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except Exception:
        return {}

    items = extract_items(parsed)
    by_id = {q["id"]: q for q in batch}
    result = {}
    ordered = []
    for item in items:
        if not isinstance(item, dict):
            continue
        qid = None
        for key in ("id", "qid", "question_id", "题号", "编号", "ID"):
            if key in item and str(item[key]) in by_id:
                qid = str(item[key])
                break
        chapter = None
        for key in ("chapter", "chapter_id", "chapterId", "章节", "章节号", "章节编号", "分类"):
            if key in item:
                chapter = normalize_chapter(item[key])
                if chapter:
                    break
        if chapter:
            ordered.append(chapter)
            if qid:
                result[qid] = chapter

    if not result and len(ordered) == len(batch) and ordered:
        for q, chapter in zip(batch, ordered):
            result[q["id"]] = chapter
    elif len(result) < len(batch):
        missing = [q["id"] for q in batch if q["id"] not in result]
        if len(ordered) == len(batch) and not missing:
            pass
        elif len(ordered) == len(batch) and len(result) == 0:
            for q, chapter in zip(batch, ordered):
                result[q["id"]] = chapter
        elif len(ordered) == len(missing) and missing:
            for qid, chapter in zip(missing, ordered):
                result[qid] = chapter
    return result


def call_api(api_key, model, batch, timeout, max_tokens):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "待分类题目（JSON）：\n" + batch_payload(batch)},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("empty choices: " + json.dumps(body, ensure_ascii=False)[:300])
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    reasoning = message.get("reasoning_content") or ""
    finish = choice.get("finish_reason")
    usage = body.get("usage") or {}
    return content, reasoning, finish, usage


def classify_batch(api_key, batch, args, batch_no, total_batches):
    results = {}
    errors = []
    pending = list(batch)
    attempt = 0
    max_tokens = args.max_tokens
    while pending and attempt <= args.retries:
        if attempt > 0:
            delay = min(args.backoff * (2 ** (attempt - 1)), 60)
            print("[batch %d/%d] retry %d/%d in %ds (%d left)" %
                  (batch_no, total_batches, attempt, args.retries, delay, len(pending)), flush=True)
            time.sleep(delay)
        try:
            content, reasoning, finish, usage = call_api(
                api_key, args.model, pending, args.timeout, max_tokens)
            parsed = parse_results(content, pending)
            if not parsed and reasoning:
                parsed = parse_results(reasoning, pending)
            if not parsed and finish == "length":
                max_tokens = min(max_tokens * 2, args.max_tokens_cap)
                errors.append("finish=length, raising max_tokens to %d" % max_tokens)
                attempt += 1
                continue
            for qid, chapter in parsed.items():
                if any(q["id"] == qid for q in pending):
                    results[qid] = chapter
            pending = [q for q in pending if q["id"] not in results]
            if parsed:
                print("[batch %d/%d] ok: %d/%d classified (tokens: prompt %s, completion %s)" %
                      (batch_no, total_batches, len(parsed), len(batch),
                       usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?")),
                      flush=True)
            else:
                errors.append("no parsable results, finish=%s" % finish)
                print("[batch %d/%d] no parsable results (finish=%s)" %
                      (batch_no, total_batches, finish), flush=True)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if exc.code in (401, 403, 402):
                raise FatalApiError("HTTP %d: %s" % (exc.code, detail))
            errors.append("HTTP %d: %s" % (exc.code, detail))
            print("[batch %d/%d] HTTP %d: %s" % (batch_no, total_batches, exc.code, detail), flush=True)
        except (socket.timeout, TimeoutError) as exc:
            errors.append("timeout: %r" % (exc,))
            print("[batch %d/%d] timeout after %ss" % (batch_no, total_batches, args.timeout), flush=True)
        except urllib.error.URLError as exc:
            errors.append("network: %r" % (exc.reason,))
            print("[batch %d/%d] network error: %r" % (batch_no, total_batches, exc.reason), flush=True)
        except Exception as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))
            print("[batch %d/%d] error: %s: %s" %
                  (batch_no, total_batches, type(exc).__name__, exc), flush=True)
        attempt += 1

    missing = [q["id"] for q in pending]
    return results, missing, errors


def load_progress():
    if PROGRESS_FILE.exists():
        try:
            data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("results", {})
                data.setdefault("errors", {})
                return data
        except Exception as exc:
            print("warning: cannot read progress file (%s), starting fresh" % exc, flush=True)
    return {
        "model": DEFAULT_MODEL,
        "updated_at": None,
        "chapters": [{"id": n, "name": CHAPTERS[n]} for n in sorted(CHAPTERS)],
        "results": {},
        "errors": {},
    }


def atomic_write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def timestamp():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def clean_stale_tmp():
    for path in (PROGRESS_FILE.with_suffix(PROGRESS_FILE.suffix + ".tmp"),
                 DATA_JS.with_suffix(DATA_JS.suffix + ".tmp")):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def run_classify(args):
    api_key = read_key()
    questions = load_questions()
    if not questions:
        raise SystemExit("no questions found in bank files")

    progress = load_progress()
    if progress.get("model") != args.model and progress.get("results"):
        print("warning: progress was produced with model %s, now using %s" %
              (progress.get("model"), args.model), flush=True)
    progress["model"] = args.model

    results = progress["results"]
    missing_questions = [q for q in questions if q["id"] not in results]
    if args.limit:
        missing_questions = missing_questions[:args.limit]
    if not missing_questions:
        print("nothing to do: %d/%d questions already classified" %
              (len(results), len(questions)), flush=True)
        return 0

    batch_size = max(1, args.batch_size)
    batches = [missing_questions[i:i + batch_size]
               for i in range(0, len(missing_questions), batch_size)]
    total_batches = len(batches)
    print("model=%s total=%d classified=%d missing=%d batches=%d workers=%d" %
          (args.model, len(questions), len(results), len(missing_questions),
           total_batches, args.workers), flush=True)

    lock = threading.Lock()
    stop = threading.Event()
    started = time.time()
    failures = {}

    def save_progress():
        progress["updated_at"] = timestamp()
        atomic_write_json(PROGRESS_FILE, progress)

    def handle_batch(index, batch):
        batch_no = index + 1
        batch_results, missing, errors = classify_batch(
            api_key, batch, args, batch_no, total_batches)
        with lock:
            if batch_results:
                results.update(batch_results)
                for qid in batch_results:
                    progress["errors"].pop(qid, None)
            if missing:
                failures[batch_no] = missing
                progress["errors"][batch_no] = {"missing": missing, "errors": errors}
            save_progress()
            done = len(results)
            elapsed = time.time() - started
            rate = done / elapsed if elapsed > 0 else 0
            print("[progress] %d/%d classified (%.1f%%), %.1fs elapsed, %.1f q/s" %
                  (done, len(questions), done * 100.0 / len(questions), elapsed, rate),
                  flush=True)

    exit_code = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {}
        for index, batch in enumerate(batches):
            if stop.is_set():
                break
            futures[pool.submit(handle_batch, index, batch)] = index
        try:
            for future in as_completed(futures):
                future.result()
        except FatalApiError as exc:
            stop.set()
            print("fatal: %s" % exc, flush=True)
            exit_code = 2
        except KeyboardInterrupt:
            stop.set()
            print("interrupted; progress saved, re-run to resume", flush=True)
            exit_code = 130

    with lock:
        save_progress()

    all_ids = {q["id"] for q in questions}
    classified = len(all_ids & set(results.keys()))
    print("finished: %d/%d classified" % (classified, len(all_ids)), flush=True)
    if failures:
        print("%d batch(es) still have missing questions; re-run to retry them" %
              len(failures), flush=True)
        exit_code = exit_code or 1
    return exit_code


def show_status(args):
    questions = load_questions()
    progress = load_progress()
    results = progress.get("results", {})
    all_ids = {q["id"] for q in questions}
    classified = all_ids & set(results.keys())
    print("progress file: %s" % PROGRESS_FILE)
    print("updated: %s" % progress.get("updated_at"))
    print("model: %s" % progress.get("model"))
    print("classified: %d/%d" % (len(classified), len(all_ids)))
    print("missing: %d" % (len(all_ids) - len(classified)))
    counts = {n: 0 for n in CHAPTERS}
    for qid in classified:
        chapter = results[qid]
        if chapter in counts:
            counts[chapter] += 1
    print("\nchapter counts:")
    for n in sorted(CHAPTERS):
        print("  %-2d %-20s %d" % (n, CHAPTERS[n], counts[n]))
    bank_counts = {}
    for q in questions:
        bank = q["bank"]
        bank_counts.setdefault(bank, [0, 0])
        bank_counts[bank][0] += 1
        if q["id"] in results:
            bank_counts[bank][1] += 1
    print("\nbank progress:")
    for bank in sorted(bank_counts):
        total, done = bank_counts[bank]
        print("  %-22s %d/%d" % (bank, done, total))
    errors = progress.get("errors") or {}
    if errors:
        print("\nlast run errors: %d batch(es), re-run to retry" % len(errors))
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


def apply_to_data_js(args):
    progress = load_progress()
    results = progress.get("results", {})
    if not results:
        raise SystemExit("no classification results yet; run without --apply first")
    if not DATA_JS.exists():
        raise SystemExit("data.js not found")
    raw = DATA_JS.read_text(encoding="utf-8")
    eq = raw.find("=")
    semi = raw.rfind(";")
    if eq == -1 or semi == -1 or semi <= eq:
        raise SystemExit("cannot locate JSON inside data.js")
    banks = json.loads(raw[eq + 1:semi].strip())
    total = 0
    tagged = 0
    counts = {n: 0 for n in CHAPTERS}
    for bank in banks:
        for q in bank.get("questions", []):
            total += 1
            qid = str(q.get("id", ""))
            chapter = results.get(qid)
            if chapter in CHAPTERS:
                q["chapter"] = chapter
                q["chapterName"] = CHAPTERS[chapter]
                q["chapterTitle"] = chapter_label(chapter)
                counts[chapter] += 1
                tagged += 1
            else:
                q.pop("chapter", None)
                q.pop("chapterName", None)
                q.pop("chapterTitle", None)
    json_text = json.dumps(banks, ensure_ascii=False, separators=(",", ":"))
    output = "window.QUESTION_BANKS = " + json_text + ";\n"
    tmp = DATA_JS.with_suffix(".js.tmp")
    tmp.write_text(output, encoding="utf-8")
    os.replace(tmp, DATA_JS)
    print("updated %s: %d/%d questions tagged" % (DATA_JS.name, tagged, total))
    if embed_into_index(json_text):
        print("updated %s with embedded question data" % INDEX_HTML.name)
    else:
        print("warning: could not update embedded data in %s" % INDEX_HTML.name)
    print("\nchapter counts in data.js:")
    for n in sorted(CHAPTERS):
        print("  %-2d %-20s %d" % (n, CHAPTERS[n], counts[n]))
    return 0


def reset_progress(args):
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


def run_test(args):
    api_key = read_key()
    questions = load_questions()
    progress = load_progress()
    results = progress.get("results", {})
    pool = [q for q in questions if q["id"] not in results]
    if not pool:
        pool = questions
    batch = pool[:args.limit or 3]
    print("testing model=%s with %d questions" % (args.model, len(batch)), flush=True)
    started = time.time()
    batch_results, missing, errors = classify_batch(
        api_key, batch, args, 1, 1)
    print("elapsed %.1fs" % (time.time() - started))
    for q in batch:
        chapter = batch_results.get(q["id"])
        label = chapter_label(chapter) if chapter else "UNCLASSIFIED"
        print("  %s -> %s | %s" % (q["id"], label, q["question"][:40]))
    if missing:
        print("missing: %s" % ", ".join(missing))
    for err in errors:
        print("error: %s" % err)
    return 0 if not missing else 1


def build_parser():
    parser = argparse.ArgumentParser(description="Classify pathology questions into 19 chapters")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--backoff", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--max-tokens-cap", type=int, default=16384)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    clean_stale_tmp()
    if args.status:
        return show_status(args)
    if args.apply:
        return apply_to_data_js(args)
    if args.reset:
        return reset_progress(args)
    if args.test:
        return run_test(args)
    return run_classify(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FatalApiError as exc:
        print("fatal: %s" % exc, flush=True)
        sys.exit(2)
    except KeyboardInterrupt:
        print("interrupted", flush=True)
        sys.exit(130)
