#!/usr/bin/env python3
import argparse
import re
import pikepdf
from pikepdf import StreamDecodeLevel

# --- TJ collapsing with space insertion (\000\r) ----------------------------

_TJ_ARRAY_RE = re.compile(r"\[\s*(?P<body>.*?)\s*\]\s*TJ\b", re.S)
_TOKEN_RE = re.compile(
    r"""
    (?P<hex><[0-9A-Fa-f\s]+>) |
    (?P<lit>\((?:\\.|[^\\()])*\)) |
    (?P<num>[+-]?(?:\d+\.\d*|\d*\.\d+|\d+))
""",
    re.S | re.X,
)


def _parse_pdf_hex_bytes(hex_token: str) -> bytes:
    h = re.sub(r"\s+", "", hex_token[1:-1])  # strip < >
    if len(h) % 2:
        h += "0"
    return bytes.fromhex(h)


def _parse_pdf_literal_bytes(lit: str) -> bytes:
    assert lit.startswith("(") and lit.endswith(")")
    s = lit[1:-1]
    out = bytearray()
    i = 0
    while i < len(s):
        ch = s[i]
        if ch != "\\":
            out.append(ord(ch))
            i += 1
            continue
        i += 1
        if i >= len(s):
            out.append(ord("\\"))
            break
        esc = s[i]
        i += 1
        if esc in "btnfr":
            out.append({"b": 8, "t": 9, "n": 10, "f": 12, "r": 13}[esc])
        elif esc in "\\()":
            out.append(ord(esc))
        elif esc in "01234567":
            octs = esc
            for _ in range(2):
                if i < len(s) and s[i] in "01234567":
                    octs += s[i]
                    i += 1
                else:
                    break
            out.append(int(octs, 8) & 0xFF)
        elif esc == "\n":
            # line continuation
            pass
        else:
            out.append(ord(esc))
    return bytes(out)


def _emit_pdf_literal(b: bytes) -> str:
    out = []
    for x in b:
        if x == 0:
            out.append("\\000")
        elif x == 8:
            out.append("\\b")
        elif x == 9:
            out.append("\\t")
        elif x == 10:
            out.append("\\n")
        elif x == 12:
            out.append("\\f")
        elif x == 13:
            out.append("\\r")
        elif x == 0x28:  # (
            out.append("\\(")
        elif x == 0x29:  # )
            out.append("\\)")
        elif x == 0x5C:  # backslash
            out.append("\\\\")
        else:
            out.append(f"\\{x:03o}")
    return "(" + "".join(out) + ")"


def collapse_tj_inserting_space_codes(s: str) -> str:
    def repl(m: re.Match) -> str:
        body = m.group("body")
        buf = bytearray()
        for tok in _TOKEN_RE.finditer(body):
            kind = tok.lastgroup
            if kind == "hex":
                buf.extend(_parse_pdf_hex_bytes(tok.group()))
            elif kind == "lit":
                buf.extend(_parse_pdf_literal_bytes(tok.group()))
            elif kind == "num":
                buf.extend(b"\x00\x01")
        return "[" + _emit_pdf_literal(bytes(buf)) + "] TJ"

    return _TJ_ARRAY_RE.sub(repl, s)


# Height of page in points (A4 = 841.8898)
PAGE_HEIGHT = 841.8898

# Regex for "Tm" with six numeric operands
_TM_RE = re.compile(
    r"(?P<a>[+-]?\d*\.?\d+)\s+"
    r"(?P<b>[+-]?\d*\.?\d+)\s+"
    r"(?P<c>[+-]?\d*\.?\d+)\s+"
    r"(?P<d>[+-]?\d*\.?\d+)\s+"
    r"(?P<e>[+-]?\d*\.?\d+)\s+"
    r"(?P<f>[+-]?\d*\.?\d+)\s+Tm"
)


