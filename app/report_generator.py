"""
Government inspection notice generator for the Legal Metrology compliance
platform (SIH26034).

Renders a :class:`~app.schemas.ComplianceReport` into a printable PDF suitable
for the file of a Legal Metrology Officer. The document is built entirely in
memory and returned as bytes -- nothing touches disk, so the API layer can
stream it straight to the caller and no notice is ever left behind on a server
that field officers share.

Evidentiary discipline carried through from the earlier phases
--------------------------------------------------------------
* **"Not declared" and "not legible" are printed differently.** A declaration
  the extractor flagged as present-but-unreadable is shown as such, never as
  absent. The first is a finding against the package; the second is a
  limitation of our photograph, and a notice that confuses them is indefensible.
* **Contraventions and unassessed provisions are separately tabulated.** The
  advisories the rules engine emits for provisions it could not evaluate are
  not contraventions and are never listed among them.
* **The verdict is never overstated.** ``PARTIALLY_COMPLIANT`` and
  ``INDETERMINATE`` are printed under their own headings rather than being
  rounded to "COMPLIANT" or "NON-COMPLIANT" -- rounding an unreadable image up
  to an adverse finding would put a false accusation on departmental letterhead.
* **Every page carries the case reference and the image digest**, so a page
  separated from the bundle can still be tied to the inspection it came from.

Typography note
---------------
ReportLab's built-in fonts are Latin-1 only: the rupee sign (U+20B9) and any
Devanagari in a transcribed declaration would render as blank boxes. This module
therefore registers a Unicode TrueType face when one can be found on the host
and sanitises text for the font actually in use, converting the rupee sign to
"Rs." and flagging any character it cannot represent rather than silently
dropping it. Complex-script shaping (Devanagari conjuncts) is beyond ReportLab's
text engine in any case, so where such text is present the notice says so and
directs the reader to the annexed transcript.
"""

from __future__ import annotations

import io
import logging
import unicodedata
from datetime import timezone
from pathlib import Path
from typing import Any, Final, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    Flowable,
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.schemas import (
    ComplianceReport,
    ComplianceStatus,
    ConsumerCareDeclaration,
    DateDeclaration,
    DeclarationField,
    EntityDeclaration,
    ExtractedLabelData,
    NetQuantityDeclaration,
    PARENT_ACT_NAME,
    PostalAddress,
    RetailSalePriceDeclaration,
    RuleViolation,
    Severity,
    TextDeclaration,
)

__all__ = ["generate_inspection_pdf", "build_case_reference"]

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Page geometry and palette                                                    #
# --------------------------------------------------------------------------- #

PAGE_SIZE: Final = A4
MARGIN_LEFT: Final[float] = 18 * mm
MARGIN_RIGHT: Final[float] = 18 * mm
MARGIN_TOP: Final[float] = 16 * mm
MARGIN_BOTTOM: Final[float] = 22 * mm
CONTENT_WIDTH: Final[float] = PAGE_SIZE[0] - MARGIN_LEFT - MARGIN_RIGHT

INK: Final = colors.HexColor("#111111")
MUTED: Final = colors.HexColor("#5A5A5A")
RULE_LINE: Final = colors.HexColor("#333333")
BAND: Final = colors.HexColor("#E8E8E8")
PANEL: Final = colors.HexColor("#F5F5F5")

COMPLIANT_COLOUR: Final = colors.HexColor("#1B5E20")
NON_COMPLIANT_COLOUR: Final = colors.HexColor("#8E1111")
QUALIFIED_COLOUR: Final = colors.HexColor("#8A5A00")

#: Verdict banner text, colour and gloss for each terminal status.
#:
#: ``COMPLIANT`` and ``NON-COMPLIANT`` appear exactly as the specification
#: requires. The remaining two statuses keep their own headings: a partially
#: compliant package has contraventions of a lesser grade or provisions that
#: went unexamined, and an indeterminate one has no verdict at all. Collapsing
#: either into the binary pair would misstate the finding on a document that
#: carries departmental authority.
_VERDICT: Final[dict[ComplianceStatus, tuple[str, Any, str]]] = {
    ComplianceStatus.COMPLIANT: (
        "COMPLIANT",
        COMPLIANT_COLOUR,
        "No contravention of the Rules was found in the declarations examined.",
    ),
    ComplianceStatus.NON_COMPLIANT: (
        "NON-COMPLIANT",
        NON_COMPLIANT_COLOUR,
        "Contraventions of the Rules were found, as particularised below.",
    ),
    ComplianceStatus.PARTIALLY_COMPLIANT: (
        "COMPLIANT IN PART",
        QUALIFIED_COLOUR,
        "Deviations of a minor grade were found, or one or more provisions "
        "could not be assessed from the material available.",
    ),
    ComplianceStatus.INDETERMINATE: (
        "NOT DETERMINED",
        QUALIFIED_COLOUR,
        "The submitted image could not be read reliably. No finding of "
        "compliance or contravention has been recorded.",
    ),
}

#: How each severity is captioned in the findings table.
_SEVERITY_CAPTION: Final[dict[Severity, str]] = {
    Severity.CRITICAL: "Critical",
    Severity.MAJOR: "Major",
    Severity.MINOR: "Minor",
    Severity.ADVISORY: "Advisory",
}

