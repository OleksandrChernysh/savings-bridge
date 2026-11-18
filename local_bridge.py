#!/usr/bin/env python3
"""
Local & Free-Cloud LLM Bridge (Ollama, LM Studio, OpenRouter, NVIDIA NIM, OpenCode)
Supports:
  - ask: general text Q&A, drafting, summarization, translation
  - code: generate / implement / append code directly to file (with truncation guard & optional check/verify)
  - review: pre-review git diff or files for bugs and edge cases
  - git-commit: generate conventional commit message from git diff
  - git-changelog: generate release notes / changelog from git log
  - parse-json: extract structured JSON from unstructured text

Automatically logs local & free-cloud token usage to ~/.config/savings-bridge/local_tokens.jsonl
"""


import argparse
import json
import os
import subprocess
import sys

from bridge_backends import (
    DEFAULT_CODER_MODEL,
    DEFAULT_PARSER_MODEL,
    LOCK_PATH,
    MODEL_ALIASES,
    OLLAMA_API,
    OPENAI_COMPAT_PROVIDERS,
    acquire_ollama_lock,
    load_secrets_env,
    log_local_usage,
    normalize_ollama_url,
    query_backend,
    query_ollama,
    query_openai_compat,
    query_opencode,
    resolve_model,
)
from bridge_common import SECRETS_ENV_PATH, STATS_LOG_PATH
from bridge_guards import (
    CLOUD_FREE_DEFAULT_LIMITS,
    DEFAULT_SYSTEM_PROMPT,
    EXT_TO_LANGUAGE,
    FREE_CLOUD_PREFIXES,
    LANGUAGE_PROFILES,
    MODEL_LIMITS,
    PLACEHOLDER_PATTERNS,
    SECRET_CONTENT_PATTERNS,
    SENSITIVE_FILENAME_PATTERNS,
    _resolve_scope_limits,
    check_scope_limits,
    detect_secrets,
    detect_truncation_or_placeholders,
    find_project_root,
    get_language_profile,
    resolve_local_imports,
    run_verify,
    strip_markdown_code,
)