def flip_tm_y(m: re.Match) -> str:
    """Replace Tm operands to account for Y-flip."""
    a = float(m.group("a"))
    b = float(m.group("b"))
    c = float(m.group("c"))
    d = float(m.group("d"))
    e = float(m.group("e"))
    f = float(m.group("f"))

    # Transform (a,b,c,d,e,f) by the flip matrix  [1 0 0 -1 0 PAGE_HEIGHT]
    # New coords: (a, -b, -c, d, e, PAGE_HEIGHT - f)
    a2 = a
    b2 = b
    c2 = c
    d2 = -d
    e2 = e
    f2 = PAGE_HEIGHT - f

    return f"{a2:.0f} {b2:.0f} {c2:.0f} {d2:.0f} {e2:.2f} {f2:.2f} Tm"


_PRE_BT_FONT_RE = re.compile(r"/F[^\s]+[ \t]+[+-]?\d*\.?\d+[ \t]+Tf\s*")


def add_preamble_move_tf_and_flip_tms(
    s: str,
    page_height: float = 841.8898,
    cs_name: str = "/d65gray",
    scn_args: str = "0",
) -> str:
    """
    Inserts:
        1 0 0 -1 0 <page_height> cm
        <cs_name> cs
        <scn_args> scn
        <Tf from stream if found>
    immediately *before* the first 'BT', and removes the first in-stream Tf,
    then rewrites all 'Tm' operands for the Y-flip.
    """
    # 1) Find and hoist the first Tf we can see anywhere in the stream.
    tf_match = _PRE_BT_FONT_RE.search(s)
    tf_op = tf_match.group(0) if tf_match else None
    if tf_op:
        # Remove only this occurrence; we'll re-insert it before BT
        s = s[: tf_match.start()] + s[tf_match.end() :]

    # 2) Build preamble
    preamble_lines = [f"1 0 0 -1 0 {page_height} cm"]
    # Avoid duplicating cs/scn if already present right at the top:
    preface = s[:2000]
    if (cs_name + " cs") not in preface:
        preamble_lines.append(f"{cs_name} cs")
    if (" scn" not in preface) or (f"{scn_args} scn" not in preface):
        preamble_lines.append(f"{scn_args} scn")
    if tf_op:
        preamble_lines.append(tf_op.strip())
    preamble = "\n".join(preamble_lines)

    # 3) Insert preamble immediately before the first BT (or prepend if none)
    bt_idx = s.find("BT")
    if bt_idx >= 0:
        s = s[:bt_idx] + preamble + "\n" + s[bt_idx:]
    else:
        s = preamble + "\n" + s

    s = _TM_RE.sub(flip_tm_y, s)
    return s


# --- pikepdf wrapper --------------------------------------------------------


def transform(data: bytes) -> bytes:
    s = data.decode("latin-1")
    s2 = collapse_tj_inserting_space_codes(s)
    s3 = add_preamble_move_tf_and_flip_tms(
        s2, page_height=PAGE_HEIGHT, cs_name="/d65gray", scn_args="0"
    )
    return s3.encode("latin-1")


def process_page(page: pikepdf.Page) -> None:
    contents = page.get("/Contents", None)
    if contents is None:
        return

    def handle_stream(obj):
        if not isinstance(obj, pikepdf.Stream):
            return
        original = obj.read_bytes(decode_level=StreamDecodeLevel.generalized)
        newdata = transform(original)
        if newdata != original:
            obj.write(newdata)

    if isinstance(contents, pikepdf.Stream):
        handle_stream(contents)
    elif isinstance(contents, pikepdf.Array):
        for item in contents:
            try:
                handle_stream(item.get_object())
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(
        description="Collapse [ ... ] TJ and insert \\000\\r for numeric gaps."
    )
    ap.add_argument("input")
    ap.add_argument("output")
    args = ap.parse_args()

    with pikepdf.open(args.input) as pdf:
        for page in pdf.pages:
            process_page(page)
        pdf.save(args.output)


if __name__ == "__main__":
    main()