#: Candidate Unicode faces, in preference order. The first that exists on the
#: host is registered and used for body text.
_UNICODE_FONT_CANDIDATES: Final[tuple[tuple[str, str], ...]] = (
    ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ("DejaVuSans", "C:/Windows/Fonts/DejaVuSans.ttf"),
    ("ArialUnicode", "C:/Windows/Fonts/ARIALUNI.TTF"),
    ("Arial", "C:/Windows/Fonts/arial.ttf"),
    ("LiberationSans", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    ("Helvetica-Neue", "/System/Library/Fonts/Helvetica.ttc"),
)
_UNICODE_BOLD_CANDIDATES: Final[dict[str, tuple[str, ...]]] = {
    "DejaVuSans": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/DejaVuSans-Bold.ttf",
    ),
    "Arial": ("C:/Windows/Fonts/arialbd.ttf",),
    "LiberationSans": (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ),
}

#: Characters replaced before rendering, whatever the font in use. The rupee
#: sign is absent from the core fonts, and "Rs." is the form the Rules
#: themselves use, so the substitution loses nothing legally.
_TRANSLITERATIONS: Final[dict[str, str]] = {
    "\u20b9": "Rs. ",  # ₹
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-",
    "\u00a0": " ",
}

#: Substituted for any character the active font cannot draw. Deliberately
#: visible and deliberately ASCII: a reader must be able to see that something
#: stood on the label which this rendering could not reproduce, and a silent
#: deletion would quietly alter a transcript that may be read as evidence.
UNRENDERABLE_MARKER: Final[str] = "[?]"

#: Printed where a value is genuinely not on the package.
NOT_DECLARED: Final[str] = "Not declared on the package"
#: Printed where a value is on the package but our image could not resolve it.
NOT_LEGIBLE: Final[str] = "Present but not legible in the submitted image"


# --------------------------------------------------------------------------- #
# Fonts and text sanitisation                                                  #
# --------------------------------------------------------------------------- #


def _register_body_font() -> tuple[str, str, bool]:
    """
    Register a Unicode body face if one can be found on this host.

    Returns:
        ``(regular_font_name, bold_font_name, is_unicode)``. Falls back to the
        Helvetica core fonts, in which case ``is_unicode`` is False and text is
        reduced to Latin-1 before rendering.
    """
    for family, path in _UNICODE_FONT_CANDIDATES:
        try:
            if not Path(path).is_file():
                continue
            regular = f"LM-{family}"
            if regular not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(regular, path))

            bold = regular
            for bold_path in _UNICODE_BOLD_CANDIDATES.get(family, ()):
                if Path(bold_path).is_file():
                    bold = f"LM-{family}-Bold"
                    if bold not in pdfmetrics.getRegisteredFontNames():
                        pdfmetrics.registerFont(TTFont(bold, bold_path))
                    break
            logger.debug("Registered Unicode body font %s from %s", regular, path)
            return regular, bold, True
        except Exception:  # noqa: BLE001 - a bad font file must not stop a notice
            logger.warning("Could not register font %s at %s; trying next.", family, path)

    logger.info("No Unicode font available; falling back to Helvetica (Latin-1).")
    return "Helvetica", "Helvetica-Bold", False


BODY_FONT, BOLD_FONT, _FONT_IS_UNICODE = _register_body_font()


def _prepare(text: object) -> str:
    """
    Make a value renderable by the active font, without escaping it.

    Applies the transliterations above, then -- when only a core Latin-1 font is
    available -- replaces any remaining unrepresentable character with U+FFFD
    rather than dropping it. A visible replacement mark tells the reader that
    something was on the label which this rendering could not reproduce; a
    silent deletion would quietly alter transcribed evidence.

    Args:
        text: Any value; non-strings are stringified first.

    Returns:
        A font-safe string, still carrying any markup it began with.
    """
    value = "" if text is None else str(text)
    for source, replacement in _TRANSLITERATIONS.items():
        value = value.replace(source, replacement)

    if not _FONT_IS_UNICODE:
        # The marker must itself be drawable by the fallback font, so it is
        # plain ASCII rather than U+FFFD -- a replacement character the core
        # fonts cannot draw would defeat the whole point of the substitution.
        value = "".join(
            character if ord(character) < 256 else UNRENDERABLE_MARKER
            for character in value
        )

    return "".join(
        character
        for character in value
        if character in "\n\t" or not unicodedata.category(character).startswith("C")
    ).strip()


def _sanitise(text: object) -> str:
    """
    Make a value safe to render *and* inert as markup.

    Use for anything transcribed from the package or otherwise outside this
    module's control. ReportLab parses paragraph text as mini-XML, so an
    unescaped angle bracket in a declaration would either vanish or abort the
    render; escaping keeps the transcript faithful and the parse safe.

    Args:
        text: Any value; non-strings are stringified first.

    Returns:
        A font-safe string with XML metacharacters escaped.
    """
    value = _prepare(text)
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _has_unrenderable_text(report: ComplianceReport) -> bool:
    """
    Detect transcribed text this build cannot faithfully render.

    Complex scripts are the practical case: a Devanagari declaration will not
    shape correctly through ReportLab's text engine even with a Unicode face, so
    the notice must say so instead of presenting mangled glyphs as a transcript.
    """
    extracted = report.extracted_data
    if extracted is None:
        return False
    sample = " ".join(
        part
        for part in (extracted.raw_text_dump, extracted.extraction_notes)
        if part
    )
    return any(ord(character) > 0x0590 for character in sample)