def handle_code(args):
    prompt_parts = []
    existing = ""
    context_contents: list[str] = []
    context_blocks: list[str] = []
    included_abs: set[str] = set()

    if args.file and os.path.exists(args.file):
        included_abs.add(os.path.abspath(args.file))
        with open(args.file, "r", encoding="utf-8") as f:
            existing = f.read()
        label = "Existing file to append to" if args.append else "Existing file content"
        prompt_parts.append(f"{label} ({args.file}):\n```\n{existing}\n```")

    if args.context:
        for ctx_file in args.context:
            if os.path.exists(ctx_file):
                included_abs.add(os.path.abspath(ctx_file))
                with open(ctx_file, "r", encoding="utf-8") as f:
                    ctx_text = f.read()
                    context_contents.append(ctx_text)
                    block = f"Context from {ctx_file}:\n```\n{ctx_text}\n```"
                    context_blocks.append(block)
                    prompt_parts.append(block)
            else:
                print(f"Warning: context file not found: {ctx_file}", file=sys.stderr)

    model = resolve_model(args.model, DEFAULT_CODER_MODEL)
    auto_ctx_paths: list[str] = []
    if not getattr(args, "no_auto_context", False) and existing and args.file:
        limits = _resolve_scope_limits(model)
        used = len(existing) + sum(len(c) for c in context_contents)
        budget = max(0, int(limits["max_total_chars"] * 0.85) - used)
        for imp_path, imp_text in resolve_local_imports(args.file, existing, included_abs, budget):
            auto_ctx_paths.append(imp_path)
            context_contents.append(imp_text)
            block = f"Auto-imported context ({imp_path}):\n```\n{imp_text}\n```"
            context_blocks.append(block)
            prompt_parts.append(block)

    if model.startswith(FREE_CLOUD_PREFIXES):
        checked_paths = [p for p in ([args.file, args.out] + (args.context or []) + auto_ctx_paths) if p]
        secret_err = detect_secrets(checked_paths, [args.prompt, existing] + context_contents)
        if secret_err:
            print(
                f"Security block (exit 3): {secret_err}. Use local Ollama (-m fast / -m coder) or Primary Cloud model instead.",
                file=sys.stderr,
            )
            sys.exit(3)

    scope_error = check_scope_limits(model, args.prompt, existing, context_contents, args.append)
    if scope_error:
        print(f"Scope limit exceeded: {scope_error}", file=sys.stderr)
        sys.exit(2)

    prompt_parts.append(f"Task / Instruction:\n{args.prompt}")
    if args.append:
        prompt_parts.append(
            "IMPORTANT: Output ONLY the new code snippet to append to the end of the file. "
            "Do NOT repeat existing code. No conversational filler, no markdown explanations."
        )
    else:
        prompt_parts.append(
            "IMPORTANT: Output only the exact raw code for the file. "
            "No conversational filler, no markdown explanations before or after."
        )

    target_file = args.out or args.file
    system_prompt, _ = get_language_profile(target_file)

    full_prompt = "\n\n".join(prompt_parts)

    raw_code = query_backend(
        command="code",
        model=model,
        prompt=full_prompt,
        system=system_prompt,
        num_ctx=args.num_ctx,
        timeout=args.timeout,
    )
    clean_code = strip_markdown_code(raw_code)

    if args.dry_run or not target_file:
        sys.stdout.write(clean_code)
        return

    if not args.append and existing and not args.force:
        guard_reason = detect_truncation_or_placeholders(existing, clean_code)
        if guard_reason:
            tmp_path = f"{target_file}.local.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(clean_code)
            print(
                f"Refused to overwrite {target_file}: {guard_reason}. "
                f"Saved output to {tmp_path} (use --force to overwrite).",
                file=sys.stderr,
            )
            sys.exit(2)

    had_target = os.path.exists(target_file)
    orig_target = ""
    if had_target:
        with open(target_file, "r", encoding="utf-8") as f:
            orig_target = f.read()

    os.makedirs(os.path.dirname(os.path.abspath(target_file)), exist_ok=True)
    if args.append and had_target:
        current = orig_target.rstrip()
        combined = f"{current}\n\n{clean_code}" if current else clean_code
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(combined)
        print(f"Appended generated code to: {target_file}")
    else:
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(clean_code)
        print(f"Saved generated code to: {target_file}")

    if args.tsc or args.check or args.verify:
        rc, err_out = run_verify(args.verify, args.tsc, target_file, check=args.check)
        retries_left = args.retries
        ctx_section = ("\n\n".join(context_blocks)[:16000] + "\n\n") if context_blocks else ""
        while rc != 0 and retries_left > 0:
            retries_left -= 1
            repair_model = DEFAULT_CODER_MODEL if model == DEFAULT_PARSER_MODEL else model
            print(
                f"Auto-repairing locally with {repair_model} (retries left: {retries_left})...",
                file=sys.stderr,
            )
            with open(target_file, "r", encoding="utf-8") as f:
                broken_content = f.read()
            repair_prompt = (
                f"Original task:\n{args.prompt}\n\n"
                f"{ctx_section}"
                f"Current file content ({target_file}):\n```\n{broken_content}\n```\n\n"
                f"Verification errors:\n{err_out[:4000]}\n\n"
                "Fix all errors and output ONLY the complete corrected raw code for the file."
            )
            repaired_raw = query_backend(
                command="code-repair",
                model=repair_model,
                prompt=repair_prompt,
                system=system_prompt,
                num_ctx=args.num_ctx,
                timeout=args.timeout,
            )
            repaired_clean = strip_markdown_code(repaired_raw)
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(repaired_clean)
            rc, err_out = run_verify(args.verify, args.tsc, target_file, check=args.check)
        if rc != 0:
            if not getattr(args, "no_rollback", False):
                failed_tmp = f"{target_file}.failed.tmp"
                with open(target_file, "r", encoding="utf-8") as f:
                    failed_content = f.read()
                with open(failed_tmp, "w", encoding="utf-8") as f:
                    f.write(failed_content)
                if had_target:
                    with open(target_file, "w", encoding="utf-8") as f:
                        f.write(orig_target)
                elif os.path.exists(target_file):
                    os.remove(target_file)
                print(
                    f"Verification failed after retries; rolled back {target_file} (failed output saved to {failed_tmp}).",
                    file=sys.stderr,
                )
            sys.exit(rc)


