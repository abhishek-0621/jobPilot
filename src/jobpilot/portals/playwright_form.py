"""Shared Playwright form reading/filling for ATS adapters. Never submits."""

import hashlib
from pathlib import Path
from typing import Any, ClassVar

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from jobpilot.contracts.enums import ErrorCode, FieldDecision, PortalCapability
from jobpilot.contracts.models import FieldAnswer, SubmitResult
from jobpilot.forms.browser import detect_blockers
from jobpilot.forms.classify import classify_sensitive
from jobpilot.portals.base import (
    ApprovedApplication,
    FillReport,
    FormField,
    FormSnapshot,
    PortalCapabilities,
    SubmissionEvidence,
    ValidationReport,
)
from jobpilot.util.hashing import content_hash

_FILLABLE = {FieldDecision.AUTO_FILL, FieldDecision.FILL_AND_REVIEW}
_USER_GATED = {FieldDecision.USER_ONLY, FieldDecision.ASK_USER}
_LAYOUT = ErrorCode.FORM_LAYOUT_CHANGED.value

# Shared JS helpers: label/help/required resolution relative to a field element.
_HELPERS = r"""
const norm = s => (s || '').replace(/\s+/g, ' ').trim();
const ownText = el => {
  if (!el) return '';
  const c = el.cloneNode(true);
  c.querySelectorAll('input, select, textarea, option, button, .help, .description, small')
    .forEach(n => n.remove());
  return norm(c.textContent);
};
const CONTAINER = '.field, .application-question, fieldset, li, div';
const container = el => el.closest(CONTAINER);
const questionText = el => {
  const c = container(el);
  if (!c) return '';
  const h = c.querySelector('legend, h1, h2, h3, h4, h5, h6, .question, .text');
  if (h) return ownText(h);
  for (const n of c.childNodes) {
    if (n.nodeType === 3 && norm(n.textContent)) return norm(n.textContent);
  }
  return '';
};
const labelFor = el => {
  if (el.id) {
    const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (l && ownText(l)) return ownText(l);
  }
  const al = el.getAttribute('aria-label');
  if (al && norm(al)) return norm(al);
  const lb = el.getAttribute('aria-labelledby');
  if (lb) {
    const t = lb.split(/\s+/).map(i => norm((document.getElementById(i) || {}).textContent))
      .filter(Boolean).join(' ');
    if (t) return t;
  }
  const wrap = el.closest('label');
  if (wrap && ownText(wrap)) return ownText(wrap);
  return questionText(el);
};
const helpFor = el => {
  const d = el.getAttribute('aria-describedby');
  if (d) {
    const t = d.split(/\s+/).map(i => norm((document.getElementById(i) || {}).textContent))
      .filter(Boolean).join(' ');
    if (t) return t;
  }
  const c = container(el);
  const h = c && c.querySelector('.help, .description, small');
  return h ? norm(h.textContent) : '';
};
const isRequired = (el, label) => {
  const c = container(el);
  return el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label)
    || !!(c && (c.classList.contains('required') || c.querySelector('.required')));
};
const pickRoot = sels => {
  for (const s of sels) { const r = document.querySelector(s); if (r) return r; }
  return document.querySelector('form') || document.body;
};
"""

