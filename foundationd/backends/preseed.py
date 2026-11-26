"""Preseed: the answers an unattended installation needs, in one small file.

A preseed file is `key=value` lines like the configuration files (`#` starts a comment). It holds

  * any setting of the configuration schema (`ota.url=...`, `apps.default_apps=...`, see `foundation config list`), which is written to
    the installed system as /etc/foundation/foundation.conf.d/50-preseed.conf: the API's own file sorts after it, so a person can still
    change it later;
  * `deploy.language` (a locale such as de_DE.UTF-8), `deploy.timezone` (such as Europe/Berlin) and `deploy.hostname`, which become
    /etc/locale.conf, /etc/localtime and /etc/hostname.

  * `installer.timezone_url`, a setting of the installer program only (the time zone service it asks, or empty for none). It is checked
    here so a typo is caught, and it is not written to the installed system.

It is checked completely before anything is erased: an unknown key, a value of the wrong type, a setting that is read-only and a bad locale,
time zone or host name are all reported together, so a typo in an unattended install fails at the start and not after the disk is wiped.
Nothing secret belongs in it: it is copied to the installed system as it is.
"""

import os
import re

from ..config import parse_conf
from ..errors import InvalidArgument

MAX_PRESEED = 64 * 1024
CONFIG_FILE = "50-preseed.conf"
LOCALE = re.compile(r"^[A-Za-z]{2,3}(_[A-Za-z]{2,3})?(\.[A-Za-z0-9-]+)?(@[A-Za-z0-9]+)?$")
ZONE = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}$")
HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
DEPLOY_KEYS = ("deploy.language", "deploy.timezone", "deploy.hostname")


def parse(text, schema, zoneinfo="/usr/share/zoneinfo"):
    """The checked contents: {"config": {key: text for the file}, "language", "timezone", "hostname"}; InvalidArgument lists every problem."""
    if not isinstance(text, str) or len(text.encode()) > MAX_PRESEED or "\0" in text:
        raise InvalidArgument(f"the preseed file must be text of at most {MAX_PRESEED // 1024} KiB")
    result = {"config": {}, "language": "", "timezone": "", "hostname": ""}
    problems, seen = [], set()
    for key, value in parse_conf(text):
        if key in seen:
            problems.append(f"{key} is given twice")
            continue
        seen.add(key)
        if key == "deploy.language":
            if value and not LOCALE.match(value):
                problems.append(f"deploy.language: not a locale: {value!r}")
            result["language"] = value
        elif key == "deploy.timezone":
            if value and (not ZONE.match(value) or not os.path.isfile(os.path.join(zoneinfo, value))):
                problems.append(f"deploy.timezone: unknown time zone: {value!r}")
            result["timezone"] = value
        elif key == "deploy.hostname":
            if value and not HOSTNAME.match(value):
                problems.append(f"deploy.hostname: not a host name (lower case letters, digits and dashes): {value!r}")
            result["hostname"] = value
        elif key == "installer.timezone_url":
            if value and not value.startswith("https://"):
                problems.append(f"installer.timezone_url: must be empty or an https:// address: {value!r}")
        elif key in schema:
            setting = schema.get(key)
            if not setting.writable:
                problems.append(f"{key} cannot be set (read-only)")
                continue
            try:
                result["config"][key] = setting.format(setting.parse(value))
            except InvalidArgument as error:
                problems.append(str(error))
        else:
            problems.append(f"unknown setting: {key}")
    if problems:
        raise InvalidArgument("the preseed file has problems: " + "; ".join(problems))
    return result


def config_text(config):
    """The file written to the installed system."""
    lines = ["# Written by the installation from a preseed file. Change settings with `foundation config set`; a later file or the API wins.", ""]
    lines += [f"{key}={value}" for key, value in sorted(config.items())]
    return "\n".join(lines) + "\n"
