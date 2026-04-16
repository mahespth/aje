#!/usr/bin/env python3
"""
AAP 2.5 curses browser

Steve Maher, AIXtreme Research Ltd.

Features
- Connects to AAP 2.5 through the platform gateway using either:
  - A pre-created token from AAP_TOKEN / config file, or
  - Username/password to create a token at /api/gateway/v1/tokens/
- Lists jobs with paging
- Opens a job and shows stdout with scrolling, top/bottom jumps, and text search
- Loads and browses job events/tasks with status search
- Popup task detail in YAML or JSON
- Saves selected task detail locally
- Bookmarks positions locally and cycles through them
- Persists cache and last-view state locally

Requirements
- Python 3.9+
- requests
- PyYAML
"""

from __future__ import annotations

import curses
import curses.textpad
import getpass
import json
import os
import re
import sys
import textwrap
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

try:
    import requests
except Exception as exc:  # pragma: no cover
    print("This application requires the 'requests' package.", file=sys.stderr)
    raise

try:
    import yaml
except Exception as exc:  # pragma: no cover
    print("This application requires the 'PyYAML' package.", file=sys.stderr)
    raise

APP_NAME = "aje"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP_NAME
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
CACHE_FILE = CACHE_DIR / "cache.json"
STATE_FILE = CACHE_DIR / "state.json"
CONFIG_FILE = CONFIG_DIR / "config.yaml"

DEFAULT_VERIFY_SSL = True
DEFAULT_PAGE_SIZE = 20
DEFAULT_CACHE_TTL = 60 * 15
MAX_EVENTS_PAGE_SIZE = 200

HELP_TEXT = """
AAP 2.5 Curses Browser

Global
  q            Quit / back
  h, ?         Help
  r            Refresh current view

Job List
  Up/Down      Move selection
  PgUp/PgDn    Move page
  Enter        Open selected job
  /            Search visible job list
  n, N         Next/previous search result

Job Output
  Up/Down      Scroll output
  PgUp/PgDn    Scroll faster
  g            Jump to top
  G            Jump to bottom
  /            Search job output text
  n, N         Next/previous text match
  t            Open task/event browser
  b            Bookmark current job location
  j            Jump to next bookmark

Task/Event Browser
  Up/Down      Move selection
  PgUp/PgDn    Move faster
  /            Search event text/status/host/task path
  n, N         Next/previous event match
  Enter        Show YAML popup for event detail
  J            Show JSON popup for event detail
  s            Save selected event detail to local file
  b            Bookmark selected event
  j            Jump to next bookmark

Detail Popup
  q, Esc       Close popup
  j/J          JSON format
  y/Y          YAML format
  s            Save current detail to local file

Configuration
  Reads config from ~/.config/aap-curses-browser/config.yaml
  Example:
    host: https://aap.example.com
    token: your_token_here
    verify_ssl: true
    page_size: 20
    cache_ttl: 900
""".strip()


# --------------------------- Utilities ---------------------------


def ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def now_ts() -> float:
    return time.time()


def deep_get(data: Dict[str, Any], path: str, default: Any = "") -> Any:
    current: Any = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part, default)
        else:
            return default
    return current


def sanitize_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "data"


def wrapped_lines(text: str, width: int) -> List[str]:
    if width <= 1:
        return [text]
    lines: List[str] = []
    for raw_line in text.splitlines() or [""]:
        expanded = raw_line.expandtabs(4)
        wrapped = textwrap.wrap(
            expanded,
            width=width,
            replace_whitespace=False,
            drop_whitespace=False,
            subsequent_indent="",
        )
        lines.extend(wrapped if wrapped else [""])
    return lines or [""]


def dump_data(data: Any, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(data, indent=2, sort_keys=True, default=str)
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)


# --------------------------- Config / State ---------------------------


