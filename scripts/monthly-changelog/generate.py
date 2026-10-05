#!/usr/bin/env python3
"""
Monthly changelog generator for Labellerr/Documentation.

Collects merged PRs targeting `develop` from tensormatics/* private repos,
filters by merged_at in target month, excludes noise, batches to Groq
for categorization/summarization (Groq-only, infinite 2m retry), bumps SemVer,
and patches changelog.mdx.

Usage:
  CHANGELOG_GH_PAT=... GROQ_API_KEY=... \
  python scripts/monthly-changelog/generate.py --month 2026-08 --dry-run

Exit 0 on empty month (no PR created). Never logs secrets.
"""
import argparse
import calendar
import datetime
import json
import os
import re
import sys
import time
import difflib
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests openai", file=sys.stderr)
    sys.exit(1)

# openai is required for Groq routing (openai/gpt-oss-120b)
# Keep requests for GitHub API; use OpenAI SDK only for Groq
try:
    from openai import OpenAI  # type: ignore
except ImportError:
    OpenAI = None  # type: ignore

# --- constants ---
DEFAULT_BATCH_SIZE = 8
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_GROQ_URL = "https://api.groq.com/openai/v1"
VALID_CATEGORIES = {"Added", "Changed", "Fixed", "Security"}
RETRY_STATUS = {429, 500, 502, 503, 504}
GH_API = "https://api.github.com"

ROOT = Path(__file__).resolve().parents[2]  # repo root (Labellerr/Documentation)
DEFAULT_REPOS_JSON = Path(__file__).parent / "repos.json"
DEFAULT_CATEGORY_MAP = Path(__file__).parent / "category-map.json"
DEFAULT_CHANGELOG = ROOT / "changelog.mdx"


def log(msg: str) -> None:
    print(msg, flush=True)


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- date helpers ---
def parse_month(s: str) -> Tuple[int, int]:
    try:
        y, m = s.split("-")
        y, m = int(y), int(m)
        if not (1 <= m <= 12):
            raise ValueError
        return y, m
    except Exception:
        raise argparse.ArgumentTypeError(f"month must be YYYY-MM, got: {s}")


def previous_month(now: Optional[datetime.datetime] = None) -> Tuple[int, int]:
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    y, m = now.year, now.month
    if m == 1:
        return y - 1, 12
    return y, m - 1


def month_bounds(year: int, month: int) -> Tuple[datetime.datetime, datetime.datetime, str]:
    start = datetime.datetime(year, month, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    if month == 12:
        nxt = datetime.datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    else:
        nxt = datetime.datetime(year, month + 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    last_day = calendar.monthrange(year, month)[1]
    release_date = f"{year}-{month:02d}-{last_day:02d}"
    return start, nxt, release_date


def iso_to_dt(s: Optional[str]) -> Optional[datetime.datetime]:
    if not s:
        return None
    try:
        # GitHub returns like 2026-08-15T12:34:56Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


# --- loading ---
def load_repos(path: Path) -> List[str]:
    data = json.loads(path.read_text())
    repos = data.get("repositories", [])
    if not repos or not isinstance(repos, list):
        raise ValueError(f"invalid {path}: missing repositories list")
    return [r.strip() for r in repos if r.strip()]


def load_category_map(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text())
    # normalize to lowercase for exclude matching; keep regex for others
    return data


# --- GitHub API ---
def gh_headers(token: str) -> Dict[str, str]:
    # classic PAT uses token, fine to use Bearer too; use token prefix for classic
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Labellerr-changelog-bot",
    }


def request_with_retry(
    method: str,
    url: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    max_retries: int = 3,
) -> requests.Response:
    backoff = 2
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=30)
            if resp.status_code in RETRY_STATUS and attempt < max_retries:
                log(f"  retry {attempt+1}/{max_retries} after {resp.status_code} backoff {backoff}s: {url}")
                time.sleep(backoff)
                backoff *= 2
                continue
            return resp
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt < max_retries:
                log(f"  retry {attempt+1}/{max_retries} after {type(e).__name__} backoff {backoff}s")
                time.sleep(backoff)
                backoff *= 2
                continue
            raise
    raise RuntimeError("unreachable retry")


