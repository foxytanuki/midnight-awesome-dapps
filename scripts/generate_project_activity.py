#!/usr/bin/env python3

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "README.md"
DORMANT_README_PATH = ROOT / "dormant_projects" / "README.md"
OUTPUT_PATH = ROOT / "docs" / "project-activity.md"
SNAPSHOT_DATE = date(2026, 4, 5)

ACTIVE_DAYS = 90
WARM_DAYS = 365

MIDNIGHT_RELEASE_URL = "https://docs.midnight.network/relnotes/overview"
COMPACT_RELEASE_URL = "https://docs.midnight.network/relnotes/compact"
COMPACT_GRAMMAR_URL = "https://docs.midnight.network/compact/reference/compact-grammar"

MIDNIGHT_DOCS_UPDATED = date(2026, 4, 2)
COMPACT_LANGUAGE_022_RELEASED = date(2026, 3, 17)
LATEST_STABLE_NOTES = {
    "Ledger": "8.0.3",
    "Node (Preview)": "0.22.3",
    "Node (Preprod/Mainnet)": "0.22.2",
    "Proof Server": "8.0.3",
    "Compact toolchain": "0.5.1",
    "Compact compiler": "0.30.0",
    "Compact language": "0.22.0",
    "Midnight.js": "4.0.2",
    "Indexer": "4.0.1",
}

ROOT_SECTIONS = {
    "Getting Started",
    "Smart Contract Primitives",
    "Starter Templates",
    "Developer Tools",
    "Finance & DeFi",
    "Identity & Privacy",
    "Gaming",
    "Governance",
    "Healthcare",
}

DORMANT_SECTIONS = {"Gaming"}

LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
HEADING_RE = re.compile(r"^(#{2,6})\s+(.*)$")
BULLET_RE = re.compile(r"^\s*-\s+(.*)$")


@dataclass(slots=True)
class Entry:
    name: str
    url: str
    section: str
    source_file: str
    repo_name_with_owner: str | None
    link_type: str


