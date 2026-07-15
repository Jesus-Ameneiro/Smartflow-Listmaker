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


# ── Comparator Updates Log ────────────────────────────────────────────────────

_COMP_UPDATES_PATH = "history_comparator/updates_log.csv"
_COMP_UPDATES_COLS = ["case_id", "organization_name", "country",
                      "batch_number", "confirmed_at"]


def get_comp_updates_log(token, repo):
    """Load comparator updates log. Returns (sha_or_None, DataFrame)."""
    sha, content = _get_file_meta(token, repo, _COMP_UPDATES_PATH)
    if content is None:
        return None, pd.DataFrame(columns=_COMP_UPDATES_COLS)
    try:
        return sha, pd.read_csv(io.StringIO(content))
    except Exception:
        return None, pd.DataFrame(columns=_COMP_UPDATES_COLS)


def push_comp_updates_log(df, token, repo, sha=None):
    """Create or update comparator updates log. Returns True on success."""
    return _put_file(
        token, repo, _COMP_UPDATES_PATH,
        df.to_csv(index=False),
        "Update comparator updates log",
        sha=sha,
    )


def confirm_cases_updated(case_rows, token, repo):
    """
    Mark cases as confirmed-updated in the comparator updates log.
    case_rows: list of dicts with case_id, organization_name, country, batch_number.
    Returns (success, updated_df).
    """
    sha, existing = get_comp_updates_log(token, repo)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing_ids = set(existing["case_id"].astype(str)) if not existing.empty else set()

    new_rows = [
        {
            "case_id":           r["case_id"],
            "organization_name": r.get("organization_name", ""),
            "country":           r.get("country", ""),
            "batch_number":      r.get("batch_number", ""),
            "confirmed_at":      now,
        }
        for r in case_rows
        if str(r["case_id"]) not in existing_ids
    ]
    if not new_rows:
        return True, existing
    updated = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True)
    ok = push_comp_updates_log(updated, token, repo, sha=sha)
    return ok, updated


# ── Update History (history_updater/) ────────────────────────────────────────

_UPDATER_FOLDER = "history_updater"


def get_update_history(token, repo, folder=_UPDATER_FOLDER):
    """
    Fetch all confirmed update history files from the GitHub repo.
    Returns list of dicts: {number, name, path, sha, confirmed_at, df}
    """
    url = f"https://api.github.com/repos/{repo}/contents/{folder}"
    resp = requests.get(url, headers=_headers(token), timeout=15)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()

    entries = [
        e for e in resp.json()
        if e["name"].startswith("update_") and e["name"].endswith(".csv")
    ]
    entries.sort(key=lambda e: e["name"])

    records = []
    for i, entry in enumerate(entries, start=1):
        raw = requests.get(entry["download_url"], timeout=15)
        raw.raise_for_status()
        df = pd.read_csv(io.StringIO(raw.text))
        records.append({
            "number":       i,
            "name":         entry["name"],
            "path":         entry["path"],
            "sha":          entry["sha"],
            "confirmed_at": _parse_ts(entry["name"].replace("update_", "batch_")),
            "df":           df,
        })
    return records


def push_update_history(output_df, token, repo, folder=_UPDATER_FOLDER):
    """Push a confirmed update file to GitHub. Returns (success, filename)."""
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"update_{ts}.csv"
    path     = f"{folder}/{filename}"
    ok = _put_file(
        token, repo, path,
        output_df.to_csv(index=False),
        f"Add update history {ts}",
    )
    return ok, filename


def delete_update_history(token, repo, path, sha):
    """Delete an update history file. Returns True on success."""
    url     = f"https://api.github.com/repos/{repo}/contents/{path}"
    payload = {"message": f"Delete {path}", "sha": sha}
    resp    = requests.delete(url, json=payload, headers=_headers(token), timeout=15)
    return resp.status_code == 200


def get_all_confirmed_update_ids(history_records):
    """Return set of all External Case IDs confirmed across all history records."""
    ids = set()
    for rec in history_records:
        col = next(
            (c for c in ["External Case ID", "Case ID", "case_id"]
             if c in rec["df"].columns),
            None,
        )
        if col:
            ids |= set(rec["df"][col].dropna().astype(str))
    return ids


# ── Comparator Draft ──────────────────────────────────────────────────────────

_DRAFT_PATH = "drafts_comparator/draft_latest.csv"


def save_draft(df, token, repo):
    """
    Overwrite the comparator draft with the current output DataFrame.
    Returns True on success.
    """
    sha, _ = _get_file_meta(token, repo, _DRAFT_PATH)
    return _put_file(
        token, repo, _DRAFT_PATH,
        df.to_csv(index=False),
        "Save comparator draft",
        sha=sha,
    )


def load_draft(token, repo):
    """
    Load the comparator draft if it exists.
    Returns (sha, DataFrame) or (None, None).
    """
    sha, content = _get_file_meta(token, repo, _DRAFT_PATH)
    if content is None:
        return None, None
    try:
        return sha, pd.read_csv(io.StringIO(content))
    except Exception:
        return None, None


def delete_draft(token, repo):
    """
    Delete the comparator draft after confirmation.
    Returns True on success.
    """
    sha, content = _get_file_meta(token, repo, _DRAFT_PATH)
    if content is None:
        return True   # already gone
    url     = f"https://api.github.com/repos/{repo}/contents/{_DRAFT_PATH}"
    payload = {"message": "Delete comparator draft after confirmation", "sha": sha}
    resp    = requests.delete(url, json=payload, headers=_headers(token), timeout=15)
    return resp.status_code == 200


# ── Tag Preferences ───────────────────────────────────────────────────────────

_TAG_PREFS_PATH = "history_comparator/tag_preferences.json"


def load_tag_preferences(token, repo):
    """
    Load saved tag preferences. Returns dict with 'include' and 'exclude' lists,
    or empty defaults if not found.
    """
    import json as _json
    sha, content = _get_file_meta(token, repo, _TAG_PREFS_PATH)
    if content is None:
        return {"include": [], "exclude": []}
    try:
        return _json.loads(content)
    except Exception:
        return {"include": [], "exclude": []}


def save_tag_preferences(include_tags, exclude_tags, token, repo):
    """Save current tag selections as default preferences. Returns True on success."""
    import json as _json
    sha, _ = _get_file_meta(token, repo, _TAG_PREFS_PATH)
    payload = _json.dumps({"include": list(include_tags), "exclude": list(exclude_tags)})
    return _put_file(
        token, repo, _TAG_PREFS_PATH,
        payload,
        "Save tag preferences",
        sha=sha,
    )


# ── Agent PIN store ───────────────────────────────────────────────────────────

_PINS_PATH = "pins/agent_pins.json"


def load_pins(token, repo):
    """
    Load agent PIN hashes from GitHub.
    Returns dict: {email: sha256_hex} or {} if not found.
    """
    import json as _json
    sha, content = _get_file_meta(token, repo, _PINS_PATH)
    if content is None:
        return {}
    try:
        return _json.loads(content)
    except Exception:
        return {}


def save_pin(email, pin_hash, token, repo):
    """
    Save or update a PIN hash for an agent. Returns True on success.
    """
    import json as _json
    sha, content = _get_file_meta(token, repo, _PINS_PATH)
    try:
        pins = _json.loads(content) if content else {}
    except Exception:
        pins = {}
    pins[email] = pin_hash
    return _put_file(
        token, repo, _PINS_PATH,
        _json.dumps(pins, indent=2),
        f"Update PIN for {email}",
        sha=sha,
    )
