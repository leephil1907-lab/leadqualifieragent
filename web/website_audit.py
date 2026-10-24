"""Deterministic audit of a supplied HTML snapshot; performs no network requests."""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any


_MAX_HTML_BYTES = 1_000_000


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.in_title = False
        self.meta_description = ""
        self.viewport = False
        self.h1_count = 0
        self.links: list[dict[str, str]] = []
        self.forms = 0
        self._anchor: dict[str, str] | None = None
        self._text_parts: list[str] = []
        self.has_tel = False
        self.has_mailto = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            if values.get("name", "").lower() == "description":
                self.meta_description = values.get("content", "").strip()
            if values.get("name", "").lower() == "viewport":
                self.viewport = bool(values.get("content", "").strip())
        if tag == "h1":
            self.h1_count += 1
        if tag == "form":
            self.forms += 1
        if tag == "a":
            href = values.get("href", "").strip()
            self._anchor = {"href": href, "text": ""}
            if href.lower().startswith("tel:"):
                self.has_tel = True
            if href.lower().startswith("mailto:"):
                self.has_mailto = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False
        if tag.lower() == "a" and self._anchor is not None:
            self._anchor["text"] = " ".join(self._text_parts).strip()[:200]
            if self._anchor["href"]:
                self.links.append(self._anchor)
            self._anchor = None
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        if self._anchor is not None:
            self._text_parts.append(data)


def audit_html_snapshot(html: str) -> dict[str, Any]:
    """Return evidence-based on-page findings for HTML obtained by a trusted fetcher."""
    if not isinstance(html, str):
        raise ValueError("html must be a string")
    if len(html.encode("utf-8")) > _MAX_HTML_BYTES:
        raise ValueError("HTML snapshot exceeds the 1 MB limit")
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise ValueError("HTML snapshot could not be parsed") from exc

    title = " ".join(parser.title.split())
    description = " ".join(parser.meta_description.split())
    findings = []

    def finding(code: str, severity: str, issue: str, evidence: str, fix: str) -> None:
        findings.append({"code": code, "severity": severity, "issue": issue,
                         "evidence": evidence[:300], "recommended_fix": fix})

    if not title:
        finding("missing_title", "high", "The page has no document title.", "No <title> text found.",
                "Add a unique, descriptive title that matches the page's service and location where relevant.")
    elif len(title) < 20 or len(title) > 65:
        finding("title_length", "medium", f"The document title is {len(title)} characters long.", title,
                "Rewrite the title to be specific and concise, usually around 20–65 characters.")
    if not description:
        finding("missing_meta_description", "medium", "No meta description was found.", "No description meta tag found.",
                "Add a unique summary of the page's offer and a clear reason to click.")
    elif len(description) < 70 or len(description) > 170:
        finding("meta_description_length", "low", f"The meta description is {len(description)} characters long.", description,
                "Rewrite the description to be useful and concise, usually around 70–170 characters.")
    if not parser.viewport:
        finding("missing_viewport", "high", "The page does not declare a mobile viewport.", "No viewport meta tag found.",
                "Add a responsive viewport meta tag and verify layouts at narrow screen widths.")
    if parser.h1_count == 0:
        finding("missing_h1", "medium", "No primary H1 heading was found.", "H1 count: 0.",
                "Add one clear main heading that explains the page's primary offer.")
    elif parser.h1_count > 1:
        finding("multiple_h1", "low", f"The page contains {parser.h1_count} H1 headings.", f"H1 count: {parser.h1_count}.",
                "Review heading hierarchy so the page has one clear primary heading where appropriate.")
    if not parser.forms and not parser.has_tel and not parser.has_mailto:
        contact_links = [x for x in parser.links if any(k in x["href"].lower() or k in x["text"].lower()
                                                        for k in ("contact", "quote", "book", "enquir", "appointment"))]
        if not contact_links:
            finding("no_obvious_contact_path", "high", "No form or obvious contact action was detected in this HTML snapshot.",
                    f"Forms: {parser.forms}; telephone links: {parser.has_tel}; email links: {parser.has_mailto}.",
                    "Provide a prominent enquiry, booking, telephone, or email action and test it on mobile.")
    return {
        "audit_type": "html_snapshot",
        "network_checks_performed": False,
        "findings": findings,
        "summary": {"finding_count": len(findings),
                    "high": sum(x["severity"] == "high" for x in findings),
                    "medium": sum(x["severity"] == "medium" for x in findings),
                    "low": sum(x["severity"] == "low" for x in findings)},
        "observed": {"title": title, "meta_description": description, "viewport_present": parser.viewport,
                     "h1_count": parser.h1_count, "form_count": parser.forms, "link_count": len(parser.links),
                     "telephone_link_present": parser.has_tel, "email_link_present": parser.has_mailto},
    }
