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


        self.popup_active = False

    # ---------- State / Persistence ----------

    def persist(self) -> None:
        self.store.set_resume(self.resume)
        self.store.set_bookmarks(self.bookmarks)
        self.store.save()

    def set_status(self, message: str) -> None:
        self.status_message = message
        self.status_ts = now_ts()

    # ---------- Loading Data ----------

    def load_jobs_page(self, use_cache: bool = True, force: bool = False) -> None:
        page_key = f"{self.resume.jobs_page}:{self.config.page_size}:{self.jobs_search_term}"
        data = None if force else self.store.cache_get("jobs_pages", page_key, ttl=self.config.cache_ttl if use_cache else 0)
        if data is None:
            data = self.client.list_jobs(page=self.resume.jobs_page, page_size=self.config.page_size, search=self.jobs_search_term)
            self.store.cache_set("jobs_pages", page_key, data)
            for job in data.get("results", []) or []:
                jid = job.get("id")
                if jid is not None:
                    self.store.cache_set("jobs", str(jid), job)
        self.jobs_page_data = data
        results = self.jobs_page_data.get("results", []) or []
        if not results:
            self.resume.jobs_cursor = 0
        else:
            self.resume.jobs_cursor = max(0, min(self.resume.jobs_cursor, len(results) - 1))
        self.build_job_list_search()

    def load_job(self, job_id: int, use_cache: bool = True, force: bool = False) -> None:
        data = None if force else self.store.cache_get("jobs", str(job_id), ttl=self.config.cache_ttl if use_cache else 0)
        if data is None:
            data = self.client.get_job(job_id)
            self.store.cache_set("jobs", str(job_id), data)
        self.current_job = data
        self.resume.selected_job_id = job_id
        self.persist()

    def load_stdout(self, use_cache: bool = True, force: bool = False) -> None:
        if self.resume.selected_job_id is None:
            return
        key = str(self.resume.selected_job_id)
        data = None if force else self.store.cache_get("job_stdout", key, ttl=self.config.cache_ttl if use_cache else 0)
        if data is None:
            data = self.client.get_job_stdout(self.resume.selected_job_id)
            self.store.cache_set("job_stdout", key, data)
        self.current_stdout = data or ""
        if self.resume.output_search and self.resume.screen != "job":
            self.resume.output_search = ""
        self._rebuild_stdout_lines()
        self.persist()

    def load_events(self, use_cache: bool = True, force: bool = False) -> None:
        if self.resume.selected_job_id is None:
            return
        key = str(self.resume.selected_job_id)
        data = None if force else self.store.cache_get("job_events", key, ttl=self.config.cache_ttl if use_cache else 0)
        if data is None:
            data = self.client.list_all_job_events(self.resume.selected_job_id)
            self.store.cache_set("job_events", key, data)
        self.current_events = data or []
        if self.current_events:
            self.resume.event_cursor = max(0, min(self.resume.event_cursor, len(self.current_events) - 1))
        else:
            self.resume.event_cursor = 0
        self.build_event_search()
        self.persist()

    def get_event_detail(self, event_id: int, use_cache: bool = True, force: bool = False) -> Dict[str, Any]:
        key = str(event_id)
        data = None if force else self.store.cache_get("event_details", key, ttl=self.config.cache_ttl if use_cache else 0)
        if data is None:
            data = self.client.get_job_event_detail(event_id)
            self.store.cache_set("event_details", key, data)
            self.persist()
        return data

    # ---------- Search ----------

    def build_job_list_search(self) -> None:
        term = self.jobs_search_term.strip().lower()
        self.jobs_search_matches = []
        if not term:
            return
        for idx, job in enumerate(self.jobs_page_data.get("results", []) or []):
            blob = " ".join([
                str(job.get("id", "")),
                str(job.get("name", "")),
                str(job.get("status", "")),
                str(job.get("job_template", "")),
                str(job.get("finished", "")),
            ]).lower()
            if term in blob:
                self.jobs_search_matches.append(idx)

    def build_output_search(self) -> None:
        term = self.resume.output_search.strip().lower()
        self.output_search_matches = []
        if not term:
            return
        for idx, line in enumerate(self.current_stdout_lines):
            if term in line.lower():
                self.output_search_matches.append(idx)

    def build_event_search(self) -> None:
        term = self.resume.event_search.strip().lower()
        self.event_search_matches = []
        if not term:
            return
        for idx, event in enumerate(self.current_events):
            blob = " ".join([
                str(event.get("event", "")),
                str(event.get("event_display", "")),
                str(event.get("failed", "")),
                str(event.get("changed", "")),
                str(event.get("host_name", "")),
                str(event.get("play", "")),
                str(event.get("task", "")),
                str(event.get("role", "")),
                str(event.get("stdout", "")),
                str(event.get("start_line", "")),
                str(event.get("end_line", "")),
            ]).lower()
            if term in blob:
                self.event_search_matches.append(idx)
        if self.event_search_matches:
            self.resume.event_cursor = self.event_search_matches[0]
            self.ensure_event_cursor_visible()

    # ---------- Helpers ----------

    def _rebuild_stdout_lines(self) -> None:
        h, w = self.stdscr.getmaxyx()
        usable_width = max(20, w - 2)
        self.current_stdout_lines = wrapped_lines(self.current_stdout, usable_width)
        self.build_output_search()
        max_scroll = max(0, len(self.current_stdout_lines) - max(1, h - 2))
        self.resume.output_scroll = max(0, min(self.resume.output_scroll, max_scroll))

    def selected_job_from_list(self) -> Optional[Dict[str, Any]]:
        results = self.jobs_page_data.get("results", []) or []
        if not results:
            return None
        if self.resume.jobs_cursor < 0 or self.resume.jobs_cursor >= len(results):
            return None
        return results[self.resume.jobs_cursor]

    def selected_event(self) -> Optional[Dict[str, Any]]:
        if not self.current_events:
            return None
        idx = self.resume.event_cursor
        if idx < 0 or idx >= len(self.current_events):
            return None
        return self.current_events[idx]

    def ensure_event_cursor_visible(self) -> None:
        h, _ = self.stdscr.getmaxyx()
        view_h = max(3, h - 4)
        if self.resume.event_cursor < self.resume.event_scroll:
            self.resume.event_scroll = self.resume.event_cursor
        elif self.resume.event_cursor >= self.resume.event_scroll + view_h:
            self.resume.event_scroll = self.resume.event_cursor - view_h + 1

    def goto_next_output_match(self, reverse: bool = False) -> None:
        if not self.output_search_matches:
            self.set_status("No output search matches")
            return
        cur = self.resume.output_scroll
        matches = list(reversed(self.output_search_matches)) if reverse else self.output_search_matches
        for line_idx in matches:
            if reverse:
                if line_idx < cur:
                    self.resume.output_scroll = line_idx
                    return
            else:
                if line_idx > cur:
                    self.resume.output_scroll = line_idx
                    return
        self.resume.output_scroll = matches[0]
        self.set_status("Wrapped search")

    def goto_next_job_match(self, reverse: bool = False) -> None:
        if not self.jobs_search_matches:
            self.set_status("No visible job matches")
            return
        cur = self.resume.jobs_cursor
        matches = list(reversed(self.jobs_search_matches)) if reverse else self.jobs_search_matches
        for idx in matches:
            if reverse:
                if idx < cur:
                    self.resume.jobs_cursor = idx
                    return
            else:
                if idx > cur:
                    self.resume.jobs_cursor = idx
                    return
        self.resume.jobs_cursor = matches[0]
        self.set_status("Wrapped search")

    def goto_next_event_match(self, reverse: bool = False) -> None:
        if not self.event_search_matches:
            self.set_status("No event matches")
            return
        cur = self.resume.event_cursor
        matches = list(reversed(self.event_search_matches)) if reverse else self.event_search_matches
        for idx in matches:
            if reverse:
                if idx < cur:
                    self.resume.event_cursor = idx
                    self.ensure_event_cursor_visible()
                    return
            else:
                if idx > cur:
                    self.resume.event_cursor = idx
                    self.ensure_event_cursor_visible()
                    return
        self.resume.event_cursor = matches[0]
        self.ensure_event_cursor_visible()
        self.set_status("Wrapped search")

    def add_bookmark(self) -> None:
        if self.resume.selected_job_id is None:
            self.set_status("No active job to bookmark")
            return
        note = ""
        if self.resume.screen == "job":
            note = f"job:{self.resume.selected_job_id}:scroll:{self.resume.output_scroll}"
            bm = Bookmark(
                job_id=self.resume.selected_job_id,
                view="job",
                cursor=0,
                scroll=self.resume.output_scroll,
                note=note,
            )
        elif self.resume.screen == "events":
            event = self.selected_event()
            note = f"job:{self.resume.selected_job_id}:event:{event.get('id') if event else 'na'}"
            bm = Bookmark(
                job_id=self.resume.selected_job_id,
                view="events",
                cursor=self.resume.event_cursor,
                scroll=self.resume.event_scroll,
                note=note,
            )
        else:
            self.set_status("Bookmarking is only available in job output or events view")
            return

        self.bookmarks.append(bm)
        self.resume.bookmark_index = len(self.bookmarks) - 1
        self.persist()
        self.set_status(f"Bookmarked {note}")

    def jump_bookmark(self) -> None:
        if not self.bookmarks:
            self.set_status("No bookmarks saved")
            return
        next_idx = (self.resume.bookmark_index + 1) % len(self.bookmarks)
        bm = self.bookmarks[next_idx]
        self.resume.bookmark_index = next_idx
        self.resume.selected_job_id = bm.job_id
        self.load_job(bm.job_id)
        self.load_stdout()
        if bm.view == "events":
            self.load_events()
            self.resume.screen = "events"
            self.resume.event_cursor = max(0, min(bm.cursor, max(0, len(self.current_events) - 1)))
            self.resume.event_scroll = max(0, bm.scroll)
            self.ensure_event_cursor_visible()
        else:
            self.resume.screen = "job"
            self.resume.output_scroll = max(0, bm.scroll)
        self.persist()
        self.set_status(f"Jumped to bookmark {next_idx + 1}/{len(self.bookmarks)}")

    def save_current_detail_to_file(self, detail: Dict[str, Any], fmt: str) -> None:
        event_id = detail.get("id", "event")
        job_id = self.resume.selected_job_id or "job"
        filename = sanitize_filename(f"job_{job_id}_event_{event_id}.{fmt}")
        path = Path.cwd() / filename
        content = dump_data(detail, fmt)
        with path.open("w", encoding="utf-8") as fh:
            fh.write(content)
        self.set_status(f"Saved {path}")


    # ---------- Input helpers ----------

    def prompt_input(self, prompt: str, initial: str = "") -> str:
        h, w = self.stdscr.getmaxyx()
        width = min(max(40, len(prompt) + 10), max(20, w - 4))
        win = curses.newwin(3, width, max(0, h // 2 - 1), max(0, (w - width) // 2))
        win.box()
        try:
            win.addstr(0, 2, " Input ")
            display_prompt = prompt[: max(1, width - 4)]
            win.addstr(1, 1, display_prompt[: width - 2])
        except curses.error:
            pass
        edit_x = min(width - 2, len(display_prompt) + 2)
        edit_w = max(5, width - edit_x - 1)
        edit = curses.newwin(1, edit_w, max(0, h // 2), max(0, (w - width) // 2 + edit_x))
        edit.addstr(0, 0, initial[: max(0, edit_w - 1)])
        curses.curs_set(1)
        box = curses.textpad.Textbox(edit)
        try:
            value = box.edit().strip()
        finally:
            curses.curs_set(0)
        return value

    def show_popup_text(self, title: str, text: str, allow_format_toggle: bool = False, detail: Optional[Dict[str, Any]] = None) -> PopupResult:
        self.popup_active = True
        scroll = 0
        fmt = self.resume.detail_format
        while True:
            h, w = self.stdscr.getmaxyx()
            ph = max(10, min(h - 4, int(h * 0.75)))
            pw = max(40, min(w - 4, int(w * 0.8)))
            py = max(0, (h - ph) // 2)
            px = max(0, (w - pw) // 2)
            win = curses.newwin(ph, pw, py, px)
            win.keypad(True)
            win.box()
            title_text = f" {title} "
            try:
                win.addstr(0, 2, title_text[: max(1, pw - 4)], curses.A_BOLD)
            except curses.error:
                pass

            if allow_format_toggle and detail is not None:
                text = dump_data(detail, fmt)
                self.resume.detail_format = fmt

            lines = wrapped_lines(text, max(10, pw - 2))
            body_h = ph - 3
            max_scroll = max(0, len(lines) - body_h)
            scroll = max(0, min(scroll, max_scroll))

            for row in range(body_h):
                idx = scroll + row
                if idx >= len(lines):
                    break
                line = lines[idx]
                try:
                    win.addstr(1 + row, 1, line[: pw - 2])
                except curses.error:
                    pass

            footer = "q/Esc close  Up/Down scroll  PgUp/PgDn fast"
            if allow_format_toggle and detail is not None:
                footer += "  y yaml  j json  s save"
            try:
                win.addstr(ph - 1, 2, footer[: max(1, pw - 4)])
            except curses.error:
                pass

            win.refresh()
            ch = win.getch()

            if ch in (ord("q"), 27):
                self.popup_active = False
                self.persist()
                return PopupResult(closed=True)
            if ch == curses.KEY_UP:
                scroll = max(0, scroll - 1)
            elif ch == curses.KEY_DOWN:
                scroll = min(max_scroll, scroll + 1)
            elif ch == curses.KEY_PPAGE:
                scroll = max(0, scroll - max(1, body_h - 1))
            elif ch == curses.KEY_NPAGE:
                scroll = min(max_scroll, scroll + max(1, body_h - 1))
            elif allow_format_toggle and detail is not None and ch in (ord("j"), ord("J")):
                fmt = "json"
            elif allow_format_toggle and detail is not None and ch in (ord("y"), ord("Y")):
                fmt = "yaml"
            elif allow_format_toggle and detail is not None and ch == ord("s"):
                self.save_current_detail_to_file(detail, fmt)

    # ---------- Drawing ----------

    def draw_status_bar(self) -> None:
        h, w = self.stdscr.getmaxyx()
        status = self.status_message if now_ts() - self.status_ts < 8 else ""
        left = f" {APP_NAME} | {self.resume.screen} "
        right = status
        try:
            self.stdscr.attron(curses.A_REVERSE)
            self.stdscr.addstr(h - 1, 0, " " * max(0, w - 1))
            self.stdscr.addstr(h - 1, 0, left[: max(0, w - 1)])
            if right:
                start = max(0, w - len(right) - 1)
                self.stdscr.addstr(h - 1, start, right[: max(0, w - start - 1)])
            self.stdscr.attroff(curses.A_REVERSE)
        except curses.error:
            pass

    def draw_jobs(self) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        results = self.jobs_page_data.get("results", []) or []
        title = f" Jobs page {self.resume.jobs_page}  total={self.jobs_page_data.get('count', 0)} "
        try:
            self.stdscr.addstr(0, 0, title[: max(1, w - 1)], curses.A_BOLD)
        except curses.error:
            pass

        rows = max(1, h - 2)
        for i in range(min(len(results), rows - 1)):
            job = results[i]
            line = (
                f"{'>' if i == self.resume.jobs_cursor else ' '} "
                f"{str(job.get('id', '')):<7} "
                f"{str(job.get('status', '')):<12} "
                f"{str(job.get('name', '') or job.get('job_template', '')):<36.36} "
                f"{str(job.get('finished', '') or ''):<22}"
            )
            attr = curses.A_REVERSE if i == self.resume.jobs_cursor else curses.A_NORMAL
            try:
                self.stdscr.addstr(1 + i, 0, line[: max(1, w - 1)], attr)
            except curses.error:
                pass

        footer = "Enter open  PgUp/PgDn page  / search  n/N next/prev  h help  q quit"
        try:
            self.stdscr.addstr(h - 2, 0, footer[: max(1, w - 1)])
        except curses.error:
            pass
        self.draw_status_bar()
        self.stdscr.refresh()

    def draw_job_output(self) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        job_id = self.resume.selected_job_id or "?"
        job_name = (self.current_job or {}).get("name") or (self.current_job or {}).get("job_template") or ""
        title = f" Job {job_id} {job_name} "
        try:
            self.stdscr.addstr(0, 0, title[: max(1, w - 1)], curses.A_BOLD)
        except curses.error:
            pass

        body_h = max(1, h - 2)
        max_scroll = max(0, len(self.current_stdout_lines) - body_h)
        self.resume.output_scroll = max(0, min(self.resume.output_scroll, max_scroll))

        for row in range(body_h - 1):
            idx = self.resume.output_scroll + row
            if idx >= len(self.current_stdout_lines):
                break
            line = self.current_stdout_lines[idx]
            attr = curses.A_NORMAL
            term = self.resume.output_search.strip().lower()
            if term and term in line.lower():
                attr = curses.A_BOLD
            try:
                self.stdscr.addstr(1 + row, 0, line[: max(1, w - 1)], attr)
            except curses.error:
                pass

        footer = "q back  g/G top/bottom  / search  n/N next/prev  t tasks  b bookmark  j jump"
        try:
            self.stdscr.addstr(h - 2, 0, footer[: max(1, w - 1)])
        except curses.error:
            pass
        self.draw_status_bar()
        self.stdscr.refresh()

    def draw_events(self) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        job_id = self.resume.selected_job_id or "?"
        title = f" Job {job_id} Events ({len(self.current_events)}) "
        try:
            self.stdscr.addstr(0, 0, title[: max(1, w - 1)], curses.A_BOLD)
        except curses.error:
            pass

        body_h = max(1, h - 2)
        self.ensure_event_cursor_visible()
        for row in range(body_h - 1):
            idx = self.resume.event_scroll + row
            if idx >= len(self.current_events):
                break
            event = self.current_events[idx]
            marker = ">" if idx == self.resume.event_cursor else " "
            line = (
                f"{marker} "
                f"{str(event.get('id', '')):<7} "
                f"{str(event.get('event_display', event.get('event', ''))):<18.18} "
                f"{str(event.get('host_name', '')):<18.18} "
                f"{str(event.get('task', '')):<34.34} "
                f"{str(event.get('start_line', '')):>6}"
            )
            attr = curses.A_REVERSE if idx == self.resume.event_cursor else curses.A_NORMAL
            try:
                self.stdscr.addstr(1 + row, 0, line[: max(1, w - 1)], attr)
            except curses.error:
                pass

        footer = "q back  Enter YAML  J JSON  / search  n/N  s save  b bookmark  j jump"
        try:
            self.stdscr.addstr(h - 2, 0, footer[: max(1, w - 1)])
        except curses.error:
            pass
        self.draw_status_bar()
        self.stdscr.refresh()

    # ---------- Actions ----------

    def open_selected_job(self) -> None:
        job = self.selected_job_from_list()
        if not job:
            self.set_status("No job selected")
            return
        job_id = int(job["id"])
        self.load_job(job_id)
        self.load_stdout()
        self.resume.screen = "job"
        self.persist()

    def open_selected_event_detail(self, fmt: str = "yaml") -> None:
        event = self.selected_event()
        if not event:
            self.set_status("No event selected")
            return
        event_id = int(event["id"])
        detail = self.get_event_detail(event_id)
        self.resume.detail_format = fmt
        text = dump_data(detail, fmt)
        self.show_popup_text(f" Event {event_id} detail ", text, allow_format_toggle=True, detail=detail)

    def refresh_current_view(self) -> None:
        if self.resume.screen == "jobs":
            self.load_jobs_page(force=True)
            self.set_status("Refreshed jobs")
        elif self.resume.screen == "job":
            if self.resume.selected_job_id is not None:
                self.load_job(self.resume.selected_job_id, force=True)
                self.load_stdout(force=True)
                self.set_status("Refreshed job output")
        elif self.resume.screen == "events":
            if self.resume.selected_job_id is not None:
                self.load_events(force=True)
                self.set_status("Refreshed job events")
        self.persist()

    # ---------- Event loop ----------

    def bootstrap(self) -> None:
        self.load_jobs_page()
        if self.resume.selected_job_id is not None and self.resume.screen in {"job", "events"}:
            try:
                self.load_job(self.resume.selected_job_id)
                self.load_stdout()
                if self.resume.screen == "events":
                    self.load_events()
            except Exception as exc:
                self.set_status(str(exc))
                self.resume.screen = "jobs"
        self.persist()

    def run(self) -> None:
        self.bootstrap()
        while True:
            self._rebuild_stdout_lines_if_needed()
            if self.resume.screen == "jobs":
                self.draw_jobs()
                ch = self.stdscr.getch()
                if self.handle_jobs_input(ch):
                    break
            elif self.resume.screen == "job":
                self.draw_job_output()
                ch = self.stdscr.getch()
                if self.handle_job_output_input(ch):
                    break
            elif self.resume.screen == "events":
                self.draw_events()
                ch = self.stdscr.getch()
                if self.handle_events_input(ch):
                    break
            else:
                self.resume.screen = "jobs"

    def _rebuild_stdout_lines_if_needed(self) -> None:
        if self.resume.screen == "job":
            self._rebuild_stdout_lines()

    def handle_common_keys(self, ch: int) -> bool:
        if ch in (ord("h"), ord("?")):
            self.show_popup_text(" Help ", HELP_TEXT)
            return False
        if ch == ord("r"):
            try:
                self.refresh_current_view()
            except Exception as exc:
                self.set_status(str(exc))
            return False
        return False

    def handle_jobs_input(self, ch: int) -> bool:
        if self.handle_common_keys(ch):
            return True
        results = self.jobs_page_data.get("results", []) or []
        if ch == ord("q"):
            return True
        if ch == curses.KEY_UP:
            self.resume.jobs_cursor = max(0, self.resume.jobs_cursor - 1)
        elif ch == curses.KEY_DOWN:
            self.resume.jobs_cursor = min(max(0, len(results) - 1), self.resume.jobs_cursor + 1)
        elif ch == curses.KEY_PPAGE:
            self.resume.jobs_page = max(1, self.resume.jobs_page - 1)
            self.resume.jobs_cursor = 0
            try:
                self.load_jobs_page()
            except Exception as exc:
                self.set_status(str(exc))
        elif ch == curses.KEY_NPAGE:
            next_url = self.jobs_page_data.get("next")
            if next_url or results:
                self.resume.jobs_page += 1
                self.resume.jobs_cursor = 0
                try:
                    self.load_jobs_page()
                except Exception as exc:
                    self.resume.jobs_page = max(1, self.resume.jobs_page - 1)
                    self.set_status(str(exc))
        elif ch in (10, 13, curses.KEY_ENTER):
            try:
                self.open_selected_job()
            except Exception as exc:
                self.set_status(str(exc))
        elif ch == ord("/"):
            term = self.prompt_input("Job search: ", self.jobs_search_term)
            self.jobs_search_term = term
            self.resume.jobs_page = 1
            self.resume.jobs_cursor = 0
            try:
                self.load_jobs_page(force=True)
            except Exception as exc:
                self.set_status(str(exc))
        elif ch == ord("n"):
            self.goto_next_job_match(reverse=False)
        elif ch == ord("N"):
            self.goto_next_job_match(reverse=True)

        self.persist()
        return False

    def handle_job_output_input(self, ch: int) -> bool:
        if self.handle_common_keys(ch):
            return True
        h, _ = self.stdscr.getmaxyx()
        stride = max(1, h - 3)
        if ch == ord("q"):
            self.resume.screen = "jobs"
            return False
        if ch == curses.KEY_UP:
            self.resume.output_scroll = max(0, self.resume.output_scroll - 1)
        elif ch == curses.KEY_DOWN:
            self.resume.output_scroll = min(max(0, len(self.current_stdout_lines) - stride), self.resume.output_scroll + 1)
        elif ch == curses.KEY_PPAGE:
            self.resume.output_scroll = max(0, self.resume.output_scroll - stride)
        elif ch == curses.KEY_NPAGE:
            self.resume.output_scroll = min(max(0, len(self.current_stdout_lines) - stride), self.resume.output_scroll + stride)
        elif ch == ord("g"):
            self.resume.output_scroll = 0
        elif ch == ord("G"):
            self.resume.output_scroll = max(0, len(self.current_stdout_lines) - stride)
        elif ch == ord("/"):
            term = self.prompt_input("Output search: ", self.resume.output_search)
            self.resume.output_search = term
            self.build_output_search()
            if self.output_search_matches:
                self.resume.output_scroll = self.output_search_matches[0]
            else:
                self.set_status("No output matches")
        elif ch == ord("n"):
            self.goto_next_output_match(reverse=False)
        elif ch == ord("N"):
            self.goto_next_output_match(reverse=True)
        elif ch == ord("t"):
            try:
                self.load_events()
                self.resume.screen = "events"
            except Exception as exc:
                self.set_status(str(exc))
        elif ch == ord("b"):
            self.add_bookmark()
        elif ch == ord("j"):
            try:
                self.jump_bookmark()
            except Exception as exc:
                self.set_status(str(exc))
        self.persist()
        return False

    def handle_events_input(self, ch: int) -> bool:
        if self.handle_common_keys(ch):
            return True
        h, _ = self.stdscr.getmaxyx()
        stride = max(1, h - 3)
        if ch == ord("q"):
            self.resume.screen = "job"
            return False
        if ch == curses.KEY_UP:
            self.resume.event_cursor = max(0, self.resume.event_cursor - 1)
            self.ensure_event_cursor_visible()
        elif ch == curses.KEY_DOWN:
            self.resume.event_cursor = min(max(0, len(self.current_events) - 1), self.resume.event_cursor + 1)
            self.ensure_event_cursor_visible()
        elif ch == curses.KEY_PPAGE:
            self.resume.event_cursor = max(0, self.resume.event_cursor - stride)
            self.ensure_event_cursor_visible()
        elif ch == curses.KEY_NPAGE:
            self.resume.event_cursor = min(max(0, len(self.current_events) - 1), self.resume.event_cursor + stride)
            self.ensure_event_cursor_visible()
        elif ch == ord("/"):
            term = self.prompt_input("Event search: ", self.resume.event_search)
            self.resume.event_search = term
            self.build_event_search()
            if not self.event_search_matches:
                self.set_status("No event matches")
        elif ch == ord("n"):
            self.goto_next_event_match(reverse=False)
        elif ch == ord("N"):
            self.goto_next_event_match(reverse=True)
        elif ch in (10, 13, curses.KEY_ENTER):
            try:
                self.open_selected_event_detail(fmt="yaml")
            except Exception as exc:
                self.set_status(str(exc))
        elif ch == ord("J"):
            try:
                self.open_selected_event_detail(fmt="json")
            except Exception as exc:
                self.set_status(str(exc))
        elif ch == ord("s"):
            event = self.selected_event()
            if event:
                try:
                    detail = self.get_event_detail(int(event["id"]))
                    self.save_current_detail_to_file(detail, self.resume.detail_format)
                except Exception as exc:
                    self.set_status(str(exc))
        elif ch == ord("b"):
            self.add_bookmark()
        elif ch == ord("j"):
            try:
                self.jump_bookmark()
            except Exception as exc:
                self.set_status(str(exc))
        self.persist()
        return False

# --------------------------- Main ---------------------------


def init_curses(stdscr: Any) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.use_default_colors()


def app_main(stdscr: Any) -> None:
    init_curses(stdscr)
    cfg = AppConfig.load()
    if not cfg.host:
        raise RuntimeError(
            f"No host configured. Set AAP_HOST or write {CONFIG_FILE} with host/token."
        )
    store = LocalStore()
    client = AAPClient(cfg)
    app = CursesApp(stdscr, client, store, cfg)
    app.run()


def main() -> int:
    try:
        curses.wrapper(app_main)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
  