# --------------------------------------------------------------------------- #
# Styles                                                                       #
# --------------------------------------------------------------------------- #


def _styles() -> dict[str, ParagraphStyle]:
    """Build the paragraph styles used throughout the notice."""
    base = getSampleStyleSheet()["BodyText"]
    common = {"fontName": BODY_FONT, "textColor": INK, "leading": 12}

    return {
        "office": ParagraphStyle(
            "office", parent=base, fontName=BOLD_FONT, fontSize=13.5, leading=17,
            alignment=TA_CENTER, textColor=INK, spaceAfter=1,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base, fontName=BODY_FONT, fontSize=8.6, leading=11.5,
            alignment=TA_CENTER, textColor=MUTED,
        ),
        "doctitle": ParagraphStyle(
            "doctitle", parent=base, fontName=BOLD_FONT, fontSize=10.6, leading=14,
            alignment=TA_CENTER, textColor=INK,
        ),
        "section": ParagraphStyle(
            "section", parent=base, fontName=BOLD_FONT, fontSize=9.6, leading=12.5,
            textColor=INK, spaceBefore=2, spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "body", parent=base, fontSize=8.4, alignment=TA_JUSTIFY, **common
        ),
        "cell": ParagraphStyle(
            "cell", parent=base, fontSize=8.2, alignment=TA_LEFT, **common
        ),
        "cell_bold": ParagraphStyle(
            "cell_bold", parent=base, fontName=BOLD_FONT, fontSize=8.2,
            leading=12, textColor=INK, alignment=TA_LEFT,
        ),
        "cell_muted": ParagraphStyle(
            "cell_muted", parent=base, fontName=BODY_FONT, fontSize=8.2,
            leading=12, textColor=MUTED, alignment=TA_LEFT,
        ),
        "verdict": ParagraphStyle(
            "verdict", parent=base, fontName=BOLD_FONT, fontSize=19, leading=23,
            alignment=TA_CENTER, textColor=colors.white,
        ),
        "verdict_gloss": ParagraphStyle(
            "verdict_gloss", parent=base, fontName=BODY_FONT, fontSize=8.4,
            leading=11.5, alignment=TA_CENTER, textColor=colors.white,
        ),
        "disclaimer": ParagraphStyle(
            "disclaimer", parent=base, fontName=BODY_FONT, fontSize=7.6,
            leading=10.2, alignment=TA_JUSTIFY, textColor=MUTED,
        ),
        "signature": ParagraphStyle(
            "signature", parent=base, fontName=BODY_FONT, fontSize=8.2,
            leading=11, alignment=TA_CENTER, textColor=INK,
        ),
    }


# --------------------------------------------------------------------------- #
# Value formatting                                                             #
# --------------------------------------------------------------------------- #


def build_case_reference(report: ComplianceReport) -> str:
    """
    Derive the case reference printed on the notice.

    Built deterministically from the report identifier and the date it was
    generated, so re-rendering the same report always yields the same case
    number and two different inspections can never collide.

    Args:
        report: The compliance report being rendered.

    Returns:
        A reference of the form ``LM/PCR/2026/09/1A2B3C4D``.
    """
    stamped = report.generated_at.astimezone(timezone.utc)
    suffix = report.report_id.hex[:8].upper()
    return f"LM/PCR/{stamped:%Y/%m}/{suffix}"


def _absence_text(report: ComplianceReport, *fields: DeclarationField) -> str:
    """
    Choose between "not declared" and "not legible" for an empty declaration.

    Args:
        report: The report under render.
        fields: The declaration identifiers this row covers.

    Returns:
        The phrase appropriate to why the value is missing.
    """
    extracted = report.extracted_data
    if extracted is not None and any(f in extracted.unreadable_fields for f in fields):
        return NOT_LEGIBLE
    return NOT_DECLARED


def _format_price(price: Optional[RetailSalePriceDeclaration]) -> str:
    """Render the retail sale price with its caption and tax wording."""
    if price is None or price.amount is None:
        return ""
    parts = [f"{price.label_prefix_as_printed or 'MRP'} Rs. {price.amount}"]
    if price.tax_declaration_text:
        parts.append(f"({price.tax_declaration_text})")
    elif price.is_inclusive_of_all_taxes:
        parts.append("(inclusive of all taxes)")
    else:
        parts.append("- no 'inclusive of all taxes' wording printed")
    return " ".join(parts)