_INSPECT_JS = "(sels) => {" + _HELPERS + r"""
  const root = pickRoot(sels);
  const strip = s => norm((s || '').replace(/\*\s*$/, ''));
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  const out = []; const radios = {};
  const boxCount = {};
  for (const b of root.querySelectorAll('input[type=checkbox][name]')) {
    if (!b.disabled && visible(b)) boxCount[b.name] = (boxCount[b.name] || 0) + 1;
  }
  for (const el of root.querySelectorAll('input, select, textarea')) {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.type || 'text').toLowerCase() : tag;
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
    if (el.disabled || el.readOnly && type !== 'file') continue;
    if (type !== 'file' && !visible(el)) continue;
    const grouped = type === 'radio' || (type === 'checkbox' && boxCount[el.name] > 1);
    if (grouped) {
      const kind = type === 'radio' ? 'radio' : 'checkbox_group';
      const key = kind + ':' + (el.name || el.id);
      const optLabel = strip(ownText(el.closest('label')) || labelFor(el));
      if (radios[key]) { radios[key].options.push(optLabel);
        radios[key].required = radios[key].required || el.required; continue; }
      const raw = questionText(el);
      radios[key] = { name: el.name || null, id: el.id || null, label: strip(raw),
        field_type: kind, required: isRequired(el, raw), options: [optLabel],
        help_text: helpFor(el) };
      out.push(radios[key]);
      continue;
    }
    const raw = labelFor(el);
    const label = strip(raw);
    let options = [];
    if (type === 'select') options = [...el.options].filter(o => o.value !== '')
      .map(o => norm(o.textContent));
    if (type === 'checkbox') options = [label];
    out.push({ name: el.getAttribute('name') || null, id: el.id || null, label,
      field_type: type, required: isRequired(el, raw), options, help_text: helpFor(el) });
  }
  return out;
}"""

_READ_JS = "(el) => {" + _HELPERS + r"""
  const tag = el.tagName.toLowerCase();
  const type = tag === 'input' ? (el.type || 'text').toLowerCase() : tag;
  if (type === 'select') return [...el.selectedOptions].map(o => norm(o.textContent));
  if (type === 'checkbox') return el.checked;
  if (type === 'file') return [...el.files].map(f => f.name);
  if (type === 'radio') {
    const group = document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`);
    const hit = [...group].find(r => r.checked);
    return hit ? norm((ownText(hit.closest('label')) || labelFor(hit)).replace(/\*\s*$/, ''))
      : null;
  }
  return el.value;
}"""

_GROUP_LABELS_JS = "(els) => {" + _HELPERS + r"""
  return els.map(r => norm((ownText(r.closest('label')) || labelFor(r)).replace(/\*\s*$/, '')));
}"""

_CHECKED_JS = "(els) => els.map(e => e.checked)"

_FILLABLE_SEL = ("input:not([type=hidden]):not([type=submit]):not([type=button])"
                 ":not([type=reset]):not([type=image])", "select", "textarea")
_TYPE_SEL = {
    "radio": ("input[type=radio]",), "checkbox": ("input[type=checkbox]",),
    "checkbox_group": ("input[type=checkbox]",), "file": ("input[type=file]",),
    "select": ("select",), "textarea": ("textarea",),
}
_ACTION_TIMEOUT_MS = 5_000

_ERRORS_JS = "(sels) => {" + _HELPERS + r"""
  const root = pickRoot(sels);
  const out = [];
  for (const e of root.querySelectorAll('.error, .field-error, [aria-invalid="true"]')) {
    const isField = e.matches('input, select, textarea');
    const c = isField ? e : (e.parentElement && container(e.parentElement));
    const f = isField ? e : (c && c.querySelector('input, select, textarea'));
    const msg = isField ? (e.validationMessage || 'invalid') : norm(e.textContent);
    if (!msg && !isField) continue;
    out.push({ name: f ? f.getAttribute('name') : null, id: f ? f.id : null,
      label: f ? labelFor(f).replace(/\*\s*$/, '').trim() : '', message: msg });
  }
  return out;
}"""


def _norm(v: Any) -> str:
    return " ".join(str(v).casefold().split())


