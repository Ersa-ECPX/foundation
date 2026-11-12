"""What an RPM's install scripts do, read from their text, and the parts of that which can be done safely.

A scriptlet is never run (a shell script from a package, as root, on an immutable system, is what an extension avoids).
Two things in them are plain data, though, and are carried out another way:

- **Users and groups** (`useradd`, `groupadd`, `usermod -aG`) become a `sysusers.d` file inside the extension, which
  `systemd-sysusers` applies. Only what can be said exactly is translated: system accounts with simple options. A command
  with anything else (a regular user, an option we do not know) is left alone and reported.
- **Services** (`systemctl preset foo.service`, the `%service_add_post` macro) become a list of unit names that foundation
  presets after the merge, which is what the scriptlet did.

Everything else is only classified, so a person is told what will be missing.
"""

import re
import shlex

# Sections of `rpm -q --scripts` output that run when a package is installed (not when it is removed).
INSTALL_SECTIONS = ("pretrans", "preinstall", "postinstall", "posttrans")
SECTION = re.compile(r"^(\w+) scriptlet \(using [^)]*\):\s*$")

NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,30}$")
UNIT = re.compile(r"^[A-Za-z0-9:_.@\\-]+\.(service|socket|timer|path)$")
PATH = re.compile(r"^/[A-Za-z0-9_./-]*$")
NUMBER = re.compile(r"^[0-9]{1,9}$")

EFFECTS = (
    ("sysusers", re.compile(r"systemd-sysusers|sysusers\.d|%sysusers")),
    ("users", re.compile(r"\b(useradd|groupadd|adduser|addgroup|usermod|gpasswd)\b")),
    ("services", re.compile(r"systemctl|service_add_|systemd-update-helper|\bpreset\b|insserv|chkconfig")),
    ("alternatives", re.compile(r"update-alternatives")),
    ("schemas", re.compile(r"glib-compile-schemas|glib2_gsettings")),
    ("icons", re.compile(r"gtk-update-icon-cache|gtk4-update-icon-cache|icon_theme_cache")),
    ("caches", re.compile(r"update-desktop-database|"
                          r"update-mime-database|gio-querymodules|gdk-pixbuf-query-loaders|gtk-query-immodules|fc-cache|"
                          r"mktexlsr|texhash|depmod|dracut|update-ca-certificates|"
                          r"desktop_database|mime_database|fonts_")),
    ("ldconfig", re.compile(r"\bldconfig\b")),
)
# schemas: compiled after each merge (postmerge.py); icons: the extension's icon directories are dated so the base cache is ignored
HANDLED = ("sysusers", "ldconfig", "schemas", "icons")
# commands that only look at the system or print, so they are not "other"
BENIGN = re.compile(r"^\s*(getent|test|\[|\]|touch|mkdir|echo|exit|return|%|[A-Za-z_]+=|rpm -ql\b|.*libreoffice-share-linker)")
TRIVIAL = re.compile(r"^\s*(#.*|exit 0|:|true|\[ ?\$1 .*|if .*|fi|then|else|esac|case .*|\*\)|;;)?\s*$")
# LibreOffice's language packs keep their files in /usr/share/libreoffice and a scriptlet links them into
# /usr/lib64/libreoffice, where the program looks (without the links: "The user interface language cannot be determined").
LINKER = re.compile(r"libreoffice-share-linker\s+(?!--unlink)\S")
USER_TOOLS = ("useradd", "groupadd", "usermod", "adduser", "addgroup", "gpasswd")


def install_text(text):
    """The part of `rpm -q --scripts` output that runs at installation, without the section headers."""
    keep, body = False, []
    for line in text.splitlines():
        header = SECTION.match(line)
        if header:
            keep = header.group(1) in INSTALL_SECTIONS
            continue
        if keep:
            body.append(line)
    return "\n".join(body)


