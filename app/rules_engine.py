"""
Deterministic rules engine for the Legal Metrology (Packaged Commodities)
Rules, 2011, as amended up to G.S.R. (E) dated 31.10.2021 (w.e.f. 01.04.2022).

No model, no probability, no inference: every finding produced here is a pure
function of the extracted declarations and the statutory text. The same label
evaluated twice yields byte-identical violations, which is what allows a notice
issued from this system to be defended -- an officer can re-run the evaluation
and reproduce the finding exactly.

Provisions implemented
----------------------
* **Rule 6(1)(a)** -- name and address of manufacturer / packer / importer,
  with Explanation III (food articles fall to the FSS Act, 2006 instead).
* **Rule 6(1)(aa)** -- country of origin for imported products.
* **Rule 6(1)(b)** -- common or generic name of the commodity.
* **Rule 6(1)(c)** -- net quantity in standard units.
* **Rule 6(1)(d)** -- month and year of manufacture, with the food, seed and
  cosmetics provisos.
* **Rule 6(1)(da)** -- best before / use by date for perishable commodities.
* **Rule 6(1)(e)** read with **Rule 2(m)** -- retail sale price declared as the
  maximum retail price, inclusive of all taxes, in Indian currency.
* **Rule 6(2)** -- consumer care name, address, telephone and e-mail.
* **Rule 7(1)-(4)** with Table-I -- principal display panel area and the
  minimum height and width of numerals and letters.
* **Rule 9(4)** -- declarations in Hindi (Devanagari) or English.
* **Rule 12(6)** -- no exaggerated or misleading quantity qualifiers.
* **Rule 13(2)-(3)** -- correct unit magnitude for the quantity declared.
* **Rule 13(4)** -- dozen, score, gross and the like prohibited.
* **Rule 13(5)** -- no system of units other than the International System.

Two disciplines run through every check
---------------------------------------
1. **Absent is not unreadable.** A declaration the extractor flagged in
   ``unreadable_fields`` is *not assessed*: it yields an ADVISORY recording
   that the check could not be performed, never a contravention. Charging a
   manufacturer because our camera caught a glare would be indefensible.
2. **Doubt favours the accused.** Measurement comparisons carry an explicit
   tolerance, non-mandatory observations are graded ADVISORY, and a rule the
   engine cannot fully evaluate is reported as unassessed rather than passed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Final, Optional

from app.schemas import (
    ComplianceReport,
    ComplianceStatus,
    ContainerSurface,
    DeclarationField,
    EvaluationContext,
    ExtractedLabelData,
    ImageQualityMetrics,
    PackageCategory,
    QuantityKind,
    RuleViolation,
    SIUnit,
    Severity,
    STATUTE_NAME,
)

__all__ = [
    "RULES_ENGINE_VERSION",
    "RulesEngine",
    "evaluate_compliance",
    "TABLE_I",
]

RULES_ENGINE_VERSION: Final[str] = "1.0.0"

#: General penal provision for a package not conforming to the declarations.
PENALTY_SECTION_36: Final[str] = "Section 36(1) of the Legal Metrology Act, 2009"
#: Compounding of offences.
COMPOUNDING_SECTION: Final[str] = "Section 48 of the Legal Metrology Act, 2009"

#: Millimetre tolerance applied to every Rule 7 comparison. Character height
#: measured off a physical package carries real uncertainty, and the benefit of
#: that uncertainty belongs to the person facing the notice.
MEASUREMENT_TOLERANCE_MM: Final[float] = 0.1

# --------------------------------------------------------------------------- #
# Table-I to Rule 7                                                            #
# --------------------------------------------------------------------------- #

#: ``(upper bound of PDP area in cm2, min height printed mm, min height when
#: blown/formed/moulded mm)``. Bounds are inclusive upper limits, matching the
#: "A <= 50", "50 < A <= 100" form of the table as substituted w.e.f.
#: 01.04.2022. The final row is open-ended.
TABLE_I: Final[tuple[tuple[float, float, float], ...]] = (
    (50.0, 1.0, 1.5),
    (100.0, 1.5, 3.0),
    (500.0, 2.5, 4.0),
    (2500.0, 4.0, 6.0),
    (math.inf, 6.0, 6.0),
)

#: Rule 7(1): a package of ten cubic centimetres or less may carry its
#: principal display panel on an affixed card or tape.
SMALL_PACKAGE_CAPACITY_CM3: Final[float] = 10.0

#: Rule 7(3): minimum width of a character as a fraction of its height.
MIN_WIDTH_TO_HEIGHT_RATIO: Final[float] = 1.0 / 3.0

# --------------------------------------------------------------------------- #
# Lexical tables                                                               #
# --------------------------------------------------------------------------- #

#: Unit symbols lawful under the International System. Compared case-sensitively
#: for the litre (both "l" and "L" are accepted) and case-insensitively
#: elsewhere, since "Kg" for "kg" is a typographic slip rather than a different
#: system of units.
LAWFUL_UNIT_SYMBOLS: Final[frozenset[str]] = frozenset(
    {
        "mg", "g", "kg",
        "ul", "ml", "l", "L", "mL",
        "mm", "cm", "m", "km",
        "cm2", "dm2", "m2", "cm3", "dm3", "m3",
        "N",
    }
)

#: Unit words that are lawful when spelled out in full.
LAWFUL_UNIT_WORDS: Final[frozenset[str]] = frozenset(
    {
        "milligram", "milligrams", "gram", "grams", "kilogram", "kilograms",
        "millilitre", "millilitres", "milliliter", "milliliters",
        "litre", "litres", "liter", "liters",
        "millimetre", "millimetres", "centimetre", "centimetres", "metre",
        "metres", "meter", "meters",
        "number", "unit", "units", "piece", "pieces", "pair", "pairs", "set",
        "sets",
    }
)

#: Rule 13(4): number denominations that may not be used on any package.
PROHIBITED_NUMBER_WORDS: Final[tuple[str, ...]] = (
    "dozen", "score", "gross", "great gross",
)

#: Rule 12(6): qualifiers that create an exaggerated, misleading or inadequate
#: impression of the quantity contained in the package.
MISLEADING_QUANTITY_TERMS: Final[tuple[str, ...]] = (
    "minimum", "not less than", "average", "about", "approximately", "approx",
    "min.", "upto", "up to",
)

#: Wording that satisfies the Rule 6(1)(e) requirement to state that the price
#: is the maximum retail price inclusive of all taxes.
_TAX_INCLUSIVE_RE: Final[re.Pattern[str]] = re.compile(
    r"incl(?:usive|\.)?\s*(?:of)?\s*all\s*tax", re.IGNORECASE
)
#: Captions that identify the figure as the *maximum* retail price.
_MRP_CAPTION_RE: Final[re.Pattern[str]] = re.compile(
    r"\bm\.?\s?r\.?\s?p\.?\b|maximum\s+retail\s+price", re.IGNORECASE
)
#: Indian currency tokens.
_INR_RE: Final[re.Pattern[str]] = re.compile(r"₹|\bRs\.?\b|\bINR\b|\bRupees?\b", re.IGNORECASE)

#: Devanagari code block, for the Rule 9(4) language test.
_DEVANAGARI_RE: Final[re.Pattern[str]] = re.compile(r"[ऀ-ॿ]")

#: Severity weights used to derive the compliance score.
_SCORE_PENALTY: Final[dict[Severity, float]] = {
    Severity.CRITICAL: 15.0,
    Severity.MAJOR: 8.0,
    Severity.MINOR: 3.0,
    Severity.ADVISORY: 0.0,
}


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _violation(
    *,
    field: DeclarationField,
    rule: str,
    title: str,
    description: str,
    severity: Severity,
    expected: str,
    observed: Optional[str] = None,
    penalty: Optional[str] = PENALTY_SECTION_36,
    remediation: Optional[str] = None,
) -> RuleViolation:
    """Construct a violation with the statute reference already attached."""
    return RuleViolation(
        field=field,
        rule_reference=rule,
        statute=STATUTE_NAME,
        title=title,
        description=description,
        severity=severity,
        observed_value=observed,
        expected_requirement=expected,
        penalty_reference=penalty,
        suggested_remediation=remediation,
    )


def _not_assessed(
    field: DeclarationField, rule: str, reason: str
) -> RuleViolation:
    """
    Record that a check could not be performed.

    Graded ADVISORY and carrying no penalty reference, so it contributes
    nothing to the compliance score and can never be mistaken for a finding
    against the manufacturer. It exists so the report states plainly what was
    *not* examined -- silence on an unexamined declaration would be misleading.
    """
    return _violation(
        field=field,
        rule=rule,
        title=f"{rule}: not assessed",
        description=reason,
        severity=Severity.ADVISORY,
        expected="Assessment deferred; no finding recorded on this declaration.",
        penalty=None,
        remediation="Re-capture the declaration panel, or record the measurement manually.",
    )


def _unreadable(data: ExtractedLabelData, *fields: DeclarationField) -> bool:
    """True if any of ``fields`` was reported present-but-unreadable."""
    return any(field in data.unreadable_fields for field in fields)


def _text_of(*candidates: Optional[str]) -> str:
    """Join the non-empty candidates into one lowercase haystack."""
    return " ".join(c for c in candidates if c).lower()


def _blank(value: Optional[str]) -> bool:
    """True when a string is absent or contains only whitespace."""
    return value is None or not value.strip()


# --------------------------------------------------------------------------- #
# Rule 6 -- mandatory declarations                                             #
# --------------------------------------------------------------------------- #


def check_manufacturer_identity(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 6(1)(a): name and complete address of the manufacturer, or of the
    manufacturer and the packer where they differ, or of the importer for an
    imported package.

    Explanation III disapplies this clause to packages containing food
    articles, for which the Food Safety and Standards Act, 2006 governs. For
    those the engine records an advisory rather than a contravention -- the
    declaration is still expected, but not under these Rules.
    """
    is_food = ctx.category is PackageCategory.FOOD
    rule = "Rule 6(1)(a)"

    if _unreadable(data, DeclarationField.MANUFACTURER_NAME, DeclarationField.MANUFACTURER_ADDRESS):
        return [
            _not_assessed(
                DeclarationField.MANUFACTURER_NAME,
                rule,
                "The manufacturer / packer / importer block was present on the "
                "package but could not be read reliably from the image.",
            )
        ]

    entity = data.declared_entity
    findings: list[RuleViolation] = []

    if entity is None or _blank(entity.name):
        findings.append(
            _violation(
                field=DeclarationField.MANUFACTURER_NAME,
                rule=rule,
                title="Name of manufacturer, packer or importer not declared",
                description=(
                    "The package does not bear the name of the manufacturer, or "
                    "where the manufacturer is not the packer, of the manufacturer "
                    "and the packer, or for an imported package of the importer."
                ),
                severity=Severity.ADVISORY if is_food else Severity.CRITICAL,
                expected=(
                    "Every package shall bear the name of the manufacturer, packer "
                    "or importer as required by Rule 6(1)(a)."
                    + (
                        " Explanation III: for food articles this requirement is "
                        "governed by the Food Safety and Standards Act, 2006."
                        if is_food
                        else ""
                    )
                ),
                penalty=None if is_food else PENALTY_SECTION_36,
                remediation="Print the declarant's registered name on the principal display panel.",
            )
        )
        return findings

    address = entity.address
    if address is None or _blank(address.full_address_as_printed):
        findings.append(
            _violation(
                field=DeclarationField.MANUFACTURER_ADDRESS,
                rule=rule,
                title="Address of manufacturer, packer or importer not declared",
                description=(
                    f"The package declares '{entity.name}' but bears no address "
                    "for that entity."
                ),
                severity=Severity.ADVISORY if is_food else Severity.CRITICAL,
                observed=entity.name,
                expected="The name shall be accompanied by the complete address of the declarant.",
                penalty=None if is_food else PENALTY_SECTION_36,
                remediation="Print the complete address, including PIN code, alongside the name.",
            )
        )
    elif not address.is_complete:
        findings.append(
            _violation(
                field=DeclarationField.MANUFACTURER_ADDRESS,
                rule=rule,
                title="Address of manufacturer, packer or importer is incomplete",
                description=(
                    "The address declared on the package lacks one or more of the "
                    "particulars needed to identify and reach the declarant "
                    "(street or premises, town or city, and postal index number)."
                ),
                severity=Severity.ADVISORY if is_food else Severity.MAJOR,
                observed=address.full_address_as_printed,
                expected="A complete address sufficient to serve process on the declarant.",
                penalty=None if is_food else PENALTY_SECTION_36,
                remediation="Add the missing address particulars, including the six-digit PIN code.",
            )
        )

    return findings