@dataclass
class AppConfig:
    host: str = ""
    token: str = ""
    username: str = ""
    password: str = ""
    verify_ssl: bool = DEFAULT_VERIFY_SSL
    page_size: int = DEFAULT_PAGE_SIZE
    cache_ttl: int = DEFAULT_CACHE_TTL

    @classmethod
    def load(cls) -> "AppConfig":
        ensure_dirs()
        data: Dict[str, Any] = {}
        if CONFIG_FILE.exists():
            with CONFIG_FILE.open("r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
                if isinstance(loaded, dict):
                    data.update(loaded)

        env_map = {
            "host": os.environ.get("AAP_HOST"),
            "token": os.environ.get("AAP_TOKEN"),
            "username": os.environ.get("AAP_USERNAME"),
            "password": os.environ.get("AAP_PASSWORD"),
            "verify_ssl": os.environ.get("AAP_VERIFY_SSL"),
            "page_size": os.environ.get("AAP_PAGE_SIZE"),
            "cache_ttl": os.environ.get("AAP_CACHE_TTL"),
        }
        for key, value in env_map.items():
            if value not in (None, ""):
                data[key] = value

        cfg = cls(
            host=str(data.get("host", "")).rstrip("/"),
            token=str(data.get("token", "")),
            username=str(data.get("username", "")),
            password=str(data.get("password", "")),
            verify_ssl=str(data.get("verify_ssl", DEFAULT_VERIFY_SSL)).lower() not in {"0", "false", "no"},
            page_size=int(data.get("page_size", DEFAULT_PAGE_SIZE)),
            cache_ttl=int(data.get("cache_ttl", DEFAULT_CACHE_TTL)),
        )
        return cfg


@dataclass
class Bookmark:
    job_id: int
    view: str
    cursor: int = 0
    scroll: int = 0
    note: str = ""
    created_at: float = field(default_factory=now_ts)


@dataclass
class ResumeState:
    screen: str = "jobs"
    jobs_page: int = 1
    jobs_cursor: int = 0
    selected_job_id: Optional[int] = None
    output_scroll: int = 0
    output_search: str = ""
    event_cursor: int = 0
    event_scroll: int = 0
    event_search: str = ""
    detail_format: str = "yaml"
    bookmark_index: int = -1


class LocalStore:
    def __init__(self) -> None:
        ensure_dirs()
        self.cache: Dict[str, Any] = self._load_json(CACHE_FILE, default={
            "jobs_pages": {},
            "jobs": {},
            "job_stdout": {},
            "job_events": {},
            "event_details": {},
        })
        self.state: Dict[str, Any] = self._load_json(STATE_FILE, default={
            "resume": asdict(ResumeState()),
            "bookmarks": [],
        })

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return default

    def save(self) -> None:
        ensure_dirs()
        tmp_cache = CACHE_FILE.with_suffix(".json.tmp")
        tmp_state = STATE_FILE.with_suffix(".json.tmp")
        with tmp_cache.open("w", encoding="utf-8") as fh:
            json.dump(self.cache, fh, indent=2)
        with tmp_state.open("w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)
        tmp_cache.replace(CACHE_FILE)
        tmp_state.replace(STATE_FILE)

    def get_resume(self) -> ResumeState:
        raw = self.state.get("resume", {}) or {}
        return ResumeState(**{**asdict(ResumeState()), **raw})

    def set_resume(self, resume: ResumeState) -> None:
        self.state["resume"] = asdict(resume)

    def get_bookmarks(self) -> List[Bookmark]:
        out: List[Bookmark] = []
        for item in self.state.get("bookmarks", []) or []:
            try:
                out.append(Bookmark(**item))
            except TypeError:
                continue
        return out

    def set_bookmarks(self, bookmarks: List[Bookmark]) -> None:
        self.state["bookmarks"] = [asdict(bm) for bm in bookmarks]

    def cache_get(self, section: str, key: str, ttl: Optional[int] = None) -> Optional[Any]:
        section_map = self.cache.get(section, {})
        payload = section_map.get(str(key))
        if not payload:
            return None
        if ttl is not None and (now_ts() - payload.get("ts", 0)) > ttl:
            return None
        return payload.get("data")

    def cache_set(self, section: str, key: str, data: Any) -> None:
        if section not in self.cache:
            self.cache[section] = {}
        self.cache[section][str(key)] = {"ts": now_ts(), "data": data}


# --------------------------- AAP API Client ---------------------------


class AAPClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.base = config.host.rstrip("/")
        self.session = requests.Session()
        self.session.verify = config.verify_ssl
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if config.token:
            self.set_token(config.token)

    def set_token(self, token: str) -> None:
        self.session.headers["Authorization"] = f"Bearer {token}"

    def ensure_auth(self) -> None:
        if "Authorization" in self.session.headers:
            return
        if self.config.username and self.config.password:
            token = self.create_gateway_token(self.config.username, self.config.password)
            self.set_token(token)
            return

        if self.config.username and not self.config.password:
            self.config.password = getpass.getpass("AAP password: ")
            token = self.create_gateway_token(self.config.username, self.config.password)
            self.set_token(token)
            return

        raise RuntimeError(
            "No authentication configured. Set token in config/AAP_TOKEN, "
            "or provide username/password."
        )

    def create_gateway_token(self, username: str, password: str) -> str:
        url = f"{self.base}/api/gateway/v1/tokens/"
        resp = self.session.post(url, json={"username": username, "password": password}, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"Gateway token request failed: {resp.status_code} {resp.text}")
        data = resp.json()
        token = data.get("token") or data.get("access") or data.get("key")
        if not token:
            raise RuntimeError("Token response did not include a token/access/key field.")
        return token

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.ensure_auth()
        url = f"{self.base}{path}"
        resp = self.session.request(method, url, timeout=45, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"API {method} {path} failed: {resp.status_code} {resp.text[:400]}")
        if resp.text.strip():
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return resp.json()
        return resp.text

    def list_jobs(self, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, search: str = "") -> Dict[str, Any]:
        params = {"page": page, "page_size": page_size, "order_by": "-finished"}
        if search:
            params["search"] = search
        return self._request("GET", "/api/controller/v2/jobs/", params=params)

    def get_job(self, job_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/api/controller/v2/jobs/{job_id}/")

    def get_job_stdout(self, job_id: int) -> str:
        try:
            text = self._request("GET", f"/api/controller/v2/jobs/{job_id}/stdout/", params={"format": "txt_download"})
            if isinstance(text, str):
                return text
        except Exception:
            pass

        data = self._request("GET", f"/api/controller/v2/jobs/{job_id}/stdout/", params={"format": "json"})
        if isinstance(data, dict):
            return data.get("stdout", "") or json.dumps(data, indent=2)
        return str(data)

    def list_job_events(self, job_id: int, page: int = 1, page_size: int = MAX_EVENTS_PAGE_SIZE) -> Dict[str, Any]:
        params = {"page": page, "page_size": page_size, "order_by": "start_line"}
        return self._request("GET", f"/api/controller/v2/jobs/{job_id}/job_events/", params=params)

    def get_job_event_detail(self, event_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/api/controller/v2/job_events/{event_id}/")

    def list_all_job_events(self, job_id: int) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        page = 1
        while True:
            data = self.list_job_events(job_id, page=page)
            page_results = data.get("results", []) or []
            results.extend(page_results)
            next_url = data.get("next")
            if not next_url:
                break
            page += 1
        return results


# --------------------------- Curses UI ---------------------------


class PopupResult:
    def __init__(self, closed: bool = True) -> None:
        self.closed = closed


class CursesApp:
    def __init__(self, stdscr: Any, client: AAPClient, store: LocalStore, config: AppConfig) -> None:
        self.stdscr = stdscr
        self.client = client
        self.store = store
        self.config = config

        self.resume = store.get_resume()
        self.bookmarks = store.get_bookmarks()

        self.jobs_page_data: Dict[str, Any] = {"results": [], "count": 0, "next": None, "previous": None}
        self.jobs_search_term: str = ""
        self.jobs_search_matches: List[int] = []

        self.current_job: Optional[Dict[str, Any]] = None
        self.current_stdout: str = ""
        self.current_stdout_lines: List[str] = []
        self.output_search_matches: List[int] = []

        self.current_events: List[Dict[str, Any]] = []
        self.event_search_matches: List[int] = []

        self.status_message: str = ""
        self.status_ts: float = 0.0
