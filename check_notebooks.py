#!/usr/bin/env python
"""
Quick utility to list all OneNote notebooks via Graph API.

Usage:
    python check_notebooks.py

This verifies that your upload succeeded and shows direct links to each notebook.
"""

import json
import os
import sys

sys.path.insert(0, "src")

from onenote_uploader import get_graph_token
import requests

# Load client ID from config
config_path = os.path.expanduser("~/.evernote_converter/config.json")
if os.path.exists(config_path):
    with open(config_path) as f:
        config = json.load(f)
        client_id = config.get("client_id")
else:
    client_id = os.environ.get("ENEX_CLIENT_ID")

if not client_id:
    print("Error: client_id not found")
    print("Either:")
    print("  1. Save it at ~/.evernote_converter/config.json")
    print('     Format: {"client_id": "YOUR_ID"}')
    print("  2. Set ENEX_CLIENT_ID environment variable")
    sys.exit(1)

# Authenticate and fetch notebooks
token = get_graph_token(client_id)
resp = requests.get(
    "https://graph.microsoft.com/v1.0/me/onenote/notebooks",
    headers={"Authorization": f"Bearer {token}"},
)

if resp.status_code != 200:
    print(f"Error: HTTP {resp.status_code}")
    print(resp.text)
    sys.exit(1)

notebooks = resp.json().get("value", [])
print(f"✓ Found {len(notebooks)} notebook(s):\n")

for nb in notebooks:
    print(f"📓 {nb['displayName']}")
    print(f"   ID: {nb['id']}")
    print(f"   Created: {nb.get('createdDateTime', 'N/A')}")
    print(f"   Modified: {nb.get('lastModifiedDateTime', 'N/A')}")

    # Try to extract a clean OneDrive link
    web_url = nb.get("links", {}).get("oneNoteWebUrl", {}).get("href", "")
    if web_url:
        print(f"   Link: {web_url}")
    print()

if not notebooks:
    print("No notebooks found. Have you uploaded any notes yet?")
