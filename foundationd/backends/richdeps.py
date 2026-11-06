"""Evaluate RPM *rich* (boolean) dependencies, such as `(libcurl.so.4()(64bit) or libcurl-gnutls.so.4()(64bit))`.

Forms handled (the ones rpm defines): `(A and B ...)`, `(A or B ...)`, `(A with B)`, `(A without B)`,
`(A if B)`, `(A if B else C)`, `(A unless B)`, `(A unless B else C)`, nested in parentheses. A name
may carry a comparison (`foo >= 1.0`); only the name is looked up, as everywhere else in this project
(version constraints are not compared). Capability names themselves can contain parentheses
(`libc.so.6()(64bit)`), which is why the tokenizer treats a "(" that follows name characters as part
of the name and only a "(" at the start of a token as grouping.

Anything that cannot be parsed returns None: the caller must report it, not assume it is satisfied.
"""

COMPARATORS = ("<", "<=", "=", "==", ">=", ">")
OPERATORS = ("and", "or", "with", "without", "if", "unless", "else")


def is_rich(requirement):
    return requirement.lstrip().startswith("(")


def tokenize(text):
    tokens, i, n = [], 0, len(text)
    while i < n:
        char = text[i]
        if char.isspace():
            i += 1
        elif char in "()":
            tokens.append(char)
            i += 1
        else:
            j, depth = i, 0
            while j < n:
                c = text[j]
                if c == "(":
                    depth += 1
                elif c == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif c.isspace() and depth == 0:
                    break
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


class _Parser:
    def __init__(self, tokens, provided):
        self.tokens, self.pos, self.provided = tokens, 0, provided

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        token = self.peek()
        if token is None:
            raise ValueError("unexpected end")
        self.pos += 1
        return token

    def atom(self):
        token = self.take()
        if token == "(":
            return self.group()
        if token == ")" or token in OPERATORS:
            raise ValueError(f"unexpected {token!r}")
        if self.peek() in COMPARATORS:
            self.take()
            self.take()
        return bool(self.provided(token))

    def group(self):
        items, ops = [self.atom()], []
        while self.peek() not in (")", None):
            op = self.take()
            if op not in OPERATORS:
                raise ValueError(f"unexpected {op!r}")
            ops.append(op)
            items.append(self.atom())
        self.take()  # the closing parenthesis (raises if missing)
        return evaluate_sequence(items, ops)


def evaluate_sequence(items, ops):
    if not ops:
        return items[0]
    kinds = set(ops)
    if kinds <= {"and", "with"}:
        return all(items)
    if kinds == {"or"}:
        return any(items)
    if ops == ["without"]:
        return items[0] and not items[1]
    if ops == ["if"]:
        return (not items[1]) or items[0]
    if ops == ["unless"]:
        return items[1] or items[0]
    if ops == ["if", "else"]:
        return items[0] if items[1] else items[2]
    if ops == ["unless", "else"]:
        return items[2] if items[1] else items[0]
    raise ValueError(f"unsupported combination {ops}")


def satisfied(requirement, provided):
    """True or False for a rich dependency, or None if it cannot be understood."""
    try:
        parser = _Parser(tokenize(requirement), provided)
        value = parser.atom()
        if parser.peek() is not None:
            raise ValueError("trailing text")
        return value
    except (ValueError, IndexError):
        return None