def fetch_prs_for_repo(
    repo: str,
    token: str,
    month_start: datetime.datetime,
    next_month: datetime.datetime,
) -> Tuple[int, List[Dict[str, Any]]]:
    """
    Fetch closed PRs targeting develop, filter by merged_at in [month_start, next_month).
    Returns (total_fetched, qualifying_prs)
    qualifying_prs elements: dict with repository, number, title, body, base, merged_at (iso), updated_at, url
    """
    total_fetched = 0
    qualifying: List[Dict[str, Any]] = []
    page = 1
    per_page = 100

    while True:
        url = f"{GH_API}/repos/{repo}/pulls"
        params = {
            "state": "closed",
            "base": "develop",
            "per_page": per_page,
            "page": page,
            "sort": "updated",
            "direction": "desc",
        }
        resp = request_with_retry("GET", url, gh_headers(token), params=params)
        if resp.status_code == 404:
            eprint(f"  WARN {repo}: repo or base branch not found (404) page {page}")
            break
        if resp.status_code == 401:
            raise SystemExit(f"ERROR: GitHub auth failed for {repo} (401) — check CHANGELOG_GH_PAT")
        if resp.status_code == 403:
            # rate-limited or forbidden; retry already handled, now fail
            body = resp.text[:500]
            raise SystemExit(f"ERROR: GitHub 403 for {repo} page {page}: {body}")
        if resp.status_code != 200:
            raise SystemExit(f"ERROR: GitHub API {resp.status_code} for {repo} page {page}: {resp.text[:500]}")

        items = resp.json()
        if not isinstance(items, list):
            raise SystemExit(f"ERROR: unexpected GitHub response for {repo}: {items}")

        if not items:
            break

        total_fetched += len(items)

        # process
        oldest_updated: Optional[datetime.datetime] = None
        page_has_qualifying = False

        for pr in items:
            try:
                base_ref = (pr.get("base") or {}).get("ref")
                if base_ref != "develop":
                    continue
                merged_at_s = pr.get("merged_at")
                if not merged_at_s:
                    continue
                merged_at = iso_to_dt(merged_at_s)
                if not merged_at:
                    continue
                # authoritative filter
                if not (month_start <= merged_at < next_month):
                    continue
                # qualifying
                page_has_qualifying = True
                qualifying.append(
                    {
                        "repository": repo,
                        "number": pr.get("number"),
                        "title": (pr.get("title") or "").strip(),
                        "body": (pr.get("body") or "")[:2000],
                        "base": base_ref,
                        "merged_at": merged_at_s,
                        "merged_at_dt": merged_at,  # internal, removed before artifact
                        "updated_at": pr.get("updated_at"),
                        "url": pr.get("html_url") or pr.get("url") or "",
                    }
                )
            except Exception as e:
                eprint(f"  WARN skip PR in {repo}: {e}")
                continue

            # track oldest updated_at for stop condition
            try:
                upd = iso_to_dt(pr.get("updated_at"))
                if upd and (oldest_updated is None or upd < oldest_updated):
                    oldest_updated = upd
            except Exception:
                pass

        # pagination stop: if oldest updated_at < month_start and no qualifying in this page, safe to stop
        # Because merged_at <= updated_at, no later pages can contain merged_at in month.
        if oldest_updated and oldest_updated < month_start and not page_has_qualifying:
            # Be conservative: we have reached before the target month.
            break

        if len(items) < per_page:
            break

        page += 1
        if page > 50:  # safety guard: 5000 PRs
            log(f"  WARN {repo}: pagination guard hit at page {page}")
            break

    return total_fetched, qualifying


# --- filtering ---
def is_noise(pr: Dict[str, Any], exclude_terms: List[str]) -> bool:
    title = (pr.get("title") or "").lower()
    body = (pr.get("body") or "").lower()
    text = f"{title} {body}"
    for term in exclude_terms:
        t = term.lower().strip()
        if not t:
            continue
        # Use word-boundary aware matching to avoid blind exclusion
        # e.g. "ci" should not match "special". For multi-word terms
        # like "gcloud track", match the phrase with boundaries.
        try:
            pattern = r"\b" + re.escape(t) + r"\b"
            if re.search(pattern, text):
                return True
        except re.error:
            if t in text:
                return True
    return False


def strip_ticket_prefix(title: str) -> str:
    # Remove [LABIMP-1234] [WA-029] etc prefixes
    t = re.sub(r"^\s*(\[[^\]]+\]\s*)+", "", title).strip()
    return t


def clean_pr_body(body: str) -> str:
    """Strip CodeRabbit/PR Info noise so Groq sees product text (used for LLM prompt)."""
    if not body:
        return ""
    # Remove HTML comments (CodeRabbit)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    # Remove PR Info block and Ticket lines
    body = re.sub(r"##\s*PR Info.*?(?=\n##|\Z)", "", body, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"\*\*Ticket:\*\*.*", "", body, flags=re.IGNORECASE)
    body = re.sub(r"https://tensormatics\.atlassian\.net/browse/[A-Z]+-\d+", "", body)
    # Remove CodeRabbit headers
    body = re.sub(r"##\s*Summary by CodeRabbit.*", "", body, flags=re.IGNORECASE)
    body = re.sub(r"##\s*Summary by CodeRa.*", "", body, flags=re.IGNORECASE)
    body = re.sub(r"Summary by CodeRabbit.*", "", body, flags=re.IGNORECASE)
    # Remove markdown headers but keep their inline text if meaningful
    body = re.sub(r"^#{1,6}\s*", "", body, flags=re.MULTILINE)
    # Collapse whitespace
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = re.sub(r"\s{2,}", " ", body)
    # Keep only non-empty lines that are not just bullets
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    # Filter out lines that are obviously auto-generated fragments
    filtered = []
    for line in lines:
        low = line.lower()
        if "auto-generated comment" in low:
            continue
        if low.startswith("**ticket:**"):
            continue
        if line.strip() == "---":
            continue
        filtered.append(line)
    body = " ".join(filtered)
    body = re.sub(r"\s{2,}", " ", body).strip()
    return body[:2000]