def _attr_sel(attr: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'[{attr}="{escaped}"]'


def label_key(label: str) -> str:
    return "lbl_" + hashlib.sha1(_norm(label).encode("utf-8")).hexdigest()[:10]


def snapshot_hash(snapshot: FormSnapshot) -> str:
    """Layout fingerprint: field ids, types, required flags and labels."""
    return content_hash(sorted(
        [f.field_id, f.field_type, f.required, f.label] for f in snapshot.fields))


def _field_key(raw: dict[str, Any]) -> str:
    return raw.get("name") or raw.get("id") or label_key(raw.get("label") or "")


class PlaywrightFormAdapter:
    """Reads and fills an ATS application form on an already-open page."""

    name: ClassVar[str] = "playwright"
    form_roots: ClassVar[tuple[str, ...]] = ("form",)

    def __init__(self, page: Page, *, upload_root: Path) -> None:
        self.page = page
        self.upload_root = upload_root.resolve()
        self._fields: dict[str, FormField] = {}

    async def capabilities(self) -> PortalCapabilities:
        return frozenset({PortalCapability.PREPARE_APPLICATION})

    async def open_job(self, url: str) -> None:
        await self.page.goto(url)
        await self.page.wait_for_load_state("domcontentloaded")
        self._fields = {}

    async def blockers(self) -> list[str]:
        return await detect_blockers(self.page)

    def snapshot_hash(self, snapshot: FormSnapshot) -> str:
        return snapshot_hash(snapshot)

    async def inspect_form(self) -> FormSnapshot:
        raw = await self.page.evaluate(_INSPECT_JS, list(self.form_roots))
        fields: list[FormField] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for r in raw:
            fid = _field_key(r)
            if fid in seen:
                warnings.append(f"duplicate field id {fid!r} ({r['label']!r}); kept the first")
                continue
            seen.add(fid)
            fields.append(FormField(field_id=fid, label=r["label"], field_type=r["field_type"],
                                    required=bool(r["required"]), options=r["options"],
                                    help_text=r["help_text"]))
        self._fields = {f.field_id: f for f in fields}
        return FormSnapshot(url=self.page.url, fields=fields, warnings=warnings)

    async def _root(self) -> Locator:
        for sel in self.form_roots:
            loc = self.page.locator(sel)
            if await loc.count():
                return loc.first
        forms = self.page.locator("form")
        return forms.first if await forms.count() else self.page.locator("body")

    async def _locate(self, field_id: str, label: str, field_type: str) -> Locator | None:
        known = self._fields.get(field_id)
        kind = known.field_type if known else field_type
        tags = _TYPE_SEL.get(kind, _FILLABLE_SEL)
        root = await self._root()
        for attr in ("name", "id"):
            attr_sel = _attr_sel(attr, field_id)
            loc = root.locator(", ".join(t + attr_sel for t in tags))
            if await loc.count():
                return loc
        if field_id.startswith("lbl_") and label and label_key(label) == field_id:
            loc = root.get_by_label(label, exact=True).and_(root.locator(", ".join(tags)))
            if await loc.count():
                return loc
        return None

    def _allowed(self, a: FieldAnswer) -> bool:
        if a.proposed_value is None:
            return False
        by_user = a.source_fact_id == "user"
        known = self._fields.get(a.field_id)
        help_text = known.help_text if known else ""
        options = known.options if known else []
        labels = [a.label] + ([known.label] if known else [])
        if any(classify_sensitive(lab, help_text, options) == "declaration" for lab in labels):
            return by_user and bool(a.proposed_value)
        if a.decision in _FILLABLE:
            return True
        return a.decision in _USER_GATED and by_user

    async def fill_fields(self, answers: list[FieldAnswer]) -> FillReport:
        filled: list[str] = []
        failed: dict[str, str] = {}
        todo = [a for a in answers if self._allowed(a)]
        blockers = await self.blockers()
        if blockers:
            reason = "BLOCKED:" + ",".join(blockers)
            return FillReport(filled=[], failed={a.field_id: reason for a in todo})
        for a in todo:
            loc = await self._locate(a.field_id, a.label, a.field_type)
            if loc is None:
                failed[a.field_id] = _LAYOUT
                continue
            if a.field_type == "file" and not self._inside_root(a.proposed_value):
                failed[a.field_id] = "UPLOAD_OUTSIDE_ROOT"
                continue
            try:
                await self._fill_one(loc, a)
            except (PlaywrightError, ValueError, FileNotFoundError) as e:
                failed[a.field_id] = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            else:
                filled.append(a.field_id)
        return FillReport(filled=filled, failed=failed)

    def _inside_root(self, value: Any) -> bool:
        return Path(str(value)).resolve().is_relative_to(self.upload_root)

    async def _fill_one(self, loc: Locator, a: FieldAnswer) -> None:
        value = a.proposed_value
        kind = self._fields[a.field_id].field_type if a.field_id in self._fields else a.field_type
        t = _ACTION_TIMEOUT_MS
        if kind == "file":
            path = Path(str(value)).resolve()
            if not path.is_file():
                raise FileNotFoundError(f"file not found: {path.name}")
            await loc.first.set_input_files(path, timeout=t)
        elif kind == "select":
            labels = value if isinstance(value, list) else [str(value)]
            await loc.first.select_option(label=labels, timeout=t)
        elif kind in {"radio", "checkbox_group"}:
            labels = [_norm(lab) for lab in await loc.evaluate_all(_GROUP_LABELS_JS)]
            wants = [_norm(v) for v in (value if isinstance(value, list) else [value])]
            missing = [w for w in wants if w not in labels]
            if missing or (kind == "radio" and len(wants) != 1):
                raise ValueError(f"no option matching {value!r}")
            for i, lab in enumerate(labels):
                if lab in wants:
                    await loc.nth(i).check(timeout=t)
                elif kind == "checkbox_group":
                    await loc.nth(i).uncheck(timeout=t)
        elif kind == "checkbox":
            if _truthy(value):
                await loc.first.check(timeout=t)
            else:
                await loc.first.uncheck(timeout=t)
        else:
            text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
            await loc.first.fill(text, timeout=t)

    async def validate_form(self, expected: dict[str, Any] | None = None) -> ValidationReport:
        errors: dict[str, str] = {}
        for fid, want in (expected or {}).items():
            known = self._fields.get(fid)
            loc = await self._locate(fid, known.label if known else "",
                                     known.field_type if known else "")
            if loc is None:
                errors[fid] = _LAYOUT
                continue
            if known and known.field_type == "checkbox_group":
                labels = await loc.evaluate_all(_GROUP_LABELS_JS)
                flags = await loc.evaluate_all(_CHECKED_JS)
                got: Any = [lab for lab, on in zip(labels, flags, strict=True) if on]
            else:
                got = await loc.first.evaluate(_READ_JS)
            if not _matches(got, want):
                errors[fid] = f"expected {want!r}, found {got!r}"
        for e in await self.page.evaluate(_ERRORS_JS, list(self.form_roots)):
            fid = e["name"] or e["id"] or (label_key(e["label"]) if e["label"] else "_form")
            errors.setdefault(fid, e["message"])
        return ValidationReport(ok=not errors, errors=errors)

    async def submit(self, approved_application: ApprovedApplication) -> SubmitResult:
        raise NotImplementedError("Plan 5")

    async def verify_submission(self) -> SubmissionEvidence:
        raise NotImplementedError("Plan 5")

    async def find_existing_submission(self, idempotency_key: str) -> SubmitResult | None:
        raise NotImplementedError("Plan 5")


def _truthy(v: Any) -> bool:
    return v is True or _norm(v) in {"true", "yes", "1"}


def _matches(got: Any, want: Any) -> bool:
    if isinstance(got, bool):
        return got == _truthy(want)
    if got is None:
        return want in (None, "", [])
    if isinstance(got, list):
        if want is None:
            return not got
        wants = want if isinstance(want, list) else [want]
        names = [Path(str(w)).name for w in wants]
        return bool(got) and ([_norm(g) for g in got] == [_norm(w) for w in wants]
                              or [_norm(g) for g in got] == [_norm(n) for n in names])
    if isinstance(want, list):
        want = ", ".join(map(str, want))
    return _norm(got) == _norm(want if want is not None else "")
