# -*- coding: utf-8 -*-
"""Создаёт GitHub Releases v1.0 и v1.1 для jckeil/ToDXF.

Токен передаётся через переменную окружения GH_TOKEN (не хранится в файле).
Тело релиза берётся из CHANGELOG.md.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

TOKEN = os.environ.get("GH_TOKEN", "")
REPO = "jckeil/ToDXF"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")


def api(method, url, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"token {TOKEN}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def section(text, version):
    pattern = rf"^## {re.escape(version)}.*?$(.*?)(?=^## |\Z)"
    match = re.search(pattern, text, re.S | re.M)
    return match.group(1).strip() if match else ""


def main():
    if not TOKEN:
        print("нет GH_TOKEN")
        return 1

    text = open(CHANGELOG, encoding="utf-8").read()

    tags = [t["name"] for t in api(
        "GET", f"https://api.github.com/repos/{REPO}/tags")]
    print("теги на GitHub:", ", ".join(tags) or "нет")

    for version in ("v1.0", "v1.1"):
        if version not in tags:
            print(f"{version}: нет тега на GitHub — пропуск")
            continue
        body = section(text, version)
        if not body:
            print(f"{version}: пустая секция в CHANGELOG — пропуск")
            continue
        payload = {
            "tag_name": version,
            "name": f"ToDXF {version}",
            "body": body,
            "draft": False,
            "prerelease": False,
        }
        try:
            release = api(
                "POST", f"https://api.github.com/repos/{REPO}/releases",
                payload)
            print(f"{version}: релиз создан — {release.get('html_url')}")
        except urllib.error.HTTPError as e:
            print(f"{version}: ошибка {e.code}: "
                  f"{e.read().decode('utf-8', 'replace')[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