def _format_quantity(quantity: Optional[NetQuantityDeclaration]) -> str:
    """
    Render the net quantity, preserving the unit exactly as printed.

    The printed abbreviation is retained alongside the normalised unit because
    the abbreviation itself is regulated: "500 gms" and "500 g" are the same
    quantity but not the same legal position.
    """
    if quantity is None or quantity.value is None:
        return ""
    printed = quantity.unit_as_printed or ""
    rendered = f"{quantity.value} {printed}".strip()
    if quantity.qualifier_as_printed:
        rendered = f"{quantity.qualifier_as_printed} {rendered}"
    if quantity.si_unit is not None and printed and printed != quantity.si_unit.value:
        rendered += f"  [normalised unit: {quantity.si_unit.value}]"
    if quantity.number_of_units_in_package:
        rendered += f"  [{quantity.number_of_units_in_package} units]"
    return rendered


def _format_date(declaration: Optional[DateDeclaration]) -> str:
    """Render a date declaration, keeping month/year-only labels honest."""
    if declaration is None:
        return ""
    caption = f"{declaration.date_label_as_printed}: " if declaration.date_label_as_printed else ""

    if declaration.has_month_and_year:
        if declaration.day is not None:
            return f"{caption}{declaration.day:02d}/{declaration.month:02d}/{declaration.year}"
        return f"{caption}{declaration.month:02d}/{declaration.year}"
    if declaration.relative_period_text:
        return f"{caption}{declaration.relative_period_text}"
    if declaration.relative_period_months is not None:
        return f"{caption}{declaration.relative_period_months} months from packing"
    if declaration.year is not None:
        return f"{caption}{declaration.year} (month not declared)"
    return declaration.raw_text or ""


def _format_address(address: Optional[PostalAddress]) -> str:
    """Render an address block, preferring the text as printed."""
    if address is None:
        return ""
    if address.full_address_as_printed:
        return address.full_address_as_printed
    parts = [
        address.street_line, address.city, address.district,
        address.state, address.pin_code, address.country,
    ]
    return ", ".join(part for part in parts if part)


def _format_entity(entity: Optional[EntityDeclaration]) -> str:
    """Render the declarant's name together with the role claimed on the label."""
    if entity is None or not entity.name:
        return ""
    if entity.role_prefix_as_printed:
        return f"{entity.role_prefix_as_printed} {entity.name}"
    if entity.role is not None:
        return f"{entity.name} ({entity.role.value})"
    return entity.name


def _format_consumer_care(care: Optional[ConsumerCareDeclaration]) -> str:
    """Render consumer care particulars, marking malformed entries as such."""
    if care is None:
        return ""
    lines: list[str] = []
    if care.contact_person_or_designation:
        lines.append(care.contact_person_or_designation)
    if care.phone_numbers:
        suffix = "" if care.has_valid_phone else "  [not a dialable number]"
        lines.append("Tel: " + ", ".join(care.phone_numbers) + suffix)
    if care.email_addresses:
        suffix = "" if care.has_valid_email else "  [malformed address]"
        lines.append("E-mail: " + ", ".join(care.email_addresses) + suffix)
    care_address = _format_address(care.address)
    if care_address:
        lines.append(care_address)
    return "\n".join(lines)


def _format_text(declaration: Optional[TextDeclaration]) -> str:
    """Render a plain text declaration."""
    if declaration is None or not declaration.value:
        return ""
    return declaration.value


# --------------------------------------------------------------------------- #
# Layout helpers                                                               #
# --------------------------------------------------------------------------- #


def _para(text: object, style: ParagraphStyle, *, markup: bool = False) -> Paragraph:
    """
    Build a paragraph, preserving newlines as line breaks.

    Args:
        text: The content.
        style: Paragraph style to apply.
        markup: ``False`` (the default) escapes the text, which is what any
            value transcribed from a package requires. ``True`` passes the
            string through with its tags intact, for captions and notes
            authored in this module -- the caller is then responsible for
            having run :func:`_sanitise` over every value it interpolated.

    Returns:
        A ReportLab paragraph ready to place in the story.
    """
    prepared = _prepare(text) if markup else _sanitise(text)
    return Paragraph(prepared.replace("\n", "<br/>"), style)


def _section_heading(number: int, title: str, styles: dict[str, ParagraphStyle]) -> Table:
    """Render a numbered section heading as a full-width banded row."""
    table = Table(
        [[_para(f"{number}.  {title.upper()}", styles["section"])]],
        colWidths=[CONTENT_WIDTH],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), BAND),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (0, 0), (-1, -1), 0.75, RULE_LINE),
            ]
        )
    )
    return table


def _key_value_table(
    rows: list[tuple[str, str]],
    styles: dict[str, ParagraphStyle],
    *,
    label_width: float = 0.30,
) -> Table:
    """
    Build a two-column label/value table.

    Values that were not declared or could not be read are rendered in the
    muted style, so an officer scanning the page can see at a glance which
    particulars are absent.
    """
    placeholders = {NOT_DECLARED, NOT_LEGIBLE}
    body = [
        [
            # Labels are authored here and carry <br/>; values are transcribed
            # from the package and must stay inert.
            _para(label, styles["cell_bold"], markup=True),
            _para(value, styles["cell_muted"] if value in placeholders else styles["cell"]),
        ]
        for label, value in rows
    ]
    table = Table(
        body,
        colWidths=[CONTENT_WIDTH * label_width, CONTENT_WIDTH * (1 - label_width)],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
                ("BACKGROUND", (0, 0), (0, -1), PANEL),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BBBBBB")),
            ]
        )
    )
    return table