def handle_git_commit(args):
    diff_proc = subprocess.run(["git", "diff", "--cached"], capture_output=True, text=True)
    diff = diff_proc.stdout.strip()

    if not diff:
        diff_proc = subprocess.run(["git", "diff", "HEAD~1"], capture_output=True, text=True)
        diff = diff_proc.stdout.strip()

    if not diff:
        diff_proc = subprocess.run(["git", "diff"], capture_output=True, text=True)
        diff = diff_proc.stdout.strip()

    if not diff:
        print("No git diff detected to summarize.", file=sys.stderr)
        sys.exit(1)

    if len(diff) > 20000:
        diff = diff[:20000] + "\n\n...[diff truncated for length]..."

    prompt = (
        "Generate a concise, conventional commit message based on the following git diff.\n"
        "Format: <type>(<scope>): <summary>\n"
        "[optional bullet points if needed]\n"
        "Output ONLY the commit message. No quotes, no markdown fences.\n\n"
        f"Diff:\n{diff}"
    )

    model = resolve_model(args.model, DEFAULT_PARSER_MODEL)
    commit_msg = query_ollama(
        command="git-commit", model=model, prompt=prompt, num_ctx=8192, temperature=0.1, timeout=args.timeout
    )
    commit_msg = commit_msg.strip("`'\"\n ")

    if args.apply:
        res = subprocess.run(["git", "commit", "-m", commit_msg])
        if res.returncode == 0:
            print(f"Committed successfully:\n{commit_msg}")
        else:
            print("Git commit failed.", file=sys.stderr)
            sys.exit(res.returncode)
    else:
        print(commit_msg)