def normalize_heading(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def extract_repo_name(url: str) -> tuple[str | None, str]:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return None, "external"

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None, "external"

    repo_name = f"{parts[0]}/{parts[1]}"
    if len(parts) >= 3 and parts[2] in {
        "blob",
        "tree",
        "releases",
        "tags",
        "issues",
        "pulls",
    }:
        return repo_name, "github-derived"
    return repo_name, "github-repo"


def parse_entries(path: Path, allowed_sections: set[str]) -> list[Entry]:
    entries: list[Entry] = []
    current_section: str | None = None

    for raw_line in path.read_text().splitlines():
        heading_match = HEADING_RE.match(raw_line)
        if heading_match:
            level = len(heading_match.group(1))
            heading = normalize_heading(heading_match.group(2))
            if level == 2:
                current_section = heading if heading in allowed_sections else None
            elif level == 3 and path == DORMANT_README_PATH:
                current_section = (
                    heading if heading in allowed_sections else current_section
                )
            continue

        if current_section is None:
            continue

        bullet_match = BULLET_RE.match(raw_line)
        if not bullet_match:
            continue

        body = bullet_match.group(1)
        link_match = LINK_RE.search(body)
        if not link_match:
            continue

        name, url = link_match.groups()
        repo_name_with_owner, link_type = extract_repo_name(url)
        entries.append(
            Entry(
                name=name.strip(),
                url=url.strip(),
                section=current_section,
                source_file=path.relative_to(ROOT).as_posix(),
                repo_name_with_owner=repo_name_with_owner,
                link_type=link_type,
            )
        )

    return entries


def batch(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def gh_graphql(query: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def fetch_repo_activity(repo_names: list[str]) -> dict[str, dict[str, Any]]:
    fragment = """
    fragment RepoFields on Repository {
      nameWithOwner
      url
      isArchived
      isDisabled
      stargazerCount
      forkCount
      createdAt
      updatedAt
      pushedAt
      defaultBranchRef {
        name
        target {
          ... on Commit {
            committedDate
          }
        }
      }
      releases(first: 1, orderBy: {field: CREATED_AT, direction: DESC}) {
        nodes {
          tagName
          publishedAt
          createdAt
        }
      }
      issues(states: OPEN, first: 1, orderBy: {field: UPDATED_AT, direction: DESC}) {
        totalCount
        nodes {
          updatedAt
          number
          title
          url
        }
      }
      pullRequests(states: OPEN, first: 1, orderBy: {field: UPDATED_AT, direction: DESC}) {
        totalCount
        nodes {
          updatedAt
          number
          title
          url
        }
      }
      repositoryTopics(first: 20) {
        nodes {
          topic {
            name
          }
        }
      }
    }
    """

    results: dict[str, dict[str, Any]] = {}
    for repo_batch in batch(repo_names, 20):
        fields: list[str] = []
        for index, repo_name in enumerate(repo_batch):
            owner, name = repo_name.split("/", 1)
            fields.append(
                f'r{index}: repository(owner: "{owner}", name: "{name}") {{ ...RepoFields }}'
            )
        query = fragment + "\nquery {\n" + "\n".join(fields) + "\n}"
        response = gh_graphql(query)
        data = response["data"]
        for index, repo_name in enumerate(repo_batch):
            results[repo_name] = data.get(f"r{index}")
    return results


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def days_old(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return (SNAPSHOT_DATE - dt.date()).days


def latest_code_signal(repo: dict[str, Any]) -> tuple[str, datetime | None]:
    primary_candidates: list[tuple[str, datetime | None]] = [
        (
            "default-branch commit",
            parse_dt(
                ((repo.get("defaultBranchRef") or {}).get("target") or {}).get(
                    "committedDate"
                )
            ),
        ),
        (
            "latest release",
            parse_dt(
                ((repo.get("releases") or {}).get("nodes") or [{}])[0].get(
                    "publishedAt"
                )
            ),
        ),
    ]
    valid_candidates = [
        (label, dt) for label, dt in primary_candidates if dt is not None
    ]
    if not valid_candidates:
        fallback_push = parse_dt(repo.get("pushedAt"))
        if fallback_push is not None:
            return "repo push (fallback)", fallback_push
        return "none", None
    return max(valid_candidates, key=lambda item: item[1])


def latest_support_signal(repo: dict[str, Any]) -> tuple[str, datetime | None]:
    candidates: list[tuple[str, datetime | None]] = [
        ("repo push", parse_dt(repo.get("pushedAt"))),
        (
            "open issue",
            parse_dt(
                (((repo.get("issues") or {}).get("nodes")) or [{}])[0].get("updatedAt")
            ),
        ),
        (
            "open PR",
            parse_dt(
                (((repo.get("pullRequests") or {}).get("nodes")) or [{}])[0].get(
                    "updatedAt"
                )
            ),
        ),
    ]
    valid_candidates = [(label, dt) for label, dt in candidates if dt is not None]
    if not valid_candidates:
        return "none", None
    return max(valid_candidates, key=lambda item: item[1])


def classify(
    link_type: str, repo: dict[str, Any] | None
) -> tuple[str, str, datetime | None, str, datetime | None, str]:
    if link_type == "external":
        return (
            "External",
            "No public GitHub repo linked in README",
            None,
            "external link",
            None,
            "none",
        )
    if repo is None:
        return (
            "Unknown",
            "GitHub repo lookup failed",
            None,
            "repo lookup failed",
            None,
            "none",
        )

    code_signal_name, code_signal_dt = latest_code_signal(repo)
    support_signal_name, support_signal_dt = latest_support_signal(repo)

    if repo.get("isArchived"):
        return (
            "Archived",
            "GitHub repo is archived",
            code_signal_dt,
            code_signal_name,
            support_signal_dt,
            support_signal_name,
        )

    age = days_old(code_signal_dt)
    if age is None:
        return (
            "Unknown",
            "No usable GitHub activity signal",
            code_signal_dt,
            code_signal_name,
            support_signal_dt,
            support_signal_name,
        )
    if age <= ACTIVE_DAYS:
        return (
            "Active",
            f"Latest code/release signal is {age} days old",
            code_signal_dt,
            code_signal_name,
            support_signal_dt,
            support_signal_name,
        )
    if age <= WARM_DAYS:
        return (
            "Warm",
            f"Latest code/release signal is {age} days old",
            code_signal_dt,
            code_signal_name,
            support_signal_dt,
            support_signal_name,
        )

    return (
        "Dormant",
        f"Latest code/release signal is {age} days old",
        code_signal_dt,
        code_signal_name,
        support_signal_dt,
        support_signal_name,
    )


def render_date(dt: datetime | None) -> str:
    return dt.date().isoformat() if dt else "-"


def render_repo_cell(entry: Entry, repo: dict[str, Any] | None) -> str:
    if entry.repo_name_with_owner and repo and repo.get("url"):
        return f"[{entry.repo_name_with_owner}]({repo['url']})"
    if entry.repo_name_with_owner:
        return entry.repo_name_with_owner
    return "-"


def build_report(entries: list[Entry], repo_lookup: dict[str, dict[str, Any]]) -> str:
    rows: list[dict[str, Any]] = []
    classification_counts = Counter()
    section_counts: dict[str, Counter[str]] = defaultdict(Counter)

    for entry in entries:
        repo = (
            repo_lookup.get(entry.repo_name_with_owner)
            if entry.repo_name_with_owner
            else None
        )
        (
            verdict,
            rationale,
            signal_dt,
            signal_name,
            support_signal_dt,
            support_signal_name,
        ) = classify(entry.link_type, repo)
        classification_counts[verdict] += 1
        section_counts[entry.section][verdict] += 1

        issues = (repo or {}).get("issues") or {}
        prs = (repo or {}).get("pullRequests") or {}
        latest_release = (((repo or {}).get("releases") or {}).get("nodes") or [{}])[0]
        topics = [
            node["topic"]["name"]
            for node in (
                ((repo or {}).get("repositoryTopics") or {}).get("nodes") or []
            )
        ]
        rows.append(
            {
                "section": entry.section,
                "source_file": entry.source_file,
                "name": entry.name,
                "entry_url": entry.url,
                "repo_cell": render_repo_cell(entry, repo),
                "repo_name_with_owner": entry.repo_name_with_owner or "-",
                "link_type": entry.link_type,
                "verdict": verdict,
                "rationale": rationale,
                "signal_name": signal_name,
                "signal_date": render_date(signal_dt),
                "support_signal_name": support_signal_name,
                "support_signal_date": render_date(support_signal_dt),
                "default_commit": render_date(
                    parse_dt(
                        (
                            ((repo or {}).get("defaultBranchRef") or {}).get("target")
                            or {}
                        ).get("committedDate")
                    )
                ),
                "repo_pushed": render_date(parse_dt((repo or {}).get("pushedAt"))),
                "release": latest_release.get("tagName") or "-",
                "release_date": render_date(
                    parse_dt(latest_release.get("publishedAt"))
                ),
                "open_issues": issues.get("totalCount", "-"),
                "open_prs": prs.get("totalCount", "-"),
                "stars": (repo or {}).get("stargazerCount", "-"),
                "forks": (repo or {}).get("forkCount", "-"),
                "archived": "yes" if (repo or {}).get("isArchived") else "no",
                "topics": ", ".join(topics[:5]) if topics else "-",
            }
        )

    rows.sort(key=lambda item: (item["verdict"], item["section"], item["name"].lower()))

    post_compact = sum(
        1
        for row in rows
        if row["signal_date"] != "-"
        and row["signal_date"] >= COMPACT_LANGUAGE_022_RELEASED.isoformat()
    )
    post_docs = sum(
        1
        for row in rows
        if row["signal_date"] != "-"
        and row["signal_date"] >= MIDNIGHT_DOCS_UPDATED.isoformat()
    )
    repo_weighted_counts = Counter()
    repo_verdicts: dict[str, str] = {}
    for row in rows:
        repo_name = row["repo_name_with_owner"]
        if repo_name == "-":
            continue
        repo_verdicts.setdefault(repo_name, row["verdict"])
    for verdict in repo_verdicts.values():
        repo_weighted_counts[verdict] += 1
    github_entries = sum(1 for row in rows if row["repo_name_with_owner"] != "-")
    unique_repos = len(
        {
            row["repo_name_with_owner"]
            for row in rows
            if row["repo_name_with_owner"] != "-"
        }
    )
    external_entries = sum(1 for row in rows if row["verdict"] == "External")

    lines: list[str] = []
    lines.append("# Project Activity Snapshot")
    lines.append("")
    lines.append(
        f"Generated on {SNAPSHOT_DATE.isoformat()} from `README.md` and `dormant_projects/README.md`."
    )
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append(
        "- Scope: project-like entries in the following sections: Getting Started, Smart Contract Primitives, Starter Templates, Developer Tools, Finance & DeFi, Identity & Privacy, Gaming, Governance, Healthcare, and dormant_projects/Gaming."
    )
    lines.append(
        "- GitHub-backed entries use **code/release signals first**: default-branch commit and latest release. Repo push is used only as a fallback/context signal."
    )
    lines.append(
        "- Open issue / open PR updates are treated as **supporting signals only** so a repo is not marked Active solely because of discussion around stale code."
    )
    lines.append(
        f"- Classification: **Active** ≤ {ACTIVE_DAYS} days on code/release signals, **Warm** {ACTIVE_DAYS + 1}-{WARM_DAYS} days on code/release signals, **Dormant** > {WARM_DAYS} days on code/release signals, **Archived** if GitHub archived, **External** if README links no public GitHub repo."
    )
    lines.append(
        "- Note: some README items point to files inside a repo (for example primitive contract links). Those inherit activity from the parent repo."
    )
    lines.append(
        "- The full table is entry-weighted because README has repeated links into the same repo; the summary also includes repo-weighted counts to avoid overstating duplicated repos."
    )
    lines.append("")
    lines.append("## Midnight / Compact release reference")
    lines.append("")
    lines.append(f"- Midnight release hub: {MIDNIGHT_RELEASE_URL}")
    lines.append(f"- Compact release notes: {COMPACT_RELEASE_URL}")
    lines.append(f"- Compact grammar reference: {COMPACT_GRAMMAR_URL}")
    lines.append(
        f"- Midnight docs latest stable page updated: **{MIDNIGHT_DOCS_UPDATED.isoformat()}**"
    )
    lines.append(
        f"- Compact compiler **0.30.0** / Compact language **0.22.0** released: **{COMPACT_LANGUAGE_022_RELEASED.isoformat()}**"
    )
    lines.append("- Latest stable versions referenced from the docs snapshot:")
    for component, version in LATEST_STABLE_NOTES.items():
        lines.append(f"  - {component}: {version}")
    lines.append("")
    lines.append(
        "This release baseline is used only as context. A repo being older than these dates does **not** automatically mean incompatibility, but it is a useful staleness signal for Midnight/Compact integration work."
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total entries assessed: **{len(rows)}**")
    lines.append(
        f"- GitHub-backed entries: **{github_entries}** across **{unique_repos}** unique repositories"
    )
    lines.append(f"- External-only entries: **{external_entries}**")
    lines.append(
        f"- Entries with a latest code/release signal on/after Compact 0.22.0 release ({COMPACT_LANGUAGE_022_RELEASED.isoformat()}): **{post_compact}**"
    )
    lines.append(
        f"- Entries with a latest code/release signal on/after the Midnight stable docs update ({MIDNIGHT_DOCS_UPDATED.isoformat()}): **{post_docs}**"
    )
    lines.append("")
    lines.append("### By verdict")
    lines.append("")
    for verdict in ["Active", "Warm", "Dormant", "Archived", "External", "Unknown"]:
        lines.append(f"- {verdict}: **{classification_counts[verdict]}**")
    lines.append("")
    lines.append("### Repo-weighted verdicts (unique GitHub repos)")
    lines.append("")
    for verdict in ["Active", "Warm", "Dormant", "Archived", "Unknown"]:
        lines.append(f"- {verdict}: **{repo_weighted_counts[verdict]}**")
    lines.append("")
    lines.append("### By section")
    lines.append("")
    lines.append(
        "| Section | Active | Warm | Dormant | Archived | External | Unknown |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for section in sorted(section_counts):
        counts = section_counts[section]
        lines.append(
            f"| {section} | {counts['Active']} | {counts['Warm']} | {counts['Dormant']} | {counts['Archived']} | {counts['External']} | {counts['Unknown']} |"
        )
    lines.append("")
    lines.append("## Recommended reading of the snapshot")
    lines.append("")
    lines.append(
        "- **Active**: currently the safest candidates to highlight, evaluate, or reference as living examples."
    )
    lines.append(
        "- **Warm**: still plausible, but likely needs a quick compatibility check before promotion."
    )
    lines.append(
        "- **Dormant**: useful as historical reference or adoption targets; expect some drift from the current Midnight/Compact stack."
    )
    lines.append(
        "- **Archived**: keep only as historical references unless maintainership is explicitly resumed."
    )
    lines.append(
        "- **External**: website-linked entries that need a separate manual check because README does not expose a public GitHub repository."
    )
    lines.append("")
    lines.append("## Full table")
    lines.append("")
    lines.append(
        "| Verdict | Section | Project | Repo | Latest code/release signal | Signal date | Latest support signal | Support date | Default branch commit | Latest release | Release date | Open issues | Open PRs | Stars | Archived | Source |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |"
    )
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    row["verdict"],
                    row["section"],
                    f"[{row['name']}]({row['entry_url']})",
                    row["repo_cell"],
                    row["signal_name"],
                    row["signal_date"],
                    row["support_signal_name"],
                    row["support_signal_date"],
                    row["default_commit"],
                    row["release"],
                    row["release_date"],
                    str(row["open_issues"]),
                    str(row["open_prs"]),
                    str(row["stars"]),
                    row["archived"],
                    row["source_file"],
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- This is a public-signal snapshot, not a code-level compatibility audit."
    )
    lines.append(
        "- For high-value repos, the next step would be checking Compact pragmas, SDK versions, and CI success against the current release matrix."
    )
    lines.append(
        "- Support signals are shown for context only; the verdict itself is intentionally anchored to code/release freshness."
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    entries = parse_entries(README_PATH, ROOT_SECTIONS) + parse_entries(
        DORMANT_README_PATH, DORMANT_SECTIONS
    )
    repo_names = sorted(
        {entry.repo_name_with_owner for entry in entries if entry.repo_name_with_owner}
    )
    repo_lookup = fetch_repo_activity(repo_names)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(build_report(entries, repo_lookup))

    print(
        f"entries={len(entries)} repos={len(repo_names)} output={OUTPUT_PATH.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    main()
