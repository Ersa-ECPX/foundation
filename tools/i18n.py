#!/usr/bin/env python3
"""Extract, merge, check and compile the two Python gettext catalogs without external tools."""
import argparse
import ast
import json
import re
import sys
import struct
import string
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAINS = {"ersa-installer": ROOT / "foundation_installer", "ersa-deck": ROOT / "foundation_deck"}
PLURAL_FORMS = {
    "hi": "nplurals=2; plural=(n > 1);",
    "de": "nplurals=2; plural=(n != 1);",
    "fr": "nplurals=2; plural=(n > 1);",
    "es": "nplurals=2; plural=(n != 1);",
    "pt_BR": "nplurals=2; plural=(n > 1);",
    "it": "nplurals=2; plural=(n != 1);",
    "ru": "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<12 || n%100>14) ? 1 : 2);",
    "ja": "nplurals=1; plural=0;",
    "zh_CN": "nplurals=1; plural=0;",
    "ko": "nplurals=1; plural=0;",
    "ar": "nplurals=6; plural=(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : n%100>=3 && n%100<=10 ? 3 : n%100>=11 && n%100<=99 ? 4 : 5);",
    "bn": "nplurals=2; plural=(n != 1);",
    "ta": "nplurals=2; plural=(n != 1);",
}


def extract(domain):
    found = {}
    for path in sorted(DOMAINS[domain].glob("*.py")):
        source = path.read_text()
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            simple = isinstance(node.func, ast.Name) and node.func.id == "_"
            plural = (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                      and node.func.value.id == "_" and node.func.attr == "ngettext")
            if not (simple or plural) or len(node.args) < (3 if plural else 1):
                continue
            args = node.args[:2 if plural else 1]
            if not all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in args):
                raise ValueError(f"{path}:{node.lineno}: gettext messages must be string literals")
            comment = []
            line = node.lineno - 2
            while line >= 0 and lines[line].lstrip().startswith("# translators:"):
                comment.insert(0, lines[line].strip()[2:].strip())
                line -= 1
            key = (None, args[0].value, args[1].value if plural else None)
            found.setdefault(key, {"comments": [], "refs": []})
            found[key]["comments"].extend(c for c in comment if c not in found[key]["comments"])
            found[key]["refs"].append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def quoted(value):
    return json.dumps(value, ensure_ascii=False)


def entry(key, translations=None, comments=(), refs=(), nplurals=2):
    context, msgid, plural = key
    out = [f"#. {c}" for c in comments] + (["#: " + " ".join(refs)] if refs else [])
    if context is not None:
        out.append("msgctxt " + quoted(context))
    out.append("msgid " + quoted(msgid))
    if plural is not None:
        out.append("msgid_plural " + quoted(plural))
        values = translations if isinstance(translations, dict) else {}
        out.extend(f"msgstr[{i}] {quoted(values.get(i, ''))}" for i in range(nplurals))
    else:
        out.append("msgstr " + quoted(translations or ""))
    return "\n".join(out) + "\n"


def parse(path):
    results = {}
    fields = {}
    current = None
    for raw in path.read_text().splitlines() + [""]:
        line = raw.strip()
        if not line:
            if "msgid" in fields:
                key = (fields.get("msgctxt"), fields["msgid"], fields.get("msgid_plural"))
                results[key] = fields.get("msgstr", {})
            fields, current = {}, None
            continue
        if line.startswith("#"):
            continue
        match = re.match(r'(msgctxt|msgid_plural|msgid|msgstr(?:\[\d+\])?)\s+(".*")$', line)
        if match:
            field, value = match.groups()
            value = json.loads(value)
            if field.startswith("msgstr["):
                fields.setdefault("msgstr", {})[int(field[7:-1])] = value
                current = ("msgstr", int(field[7:-1]))
            else:
                fields[field] = value
                current = field
        elif line.startswith('"') and current:
            value = json.loads(line)
            if isinstance(current, tuple):
                fields[current[0]][current[1]] += value
            else:
                fields[current] += value
        else:
            raise ValueError(f"{path}: invalid PO line {raw!r}")
    return results


def header(domain, language=None):
    comment = ("# Initial, unreviewed Hindi translation.\n" if language == "hi" else
               "# Initial, unreviewed machine-assisted translation.\n" if language else "")
    fields = "Content-Type: text/plain; charset=UTF-8\n"
    if language == "hi":
        fields += f"Plural-Forms: {PLURAL_FORMS[language]}\n"
    elif language:
        fields += f"Language: {language}\nPlural-Forms: {PLURAL_FORMS[language]}\n"
    return comment + entry((None, "", None), fields)


def write_pot(domain):
    target = ROOT / "data/po" / domain / f"{domain}.pot"
    found = extract(domain)
    target.write_text(header(domain) + "\n" + "\n".join(entry(k, comments=v["comments"], refs=v["refs"]) for k, v in found.items()))
    return target


