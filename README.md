# AAP 2.5 Curses Browser

A Python 3 curses app for browsing Ansible Automation Platform 2.5 jobs through the platform gateway.

Steve Maher, AIXtreme Research Ltd


## What it does

- Connects to AAP through the platform gateway
- Lists jobs with API paging
- Opens job output and lets you scroll/search/jump top or bottom
- Opens job tasks/events and lets you search by text/status/task fields
- Shows task/event detail in a popup as YAML or JSON
- Saves selected event detail locally as YAML or JSON
- Stores bookmarks locally and cycles through them with `j`
- Caches job pages, stdout, events, and event details locally
- Restores the last screen and position when reopened
- Includes an in-app help popup

## Files

- App: `aje.py`
- Example config: `config.example.yaml`

## Requirements

```bash
pip install requests PyYAML