def _commands(text):
    """Every simple command of the script as a list of words (redirections removed), with its raw line."""
    for raw in text.replace("\\\n", " ").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            lexer.commenters = "#"
            words = list(lexer)
        except ValueError:
            yield [], raw
            continue
        current = []
        for word in words:
            if word in (";", "&&", "||", "|", "&", "(", ")", ";;"):
                if current:
                    yield current, raw
                current = []
            elif re.fullmatch(r"[0-9]*[<>]+&?[0-9-]*", word) or word in ("2>&1", ">&2"):
                continue
            else:
                current.append(word)
        if current:
            yield current, raw


def _strip_redirects(words):
    cleaned, skip = [], False
    for word in words:
        if skip:
            skip = False
            continue
        if re.fullmatch(r"[0-9]*[<>]+", word):
            skip = True
            continue
        if word.startswith((">", "<")) or re.match(r"^[0-9]+[<>]", word):
            continue
        cleaned.append(word)
    return cleaned


def _tool(words):
    first = words[0].rsplit("/", 1)[-1] if words else ""
    return first if first in USER_TOOLS else None


def _options(words, takes):
    """(options dict, flags set, positional list) of a short-option command line."""
    values, flags, positional = {}, set(), []
    index = 1
    while index < len(words):
        word = words[index]
        if word == "--":
            positional += words[index + 1:]
            break
        if word.startswith("--"):
            name, _eq, value = word[2:].partition("=")
            alias = {"system": "r", "gid": "g", "home-dir": "d", "shell": "s", "comment": "c", "uid": "u",
                     "groups": "G", "append": "a", "no-create-home": "M", "create-home": "m", "no-user-group": "N",
                     "user-group": "U", "non-unique": "o", "force": "f"}.get(name)
            if alias is None:
                raise ValueError(f"unknown option {word}")
            if alias in takes:
                if not _eq:
                    index += 1
                    value = words[index] if index < len(words) else ""
                values[alias] = value
            else:
                flags.add(alias)
        elif word.startswith("-") and len(word) > 1:
            letters = word[1:]
            position = 0
            while position < len(letters):
                letter = letters[position]
                if letter in takes:
                    rest = letters[position + 1:]
                    if rest:
                        values[letter] = rest
                    else:
                        index += 1
                        values[letter] = words[index] if index < len(words) else ""
                    break
                if letter not in "rmMNUofa":
                    raise ValueError(f"unknown option -{letter}")
                flags.add(letter)
                position += 1
        else:
            positional.append(word)
        index += 1
    return values, flags, positional


def _quote(text):
    cleaned = "".join(c for c in text if c.isprintable() and c not in '"\\')
    return f'"{cleaned}"'


def parse_accounts(text):
    """(sysusers lines, unparsed commands, homes) for the useradd, groupadd and usermod commands in `text`.
    Lines come ordered groups, users, memberships, without duplicates. `homes` are (user, group, directory) for
    `useradd -m`: sysusers.d cannot create a home directory, so the caller makes it with tmpfiles.d."""
    groups, users, members, unparsed, homes = [], [], [], [], []
    for words, raw in _commands(text):
        words = _strip_redirects(words)
        tool = _tool(words)
        if not tool:
            continue
        shown = " ".join(words)
        try:
            if tool in ("groupadd", "addgroup"):
                values, flags, positional = _options(words, takes={"g"})
                if len(positional) != 1 or not NAME.match(positional[0]):
                    raise ValueError("unusual group")
                gid = values.get("g", "-")
                if gid != "-" and not NUMBER.match(gid):
                    raise ValueError("odd group id")
                groups.append(f"g {positional[0]} {gid}")
            elif tool in ("useradd", "adduser"):
                values, flags, positional = _options(words, takes={"g", "G", "d", "s", "c", "u"})
                if "r" not in flags:
                    raise ValueError("not a system account")
                if "o" in flags:
                    raise ValueError("a shared id cannot be said in sysusers.d")
                if len(positional) != 1 or not NAME.match(positional[0]):
                    raise ValueError("unusual user")
                name = positional[0]
                uid = values.get("u", "-")
                if uid != "-" and not NUMBER.match(uid):
                    raise ValueError("odd user id")
                group = values.get("g")
                if group and not NAME.match(group):
                    raise ValueError("unusual group")
                home = values.get("d", "-")
                shell = values.get("s", "-")
                for path in (home, shell):
                    if path != "-" and not PATH.match(path):
                        raise ValueError("odd path")
                ident = f"{uid}:{group}" if group else uid
                users.append(f"u {name} {ident} {_quote(values.get('c', ''))} {home} {shell}")
                if "m" in flags:
                    directory = home if home != "-" else f"/home/{name}"
                    homes.append((name, group or name, directory))
                for extra in filter(None, values.get("G", "").split(",")):
                    if not NAME.match(extra):
                        raise ValueError("unusual group")
                    members.append(f"m {name} {extra}")
            elif tool == "usermod":
                values, flags, positional = _options(words, takes={"G"})
                if "a" not in flags or "G" not in values or len(positional) != 1 or not NAME.match(positional[0]):
                    raise ValueError("not just adding groups")
                for extra in filter(None, values["G"].split(",")):
                    if not NAME.match(extra):
                        raise ValueError("unusual group")
                    members.append(f"m {positional[0]} {extra}")
            else:
                raise ValueError("not translated")
        except ValueError:
            unparsed.append(shown)
    lines, seen = [], set()
    for line in groups + users + members:
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines, unparsed, homes