# --- Groq ---
SYSTEM_PROMPT = """You are Labellerr's product marketer for a data labeling platform (images, video, text, audio, PDF, geospatial) for AI team leads, annotators, and business stakeholders — not engineers. Return JSON only.

For each input PR, you must:
1. Categorize exactly one: Added, Changed, Fixed, Security
   - Security: access, login, permissions, compliance, data protection
   - Added: new capability the user can see/use
   - Fixed: reliability or correctness — prevents failures, rework, or wrong data
   - Changed: existing capability made faster, smoother, or more accurate
2. Title: 3-8 words, Title Case, outcome-focused (what user gains, not what code changed). No ticket prefixes, no PR numbers, no repo names, no system/database/service names, no file/tech identifiers.
3. Summary: ONE sentence ending with "." , business outcome in plain language. Must answer: what can the user do now or what problem is gone, and why it matters (time saved, higher data quality, fewer errors, easier collaboration, stronger security). Never mention how it was built, never mention internal services, databases, storage, indexes, pipelines, protocols, or code patterns. Translate any technical title/body into user-visible benefit — do not copy jargon.
4. Strip [LABIMP-…]/[WA-…] before writing; never output them.
5. Do not invent beyond PR title/body; if PR is unclear, infer the user-visible effect conservatively.

Few-shot (diverse, generic — not tied to specific infra):
Input: {"id":"tensormatics/Actions#121","title":"Fix the video annotation delta handling process","body":"Fix delta handling for video saves"}
Output: {"id":"tensormatics/Actions#121","repository":"tensormatics/Actions","pr":121,"category":"Fixed","title":"More Reliable Video Annotations","summary":"Video annotations now save correctly, so work is not lost and review is faster."}
Input: {"id":"tensormatics/UsersCore#27","title":"Ability to add workspace level users and assign workspace roles","body":"Added workspace member management"}
Output: {"id":"tensormatics/UsersCore#27","repository":"tensormatics/UsersCore","pr":27,"category":"Added","title":"Workspace-Level User Management","summary":"Onboard teams once at the workspace and assign roles across projects without repeating setup."}
Input: {"id":"tensormatics/APIGateway#40","title":"Fix Ip allowlist for SDK requests and use token bucket for rate limit","body":"..."}
Output: {"id":"tensormatics/APIGateway#40","repository":"tensormatics/APIGateway","pr":40,"category":"Security","title":"More Secure SDK Access","summary":"SDK access can be limited to approved networks and handled fairly under load."}

Return strict JSON: {"results": [{"id": "repo#number", "repository": "...", "pr": 123, "category": "...", "title": "...", "summary": "..."}]} - id = "repository#number" exactly, every input appears once.
"""


def build_user_prompt(prs: List[Dict[str, Any]]) -> str:
    lines = ["Labellerr is a simple platform for labeling data for AI — images, video, text, PDFs, audio — quickly and accurately. Focus on annotator productivity, data quality, enterprise security. Summarize the following PRs. Return JSON only. Each input has an id like \"repo#number\" — echo it exactly in output.\n"]
    for pr in prs:
        lines.append(
            json.dumps(
                {
                    "id": f"{pr['repository']}#{pr['number']}",
                    "repository": pr["repository"],
                    "number": pr["number"],
                    "title": pr["title"],
                    "body": clean_pr_body((pr.get("body") or "")[:2000]),
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)


def call_groq(
    prs_batch: List[Dict[str, Any]],
    model: str,
    api_key: str,
    base_url: str,
    max_retries: int = 3,
) -> Optional[Dict[str, Any]]:
    if not api_key:
        return None
    # Use model from .env / workflow (default openai/gpt-oss-120b); changing .env changes it everywhere
    if not model:
        model = DEFAULT_GROQ_MODEL
    if OpenAI is None:
        eprint("    Groq: openai SDK not installed (pip install openai) — Groq is required")
        return None
    # Derive base_url (strip /chat/completions if present)
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
    if not base_url.rstrip("/").endswith("/openai/v1"):
        base_url = DEFAULT_GROQ_URL
    try:
        client = OpenAI(
            api_key=api_key.strip(),
            base_url=base_url,
            timeout=60.0,
        )
    except Exception as e:
        eprint(f"    Groq client init failed: {e}")
        return None

    backoff = 2
    for attempt in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(prs_batch)},
                ],
                temperature=0.2,
                max_tokens=4000,
                response_format={"type": "json_object"},
            )
            try:
                content = resp.choices[0].message.content or ""
            except Exception:
                eprint(f"    Groq unexpected response: {resp}")
                return None
            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)
            parsed = json.loads(content)
            return parsed
        except Exception as e:
            err_str = str(e)
            status = getattr(e, "status_code", None)
            is_retryable = False
            if status in RETRY_STATUS:
                is_retryable = True
            elif "429" in err_str or "500" in err_str or "502" in err_str or "503" in err_str or "504" in err_str:
                is_retryable = True
            elif isinstance(e, (TimeoutError,)):
                is_retryable = True
            try:
                import openai as _openai  # type: ignore

                if isinstance(e, (_openai.RateLimitError, _openai.APIConnectionError, _openai.APITimeoutError)):  # type: ignore
                    is_retryable = True
                if isinstance(e, _openai.APIStatusError) and getattr(e, "status_code", None) in RETRY_STATUS:  # type: ignore
                    is_retryable = True
            except Exception:
                pass

            if is_retryable and attempt < max_retries:
                log(f"    Groq retry {attempt+1}/{max_retries} after {type(e).__name__} ({status or err_str[:120]}) backoff {backoff}s")
                time.sleep(backoff)
                backoff *= 2
                continue
            if isinstance(e, json.JSONDecodeError):
                eprint(f"    Groq invalid JSON: {e} – content: {content[:600] if 'content' in locals() else 'N/A'}")
            else:
                eprint(f"    Groq error: {type(e).__name__}: {err_str[:600]}")
            if is_retryable and attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            return None
    return None