# --------------------------------------------------------------------------- #
# Sections                                                                     #
# --------------------------------------------------------------------------- #


def _header_section(
    report: ComplianceReport, styles: dict[str, ParagraphStyle]
) -> list[Flowable]:
    """
    Section 1: departmental letterhead, case reference and inspection stamp.

    The image digest is printed here rather than buried in an annexure: it is
    what binds this notice to the exact photograph examined, and a notice whose
    subject matter cannot be identified is worth little in proceedings.
    """
    stamped = report.generated_at.astimezone(timezone.utc)
    flowables: list[Flowable] = [
        _para("GOVERNMENT OF INDIA", styles["subtitle"]),
        _para("OFFICE OF THE LEGAL METROLOGY DEPARTMENT", styles["office"]),
        _para(
            "Ministry of Consumer Affairs, Food and Public Distribution", styles["subtitle"]
        ),
        Spacer(1, 5),
        HRFlowable(width="100%", thickness=1.1, color=RULE_LINE, spaceAfter=5),
        _para(
            "PRELIMINARY INSPECTION REPORT -- PRE-PACKAGED COMMODITY LABEL<br/>"
            f"under the {_sanitise(report.statute_reference)}",
            styles["doctitle"],
            markup=True,
        ),
        Spacer(1, 7),
    ]

    identification: list[tuple[str, str]] = [
        ("Case reference", build_case_reference(report)),
        ("Report identifier", str(report.report_id)),
        ("Date and time of inspection", f"{stamped:%d %B %Y, %H:%M:%S} UTC"),
        ("Jurisdiction", report.jurisdiction or "Not specified"),
        ("Inspecting officer", report.inspector_id or "Not specified"),
        ("Sample image", report.image_filename or "Not supplied"),
        ("Image digest (SHA-256)", report.image_sha256),
    ]
    flowables.append(_key_value_table(identification, styles))
    return flowables


def _extracted_data_section(
    report: ComplianceReport, styles: dict[str, ParagraphStyle]
) -> list[Flowable]:
    """
    Section 2: the declarations as read from the package.

    Renders every mandatory particular whether or not it was found, because the
    absence of a declaration is itself the material fact in most of these
    inspections. Where no extraction exists at all, the section says so plainly
    instead of presenting an empty table as though the package bore nothing.
    """
    flowables: list[Flowable] = [
        Spacer(1, 9),
        _section_heading(2, "Declarations as extracted from the label", styles),
        Spacer(1, 5),
    ]

    extracted: Optional[ExtractedLabelData] = report.extracted_data
    if extracted is None:
        flowables.append(
            _para(
                "No declarations were extracted from the submitted image. The "
                "photograph could not be read with sufficient reliability, and "
                "accordingly no particulars are reproduced here and no finding "
                "has been recorded against the package.",
                styles["body"],
            )
        )
        if report.image_quality is not None and report.image_quality.quality_notes:
            flowables.extend(
                [
                    Spacer(1, 5),
                    _para(
                        "Measured defects: " + " ".join(report.image_quality.quality_notes),
                        styles["body"],
                    ),
                ]
            )
        return flowables

    entity = extracted.declared_entity
    rows: list[tuple[str, str]] = [
        (
            "Common or generic name<br/>[Rule 6(1)(b)]",
            _format_text(extracted.commodity_name)
            or _absence_text(report, DeclarationField.COMMODITY_NAME),
        ),
        ("Brand or trade name", _format_text(extracted.brand_name) or "Not declared"),
        (
            "Manufacturer / packer / importer<br/>[Rule 6(1)(a)]",
            _format_entity(entity) or _absence_text(report, DeclarationField.MANUFACTURER_NAME),
        ),
        (
            "Address of the declarant<br/>[Rule 6(1)(a)]",
            _format_address(entity.address if entity else None)
            or _absence_text(report, DeclarationField.MANUFACTURER_ADDRESS),
        ),
        (
            "Net quantity<br/>[Rule 6(1)(c)]",
            _format_quantity(extracted.net_quantity)
            or _absence_text(report, DeclarationField.NET_QUANTITY),
        ),
        (
            "Retail sale price<br/>[Rule 6(1)(e)]",
            _format_price(extracted.retail_sale_price)
            or _absence_text(
                report,
                DeclarationField.RETAIL_SALE_PRICE,
                DeclarationField.TAX_INCLUSIVE_DECLARATION,
            ),
        ),
        (
            "Month and year of manufacture<br/>[Rule 6(1)(d)]",
            _format_date(extracted.manufacture_date)
            or _absence_text(report, DeclarationField.MANUFACTURE_DATE),
        ),
        (
            "Month and year of pre-packing",
            _format_date(extracted.packing_date)
            or _absence_text(report, DeclarationField.PACKING_DATE),
        ),
        (
            "Best before / use by<br/>[Rule 6(1)(da)]",
            _format_date(extracted.best_before)
            or _absence_text(report, DeclarationField.BEST_BEFORE),
        ),
        (
            "Consumer care particulars<br/>[Rule 6(2)]",
            _format_consumer_care(extracted.consumer_care)
            or _absence_text(
                report,
                DeclarationField.CONSUMER_CARE_PHONE,
                DeclarationField.CONSUMER_CARE_EMAIL,
                DeclarationField.CONSUMER_CARE_ADDRESS,
            ),
        ),
    ]

    if extracted.is_imported_package:
        rows.extend(
            [
                (
                    "Importer<br/>[Rule 6(1)(a)]",
                    _format_entity(extracted.importer) or NOT_DECLARED,
                ),
                (
                    "Country of origin<br/>[Rule 6(1)(aa)]",
                    _format_text(extracted.country_of_origin)
                    or _absence_text(report, DeclarationField.COUNTRY_OF_ORIGIN),
                ),
            ]
        )

    rows.extend(
        [
            ("Batch / lot / code number", _format_text(extracted.batch_number) or NOT_DECLARED),
            (
                "Language of the declarations<br/>[Rule 9(4)]",
                ", ".join(extracted.languages_detected) or "Could not be established",
            ),
        ]
    )
    if extracted.fssai_licence_number is not None and extracted.fssai_licence_number.value:
        rows.append(
            (
                "FSSAI licence number",
                f"{extracted.fssai_licence_number.value}  "
                "[recorded for information; governed by the Food Safety and "
                "Standards Act, 2006]",
            )
        )

    flowables.append(_key_value_table(rows, styles, label_width=0.34))

    if extracted.unreadable_fields:
        unreadable = ", ".join(
            field.value.replace("_", " ") for field in extracted.unreadable_fields
        )
        flowables.extend(
            [
                Spacer(1, 5),
                _para(
                    "<b>Particulars present on the package but not legible in the "
                    f"submitted image:</b> {_sanitise(unreadable)}. These have not "
                    "been assessed and no finding has been recorded in respect of "
                    "them.",
                    styles["body"],
                    markup=True,
                ),
            ]
        )
    if extracted.extraction_notes:
        flowables.extend(
            [
                Spacer(1, 4),
                _para(
                    "<b>Observations on capture:</b> "
                    f"{_sanitise(extracted.extraction_notes)}",
                    styles["body"],
                    markup=True,
                ),
            ]
        )
    if _has_unrenderable_text(report):
        flowables.extend(
            [
                Spacer(1, 4),
                _para(
                    "<b>Note:</b> the package carries declarations in a script this "
                    "rendering cannot reproduce faithfully. Those declarations must "
                    "be read from the original photograph, which is annexed to this "
                    "report by its digest recorded above.",
                    styles["body"],
                    markup=True,
                ),
            ]
        )
    return flowables