def service_units(text):
    """Unit names a script presets or enables at installation (not the ones it only stops or disables)."""
    units = []
    for words, _raw in _commands(text):
        words = _strip_redirects(words)
        if not words:
            continue
        joined = " ".join(words)
        if not re.search(r"systemctl|service_add|update-helper|preset|enable", joined):
            continue
        if re.search(r"\b(disable|stop|is-active|is-enabled|mask|kill|status)\b", joined):
            continue
        for word in words[1:]:
            if UNIT.match(word) and word not in units:
                units.append(word)
    return units


def analyse(scripts_text):
    """{"effects": [...unhandled effect names], "sysusers": [lines], "units": [names]} for one package."""
    text = install_text(scripts_text)
    if not text.strip():
        return {"effects": [], "sysusers": [], "units": [], "homes": [], "link_share": False}
    lines, unparsed, homes = parse_accounts(text)
    units = service_units(text)
    found = [name for name, pattern in EFFECTS if pattern.search(text)]
    if "users" in found and lines and not unparsed:
        found.remove("users")
    if "services" in found and units:
        found.remove("services")
    unknown = [line for line in text.splitlines()
               if not TRIVIAL.match(line) and not BENIGN.match(line) and not any(p.search(line) for _n, p in EFFECTS)]
    if unknown:
        found.append("other")
    return {"effects": [e for e in found if e not in HANDLED], "sysusers": lines, "units": units,
            "homes": [list(h) for h in homes], "link_share": bool(LINKER.search(text))}


LINK_PREFIX = "/usr/share/libreoffice"
LINK_LIBDIRS = ("usr/lib", "usr/lib64", "usr/lib32")


def link_libreoffice_share(tree, listing):
    """What `libreoffice-share-linker LIST` does, inside the extension `tree`: for every listed path under
    /usr/share/libreoffice make the same path under each /usr/lib*/libreoffice that exists, as a directory or as a link to
    the file. `listing` is the package's file list. Returns the number of links and directories made."""
    import os
    made = 0
    paths = sorted(line.strip() for line in listing.splitlines() if line.strip().startswith(LINK_PREFIX))
    for libdir in LINK_LIBDIRS:
        if not os.path.isdir(os.path.join(tree, libdir, "libreoffice")) or os.path.islink(os.path.join(tree, libdir, "libreoffice")):
            continue
        for path in paths:
            if ".." in path.split("/"):
                continue
            source = os.path.join(tree, path.lstrip("/"))
            link = os.path.join(tree, libdir, os.path.relpath(path, "/usr/share"))
            if os.path.lexists(link):
                continue
            if os.path.isdir(source) and not os.path.islink(source):
                os.makedirs(link, exist_ok=True)
                os.chmod(link, os.stat(source).st_mode & 0o7777)
            else:
                os.makedirs(os.path.dirname(link), exist_ok=True)
                os.symlink(path, link)
            made += 1
    return made