def check_country_of_origin(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 6(1)(aa): the country of origin, manufacture or assembly shall be
    declared on imported products.

    Applies only where the package is established as imported -- explicitly by
    the caller, or by the presence of an importer declaration on the label.
    """
    is_imported = ctx.is_imported if ctx.is_imported is not None else data.is_imported_package
    if not is_imported:
        return []

    rule = "Rule 6(1)(aa)"
    if _unreadable(data, DeclarationField.COUNTRY_OF_ORIGIN):
        return [
            _not_assessed(
                DeclarationField.COUNTRY_OF_ORIGIN,
                rule,
                "A country-of-origin declaration appears on the package but could not be read.",
            )
        ]

    origin = data.country_of_origin
    if origin is not None and not _blank(origin.value):
        return []

    return [
        _violation(
            field=DeclarationField.COUNTRY_OF_ORIGIN,
            rule=rule,
            title="Country of origin not declared on an imported package",
            description=(
                "The package is an imported product but does not bear the name of "
                "the country of origin, manufacture or assembly."
            ),
            severity=Severity.CRITICAL,
            expected="Imported packages shall declare the country of origin, manufacture or assembly.",
            remediation="Print 'Country of Origin: <country>' on the principal display panel.",
        )
    ]


def check_commodity_name(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 6(1)(b): the common or generic name of the commodity contained in the
    package.

    A brand name is not a substitute: "Kurkure" identifies a trade source,
    "extruded snack" identifies the commodity, and the clause requires the
    latter.
    """
    rule = "Rule 6(1)(b)"
    if _unreadable(data, DeclarationField.COMMODITY_NAME):
        return [
            _not_assessed(
                DeclarationField.COMMODITY_NAME,
                rule,
                "The commodity name appears on the package but could not be read.",
            )
        ]

    name = data.commodity_name
    if name is not None and not _blank(name.value):
        return []

    observed = data.brand_name.value if data.brand_name else None
    return [
        _violation(
            field=DeclarationField.COMMODITY_NAME,
            rule=rule,
            title="Common or generic name of the commodity not declared",
            description=(
                "The package does not bear the common or generic name of the "
                "commodity it contains."
                + (
                    f" Only the brand name '{observed}' was found, which does not "
                    "discharge this requirement."
                    if observed
                    else ""
                )
            ),
            severity=Severity.CRITICAL,
            observed=observed,
            expected="Every package shall declare the common or generic name of the commodity.",
            remediation="Add the generic description of the commodity to the principal display panel.",
        )
    ]


def check_net_quantity_declared(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 6(1)(c): the net quantity in terms of the standard unit of weight or
    measure, or the number of commodities where sold by number.
    """
    rule = "Rule 6(1)(c)"
    if _unreadable(data, DeclarationField.NET_QUANTITY):
        return [
            _not_assessed(
                DeclarationField.NET_QUANTITY,
                rule,
                "The net quantity declaration is present but could not be read.",
            )
        ]

    quantity = data.net_quantity
    if quantity is not None and quantity.value is not None and not _blank(quantity.unit_as_printed):
        return []

    findings = [
        _violation(
            field=DeclarationField.NET_QUANTITY,
            rule=rule,
            title="Net quantity not declared",
            description=(
                "The package does not bear a net quantity declaration in terms of "
                "the standard unit of weight or measure, or of number where the "
                "commodity is sold by number."
            ),
            severity=Severity.CRITICAL,
            observed=quantity.raw_text if quantity else None,
            expected="Net quantity shall be declared in standard units on the principal display panel.",
            remediation="Declare the net quantity, e.g. 'Net Quantity: 500 g'.",
        )
    ]
    return findings


def check_manufacture_date(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 6(1)(d), as substituted on 31.10.2021: the month and year in which the
    commodity was manufactured.

    The day is not required. The provisos defer to the food, seed and cosmetics
    regimes, so those categories yield an advisory rather than a contravention
    under these Rules.
    """
    rule = "Rule 6(1)(d)"
    deferred_to = {
        PackageCategory.FOOD: "the Food Safety and Standards Act, 2006",
        PackageCategory.SEED: "the Seeds Act, 1966",
        PackageCategory.COSMETIC: "the Drugs and Cosmetics Rules, 1945",
    }.get(ctx.category)

    if _unreadable(
        data, DeclarationField.MANUFACTURE_DATE, DeclarationField.PACKING_DATE
    ):
        return [
            _not_assessed(
                DeclarationField.MANUFACTURE_DATE,
                rule,
                "A date declaration is present on the package but could not be read.",
            )
        ]

    # Either declaration discharges the clause in practice; enforcement accepts
    # a pre-packing date where manufacture and packing coincide.
    for declaration in (data.manufacture_date, data.packing_date):
        if declaration is not None and declaration.has_month_and_year:
            return []

    partial = next(
        (d for d in (data.manufacture_date, data.packing_date) if d is not None), None
    )
    if partial is not None and partial.raw_text:
        return [
            _violation(
                field=DeclarationField.MANUFACTURE_DATE,
                rule=rule,
                title="Date of manufacture incomplete",
                description=(
                    "A date is printed on the package but does not state both the "
                    "month and the year of manufacture as the clause requires."
                ),
                severity=Severity.ADVISORY if deferred_to else Severity.MAJOR,
                observed=partial.raw_text,
                expected=(
                    "The month and year of manufacture shall be declared, in words, "
                    "in numerals, or in both."
                ),
                penalty=None if deferred_to else PENALTY_SECTION_36,
                remediation="Print the month and year, e.g. 'MFD: 03/2024'.",
            )
        ]

    return [
        _violation(
            field=DeclarationField.MANUFACTURE_DATE,
            rule=rule,
            title="Month and year of manufacture not declared",
            description=(
                "The package does not bear the month and year in which the "
                "commodity was manufactured."
                + (
                    f" Note: for this category the requirement is governed by {deferred_to}."
                    if deferred_to
                    else ""
                )
            ),
            severity=Severity.ADVISORY if deferred_to else Severity.CRITICAL,
            expected="The month and year of manufacture shall be declared on the package.",
            penalty=None if deferred_to else PENALTY_SECTION_36,
            remediation="Print the month and year of manufacture on the principal display panel.",
        )
    ]


def check_best_before(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 6(1)(da): where a commodity may become unfit for human consumption
    after a period of time, the best before or use by date, month and year.

    Asserted only where perishability has been established by the caller.
    Inferring it from the commodity name would be guesswork with penal
    consequences, so an unestablished perishability yields no finding at all.
    """
    if not ctx.is_perishable:
        return []

    rule = "Rule 6(1)(da)"
    if _unreadable(data, DeclarationField.BEST_BEFORE):
        return [
            _not_assessed(
                DeclarationField.BEST_BEFORE,
                rule,
                "A best-before declaration is present but could not be read.",
            )
        ]

    best_before = data.best_before
    if best_before is not None and (
        best_before.has_month_and_year
        or best_before.relative_period_months is not None
        or not _blank(best_before.relative_period_text)
    ):
        return []

    return [
        _violation(
            field=DeclarationField.BEST_BEFORE,
            rule=rule,
            title="Best before or use by date not declared",
            description=(
                "The commodity may become unfit for human consumption after a "
                "period of time, but the package bears no best before or use by "
                "date, month and year."
            ),
            severity=Severity.CRITICAL,
            expected="The best before or use by date, month and year shall be declared.",
            remediation="Print 'Best before <date/month/year>' or an equivalent use-by declaration.",
        )
    ]


def check_retail_sale_price(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 6(1)(e) read with Rule 2(m): the retail sale price of the package,
    which shall clearly indicate that it is the maximum retail price inclusive
    of all taxes, in Indian currency.

    Three requirements are tested separately, because a package can satisfy one
    and fail another: the price figure, the "maximum retail price" character of
    that figure, and the express statement that it is inclusive of all taxes.
    """
    rule = "Rule 6(1)(e)"
    if _unreadable(
        data,
        DeclarationField.RETAIL_SALE_PRICE,
        DeclarationField.TAX_INCLUSIVE_DECLARATION,
    ):
        return [
            _not_assessed(
                DeclarationField.RETAIL_SALE_PRICE,
                rule,
                "The price declaration is present on the package but could not be read.",
            )
        ]

    price = data.retail_sale_price
    if price is None or price.amount is None:
        return [
            _violation(
                field=DeclarationField.RETAIL_SALE_PRICE,
                rule=rule,
                title="Retail sale price not declared",
                description="The package does not bear the retail sale price of the package.",
                severity=Severity.CRITICAL,
                observed=price.raw_text if price else None,
                expected=(
                    "The retail sale price shall be declared as the maximum retail "
                    "price inclusive of all taxes, in Indian currency."
                ),
                remediation="Print 'MRP Rs. <amount> (inclusive of all taxes)'.",
            )
        ]

    findings: list[RuleViolation] = []
    haystack = _text_of(
        price.raw_text, price.label_prefix_as_printed, price.tax_declaration_text
    )

    if price.amount <= Decimal("0"):
        findings.append(
            _violation(
                field=DeclarationField.RETAIL_SALE_PRICE,
                rule=rule,
                title="Retail sale price is not a positive amount",
                description="The declared retail sale price is zero or negative.",
                severity=Severity.MAJOR,
                observed=price.raw_text,
                expected="The retail sale price shall be a positive amount in Indian currency.",
            )
        )

    if not _MRP_CAPTION_RE.search(haystack):
        findings.append(
            _violation(
                field=DeclarationField.RETAIL_SALE_PRICE,
                rule=rule,
                title="Price not identified as the maximum retail price",
                description=(
                    "A price is printed on the package, but it is not clearly "
                    "indicated to be the maximum retail price as Rule 2(m) and "
                    "Rule 6(1)(e) require."
                ),
                severity=Severity.MAJOR,
                observed=price.raw_text,
                expected="The price shall be captioned 'Maximum Retail Price' or 'MRP'.",
                remediation="Prefix the amount with 'MRP' or 'Maximum Retail Price'.",
            )
        )

    tax_stated = price.is_inclusive_of_all_taxes or bool(_TAX_INCLUSIVE_RE.search(haystack))
    if not tax_stated:
        findings.append(
            _violation(
                field=DeclarationField.TAX_INCLUSIVE_DECLARATION,
                rule=rule,
                title="Price not declared to be inclusive of all taxes",
                description=(
                    "The package bears a price but does not state that it is "
                    "inclusive of all taxes, as required by Rule 6(1)(e) read with "
                    "the definition of retail sale price in Rule 2(m)."
                ),
                severity=Severity.MAJOR,
                observed=price.raw_text,
                expected="The declaration shall state that the price is inclusive of all taxes.",
                remediation="Add the words 'inclusive of all taxes' to the price declaration.",
            )
        )

    if not _INR_RE.search(haystack) and _blank(price.currency_symbol_as_printed):
        findings.append(
            _violation(
                field=DeclarationField.RETAIL_SALE_PRICE,
                rule=rule,
                title="Price not expressed in Indian currency",
                description=(
                    "The price declaration does not carry an Indian currency token "
                    "such as the rupee sign, 'Rs.' or 'INR'."
                ),
                severity=Severity.MINOR,
                observed=price.raw_text,
                expected="The retail sale price shall be declared in Indian currency.",
                remediation="Prefix the amount with the rupee sign or 'Rs.'.",
            )
        )

    return findings


def check_consumer_care(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 6(2): every package shall bear the name, address, telephone number and
    e-mail address of the person, or the office, which can be contacted in case
    of complaints.

    All four particulars are mandatory. Each is tested separately so a notice
    can specify precisely which is missing, and a printed but malformed number
    is graded below an absent one.
    """
    rule = "Rule 6(2)"
    if _unreadable(
        data,
        DeclarationField.CONSUMER_CARE_PHONE,
        DeclarationField.CONSUMER_CARE_EMAIL,
        DeclarationField.CONSUMER_CARE_ADDRESS,
    ):
        return [
            _not_assessed(
                DeclarationField.CONSUMER_CARE_PHONE,
                rule,
                "Consumer care details are present on the package but could not be read.",
            )
        ]

    care = data.consumer_care
    if care is None:
        return [
            _violation(
                field=DeclarationField.CONSUMER_CARE_PHONE,
                rule=rule,
                title="Consumer care details not declared",
                description=(
                    "The package does not bear the name, address, telephone number "
                    "and e-mail address of the person or office which can be "
                    "contacted in case of complaints."
                ),
                severity=Severity.CRITICAL,
                expected=(
                    "Every package shall bear consumer care contact particulars: "
                    "name, address, telephone number and e-mail address."
                ),
                remediation=(
                    "Add a consumer care block giving the contact name or office, "
                    "address, telephone number and e-mail address."
                ),
            )
        ]

    findings: list[RuleViolation] = []

    if not care.phone_numbers:
        findings.append(
            _violation(
                field=DeclarationField.CONSUMER_CARE_PHONE,
                rule=rule,
                title="Consumer care telephone number not declared",
                description="No telephone number for consumer complaints appears on the package.",
                severity=Severity.MAJOR,
                observed=care.raw_text,
                expected="A telephone number for consumer complaints shall be declared.",
                remediation="Print a working consumer care telephone number.",
            )
        )
    elif not care.has_valid_phone:
        findings.append(
            _violation(
                field=DeclarationField.CONSUMER_CARE_PHONE,
                rule=rule,
                title="Consumer care telephone number is not a dialable number",
                description=(
                    "A telephone number is printed but does not form a valid "
                    "dialable Indian number, so it cannot serve the purpose of the rule."
                ),
                severity=Severity.MINOR,
                observed="; ".join(care.phone_numbers),
                expected="The declared telephone number shall be complete and dialable.",
                remediation="Correct the consumer care telephone number.",
            )
        )

    if not care.email_addresses:
        findings.append(
            _violation(
                field=DeclarationField.CONSUMER_CARE_EMAIL,
                rule=rule,
                title="Consumer care e-mail address not declared",
                description="No e-mail address for consumer complaints appears on the package.",
                severity=Severity.MAJOR,
                observed=care.raw_text,
                expected="An e-mail address for consumer complaints shall be declared.",
                remediation="Print a monitored consumer care e-mail address.",
            )
        )
    elif not care.has_valid_email:
        findings.append(
            _violation(
                field=DeclarationField.CONSUMER_CARE_EMAIL,
                rule=rule,
                title="Consumer care e-mail address is malformed",
                description=(
                    "An e-mail address is printed but is not well-formed, so it "
                    "cannot receive consumer complaints."
                ),
                severity=Severity.MINOR,
                observed="; ".join(care.email_addresses),
                expected="The declared e-mail address shall be well-formed.",
                remediation="Correct the consumer care e-mail address.",
            )
        )

    # The rule requires an address for the contact point. A separate consumer
    # care address is not needed where the manufacturer's address is declared
    # and the contact point is that same entity.
    entity = data.declared_entity
    has_address = (care.address is not None and not _blank(care.address.full_address_as_printed)) or (
        entity is not None
        and entity.address is not None
        and not _blank(entity.address.full_address_as_printed)
    )
    if not has_address:
        findings.append(
            _violation(
                field=DeclarationField.CONSUMER_CARE_ADDRESS,
                rule=rule,
                title="Consumer care address not declared",
                description=(
                    "No address is given for the person or office to be contacted "
                    "in case of complaints."
                ),
                severity=Severity.MAJOR,
                expected="An address for the consumer care contact point shall be declared.",
                remediation="Print the address of the consumer care office.",
            )
        )

    return findings


# --------------------------------------------------------------------------- #
# Rules 12 and 13 -- units and the manner of the quantity declaration          #
# --------------------------------------------------------------------------- #


def _normalise_unit(printed: str) -> str:
    """Strip punctuation and whitespace from a printed unit token."""
    return re.sub(r"[\s.·]", "", printed).strip()


def check_si_unit_system(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 13(5)(i): no system of units other than the International System of
    Units shall be used in furnishing the net quantity of a package.

    The printed abbreviation is tested independently of the extractor's
    normalisation. That matters: "500 gms" carries a perfectly determinate
    quantity, and a normalising pipeline that quietly reads it as "500 g" would
    erase the contravention entirely.
    """
    quantity = data.net_quantity
    if quantity is None or _blank(quantity.unit_as_printed):
        return []  # Absence is handled by the Rule 6(1)(c) check.

    rule = "Rule 13(5)(i)"
    printed = quantity.unit_as_printed or ""
    token = _normalise_unit(printed)
    findings: list[RuleViolation] = []

    is_lawful_symbol = token in LAWFUL_UNIT_SYMBOLS or token.lower() in {
        s.lower() for s in LAWFUL_UNIT_SYMBOLS
    }
    is_lawful_word = token.lower() in LAWFUL_UNIT_WORDS

    if not is_lawful_symbol and not is_lawful_word:
        findings.append(
            _violation(
                field=DeclarationField.NET_QUANTITY,
                rule=rule,
                title="Net quantity declared in a non-standard unit",
                description=(
                    f"The net quantity is declared using '{printed}', which is not "
                    "a symbol of the International System of Units. No system of "
                    "units other than the International System may be used in "
                    "furnishing the net quantity of a package."
                ),
                severity=Severity.MAJOR,
                observed=quantity.raw_text or printed,
                expected="The net quantity shall be declared using International System units.",
                remediation=(
                    "Replace the abbreviation with the correct SI symbol "
                    "(g, kg, ml, l, cm, m, or the count for goods sold by number)."
                ),
            )
        )
    elif quantity.si_unit is SIUnit.NON_STANDARD:
        # The extractor flagged the printed abbreviation even though it matched
        # a lawful token after punctuation was stripped -- report it lightly.
        findings.append(
            _violation(
                field=DeclarationField.NET_QUANTITY,
                rule=rule,
                title="Net quantity unit printed in a non-standard form",
                description=(
                    f"The unit is printed as '{printed}'. The symbol is "
                    "recognisable but is not rendered in its standard form."
                ),
                severity=Severity.MINOR,
                observed=quantity.raw_text or printed,
                expected="Unit symbols shall be printed in their standard form.",
            )
        )

    # Rule 13(4): denominations of number that may not appear on any package.
    haystack = _text_of(quantity.raw_text, quantity.qualifier_as_printed, printed)
    for word in PROHIBITED_NUMBER_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", haystack):
            findings.append(
                _violation(
                    field=DeclarationField.NET_QUANTITY,
                    rule="Rule 13(4)",
                    title=f"Prohibited number denomination '{word}' used",
                    description=(
                        f"The quantity declaration uses the denomination '{word}'. "
                        "No number called the dozen, score, gross, great gross or "
                        "the like shall be specified or indicated on any package."
                    ),
                    severity=Severity.MAJOR,
                    observed=quantity.raw_text,
                    expected="Quantities sold by number shall be declared as a plain count.",
                    remediation=f"Replace '{word}' with the equivalent numeric count.",
                )
            )
            break

    return findings


def check_unit_magnitude(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 13(2) and 13(3): the unit must match the magnitude declared.

    Below one kilogram the unit is the gram; below one metre, the centimetre;
    below one litre, the millilitre. At or above those thresholds the larger
    unit applies. The proviso permits a quantity of exactly one kilogram, metre
    or litre to be expressed either way, so the boundary itself is never a
    contravention.

    The sub-rule 13(3)(a) allowance -- that a fraction of a kilogram "shall be
    expressed in terms of decimal of sub-multiples of kilogram **or in terms of
    grams**" -- leaves genuine room for argument about a package marked
    "1500 g". Findings in that direction are therefore graded MINOR, while the
    unambiguous direction (a sub-unit quantity printed in the larger unit, such
    as "0.5 kg") is graded MAJOR.
    """
    quantity = data.net_quantity
    if quantity is None or quantity.value is None or quantity.si_unit is None:
        return []

    value, unit = quantity.value, quantity.si_unit
    rule = "Rule 13(2) and 13(3)"
    observed = quantity.raw_text or f"{value} {quantity.unit_as_printed or unit.value}"

    def below_threshold(required: str, threshold: str, sub_unit: str) -> RuleViolation:
        return _violation(
            field=DeclarationField.NET_QUANTITY,
            rule="Rule 13(2)",
            title=f"Quantity below {threshold} not declared in {sub_unit}",
            description=(
                f"The net quantity is less than {threshold} but is declared in "
                f"{required}. When expressing a quantity less than {threshold}, the "
                f"unit shall be the {sub_unit}."
            ),
            severity=Severity.MAJOR,
            observed=observed,
            expected=f"Quantities below {threshold} shall be declared in {sub_unit}.",
            remediation=f"Re-express the net quantity in {sub_unit}.",
        )

    def above_threshold(current: str, threshold: str, main_unit: str) -> RuleViolation:
        return _violation(
            field=DeclarationField.NET_QUANTITY,
            rule="Rule 13(3)",
            title=f"Quantity of {threshold} or more not declared in {main_unit}",
            description=(
                f"The net quantity is greater than {threshold} but is declared in "
                f"{current}. When expressing a quantity equal to or more than "
                f"{threshold}, the unit shall be the {main_unit}, any fraction "
                f"being expressed as a decimal sub-multiple."
            ),
            severity=Severity.MINOR,
            observed=observed,
            expected=f"Quantities of {threshold} or more shall be declared in {main_unit}.",
            remediation=f"Re-express the net quantity in {main_unit}.",
        )

    findings: list[RuleViolation] = []

    if unit is SIUnit.KILOGRAM and value < Decimal("1"):
        findings.append(below_threshold("kilograms", "one kilogram", "gram"))
    elif unit is SIUnit.GRAM and value > Decimal("1000"):
        findings.append(above_threshold("grams", "one kilogram", "kilogram"))
    elif unit is SIUnit.LITRE and value < Decimal("1"):
        findings.append(below_threshold("litres", "one litre", "millilitre"))
    elif unit is SIUnit.MILLILITRE and value > Decimal("1000"):
        findings.append(above_threshold("millilitres", "one litre", "litre"))
    elif unit is SIUnit.METRE and value < Decimal("1"):
        findings.append(below_threshold("metres", "one metre", "centimetre"))
    elif unit is SIUnit.CENTIMETRE and value > Decimal("100"):
        findings.append(above_threshold("centimetres", "one metre", "metre"))

    # Rule 12(2): mass for solids, volume for liquids. A mismatch between the
    # declared dimension and the unit indicates one or the other is wrong.
    kind = quantity.quantity_kind
    dimension_of = {
        SIUnit.MILLIGRAM: QuantityKind.WEIGHT,
        SIUnit.GRAM: QuantityKind.WEIGHT,
        SIUnit.KILOGRAM: QuantityKind.WEIGHT,
        SIUnit.MICROLITRE: QuantityKind.VOLUME,
        SIUnit.MILLILITRE: QuantityKind.VOLUME,
        SIUnit.LITRE: QuantityKind.VOLUME,
        SIUnit.MILLIMETRE: QuantityKind.LENGTH,
        SIUnit.CENTIMETRE: QuantityKind.LENGTH,
        SIUnit.METRE: QuantityKind.LENGTH,
        SIUnit.SQUARE_METRE: QuantityKind.AREA,
        SIUnit.NUMBER: QuantityKind.NUMBER,
    }.get(unit)

    if kind is not None and dimension_of is not None and kind is not dimension_of:
        findings.append(
            _violation(
                field=DeclarationField.NET_QUANTITY,
                rule="Rule 12(2)",
                title="Unit does not match the dimension declared",
                description=(
                    f"The quantity is declared as a measure of {kind.value} but the "
                    f"unit '{unit.value}' is a unit of {dimension_of.value}."
                ),
                severity=Severity.MAJOR,
                observed=observed,
                expected=(
                    "The declaration shall be in units of mass for solid, semi-solid "
                    "or viscous commodities and of volume for liquids."
                ),
            )
        )

    return findings


def check_misleading_quantity_terms(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 12(6): the declaration of quantity shall not contain any word or
    expression tending to create an exaggerated, misleading or inadequate
    impression of the quantity contained -- 'minimum', 'not less than',
    'average', 'about', 'approximately' and words of a similar nature.
    """
    quantity = data.net_quantity
    if quantity is None:
        return []

    haystack = _text_of(quantity.raw_text, quantity.qualifier_as_printed)
    if not haystack:
        return []

    for term in MISLEADING_QUANTITY_TERMS:
        if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", haystack):
            return [
                _violation(
                    field=DeclarationField.NET_QUANTITY,
                    rule="Rule 12(6)",
                    title=f"Misleading quantity qualifier '{term}' used",
                    description=(
                        f"The quantity declaration contains the expression '{term}', "
                        "which tends to create an exaggerated, misleading or "
                        "inadequate impression of the quantity contained in the package."
                    ),
                    severity=Severity.MAJOR,
                    observed=quantity.raw_text,
                    expected="The quantity shall be declared as a definite figure, without qualification.",
                    remediation=f"Remove '{term}' and declare the exact net quantity.",
                )
            ]

    return []


# --------------------------------------------------------------------------- #
# Rule 7 -- principal display panel, size of letters and numerals              #
# --------------------------------------------------------------------------- #


def minimum_character_height_mm(
    pdp_area_cm2: float, surface: ContainerSurface
) -> float:
    """
    Minimum height of numerals and letters under Table-I to Rule 7.

    Args:
        pdp_area_cm2: Area of the principal display panel, per Rule 7(4).
        surface: Whether the declarations are printed, or blown, formed or
            moulded on the surface of the container.

    Returns:
        The minimum permissible character height in millimetres.
    """
    column = 2 if surface is ContainerSurface.BLOWN_FORMED_MOULDED else 1
    for upper_bound, printed_mm, moulded_mm in TABLE_I:
        if pdp_area_cm2 <= upper_bound:
            return moulded_mm if column == 2 else printed_mm
    return TABLE_I[-1][2 if column == 2 else 1]


def check_character_size(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 7(2) with Table-I, and Rule 7(3): minimum height of numerals and
    letters, scaled to the area of the principal display panel, and minimum
    character width of one third of the height.

    This check depends on absolute physical measurements. An uncalibrated
    photograph carries no millimetres -- without a scale reference in frame,
    apparent character height is a function of camera distance alone. So when
    measurements are absent the check reports itself as *not assessed*; it
    never passes a package it could not measure, and never fails one either.
    """
    rule = "Rule 7(2) read with Table-I"
    measurements = ctx.measurements

    if measurements is None:
        return [
            _not_assessed(
                DeclarationField.NET_QUANTITY,
                rule,
                "Character size under Rule 7 requires physical measurement of the "
                "package and of the printed characters. No measurements were "
                "supplied, so the size of the declarations was not assessed.",
            )
        ]

    pdp_area = measurements.principal_display_panel_area_cm2
    if pdp_area is None:
        return [
            _not_assessed(
                DeclarationField.NET_QUANTITY,
                rule,
                "The area of the principal display panel could not be computed: the "
                f"dimensions required for a {measurements.shape.value} package under "
                "Rule 7(4) were not supplied.",
            )
        ]

    findings: list[RuleViolation] = []
    required_mm = minimum_character_height_mm(pdp_area, measurements.surface)
    surface_note = (
        " (blown, formed or moulded on the surface of the container)"
        if measurements.surface is ContainerSurface.BLOWN_FORMED_MOULDED
        else ""
    )

    if (
        measurements.capacity_cubic_cm is not None
        and measurements.capacity_cubic_cm <= SMALL_PACKAGE_CAPACITY_CM3
    ):
        findings.append(
            _violation(
                field=DeclarationField.NET_QUANTITY,
                rule="Rule 7(1)",
                title="Small package: principal display panel may be a card or tape",
                description=(
                    f"The package capacity is {measurements.capacity_cubic_cm:g} cubic "
                    "centimetres or less, so the principal display panel may be a card "
                    "or tape affixed firmly to the package bearing the required information."
                ),
                severity=Severity.ADVISORY,
                expected="Rule 7(1) relaxation noted; no contravention arises from panel form.",
                penalty=None,
            )
        )

    for label, measured, field in (
        ("numeral", measurements.measured_numeral_height_mm, DeclarationField.NET_QUANTITY),
        ("letter", measurements.measured_letter_height_mm, DeclarationField.NET_QUANTITY),
    ):
        if measured is None:
            findings.append(
                _not_assessed(
                    field,
                    rule,
                    f"The height of the {label}s in the declarations was not measured.",
                )
            )
            continue

        if measured + MEASUREMENT_TOLERANCE_MM < required_mm:
            findings.append(
                _violation(
                    field=field,
                    rule=rule,
                    title=f"Height of {label}s below the minimum prescribed by Table-I",
                    description=(
                        f"The principal display panel measures {pdp_area:.1f} square "
                        f"centimetres, for which Table-I to Rule 7 prescribes a minimum "
                        f"{label} height of {required_mm:g} mm{surface_note}. The "
                        f"{label}s on the package measure {measured:g} mm."
                    ),
                    severity=Severity.MAJOR,
                    observed=f"{measured:g} mm",
                    expected=(
                        f"Minimum {label} height of {required_mm:g} mm for a principal "
                        f"display panel of {pdp_area:.1f} sq cm."
                    ),
                    remediation=(
                        f"Re-set the declarations at not less than {required_mm:g} mm, "
                        "or enlarge the principal display panel."
                    ),
                )
            )

    height = measurements.measured_numeral_height_mm
    width = measurements.measured_numeral_width_mm
    if height is not None and width is not None:
        required_width = height * MIN_WIDTH_TO_HEIGHT_RATIO
        if width + MEASUREMENT_TOLERANCE_MM < required_width:
            findings.append(
                _violation(
                    field=DeclarationField.NET_QUANTITY,
                    rule="Rule 7(3)",
                    title="Character width below one third of its height",
                    description=(
                        f"The characters measure {width:g} mm wide against a height of "
                        f"{height:g} mm. The width of a letter or numeral shall not be "
                        "less than one third of its height, except in the case of the "
                        "numeral '1' and the letters (i), (I) and (l)."
                    ),
                    severity=Severity.MINOR,
                    observed=f"{width:g} mm wide x {height:g} mm high",
                    expected=f"Minimum width of {required_width:.2f} mm at this character height.",
                    remediation="Use a less condensed typeface for the declarations.",
                )
            )

    return findings


# --------------------------------------------------------------------------- #
# Rule 9 -- manner of declaration                                              #
# --------------------------------------------------------------------------- #


def check_declaration_language(
    data: ExtractedLabelData, ctx: EvaluationContext
) -> list[RuleViolation]:
    """
    Rule 9(4): the particulars of the declarations shall be either in Hindi in
    Devanagari script or in English.
    """
    rule = "Rule 9(4)"
    languages = {lang.strip().lower() for lang in data.languages_detected if lang.strip()}

    if not languages:
        # Fall back to the transcript: Devanagari or Latin characters in the
        # raw text dump establish the script even when no language was reported.
        dump = data.raw_text_dump or ""
        if _DEVANAGARI_RE.search(dump) or re.search(r"[A-Za-z]", dump):
            return []
        return [
            _not_assessed(
                DeclarationField.DECLARATION_LANGUAGE,
                rule,
                "The language of the declarations could not be established from the image.",
            )
        ]

    if languages & {"english", "hindi"}:
        return []

    return [
        _violation(
            field=DeclarationField.DECLARATION_LANGUAGE,
            rule=rule,
            title="Declarations not made in Hindi or English",
            description=(
                "The mandatory declarations appear only in "
                f"{', '.join(sorted(languages))}. The particulars of the declarations "
                "shall be either in Hindi in Devanagari script or in English."
            ),
            severity=Severity.MAJOR,
            observed=", ".join(sorted(languages)),
            expected="Declarations shall be in Hindi (Devanagari script) or in English.",
            remediation="Add the mandatory declarations in Hindi or English.",
        )
    ]


# --------------------------------------------------------------------------- #
# Check registry                                                               #
# --------------------------------------------------------------------------- #

CheckFunction = Callable[[ExtractedLabelData, EvaluationContext], list[RuleViolation]]


@dataclass(frozen=True)
class Check:
    """One statutory check, registered so the suite is introspectable."""

    identifier: str
    rule_reference: str
    description: str
    run: CheckFunction


#: Evaluation order is the order of the Rules themselves, so that a report
#: reads in the same sequence as the instrument it applies.
CHECKS: Final[tuple[Check, ...]] = (
    Check("manufacturer_identity", "Rule 6(1)(a)", "Name and address of manufacturer/packer/importer", check_manufacturer_identity),
    Check("country_of_origin", "Rule 6(1)(aa)", "Country of origin for imported packages", check_country_of_origin),
    Check("commodity_name", "Rule 6(1)(b)", "Common or generic name of the commodity", check_commodity_name),
    Check("net_quantity_declared", "Rule 6(1)(c)", "Net quantity in standard units", check_net_quantity_declared),
    Check("manufacture_date", "Rule 6(1)(d)", "Month and year of manufacture", check_manufacture_date),
    Check("best_before", "Rule 6(1)(da)", "Best before / use by date for perishables", check_best_before),
    Check("retail_sale_price", "Rule 6(1)(e)", "Maximum retail price inclusive of all taxes", check_retail_sale_price),
    Check("consumer_care", "Rule 6(2)", "Consumer care name, address, telephone and e-mail", check_consumer_care),
    Check("character_size", "Rule 7", "Character height and width against Table-I", check_character_size),
    Check("declaration_language", "Rule 9(4)", "Declarations in Hindi or English", check_declaration_language),
    Check("misleading_quantity_terms", "Rule 12(6)", "No misleading quantity qualifiers", check_misleading_quantity_terms),
    Check("unit_magnitude", "Rule 13(2)-(3)", "Correct unit for the magnitude declared", check_unit_magnitude),
    Check("si_unit_system", "Rule 13(4)-(5)", "International System units only", check_si_unit_system),
)


# --------------------------------------------------------------------------- #
# Engine                                                                       #
# --------------------------------------------------------------------------- #


class RulesEngine:
    """
    Applies the registered statutory checks to an extracted label.

    Stateless and side-effect free: a single instance may be shared across
    concurrent requests, and two evaluations of the same inputs always produce
    the same violations in the same order.
    """

    version: Final[str] = RULES_ENGINE_VERSION

    def __init__(self, checks: tuple[Check, ...] = CHECKS) -> None:
        self._checks = checks

    # -- public API --------------------------------------------------------- #

    def evaluate(
        self,
        extracted: Optional[ExtractedLabelData],
        *,
        image_sha256: str,
        context: Optional[EvaluationContext] = None,
        image_filename: Optional[str] = None,
        image_quality: Optional[ImageQualityMetrics] = None,
        vision_model_id: Optional[str] = None,
        processing_time_ms: Optional[float] = None,
    ) -> ComplianceReport:
        """
        Evaluate a label and produce an adjudicable report.

        Args:
            extracted: The extraction, or ``None`` where the image could not be
                read. ``None`` yields an INDETERMINATE report -- an unreadable
                image is an evidentiary failure on the inspecting side, never a
                finding against the manufacturer.
            image_sha256: Digest binding the report to the bytes examined.
            context: Category, perishability, import status and physical
                measurements. Defaults are conservative where unspecified.
            image_filename: Original filename, for the record.
            image_quality: OpenCV measurements, carried into the report.
            vision_model_id: Model that performed the extraction.
            processing_time_ms: End-to-end processing time.

        Returns:
            A fully populated :class:`~app.schemas.ComplianceReport`.
        """
        if extracted is None:
            return ComplianceReport.indeterminate(
                image_sha256=image_sha256,
                image_filename=image_filename,
                image_quality=image_quality,
                vision_model_id=vision_model_id,
                rules_engine_version=self.version,
                processing_time_ms=processing_time_ms,
                jurisdiction=context.jurisdiction if context else None,
                inspector_id=context.inspector_id if context else None,
            )

        ctx = context or EvaluationContext()
        violations = self.run_checks(extracted, ctx)
        status = self._status_for(violations)

        return ComplianceReport(
            image_sha256=image_sha256,
            image_filename=image_filename,
            image_quality=image_quality,
            extracted_data=extracted,
            status=status,
            violations=violations,
            compliance_score=self.score(violations),
            summary=self._summarise(status, violations),
            vision_model_id=vision_model_id,
            rules_engine_version=self.version,
            jurisdiction=ctx.jurisdiction,
            inspector_id=ctx.inspector_id,
            processing_time_ms=processing_time_ms,
        )

    def run_checks(
        self, extracted: ExtractedLabelData, context: EvaluationContext
    ) -> list[RuleViolation]:
        """
        Run every registered check and return the findings in statutory order.

        A check that raises is not allowed to void the whole inspection: the
        failure is converted into an explicit "not assessed" advisory so the
        report states which provision went unexamined, and the remaining checks
        continue.
        """
        findings: list[RuleViolation] = []
        for check in self._checks:
            try:
                findings.extend(check.run(extracted, context))
            except Exception as exc:  # noqa: BLE001 - see docstring
                findings.append(
                    _not_assessed(
                        DeclarationField.COMMODITY_NAME,
                        check.rule_reference,
                        f"The check for {check.description} could not be completed "
                        f"({type(exc).__name__}). No finding is recorded for this provision.",
                    )
                )
        return findings

    # -- scoring and verdict ------------------------------------------------ #

    @staticmethod
    def score(violations: list[RuleViolation]) -> float:
        """
        Weighted conformity score out of 100.

        Advisories carry no penalty, so a package whose declarations could not
        all be assessed is not scored down for our own measurement gaps.
        """
        penalty = sum(_SCORE_PENALTY[v.severity] for v in violations)
        return round(max(0.0, 100.0 - penalty), 2)

    @staticmethod
    def _status_for(violations: list[RuleViolation]) -> ComplianceStatus:
        """
        Derive the verdict.

        A package with no contraventions but with unassessed declarations is
        PARTIALLY_COMPLIANT, never COMPLIANT: certifying conformity on
        provisions that were never examined would overstate the finding.
        """
        severities = {v.severity for v in violations}
        if severities & {Severity.CRITICAL, Severity.MAJOR}:
            return ComplianceStatus.NON_COMPLIANT
        if Severity.MINOR in severities:
            return ComplianceStatus.PARTIALLY_COMPLIANT
        if Severity.ADVISORY in severities:
            return ComplianceStatus.PARTIALLY_COMPLIANT
        return ComplianceStatus.COMPLIANT

    @staticmethod
    def _summarise(status: ComplianceStatus, violations: list[RuleViolation]) -> str:
        """Compose the one-paragraph verdict carried into the PDF notice."""
        actionable = [v for v in violations if v.severity is not Severity.ADVISORY]
        unassessed = len(violations) - len(actionable)

        if status is ComplianceStatus.COMPLIANT:
            return (
                "The package bears all declarations required by the Legal Metrology "
                "(Packaged Commodities) Rules, 2011 in the form prescribed. No "
                "contravention was found."
            )

        if not actionable:
            return (
                f"No contravention was found, but {unassessed} provision(s) could not "
                "be assessed from the material available. Conformity is therefore not "
                "certified."
            )

        critical = sum(1 for v in actionable if v.severity is Severity.CRITICAL)
        rules = sorted({v.rule_reference for v in actionable})
        return (
            f"{len(actionable)} contravention(s) of the Legal Metrology (Packaged "
            f"Commodities) Rules, 2011 were found, of which {critical} concern the "
            f"total absence of a mandatory declaration. Provisions contravened: "
            f"{', '.join(rules)}."
            + (
                f" A further {unassessed} provision(s) could not be assessed."
                if unassessed
                else ""
            )
        )


#: Shared, stateless instance for callers that do not need a custom check set.
_DEFAULT_ENGINE: Final[RulesEngine] = RulesEngine()


def evaluate_compliance(
    extracted: Optional[ExtractedLabelData],
    *,
    image_sha256: str,
    context: Optional[EvaluationContext] = None,
    image_filename: Optional[str] = None,
    image_quality: Optional[ImageQualityMetrics] = None,
    vision_model_id: Optional[str] = None,
    processing_time_ms: Optional[float] = None,
) -> ComplianceReport:
    """Evaluate a label using the default engine. See :meth:`RulesEngine.evaluate`."""
    return _DEFAULT_ENGINE.evaluate(
        extracted,
        image_sha256=image_sha256,
        context=context,
        image_filename=image_filename,
        image_quality=image_quality,
        vision_model_id=vision_model_id,
        processing_time_ms=processing_time_ms,
    )