def _findings_section(
    report: ComplianceReport, styles: dict[str, ParagraphStyle]
) -> list[Flowable]:
    """
    Section 3: the contraventions found, and separately the provisions that
    could not be assessed.

    The two are never merged. An advisory recording that a provision went
    unexamined is not a contravention, and listing it among them would inflate
    the case against the manufacturer. Where nothing was found, the section
    states that affirmatively rather than being omitted.
    """
    flowables: list[Flowable] = [
        Spacer(1, 9),
        _section_heading(3, "Compliance findings", styles),
        Spacer(1, 5),
    ]

    contraventions = [v for v in report.violations if v.severity is not Severity.ADVISORY]
    unassessed = [v for v in report.violations if v.severity is Severity.ADVISORY]

    if contraventions:
        flowables.append(_violations_table(contraventions, styles))
    else:
        flowables.append(
            _para(
                "No contravention of the "
                f"{report.statute_reference} was found in the declarations "
                "examined. Every mandatory particular that could be assessed was "
                "found to be present and in the form prescribed.",
                styles["body"],
            )
        )

    if unassessed:
        flowables.extend(
            [
                Spacer(1, 8),
                _para(
                    "Provisions not assessed (no finding recorded)", styles["section"]
                ),
                Spacer(1, 3),
                _para(
                    "The following provisions could not be evaluated from the "
                    "material available. They are recorded for completeness and do "
                    "not constitute findings against the package.",
                    styles["body"],
                ),
                Spacer(1, 4),
                _unassessed_table(unassessed, styles),
            ]
        )
    return flowables