def handle_git_changelog(args):
    git_range = args.range or "HEAD~10..HEAD"
    log_proc = subprocess.run(
        ["git", "log", git_range, "--pretty=format:%h - %s (%an)"],
        capture_output=True,
        text=True,
    )
    commits = log_proc.stdout.strip()

    if not commits:
        print(f"No commits found for range {git_range}", file=sys.stderr)
        sys.exit(1)

    prompt = (
        "Summarize these commits into clear release notes / changelog grouped by category "
        "(Features, Bug Fixes, Refactoring, Maintenance).\n"
        "Commits:\n"
        f"{commits}\n\n"
        "Format as clean GitHub Flavored Markdown."
    )

    model = resolve_model(args.model, DEFAULT_PARSER_MODEL)
    changelog = query_ollama(
        command="git-changelog", model=model, prompt=prompt, num_ctx=8192, temperature=0.2, timeout=args.timeout
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(changelog + "\n")
        print(f"Saved changelog to: {args.out}")
    else:
        print(changelog)


def handle_parse_json(args):
    if args.input and os.path.exists(args.input):
        with open(args.input, "r", encoding="utf-8") as f:
            raw_text = f.read()
    elif args.text:
        raw_text = args.text
    else:
        raw_text = sys.stdin.read()

    if args.schema:
        if os.path.exists(args.schema):
            with open(args.schema, "r", encoding="utf-8") as f:
                schema_obj = json.load(f)
        else:
            schema_obj = json.loads(args.schema)
    else:
        schema_obj = "json"

    system_prompt = (
        "You are an accurate data extraction and parsing model. "
        "Extract the requested structured information from the provided raw text and return strictly valid JSON. "
        "Do not include explanations or markdown fences."
    )

    prompt = (
        f"Raw text:\n{raw_text}\n\n"
        f"Extraction instruction:\n{args.instruction or 'Extract key information into JSON.'}"
    )

    model = resolve_model(args.model, DEFAULT_PARSER_MODEL)
    result = query_ollama(
        command="parse-json",
        model=model,
        prompt=prompt,
        system=system_prompt,
        schema=schema_obj,
        num_ctx=args.num_ctx,
        temperature=0.0,
        timeout=args.timeout,
    )

    clean_json = strip_markdown_code(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(clean_json)
        print(f"Saved parsed JSON to: {args.out}")
    else:
        print(clean_json)


def handle_review(args):
    model = resolve_model(args.model, MODEL_ALIASES["pickle"])
    file_paths = args.file or []
    parts = []

    if file_paths:
        for path in file_paths:
            if not os.path.exists(path):
                print(f"Review target file not found: {path}", file=sys.stderr)
                sys.exit(1)
            with open(path, "r", encoding="utf-8") as f:
                parts.append(f"--- FILE: {path} ---\n{f.read()}")

    if not file_paths or args.diff:
        diff_target = args.diff or "HEAD"
        diff_proc = subprocess.run(
            ["git", "diff", diff_target],
            capture_output=True,
            text=True,
        )
        diff_text = diff_proc.stdout.strip()
        if not diff_text and not args.diff:
            diff_proc = subprocess.run(["git", "diff", "--cached"], capture_output=True, text=True)
            diff_text = diff_proc.stdout.strip()
        if diff_text:
            parts.append(f"--- GIT DIFF ({diff_target}) ---\n{diff_text}")

    if not parts:
        print("No changes or files to review.", file=sys.stderr)
        sys.exit(1)

    payload = "\n\n".join(parts)
    focus = args.prompt or "Find bugs, edge cases, null/undefined issues, type coercion bugs, and security flaws."

    if model.startswith(FREE_CLOUD_PREFIXES):
        secret_err = detect_secrets(file_paths, [focus, payload])
        if secret_err:
            print(f"SECURITY BLOCK ({secret_err}). Sensitive diffs must be reviewed in Primary Cloud model.", file=sys.stderr)
            sys.exit(3)

    scope_err = check_scope_limits(model, focus, "", [payload], append=True)
    if scope_err:
        print(f"SCOPE LIMIT EXCEEDED: {scope_err}", file=sys.stderr)
        sys.exit(2)

    system_prompt = (
        "You are a senior code reviewer. Be terse and concrete. "
        "Report only real bugs, edge cases, type/null hazards, or security issues with file/line references. "
        "If none found, reply: NO_ISSUES."
    )
    prompt = f"Focus: {focus}\n\n{payload}"
    result = query_backend(
        command="review",
        model=model,
        prompt=prompt,
        system=system_prompt,
        num_ctx=16384,
        temperature=0.1,
        timeout=args.timeout,
    )
    print(result)


def handle_ask(args):
    """Send a general text prompt (Q&A, drafting, summarization, translation) to local or free cloud models."""
    prompt_text = args.prompt if args.prompt is not None else sys.stdin.read().strip()
    if not prompt_text:
        print("Prompt is empty. Pass --prompt or pipe text via stdin.", file=sys.stderr)
        sys.exit(1)

    model = resolve_model(args.model, MODEL_ALIASES["free"])
    context_files = args.context or []
    context_parts = []
    for path in context_files:
        if not os.path.exists(path):
            print(f"Context file not found: {path}", file=sys.stderr)
            sys.exit(1)
        with open(path, "r", encoding="utf-8") as f:
            context_parts.append(f"--- CONTEXT: {path} ---\n{f.read()}")

    if model.startswith(FREE_CLOUD_PREFIXES):
        secret_err = detect_secrets(context_files, [args.system or "", prompt_text, *context_parts])
        if secret_err:
            print(
                f"SECURITY BLOCK ({secret_err}). Use local Ollama (-m fast / -m coder) for sensitive text.",
                file=sys.stderr,
            )
            sys.exit(3)

    full_prompt = "\n\n".join(context_parts + [prompt_text]) if context_parts else prompt_text
    system_prompt = args.system or "You are a concise, helpful assistant. Answer directly without filler."

    result = query_backend(
        command="ask",
        model=model,
        prompt=full_prompt,
        system=system_prompt,
        num_ctx=args.num_ctx,
        temperature=args.temperature,
        timeout=args.timeout,
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(result.rstrip() + "\n")
        print(f"Saved response to: {args.out}")
    else:
        print(result)


def handle_models(args):
    """Discover hardware specs, installed local models, and dynamic role defaults."""
    from model_discovery import build_profile, load_profile, save_profile

    profile = None if args.refresh else load_profile()
    if not profile:
        profile = build_profile(OLLAMA_API)
        if args.save or args.refresh:
            save_profile(profile)

    if args.json:
        print(json.dumps(profile, indent=2))
        return

    hw = profile["hardware"]
    print(f"Hardware: {hw['os']} ({hw['arch']}), RAM: {hw['ram_gb']} GB")
    print(f"Defaults: coder={profile['defaults']['coder']} | text={profile['defaults']['text']}")
    print("Installed local models:")
    if profile["installed"]:
        for m in profile["installed"]:
            print(f"  - {m['name']} ({m['provider']}, {m['size_gb']} GB)")
    else:
        rec = profile["recommended_pull"]
        print(f"  (none) Recommended for your RAM: ollama pull {rec['fast']} && ollama pull {rec['coder']}")


def main():
    parser = argparse.ArgumentParser(description="Antigravity Local LLM Bridge (Ollama)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: code
    p_code = subparsers.add_parser("code", help="Generate code or implement files locally")
    p_code.add_argument("--prompt", "-p", required=True, help="Instruction / task description")
    p_code.add_argument("--file", "-f", help="Target file to implement / edit")
    p_code.add_argument("--out", "-o", help="Output file path (defaults to --file)")
    p_code.add_argument("--context", "-c", action="append", help="Additional context file(s)")
    p_code.add_argument("--model", "-m", default=DEFAULT_CODER_MODEL, help=f"Model name or alias 7b/14b/free/pickle/glm-free/nim (default: {DEFAULT_CODER_MODEL})")
    p_code.add_argument("--num-ctx", type=int, default=16384, help="Context size (default: 16384)")
    p_code.add_argument("--timeout", type=int, default=300, help="Request timeout in seconds (default: 300)")
    p_code.add_argument("--append", action="store_true", help="Append generated snippet to target file instead of overwriting")
    p_code.add_argument("--dry-run", action="store_true", help="Print generated code to stdout without writing file")
    p_code.add_argument("--force", action="store_true", help="Bypass truncation/placeholder guard on overwrite")
    p_code.add_argument("--tsc", action="store_true", help="Run npx tsc --noEmit from nearest tsconfig.json after writing")
    p_code.add_argument("--check", dest="check", action="store_true", default=True, help="Run language-appropriate compiler/syntax check (default: enabled)")
    p_code.add_argument("--no-check", dest="check", action="store_false", help="Skip automatic compiler/syntax check")
    p_code.add_argument("--no-auto-context", action="store_true", help="Disable auto-resolving relative imports from --file")
    p_code.add_argument("--no-rollback", action="store_true", help="Keep failed output in target file instead of rolling back on verify failure")
    p_code.add_argument("--verify", help="Custom verification command to run after writing")
    p_code.add_argument("--retries", type=int, default=1, help="Auto-repair retries on --tsc/--verify failure (default: 1, escalates 7b->14b)")
    p_code.set_defaults(func=handle_code)

    # Subcommand: git-commit
    p_commit = subparsers.add_parser("git-commit", help="Generate conventional commit message from git diff")
    p_commit.add_argument("--apply", "-a", action="store_true", help="Automatically run git commit with generated message")
    p_commit.add_argument("--model", "-m", default=DEFAULT_PARSER_MODEL, help=f"Model name or alias (default: {DEFAULT_PARSER_MODEL})")
    p_commit.add_argument("--timeout", type=int, default=180, help="Request timeout in seconds (default: 180)")
    p_commit.set_defaults(func=handle_git_commit)

    # Subcommand: git-changelog
    p_changelog = subparsers.add_parser("git-changelog", help="Generate changelog / release notes from git log")
    p_changelog.add_argument("--range", "-r", help="Git revision range (e.g. HEAD~15..HEAD)")
    p_changelog.add_argument("--out", "-o", help="Output markdown file")
    p_changelog.add_argument("--model", "-m", default=DEFAULT_PARSER_MODEL, help=f"Model name or alias (default: {DEFAULT_PARSER_MODEL})")
    p_changelog.add_argument("--timeout", type=int, default=180, help="Request timeout in seconds (default: 180)")
    p_changelog.set_defaults(func=handle_git_changelog)

    # Subcommand: parse-json
    p_parse = subparsers.add_parser("parse-json", help="Extract structured JSON from text (Telegram, logs, etc.)")
    p_parse.add_argument("--text", "-t", help="Raw input text")
    p_parse.add_argument("--input", "-i", help="Input file path")
    p_parse.add_argument("--instruction", help="Custom extraction instruction")
    p_parse.add_argument("--schema", "-s", help="Path to JSON Schema file or JSON schema string")
    p_parse.add_argument("--out", "-o", help="Output JSON file")
    p_parse.add_argument("--model", "-m", default=DEFAULT_PARSER_MODEL, help=f"Model name or alias (default: {DEFAULT_PARSER_MODEL})")
    p_parse.add_argument("--num-ctx", type=int, default=8192, help="Context size (default: 8192)")
    p_parse.add_argument("--timeout", type=int, default=180, help="Request timeout in seconds (default: 180)")
    p_parse.set_defaults(func=handle_parse_json)

    # Subcommand: review
    p_review = subparsers.add_parser("review", help="Pre-review git diff or files for bugs and edge cases")
    p_review.add_argument("--diff", "-d", help="Git diff target/range (default: HEAD)")
    p_review.add_argument("--file", "-f", action="append", help="Specific file(s) to review")
    p_review.add_argument("--prompt", "-p", help="Custom review focus")
    p_review.add_argument("--model", "-m", default="pickle", help="Model name or alias (default: pickle; sensitive diffs must be reviewed in Primary Cloud model)")
    p_review.add_argument("--timeout", type=int, default=180, help="Request timeout in seconds (default: 180)")
    p_review.set_defaults(func=handle_review)

    # Subcommand: ask (text/chat/summarization/translation)
    p_ask = subparsers.add_parser("ask", aliases=["chat", "text"], help="Send a text prompt to local or free cloud models")
    p_ask.add_argument("--prompt", "-p", help="Prompt text (reads stdin if omitted)")
    p_ask.add_argument("--system", "-s", help="Optional system instruction")
    p_ask.add_argument("--context", "-c", action="append", help="Optional context file(s)")
    p_ask.add_argument("--out", "-o", help="Optional output file path")
    p_ask.add_argument("--model", "-m", default="free", help="Model name or alias (default: free; also fast/coder/openrouter/glm-free/pickle/nim)")
    p_ask.add_argument("--temperature", "-t", type=float, default=0.3, help="Sampling temperature (default: 0.3)")
    p_ask.add_argument("--num-ctx", type=int, default=8192, help="Context size for Ollama (default: 8192)")
    p_ask.add_argument("--timeout", type=int, default=180, help="Request timeout in seconds (default: 180)")
    p_ask.set_defaults(func=handle_ask)

    # Subcommand: models (dynamic discovery)
    p_models = subparsers.add_parser("models", help="Inspect hardware, installed local models, and dynamic defaults")
    p_models.add_argument("--json", action="store_true", help="Output JSON for agent consumption")
    p_models.add_argument("--refresh", action="store_true", help="Re-scan local runtimes and update models.json")
    p_models.add_argument("--save", action="store_true", help="Save discovered profile to models.json")
    p_models.set_defaults(func=handle_models)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