def validate_llm_results(
    parsed: Dict[str, Any],
    input_prs: List[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(parsed, dict) or "results" not in parsed:
        eprint("    LLM validation: missing 'results'")
        return None
    results = parsed["results"]
    if not isinstance(results, list):
        eprint("    LLM validation: results not list")
        return None
    # map input ids (repo#number) - repo-aware to handle duplicate numbers across repos
    input_ids = {f"{pr['repository']}#{pr['number']}": pr for pr in input_prs}
    input_numbers = {pr["number"]: pr for pr in input_prs}  # fallback for old LLM output
    validated = []
    seen = set()
    for r in results:
        if not isinstance(r, dict):
            eprint(f"    LLM validation: result not dict: {r}")
            return None
        # Prefer id, fallback to repository+pr composite
        rid = r.get("id")
        repo = r.get("repository")
        pr_num = r.get("pr")
        # Normalize pr_num to int if possible
        try:
            if pr_num is not None:
                pr_num = int(pr_num)
        except Exception:
            pass
        # Derive id if missing
        if not rid and repo and pr_num is not None:
            rid = f"{repo}#{pr_num}"
        elif not rid and pr_num is not None and pr_num in input_numbers:
            # Old LLM output with only pr number and no repo - try to resolve if unique
            # If duplicate numbers exist across repos, this is ambiguous -> fail
            candidates = [k for k in input_ids if k.endswith(f"#{pr_num}")]
            if len(candidates) == 1:
                rid = candidates[0]
                repo = rid.split("#")[0]
            else:
                eprint(f"    LLM validation: ambiguous pr {pr_num} without repository (candidates {candidates})")
                return None
        if not rid or rid not in input_ids:
            eprint(f"    LLM validation: id {rid!r} (pr {pr_num}) not in input")
            return None
        cat = r.get("category")
        title = r.get("title")
        summary = r.get("summary")
        if cat not in VALID_CATEGORIES:
            eprint(f"    LLM validation: invalid category {cat} for {rid}")
            return None
        if not title or not isinstance(title, str) or not title.strip():
            eprint(f"    LLM validation: missing title for {rid}")
            return None
        if not summary or not isinstance(summary, str) or not summary.strip():
            eprint(f"    LLM validation: missing summary for {rid}")
            return None
        title_clean = strip_ticket_prefix(title.strip())
        summary_clean = summary.strip()
        if not summary_clean.endswith("."):
            summary_clean += "."
        # Generic infra leak guard — no specific blocklist, works for any future infra
        # Reject business summaries that still contain code/system identifiers
        leak_check = f"{title_clean} {summary_clean}".lower()
        if "_" in title_clean or "_" in summary_clean:
            eprint(f"    LLM validation: infra leak underscore in {rid}: {title_clean[:60]!r}")
            return None
        if "tensormatics/" in leak_check or "labimp" in leak_check or "[wa-" in leak_check:
            eprint(f"    LLM validation: infra leak repo/ticket in {rid}")
            return None
        # Business titles should be plain without brackets (slashes like Undo/Redo are allowed)
        if "[" in title_clean or "]" in title_clean:
            eprint(f"    LLM validation: infra leak bracket in title {rid}: {title_clean[:60]!r}")
            return None
        if rid in seen:
            eprint(f"    LLM validation: duplicate id {rid}")
            return None
        seen.add(rid)
        validated.append(
            {
                "id": rid,
                "repository": rid.split("#")[0],
                "pr": pr_num,
                "category": cat,
                "title": title_clean[:120],
                "summary": summary_clean[:300],
            }
        )
    if len(validated) != len(input_prs):
        eprint(f"    LLM validation: expected {len(input_prs)} results, got {len(validated)}")
        return None
    return validated


# --- SemVer & changelog ---
VERSION_RE = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]\s*-\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)


def parse_latest_version(changelog_text: str) -> Tuple[int, int, int, str]:
    m = VERSION_RE.search(changelog_text)
    if not m:
        raise ValueError("Could not parse latest version from changelog.mdx")
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)


def bump_version(major: int, minor: int, patch: int, categories: Dict[str, List[Dict[str, str]]]) -> str:
    # if Added exists: MINOR bump else PATCH; never MAJOR
    has_added = bool(categories.get("Added"))
    if has_added:
        return f"{major}.{minor+1}.0"
    else:
        return f"{major}.{minor}.{patch+1}"