def _violations_table(
    violations: list[RuleViolation], styles: dict[str, ParagraphStyle]
) -> Table:
    """
    Tabulate the contraventions, one row each.

    Every row carries the provision contravened, what was observed, what the
    provision required and the penal section -- the four things a notice must
    state for the recipient to be able to answer it.
    """
    header = [
        _para("S.No.", styles["cell_bold"]),
        _para("Provision contravened", styles["cell_bold"]),
        _para("Finding", styles["cell_bold"]),
        _para("Grade", styles["cell_bold"]),
    ]
    body: list[list[Flowable]] = [header]

    for index, violation in enumerate(violations, start=1):
        detail = f"<b>{_sanitise(violation.title)}.</b> {_sanitise(violation.description)}"
        if violation.observed_value:
            detail += f"<br/><i>As printed on the package:</i> \u201c{_sanitise(violation.observed_value)}\u201d"
        detail += f"<br/><i>Required:</i> {_sanitise(violation.expected_requirement)}"
        if violation.penalty_reference:
            detail += f"<br/><i>Penal provision:</i> {_sanitise(violation.penalty_reference)}"

        body.append(
            [
                _para(f"{index}.", styles["cell"]),
                _para(
                    f"<b>{_sanitise(violation.rule_reference)}</b><br/>"
                    f"{_sanitise(violation.statute)}",
                    styles["cell"],
                    markup=True,
                ),
                _para(detail, styles["cell"], markup=True),
                _para(_SEVERITY_CAPTION[violation.severity], styles["cell"]),
            ]
        )

    table = Table(
        body,
        colWidths=[
            CONTENT_WIDTH * 0.06,
            CONTENT_WIDTH * 0.22,
            CONTENT_WIDTH * 0.60,
            CONTENT_WIDTH * 0.12,
        ],
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), BAND),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BBBBBB")),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _unassessed_table(
    advisories: list[RuleViolation], styles: dict[str, ParagraphStyle]
) -> Table:
    """Tabulate the provisions that could not be evaluated."""
    body: list[list[Flowable]] = [
        [
            _para("Provision", styles["cell_bold"]),
            _para("Reason not assessed", styles["cell_bold"]),
        ]
    ]
    for advisory in advisories:
        body.append(
            [
                _para(advisory.rule_reference, styles["cell"]),
                _para(advisory.description, styles["cell_muted"]),
            ]
        )

    table = Table(
        body,
        colWidths=[CONTENT_WIDTH * 0.24, CONTENT_WIDTH * 0.76],
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), PANEL),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ]
        )
    )
    return table


def _verdict_section(
    report: ComplianceReport, styles: dict[str, ParagraphStyle]
) -> list[Flowable]:
    """
    Section 4: the verdict banner and the summary beneath it.

    Kept together as one block so the banner is never orphaned from the summary
    that qualifies it across a page break.
    """
    caption, colour, gloss = _VERDICT[report.status]

    banner = Table(
        [
            [_para(caption, styles["verdict"])],
            [_para(gloss, styles["verdict_gloss"])],
        ],
        colWidths=[CONTENT_WIDTH],
    )
    banner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colour),
                ("TOPPADDING", (0, 0), (0, 0), 9),
                ("BOTTOMPADDING", (0, 0), (0, 0), 1),
                ("TOPPADDING", (0, 1), (0, 1), 0),
                ("BOTTOMPADDING", (0, 1), (0, 1), 9),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )

    particulars: list[tuple[str, str]] = [
        ("Contraventions recorded", str(report.violation_count - _advisory_count(report))),
        ("Of which mandatory declarations wholly absent", str(report.critical_violation_count)),
        ("Provisions not assessed", str(_advisory_count(report))),
        ("Conformity score", f"{report.compliance_score:.1f} / 100"),
    ]

    block: list[Flowable] = [
        Spacer(1, 11),
        _section_heading(4, "Verdict", styles),
        Spacer(1, 6),
        banner,
        Spacer(1, 6),
        _key_value_table(particulars, styles, label_width=0.46),
    ]
    if report.summary:
        block.extend([Spacer(1, 5), _para(report.summary, styles["body"])])
    return [KeepTogether(block)]


def _advisory_count(report: ComplianceReport) -> int:
    """Count the entries that record an unassessed provision rather than a finding."""
    return sum(1 for v in report.violations if v.severity is Severity.ADVISORY)


def _closing_section(
    report: ComplianceReport, styles: dict[str, ParagraphStyle]
) -> list[Flowable]:
    """
    Section 5: the statutory disclaimer and the officer's signature block.

    The disclaimer is not boilerplate here: this document is produced by an
    automated reading of a photograph, and it must not be mistaken for a notice
    issued on a physical examination of the package.
    """
    disclaimer = (
        "<b>Disclaimer.</b> This is a system-generated preliminary report. The "
        "declarations reproduced above were read from a photograph by an "
        "automated vision system and evaluated against the "
        f"{_sanitise(report.statute_reference)} by a deterministic rules engine. It "
        "is subject to manual verification by the inspecting officer and does "
        "not by itself constitute a notice, a determination, or the initiation "
        "of proceedings under the "
        f"{_sanitise(PARENT_ACT_NAME)}. No inference adverse to the manufacturer, "
        "packer or importer may be drawn from any particular recorded as not "
        "legible or not assessed. Findings must be confirmed against the "
        "physical package before any action is taken."
    )
    if report.status is ComplianceStatus.INDETERMINATE:
        disclaimer += (
            " <b>This report records no finding of any kind: the submitted "
            "image could not be read.</b>"
        )

    provenance = (
        f"Vision model: {report.vision_model_id or 'not recorded'}  |  "
        f"Rules engine: v{report.rules_engine_version or 'not recorded'}  |  "
        f"Processing time: "
        + (
            f"{report.processing_time_ms:.0f} ms"
            if report.processing_time_ms is not None
            else "not recorded"
        )
    )

    signature = Table(
        [
            [_para("", styles["signature"]), _para("", styles["signature"])],
            [
                _para("Signature of the Inspecting Officer", styles["signature"]),
                _para("Office seal", styles["signature"]),
            ],
            [
                _para(
                    f"Name / ID: {report.inspector_id or '________________________'}",
                    styles["signature"],
                ),
                _para("Date: ________________________", styles["signature"]),
            ],
        ],
        colWidths=[CONTENT_WIDTH * 0.5, CONTENT_WIDTH * 0.5],
        rowHeights=[26 * mm * 0.62, None, None],
    )
    signature.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                ("LINEBELOW", (0, 0), (0, 0), 0.7, INK),
                ("LINEBELOW", (1, 0), (1, 0), 0.7, INK),
                ("TOPPADDING", (0, 1), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 1),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ]
        )
    )

    return [
        Spacer(1, 11),
        _section_heading(5, "Declaration and attestation", styles),
        Spacer(1, 5),
        _para(disclaimer, styles["disclaimer"], markup=True),
        Spacer(1, 4),
        _para(provenance, styles["disclaimer"]),
        Spacer(1, 12),
        KeepTogether([signature]),
    ]