def update(domain, languages=None):
    found = extract(domain)
    directory = ROOT / "data/po" / domain
    for language in languages or ():                 # a language that has no file yet gets one (all msgstr empty)
        (directory / f"{language}.po").touch()
    for path in directory.glob("*.po"):
        if languages and path.stem not in languages:
            continue
        old = parse(path)
        lang = path.stem
        nplurals = int(re.search(r"nplurals=(\d+)", PLURAL_FORMS[lang]).group(1))
        text = header(domain, lang) + "\n" + "\n".join(entry(k, old.get(k), v["comments"], v["refs"], nplurals) for k, v in found.items())
        path.write_text(text)


def markers(value):
    braces = [name for _, name, _, _ in string.Formatter().parse(value) if name is not None]
    percents = re.findall(r'%(?:\([^)]+\))?[#0 +\-]*(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrsa%]', value)
    return sorted(braces), sorted(percents)


def check(domain, languages=None, strict=False):
    """Structure is always checked. A message with no translation yet is normal (it shows the English text); it is reported, and an
    error only with --strict."""
    untranslated = {}
    directory = ROOT / "data/po" / domain
    pot = parse(directory / f"{domain}.pot")
    used = set(extract(domain))
    missing = used - set(pot)
    if missing:
        raise ValueError(f"{domain}: {len(missing)} used messages missing from POT: {sorted(missing)[:3]}")
    for path in directory.glob("*.po"):
        if languages and path.stem not in languages:
            continue
        catalog = parse(path)
        header_text = catalog[(None, "", None)]
        match = re.search(r"(?m)^Plural-Forms: nplurals=(\d+); plural=.+;$", header_text)
        if not match:
            raise ValueError(f"{path}: missing or invalid plural header")
        nplurals = int(match.group(1))
        if set(catalog) != set(pot):
            raise ValueError(f"{path}: catalog messages differ from POT")
        for (context, msgid, plural), value in catalog.items():
            if not msgid:
                continue
            values = value if isinstance(value, dict) else {0: value}
            if plural is not None and set(values) != set(range(nplurals)):
                raise ValueError(f"{path}: wrong plural count: {msgid}")
            for index, translated in values.items():
                if not translated:
                    if strict:
                        raise ValueError(f"{path}: empty translation: {msgid}")
                    untranslated[path.stem] = untranslated.get(path.stem, 0) + 1
                    continue
                expected = plural if index else msgid
                if markers(translated) != markers(expected):
                    raise ValueError(f"{path}: placeholders differ: {msgid}")
    for language, count in sorted(untranslated.items()):
        print(f"{domain}/{language}: {count} message(s) not translated yet (shown in English)", file=sys.stderr)


def compile_po(path, target):
    catalog = parse(path)
    ids, strings = [], []
    def original_bytes(key):
        context, msgid, plural = key
        return ((context + "\x04" if context else "") + msgid +
                ("\0" + plural if plural is not None else "")).encode("utf-8")

    for (context, msgid, plural), value in sorted(catalog.items(), key=lambda item: original_bytes(item[0])):
        original = (context + "\x04" if context else "") + msgid + ("\0" + plural if plural is not None else "")
        translated = "\0".join(value[i] for i in sorted(value)) if isinstance(value, dict) else value
        if not translated.replace("\0", "") and msgid:         # not translated yet: leave it out, so the English text shows
            continue
        ids.append(original.encode("utf-8"))
        strings.append(translated.encode("utf-8"))
    count = len(ids)
    offset = 28 + count * 16
    id_data = b"".join(item + b"\0" for item in ids)
    str_data = b"".join(item + b"\0" for item in strings)
    cursor = offset
    id_table = []
    for item in ids:
        id_table.extend((len(item), cursor))
        cursor += len(item) + 1
    str_table = []
    for item in strings:
        str_table.extend((len(item), cursor))
        cursor += len(item) + 1
    data = struct.pack("<7I", 0x950412de, 0, count, 28, 28 + count * 8, 0, 0)
    data += struct.pack(f"<{count*2}I", *id_table) + struct.pack(f"<{count*2}I", *str_table)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data + id_data + str_data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("extract", "update", "compile", "check"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--strict", action="store_true", help="check: an untranslated message is an error")
    parser.add_argument("--lang", action="append", metavar="LANG",
                        help="update (creating the file if needed) or check only this language; repeatable")
    args = parser.parse_args()
    for domain in DOMAINS:
        if args.command == "extract":
            write_pot(domain)
        elif args.command == "update":
            update(domain, args.lang)
        elif args.command == "check":
            check(domain, args.lang, args.strict)
        else:
            if args.out is None:
                parser.error("compile requires --out DIR")
            for path in (ROOT / "data/po" / domain).glob("*.po"):
                compile_po(path, args.out / path.stem / "LC_MESSAGES" / f"{domain}.mo")


if __name__ == "__main__":
    main()