def generate_changelog_block(
    version: str,
    release_date: str,
    grouped: Dict[str, List[Dict[str, str]]],
) -> str:
    # Order: Added, Changed, Fixed, Security, only non-empty
    order = ["Added", "Changed", "Fixed", "Security"]
    lines = [f"## [{version}] - {release_date}", ""]
    for cat in order:
        items = grouped.get(cat, [])
        if not items:
            continue
        lines.append(f"### {cat}")
        lines.append("")
        for it in items:
            # ensure format: - **Feature name** — One concise sentence.
            title = it["title"].strip()
            summary = it["summary"].strip()
            # normalize dash
            lines.append(f"- **{title}** — {summary}")
        lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def find_insertion_point(text: str) -> int:
    # Find after Changelog Categories --- and before latest release
    # marker is: ## Changelog Categories ... ---
    # The file has:
    # ## Changelog Categories\n ... \n---\n\n## [2.0.2]
    # We want to insert immediately after that --- (the one after categories)
    # Simplest: find first occurrence of "\n---\n\n## [" and insert before "## ["
    m = re.search(r"## Changelog Categories.*?\n---\n", text, re.DOTALL)
    if not m:
        raise ValueError("Could not find Changelog Categories insertion point")
    # insertion is at end of match + one newline? Currently match ends at "---\n"
    # We need to insert after that line break.
    # Next char after match is "\n" then "## ["
    insert_at = m.end()
    # Ensure there's a blank line handling: the generated block starts with "## ["
    # The original has "\n\n## [2.0.2]" after ---. Our m ends at "---\n", so next is "\n## ["
    # So insert_at is correct (before the extra newline + ##)
    # But we want to ensure exactly one blank line before new entry? We'll handle.
    # Check: text[insert_at: insert_at+10] should be "\n## ["
    # We'll insert block + "\n" + rest
    return insert_at


def has_existing_entry(text: str, release_date: str) -> bool:
    # Check if exact release_date already exists (e.g. 2026-08-31).
    # We check exact date, not just month, so that a historical mid-month
    # entry like 2026-08-27 does not block generation of 2026-08-31.
    # Reruns for the same target month use the same last-day date, so
    # duplicate 2026-08-31 is still prevented. Branch idempotency
    # (chore/changelog-YYYY-MM) provides the month-level guard.
    pattern = re.compile(rf"^## \[\d+\.\d+\.\d+\]\s*-\s*{re.escape(release_date)}\b", re.MULTILINE)
    return bool(pattern.search(text))


# --- artifact ---
def write_artifact(
    prs: List[Dict[str, Any]],
    enriched: List[Dict[str, Any]],
    target_month: str,
    output_dir: Path = Path("."),
) -> Path:
    # enriched is list of dicts with repository, number, title, base, merged_at, category, summary_source, title/summary?
    # Build prs-YYYY-MM.json per spec §32
    data = []
    # create map from (repo, number) -> enriched
    enrich_map = {(e["repository"], e["number"]): e for e in enriched}
    for pr in prs:
        key = (pr["repository"], pr["number"])
        e = enrich_map.get(key, {})
        data.append(
            {
                "repository": pr["repository"],
                "number": pr["number"],
                "title": pr["title"],
                "base": pr["base"],
                "merged_at": pr["merged_at"],
                "category": e.get("category", "Unknown"),
                "summary_source": e.get("summary_source", "unknown"),
                "generated_title": e.get("title", ""),
                "generated_summary": e.get("summary", ""),
                "url": pr.get("url", ""),
            }
        )
    # sort by merged_at ascending per spec §24 (but prs already sorted, ensure)
    data.sort(key=lambda x: x["merged_at"])
    out_path = output_dir / f"prs-{target_month}.json"
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