# --------------------------------------------------------------------------- #
# Page furniture                                                               #
# --------------------------------------------------------------------------- #


class _NumberedCanvas(Canvas):
    """
    Canvas that stamps a running footer with "Page X of Y".

    The total page count is unknown until the document is fully laid out, so
    pages are held and their state replayed at save time -- the standard
    ReportLab approach to a two-pass footer.
    """

    def __init__(self, *args: Any, footer_left: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._footer_left = footer_left
        self._saved_states: list[dict[str, Any]] = []

    def showPage(self) -> None:  # noqa: N802 - ReportLab's API
        """Defer the page so its footer can be stamped once the total is known."""
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        """Stamp every held page with the footer, then write the document."""
        total = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total_pages: int) -> None:
        """Draw the separator rule, the case reference and the page number."""
        width, _ = PAGE_SIZE
        y = MARGIN_BOTTOM - 8 * mm

        self.saveState()
        self.setStrokeColor(RULE_LINE)
        self.setLineWidth(0.5)
        self.line(MARGIN_LEFT, y + 6 * mm, width - MARGIN_RIGHT, y + 6 * mm)

        self.setFont(BODY_FONT, 7)
        self.setFillColor(MUTED)
        self.drawString(MARGIN_LEFT, y + 2.6 * mm, self._footer_left)
        self.drawRightString(
            width - MARGIN_RIGHT,
            y + 2.6 * mm,
            f"Page {self._pageNumber} of {total_pages}",
        )
        self.drawCentredString(
            width / 2.0,
            y - 0.6 * mm,
            "System-generated preliminary report -- subject to manual verification",
        )
        self.restoreState()


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #


def generate_inspection_pdf(report: ComplianceReport) -> bytes:
    """
    Render a compliance report as a printable government inspection notice.

    The document is assembled entirely in memory. Layout, top to bottom:
    departmental letterhead with the case reference and image digest; the
    declarations as extracted from the label; the contraventions found and,
    separately, any provisions that could not be assessed; the verdict banner
    with its summary; and the disclaimer and signature block.

    Every state of a report renders a complete document. A report with no
    contraventions still carries a full findings section stating affirmatively
    that none were found, and a report whose image could not be read renders
    with the declarations section explaining why it is empty and a verdict of
    "NOT DETERMINED" -- never a blank page and never an adverse finding by
    omission.

    Args:
        report: The compliance report to render. Its ``extracted_data`` may be
            ``None``, and its ``violations`` may be empty.

    Returns:
        The complete PDF as raw bytes, ready to be streamed as an HTTP response
        body. Nothing is written to disk.
    """
    styles = _styles()
    buffer = io.BytesIO()
    case_reference = build_case_reference(report)

    document = SimpleDocTemplate(
        buffer,
        pagesize=PAGE_SIZE,
        leftMargin=MARGIN_LEFT,
        rightMargin=MARGIN_RIGHT,
        topMargin=MARGIN_TOP,
        bottomMargin=MARGIN_BOTTOM,
        title=f"Legal Metrology Inspection Report {case_reference}",
        author="Office of the Legal Metrology Department",
        subject=f"Preliminary inspection under the {report.statute_reference}",
        creator="Legal Metrology Compliance Scanner",
    )

    story: list[Flowable] = []
    story.extend(_header_section(report, styles))
    story.extend(_extracted_data_section(report, styles))
    story.extend(_findings_section(report, styles))
    story.extend(_verdict_section(report, styles))
    story.extend(_closing_section(report, styles))

    footer_left = f"{case_reference}  |  Image SHA-256 {report.image_sha256[:16]}..."

    def _canvas_factory(*args: Any, **kwargs: Any) -> _NumberedCanvas:
        """Bind the running footer text to each canvas the platypus engine builds."""
        return _NumberedCanvas(*args, footer_left=footer_left, **kwargs)

    document.build(story, canvasmaker=_canvas_factory)
    return buffer.getvalue()
