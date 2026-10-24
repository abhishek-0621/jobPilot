import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobpilot.util.hashing import content_hash

_TRACKING_PARAM = re.compile(
    r"^(utm_.*|gh_src|gh_jid|lever-source.*|ref|source|src|trk|fbclid|gclid)$", re.IGNORECASE
)


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_PARAM.match(k)
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def normalize_title(title: str) -> str:
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title.lower())
    t = re.sub(r"\bsr\b\.?", "senior", t)
    t = re.sub(r"\b(?:engg|eng)\b\.?", "engineer", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def dedupe_key(company: str, title: str, location: str | None) -> str:
    return content_hash([
        company.casefold().strip(),
        normalize_title(title),
        (location or "").casefold().split(",")[0].strip(),
    ])


_DROP = "script, style, noscript, nav, footer, header, form, svg"
_BLOCK = frozenset({
    "p", "div", "section", "article", "main", "aside", "ul", "ol", "li", "br", "tr",
    "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr", "dl", "dt", "dd",
})


def _walk(node: LexborNode, out: list[str]) -> None:
    for child in node.iter(include_text=True):
        tag = child.tag
        if tag == "-text":
            out.append(child.text(deep=False))
        elif tag == "-comment":
            continue
        else:
            block = tag in _BLOCK
            if block:
                out.append("\n")
            if tag == "li":
                out.append("- ")
            _walk(child, out)
            if block:
                out.append("\n")


def html_to_text(html: str, max_chars: int = 12000) -> str:
    tree = LexborHTMLParser(html)
    for n in tree.css(_DROP):
        n.decompose()
    out: list[str] = []
    if tree.body is not None:
        _walk(tree.body, out)
    lines = (" ".join(line.split()) for line in "".join(out).split("\n"))
    text = "\n".join(line for line in lines if line and line != "-")
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[truncated]"
    return text