# --- main ---
def main() -> int:
    parser = argparse.ArgumentParser(description="Generate monthly changelog")
    parser.add_argument("--month", type=str, default=None, help="Month to process YYYY-MM, defaults to previous month")
    parser.add_argument("--dry-run", action="store_true", help="Generate artifacts without modifying changelog.mdx or pushing")
    parser.add_argument("--repos-config", type=str, default=str(DEFAULT_REPOS_JSON))
    parser.add_argument("--category-map", type=str, default=str(DEFAULT_CATEGORY_MAP))
    parser.add_argument("--changelog", type=str, default=str(DEFAULT_CHANGELOG))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="PRs per LLM request")
    parser.add_argument("--output-dir", type=str, default=".", help="Directory for artifacts")
    parser.add_argument("--groq-model", type=str, default=None, help="Override GROQ_MODEL")
    parser.add_argument("--groq-url", type=str, default=DEFAULT_GROQ_URL)

    args = parser.parse_args()

    # Determine target month
    if args.month:
        try:
            year, month = parse_month(args.month)
            target_month = f"{year:04d}-{month:02d}"
        except Exception as e:
            eprint(f"ERROR: invalid --month: {e}")
            return 1
    else:
        y, m = previous_month()
        year, month = y, m
        target_month = f"{year:04d}-{month:02d}"

    month_start, next_month, release_date = month_bounds(year, month)
    branch_name = f"chore/changelog-{target_month}"
    log(f"Target month: {target_month}")
    log(f"  range: {month_start.isoformat()} <= merged_at < {next_month.isoformat()}")
    log(f"  release_date: {release_date}")
    log(f"  branch: {branch_name}")
    log(f"  dry_run: {args.dry_run}")

    repos_path = Path(args.repos_config)
    cat_path = Path(args.category_map)
    changelog_path = Path(args.changelog)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load configs
    try:
        repos = load_repos(repos_path)
    except Exception as e:
        eprint(f"ERROR: failed to load repos {repos_path}: {e}")
        return 1
    try:
        cat_map = load_category_map(cat_path)
    except Exception as e:
        eprint(f"ERROR: failed to load category map {cat_path}: {e}")
        return 1

    log(f"Repositories: {len(repos)}")
    for r in repos:
        log(f"  - {r}")

    gh_pat = os.environ.get("CHANGELOG_GH_PAT", "").strip()
    if not gh_pat:
        eprint("ERROR: CHANGELOG_GH_PAT not set")
        return 1

    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    # Model is configurable via .env / workflow vars — changing .env changes it everywhere
    groq_model = getattr(args, "groq_model", None) or os.environ.get("GROQ_MODEL", "").strip() or DEFAULT_GROQ_MODEL  # type: ignore
    groq_url = getattr(args, "groq_url", None) or os.environ.get("GROQ_URL", DEFAULT_GROQ_URL)  # type: ignore
    log(f"Using Groq model: {groq_model}")
    batch_size = args.batch_size
    if not (5 <= batch_size <= 50):
        eprint(f"WARN: batch_size {batch_size} unusual, clamping to 15")
        batch_size = 15

    if not changelog_path.exists():
        eprint(f"ERROR: changelog not found {changelog_path}")
        return 1

    changelog_text = changelog_path.read_text(encoding="utf-8")

    # existing entry protection §29
    if has_existing_entry(changelog_text, release_date):
        log(f"Existing entry for {target_month} ({release_date}) already present — skipping generation.")
        # still need to ensure artifacts? Spec says do not add another. We should exit 0 without modifying.
        # Create artifact indicating skipped?
        # For idempotency, just exit.
        return 0

    # Check branch/PR idempotency early? We'll handle after generation.

    # --- fetch PRs ---
    all_prs: List[Dict[str, Any]] = []
    per_repo_stats: List[Tuple[str, int, int]] = []
    total_fetched = 0
    for repo in repos:
        try:
            fetched, qualifying = fetch_prs_for_repo(repo, gh_pat, month_start, next_month)
            total_fetched += fetched
            per_repo_stats.append((repo, fetched, len(qualifying)))
            log(f"{repo}: fetched {fetched}, qualifying {len(qualifying)}")
            all_prs.extend(qualifying)
        except SystemExit as e:
            eprint(str(e))
            return 1
        except Exception as e:
            eprint(f"ERROR fetching {repo}: {e}")
            return 1

    log(f"Total fetched: {total_fetched}")
    # Deduplicate repo+number §24
    seen_keys = set()
    deduped: List[Dict[str, Any]] = []
    for pr in all_prs:
        key = (pr["repository"], pr["number"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(pr)
    all_prs = deduped
    # Sort by merged_at ascending §24 — do not depend on LLM ordering
    all_prs.sort(key=lambda x: x["merged_at_dt"])

    # Re-filter? already filtered, but ensure
    merged_in_month = len(all_prs)
    log(f"Merged in month: {merged_in_month}")

    # Exclude noise §14
    exclude_terms = cat_map.get("exclude", [])
    filtered: List[Dict[str, Any]] = []
    excluded = 0
    for pr in all_prs:
        if is_noise(pr, exclude_terms):
            excluded += 1
            log(f"  excluded noise: {pr['repository']}#{pr['number']} {pr['title'][:80]}")
        else:
            filtered.append(pr)
    log(f"Excluded: {excluded}")
    log(f"Sent to LLM: {len(filtered)}")

    if not filtered:
        log(f"No qualifying merged PRs found for {target_month}.")
        # write empty artifact for observability
        empty_artifact = output_dir / f"prs-{target_month}.json"
        empty_artifact.write_text(json.dumps([], indent=2), encoding="utf-8")
        log(f"Artifact: {empty_artifact}")
        return 0

    # --- LLM summarization (Groq-only, infinite 2m retry, no heuristic) ---
    llm_summarized = 0
    enriched: List[Dict[str, Any]] = []  # each with repository, number, category, title, summary, summary_source, merged_at_dt

    # batch
    batches = [filtered[i : i + batch_size] for i in range(0, len(filtered), batch_size)]
    log(f"Batching {len(filtered)} PRs into {len(batches)} request(s) (batch_size={batch_size})")

    for idx, batch in enumerate(batches):
        log(f" Batch {idx+1}/{len(batches)}: {len(batch)} PRs")
        if not groq_key:
            eprint("ERROR: GROQ_API_KEY not set — Groq is required (heuristic fallback removed)")
            return 1
        if OpenAI is None:
            eprint("ERROR: openai SDK not installed (pip install openai) — Groq is required")
            return 1
        gap_attempt = 0
        while True:
            llm_ok = False
            validated = None
            for attempt in range(2):  # tight retry for transient/validation
                parsed = call_groq(batch, groq_model, groq_key, groq_url)
                if parsed is None:
                    if attempt == 0:
                        log("    Groq call failed, retrying batch...")
                        continue
                    else:
                        break
                validated = validate_llm_results(parsed, batch)
                if validated is None:
                    if attempt == 0:
                        log("    Groq validation failed, retrying batch...")
                        continue
                    else:
                        break
                # success - map via repo-aware id (e.g. tensormatics/Actions#121)
                id_to_validated = {v["id"]: v for v in validated}
                temp_enriched = []
                all_found = True
                for pr in batch:
                    pid = f"{pr['repository']}#{pr['number']}"
                    v = id_to_validated.get(pid)
                    if not v:
                        log(f"    WARN missing Groq result for {pid} — will retry batch")
                        all_found = False
                        break
                    temp_enriched.append(
                        {
                            "repository": pr["repository"],
                            "number": pr["number"],
                            "merged_at": pr["merged_at"],
                            "merged_at_dt": pr["merged_at_dt"],
                            "category": v["category"],
                            "title": v["title"],
                            "summary": v["summary"],
                            "summary_source": "llm",
                            "url": pr["url"],
                            "original_title": pr["title"],
                        }
                    )
                if all_found:
                    enriched.extend(temp_enriched)
                    llm_summarized += len(validated)
                    llm_ok = True
                    break
                else:
                    break
            if llm_ok:
                break
            gap_attempt += 1
            log(f"  Groq failed for batch {idx+1}, waiting 120s before infinite retry {gap_attempt} (model {groq_model})...")
            time.sleep(120)

    # sort enriched by merged_at ascending (deterministic)
    enriched.sort(key=lambda x: x["merged_at_dt"])

    # Observability §42
    # Count per category
    cat_counts = {c: 0 for c in VALID_CATEGORIES}
    for e in enriched:
        cat_counts[e["category"]] += 1
    log(f"LLM summarized: {llm_summarized} (Groq {groq_model})")
    log(f"Added: {cat_counts['Added']}")
    log(f"Changed: {cat_counts['Changed']}")
    log(f"Fixed: {cat_counts['Fixed']}")
    log(f"Security: {cat_counts['Security']}")

    # Prepare grouped for changelog
    grouped: Dict[str, List[Dict[str, str]]] = {c: [] for c in VALID_CATEGORIES}
    for e in enriched:
        grouped[e["category"]].append({"title": e["title"], "summary": e["summary"]})

    # --- SemVer ---
    try:
        major, minor, patch, _ = parse_latest_version(changelog_text)
        log(f"Latest version: {major}.{minor}.{patch}")
    except Exception as e:
        eprint(f"ERROR: could not parse version: {e}")
        return 1
    next_version = bump_version(major, minor, patch, grouped)
    log(f"Next version: {next_version}")

    # --- Generate block ---
    block = generate_changelog_block(next_version, release_date, grouped)
    log("Generated block:")
    log(block)

    # --- Insertion ---
    try:
        insert_at = find_insertion_point(changelog_text)
    except Exception as e:
        eprint(f"ERROR: {e}")
        return 1

    # Build new text
    # insert_at points to right after "---\n" following categories
    # Need to handle newline: ensure block inserted with proper spacing
    before = changelog_text[:insert_at]
    after = changelog_text[insert_at:]
    # before ends with "---\n", after starts with "\n## ["
    # block already ends with "---\n\n"
    # So new_text = before + "\n" + block + after.lstrip("\n")? Let's preserve.
    # Original: before = "...\n---\n", after = "\n## [2.0.2]..."
    # Desired: before + "\n" + block + after
    # Which gives "...\n---\n\n## [new] ...\n---\n\n\n## [2.0.2]"
    # But we want "...\n---\n\n## [new] ...\n---\n\n## [2.0.2]"
    # So we need to ensure exactly one blank line.
    # Let's construct:
    if not before.endswith("\n"):
        before += "\n"
    # ensure block starts without extra newline if before already ends with \n
    # block starts with "## ["
    new_text = before + "\n" + block + after.lstrip("\n")

    # --- Dry-run handling ---
    # Prepare artifacts
    prs_artifact = write_artifact(filtered, enriched, target_month, output_dir)
    log(f"Artifact: {prs_artifact}")

    # Generate patch for dry-run
    patch_path = output_dir / "changelog.patch"
    patch_text = "".join(
        difflib.unified_diff(
            changelog_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile="changelog.mdx",
            tofile="changelog.mdx",
        )
    )
    if not patch_text:
        patch_text = f"No changes (already up to date for {target_month})\n"
    patch_path.write_text(patch_text, encoding="utf-8")
    log(f"Patch: {patch_path} ({len(patch_text)} bytes)")

    if args.dry_run:
        log("Dry-run: NO push, NO branch, NO PR, NO modification of changelog.mdx (verified)")
        # verify working tree unchanged
        # we already didn't write to changelog_path, so check
        current = changelog_path.read_text(encoding="utf-8")
        if current != changelog_text:
            eprint("ERROR: dry-run would have modified working tree!")
            return 1
        log("Working tree unchanged: OK")
        return 0

    # --- Non-dry-run: modify file, create branch/PR ---
    # Check if file already has entry (again, in case race)
    if has_existing_entry(changelog_path.read_text(encoding="utf-8"), release_date):
        log(f"Existing entry for {target_month} found just before write — aborting to avoid duplicate.")
        return 0

    # Write new changelog
    changelog_path.write_text(new_text, encoding="utf-8")
    log(f"Wrote {changelog_path}")

    # Git operations — only if GITHUB_TOKEN available and we are in git repo
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    github_repo = os.environ.get("GITHUB_REPOSITORY", "Labellerr/Documentation")
    # Check if we're in CI or local git; attempt to create branch/PR via git + gh api if possible
    # Use git CLI
    import subprocess

    def run_git(*args, check=True, capture=False):
        cmd = ["git"] + list(args)
        log(f"  $ {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=capture, text=True)
        if check and result.returncode != 0:
            eprint(f"git {' '.join(args)} failed: {result.stderr[:500] if result.stderr else ''}")
            raise SystemExit(1)
        return result

    try:
        # Ensure git config
        run_git("config", "user.name", "changelog-bot")
        run_git("config", "user.email", "changelog-bot@users.noreply.github.com")

        # Check if branch exists locally or remotely
        # Fetch to know remote branches
        try:
            run_git("fetch", "origin", check=False)
        except Exception:
            pass

        # check local
        r = run_git("branch", "--list", branch_name, capture=True, check=False)
        local_branch_exists = branch_name in (r.stdout or "")
        # check remote
        r2 = run_git("ls-remote", "--heads", "origin", branch_name, capture=True, check=False)
        remote_branch_exists = branch_name in (r2.stdout or "")

        if remote_branch_exists:
            log(f"Branch {branch_name} exists remotely — basing update on origin/{branch_name}")
            run_git("checkout", "-B", branch_name, f"origin/{branch_name}")
        elif local_branch_exists:
            log(f"Branch {branch_name} exists locally — updating")
            run_git("checkout", "-B", branch_name)
        else:
            log(f"Creating branch {branch_name}")
            run_git("checkout", "-b", branch_name)

        # Add and commit
        run_git("add", str(changelog_path))
        # also add artifacts? No, only changelog.mdx per §3
        # Check if there's diff to commit
        r = run_git("status", "--porcelain", capture=True)
        if not r.stdout.strip():
            log("No changes to commit (maybe already committed)")
        else:
            run_git("commit", "-m", f"chore: update changelog for {target_month}")

        # Push
        if github_token:
            # Use token for push via https
            # Configure remote URL with token
            # Safer to rely on already configured credential helper in Actions; but we can push directly
            # In Actions, GITHUB_TOKEN is already available via git credential
            # Try simple push
            try:
                run_git("push", "-u", "origin", branch_name)
            except SystemExit:
                # Try with token URL
                remote_url = f"https://x-access-token:{github_token}@github.com/{github_repo}.git"
                run_git("push", remote_url, f"HEAD:{branch_name}")
        else:
            log("WARN: GITHUB_TOKEN not set — skipping push (local run?)")
            log("Branch prepared locally, push manually: git push -u origin " + branch_name)
            return 0

        # Create/update PR via GitHub API
        if not github_token:
            log("No GITHUB_TOKEN, skipping PR creation")
            return 0

        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # Check if open PR exists for branch
        pr_list_url = f"{GH_API}/repos/{github_repo}/pulls"
        resp = request_with_retry("GET", pr_list_url, headers, params={"head": f"Labellerr:{branch_name}", "state": "open"})
        open_pr = None
        if resp.status_code == 200:
            prs = resp.json()
            if isinstance(prs, list) and prs:
                open_pr = prs[0]
                log(f"Found existing PR #{open_pr['number']}: {open_pr['html_url']}")
        else:
            log(f"WARN: could not list PRs: {resp.status_code} {resp.text[:300]}")

        # Prepare PR title/body
        month_name = datetime.date(year, month, 1).strftime("%B %Y")
        pr_title = f"chore: update changelog for {month_name}"
        total_qualifying = len(enriched)
        pr_body = f"""Auto-generated monthly changelog.

Source period: {target_month}
Source repositories: {len(repos)}
Qualifying merged PRs: {total_qualifying}

Added: {cat_counts['Added']}
Changed: {cat_counts['Changed']}
Fixed: {cat_counts['Fixed']}
Security: {cat_counts['Security']}

Summarization:
- Groq model: `{groq_model}` (infinite 2m retry, no heuristic fallback)
- LLM summarized: {llm_summarized} PRs

Branch: `{branch_name}`
Release: `[{next_version}] - {release_date}`

> Generated by `scripts/monthly-changelog/generate.py` — review bullets before merging.
"""

        if open_pr:
            # Update PR
            patch_url = f"{GH_API}/repos/{github_repo}/pulls/{open_pr['number']}"
            resp = request_with_retry("PATCH", patch_url, headers, json_body={"title": pr_title, "body": pr_body})
            if resp.status_code == 200:
                log(f"Updated PR #{open_pr['number']}")
            else:
                log(f"WARN failed to update PR: {resp.status_code} {resp.text[:500]}")
        else:
            # Create PR
            create_body = {
                "title": pr_title,
                "body": pr_body,
                "head": branch_name,
                "base": "main",
            }
            resp = request_with_retry("POST", pr_list_url, headers, json_body=create_body)
            if resp.status_code in (200, 201):
                new_pr = resp.json()
                log(f"Created PR #{new_pr['number']}: {new_pr['html_url']}")
            else:
                eprint(f"ERROR creating PR: {resp.status_code} {resp.text[:800]}")
                # Don't fail workflow if PR creation fails? But spec says fail on auth errors.
                # We'll return 1 to make visible.
                return 1

        return 0

    except SystemExit:
        raise
    except Exception as e:
        eprint(f"ERROR during git/PR: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
