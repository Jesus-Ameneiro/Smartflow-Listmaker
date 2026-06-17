"""
Smartflow Batch Selector — GitHub Manager
All GitHub API read/write/delete operations for history management.
"""

import base64
import io
from datetime import datetime

import pandas as pd
import requests


# ── Internals ─────────────────────────────────────────────────────────────────

def _headers(token):
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def _parse_ts(filename):
    """Extract datetime from batch_YYYYMMDD_HHMMSS.csv filename."""
    try:
        ts = filename.replace("batch_", "").replace(".csv", "")
        return datetime.strptime(ts, "%Y%m%d_%H%M%S")
    except Exception:
        return datetime.min


def _get_file_meta(token, repo, path):
    """Return (sha, decoded_content_str) for a single file, or (None, None)."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    resp = requests.get(url, headers=_headers(token), timeout=15)
    if resp.status_code == 404:
        return None, None
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return data["sha"], content


def _put_file(token, repo, path, content_str, message, sha=None):
    """Create or update a file. Returns True on success."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content_str.encode()).decode(),
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(url, json=payload, headers=_headers(token), timeout=15)
    return resp.status_code in (200, 201)


# ── Batch Files ───────────────────────────────────────────────────────────────

def get_all_batches(token, repo, folder="history"):
    """
    Fetch all confirmed batch files from GitHub, sorted chronologically.
    Returns a list of dicts:
        number          int        1-based sequential batch number
        name            str        filename (batch_YYYYMMDD_HHMMSS.csv)
        path            str        full repo path
        sha             str        needed for deletion
        confirmed_at    datetime   parsed from filename
        df              DataFrame  loaded case data
    """
    url = f"https://api.github.com/repos/{repo}/contents/{folder}"
    resp = requests.get(url, headers=_headers(token), timeout=15)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()

    entries = [
        e for e in resp.json()
        if e["name"].startswith("batch_") and e["name"].endswith(".csv")
    ]
    entries.sort(key=lambda e: e["name"])   # lexicographic = chronological

    batches = []
    for i, entry in enumerate(entries, start=1):
        raw = requests.get(entry["download_url"], timeout=15)
        raw.raise_for_status()
        df = pd.read_csv(io.StringIO(raw.text))
        batches.append({
            "number":       i,
            "name":         entry["name"],
            "path":         entry["path"],
            "sha":          entry["sha"],
            "confirmed_at": _parse_ts(entry["name"]),
            "df":           df,
        })
    return batches


def push_batch(output_df, token, repo, folder="history"):
    """Push a new batch CSV. Returns (success, filename)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"batch_{ts}.csv"
    path = f"{folder}/{filename}"
    ok = _put_file(
        token, repo, path,
        output_df.to_csv(index=False),
        f"Add confirmed batch {ts}",
    )
    return ok, filename


def delete_batch(token, repo, path, sha):
    """Delete a batch file. Returns True on success."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    payload = {"message": f"Delete {path}", "sha": sha}
    resp = requests.delete(url, json=payload, headers=_headers(token), timeout=15)
    return resp.status_code == 200


# ── Updates Log ───────────────────────────────────────────────────────────────

_UPDATES_PATH = "history/updates_log.csv"
_UPDATES_COLS = ["external_case_id", "confirmed_at", "source", "batch_numbers"]


def get_updates_log(token, repo):
    """
    Load updates_log.csv from GitHub.
    Returns (sha_or_None, DataFrame).
    """
    sha, content = _get_file_meta(token, repo, _UPDATES_PATH)
    if content is None:
        return None, pd.DataFrame(columns=_UPDATES_COLS)
    return sha, pd.read_csv(io.StringIO(content))


def push_updates_log(df, token, repo, sha=None):
    """Create or update updates_log.csv. Returns True on success."""
    return _put_file(
        token, repo, _UPDATES_PATH,
        df.to_csv(index=False),
        "Update confirmed-updated log",
        sha=sha,
    )


# ── Validation helpers ────────────────────────────────────────────────────────

def find_repeated_within_window(current_ids, batches, months_threshold, today):
    """
    Return a dict:  external_case_id → {batch_number, confirmed_at, times_confirmed}
    Only cases confirmed within the last `months_threshold` months are included
    (cases older than the window are eligible for re-selection).
    """
    from dateutil.relativedelta import relativedelta

    cutoff = today - relativedelta(months=int(months_threshold))
    result = {}     # id → list of batch entries

    for batch in batches:
        if batch["confirmed_at"] is None:
            continue
        if batch["confirmed_at"] < cutoff:
            continue    # outside window — eligible for re-selection
        col = "External Case ID"
        if col not in batch["df"].columns:
            continue
        for eid in batch["df"][col].dropna().astype(str):
            if eid in current_ids:
                result.setdefault(eid, []).append(batch)

    # Collapse into summary dicts
    summary = {}
    for eid, matched in result.items():
        latest = max(matched, key=lambda b: b["confirmed_at"])
        batch_nums = sorted({b["number"] for b in matched})
        summary[eid] = {
            "batch_numbers":    batch_nums,
            "latest_batch":     latest["number"],
            "latest_confirmed": latest["confirmed_at"],
            "times_confirmed":  len(matched),
        }
    return summary


def find_batch_for_ids(ids, batches):
    """
    For each id in ids, return a dict: id → list of batch numbers it appears in.
    Returns also a set of ids not found in any batch.
    """
    found = {}
    for batch in batches:
        col = "External Case ID"
        if col not in batch["df"].columns:
            continue
        for eid in batch["df"][col].dropna().astype(str):
            if eid in ids:
                found.setdefault(eid, []).append(batch["number"])
    not_found = ids - set(found.keys())
    return found, not_found


# ── Blacklist ─────────────────────────────────────────────────────────────────

_BLACKLIST_PATH = "history_comparator/blacklist.csv"
_BLACKLIST_COLS = ["case_id", "organization_name", "country", "added_at"]


def get_blacklist(token, repo):
    """
    Load blacklist.csv from GitHub.
    Returns (sha_or_None, DataFrame).
    """
    sha, content = _get_file_meta(token, repo, _BLACKLIST_PATH)
    if content is None:
        return None, pd.DataFrame(columns=_BLACKLIST_COLS)
    try:
        return sha, pd.read_csv(io.StringIO(content))
    except Exception:
        return sha, pd.DataFrame(columns=_BLACKLIST_COLS)


def save_blacklist(df, token, repo, sha=None):
    """Create or update blacklist.csv. Returns True on success."""
    return _put_file(
        token, repo, _BLACKLIST_PATH,
        df.to_csv(index=False),
        "Update comparator blacklist",
        sha=sha,
    )


def add_to_blacklist(case_rows, token, repo):
    """
    Add a list of dicts {case_id, organization_name, country} to the blacklist.
    Returns (success, updated_df).
    """
    sha, existing = get_blacklist(token, repo)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_rows = []
    existing_ids = set(existing["case_id"].astype(str)) if not existing.empty else set()
    for row in case_rows:
        if str(row["case_id"]) not in existing_ids:
            new_rows.append({
                "case_id":           row["case_id"],
                "organization_name": row.get("organization_name", ""),
                "country":           row.get("country", ""),
                "added_at":          now,
            })
    if not new_rows:
        return True, existing  # nothing new to add
    updated = pd.concat(
        [existing, pd.DataFrame(new_rows)], ignore_index=True
    )
    ok = save_blacklist(updated, token, repo, sha=sha)
    return ok, updated


def remove_from_blacklist(case_ids_to_remove, token, repo):
    """
    Remove specific case_ids from the blacklist.
    Returns (success, updated_df).
    """
    sha, existing = get_blacklist(token, repo)
    if existing.empty:
        return True, existing
    updated = existing[
        ~existing["case_id"].astype(str).isin(set(str(i) for i in case_ids_to_remove))
    ].reset_index(drop=True)
    ok = save_blacklist(updated, token, repo, sha=sha)
    return ok, updated
