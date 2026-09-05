"""
Pydantic V2 contracts for the Legal Metrology compliance platform (SIH26034).

Two families of models live here:

1. **Vision-facing models** (``ExtractedLabelData`` and everything it nests).
   These are serialised to a JSON Schema and handed to the Vision LLM as a
   structured-output contract. Design rules for anything in this family:

   * Every field is explicitly ``Optional[...]`` **without a default**, so the
     model is forced to emit a value (possibly ``null``) for each declaration
     rather than silently omitting it. A declaration that is absent from the
     image must come back as ``null`` -- never as a plausible-looking guess.
   * No ``computed_field`` -- derived values are plain ``@property`` so they
     never leak into the schema the model is asked to satisfy.
   * ``extra="forbid"`` maps to ``additionalProperties: false``, which the
     structured-output layer requires.
   * Every declaration carries ``raw_text`` -- the verbatim string as printed
     on the package. This is the evidentiary anchor: a legal notice quotes what
     was printed, never a normalised value.

2. **System-facing models** (``ImageQualityMetrics``, ``RuleViolation``,
   ``ComplianceReport``). These are produced by our own deterministic code
   (Phase 2 rules engine, Phase 4 PDF generator) and may use computed fields,
   defaults and factories freely.

Statutory basis: the Legal Metrology (Packaged Commodities) Rules, 2011, framed
under the Legal Metrology Act, 2009 (Ministry of Consumer Affairs, Food and
Public Distribution).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Optional
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    computed_field,
    field_validator,
)

__all__ = [
    "STATUTE_NAME",
    "PARENT_ACT_NAME",
    "SIUnit",
    "QuantityKind",
    "EntityRole",
    "DeclarationField",
    "Severity",
    "ComplianceStatus",
    "DeclarationEvidence",
    "TextDeclaration",
    "PostalAddress",
    "EntityDeclaration",
    "NetQuantityDeclaration",
    "RetailSalePriceDeclaration",
    "DateDeclaration",
    "ConsumerCareDeclaration",
    "ExtractedLabelData",
    "ImageQualityMetrics",
    "PackageCategory",
    "PackageShape",
    "ContainerSurface",
    "PackageMeasurements",
    "EvaluationContext",
    "RuleViolation",
    "ComplianceReport",
]

STATUTE_NAME = "Legal Metrology (Packaged Commodities) Rules, 2011"
PARENT_ACT_NAME = "Legal Metrology Act, 2009"

# --------------------------------------------------------------------------- #
# Annotated primitives                                                         #
# --------------------------------------------------------------------------- #

Confidence = Annotated[
    float,
    Field(
        ge=0.0,
        le=1.0,
        description=(
            "Calibrated confidence that this value was actually read off the "
            "package (0.0 = pure guess, 1.0 = plainly legible)."
        ),
    ),
]

#: Money is carried as ``Decimal`` for exactness, but is transported over the
#: wire as a *string* so no binary-float rounding is introduced between the
#: vision model and a court-admissible notice ("145.00" stays "145.00").
MonetaryAmount = Annotated[
    Decimal,
    WithJsonSchema(
        {
            "type": "string",
            "description": (
                "Plain decimal string exactly as printed, without currency "
                "symbol or thousands separators, e.g. '145.00' or '1250.50'."
            ),
        }
    ),
]

#: Net-quantity magnitudes use the same string transport for the same reason:
#: 99.9 g must never become 99.90000000000001 g in a prosecution document.
QuantityAmount = Annotated[
    Decimal,
    WithJsonSchema(
        {
            "type": "string",
            "description": (
                "Plain decimal string of the numeric magnitude as printed, "
                "e.g. '500', '1.5', '99.9'."
            ),
        }
    ),
]


# --------------------------------------------------------------------------- #
# Enumerations                                                                 #
# --------------------------------------------------------------------------- #


class SIUnit(str, Enum):
    """
    Legal units of measure permitted under the Legal Metrology Act, 2009.

    ``NON_STANDARD`` is deliberately part of the vocabulary: a package printed
    with "gms", "Ltr" or "lbs" *is* readable, but Rule 13(5)(i) forbids any
    system of units other than the International System. Recording it as
    ``NON_STANDARD`` (with ``unit_as_printed`` preserved verbatim) lets the
    Phase 2 rules engine raise a precise violation instead of the extractor
    quietly "correcting" the label.
    """

    MILLIGRAM = "mg"
    GRAM = "g"
    KILOGRAM = "kg"
    MICROLITRE = "ul"
    MILLILITRE = "ml"
    LITRE = "l"
    MILLIMETRE = "mm"
    CENTIMETRE = "cm"
    METRE = "m"
    SQUARE_METRE = "m2"
    NUMBER = "N"
    NON_STANDARD = "non_standard"


class QuantityKind(str, Enum):
    """Physical dimension of the net-quantity declaration."""

    WEIGHT = "weight"
    VOLUME = "volume"
    LENGTH = "length"
    AREA = "area"
    NUMBER = "number"


class EntityRole(str, Enum):
    """Role of a declared legal person under Rule 6(1)(a)."""

    MANUFACTURER = "manufacturer"
    PACKER = "packer"
    IMPORTER = "importer"
    MARKETER = "marketer"
    BRAND_OWNER = "brand_owner"


class DeclarationField(str, Enum):
    """
    Canonical identifiers for the mandatory declarations.

    Used both by the extractor (to report what it could *not* read) and by the
    rules engine (to anchor every violation to a specific declaration).
    """

    MANUFACTURER_NAME = "manufacturer_name"
    MANUFACTURER_ADDRESS = "manufacturer_address"
    COMMODITY_NAME = "commodity_name"
    NET_QUANTITY = "net_quantity"
    RETAIL_SALE_PRICE = "retail_sale_price"
    TAX_INCLUSIVE_DECLARATION = "tax_inclusive_declaration"
    MANUFACTURE_DATE = "manufacture_date"
    PACKING_DATE = "packing_date"
    IMPORT_DATE = "import_date"
    BEST_BEFORE = "best_before"
    CONSUMER_CARE_PHONE = "consumer_care_phone"
    CONSUMER_CARE_EMAIL = "consumer_care_email"
    CONSUMER_CARE_ADDRESS = "consumer_care_address"
    COUNTRY_OF_ORIGIN = "country_of_origin"
    BATCH_NUMBER = "batch_number"
    DECLARATION_LANGUAGE = "declaration_language"


class Severity(str, Enum):
    """Grading used to prioritise enforcement action."""

    CRITICAL = "critical"  # mandatory declaration wholly absent
    MAJOR = "major"  # present but materially non-conforming
    MINOR = "minor"  # cosmetic / formatting deviation
    ADVISORY = "advisory"  # best-practice observation, not a contravention


class ComplianceStatus(str, Enum):
    """Terminal verdict of an inspection."""

    COMPLIANT = "compliant"
    NON_COMPLIANT = "non_compliant"
    PARTIALLY_COMPLIANT = "partially_compliant"
    #: Image could not be read reliably -- no verdict may lawfully be issued.
    INDETERMINATE = "indeterminate"


# --------------------------------------------------------------------------- #
# Vision-facing models                                                         #
# --------------------------------------------------------------------------- #


class _VisionModel(BaseModel):
    """Base configuration shared by every vision-facing schema."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=False,
    )


class DeclarationEvidence(_VisionModel):
    """
    Common evidentiary envelope attached to every extracted declaration.

    ``raw_text`` is what a Legal Metrology Officer will see quoted in the
    notice; the normalised fields on the subclasses exist only so the
    deterministic rules engine can reason about them.
    """

    raw_text: Optional[str] = Field(
        description=(
            "Verbatim text as printed on the package, preserving original "
            "casing, punctuation and abbreviations. Null if not present."
        )
    )
    confidence: Confidence = Field(
        description="Confidence that this declaration was read, not inferred."
    )
    is_legible: bool = Field(
        description=(
            "True only if the characters were clearly resolvable. Set False "
            "when text is present but blurred, glared out, cropped or occluded."
        )
    )


class TextDeclaration(DeclarationEvidence):
    """A free-text declaration (commodity name, country of origin, batch, ...)."""

    value: Optional[str] = Field(
        description="Normalised text value, or null if unreadable/absent."
    )


class PostalAddress(DeclarationEvidence):
    """
    Address of a manufacturer / packer / importer -- Rule 6(1)(a).

    The Rules require the *complete* address; ``is_complete`` encodes the
    minimum set our engine treats as complete (street line, locality and a
    six-digit PIN code).
    """

    full_address_as_printed: Optional[str] = Field(
        description="Entire address block as printed, newlines collapsed to ', '."
    )
    street_line: Optional[str] = Field(description="Premises / street / locality line.")
    city: Optional[str] = Field(description="City, town or village.")
    district: Optional[str] = Field(description="District, if printed.")
    state: Optional[str] = Field(description="State or union territory.")
    pin_code: Optional[str] = Field(description="Six-digit Indian postal index number.")
    country: Optional[str] = Field(description="Country, if printed.")

    @field_validator("pin_code")
    @classmethod
    def _normalise_pin(cls, value: Optional[str]) -> Optional[str]:
        """Keep only digits; discard anything that is not a six-digit PIN."""
        if value is None:
            return None
        digits = re.sub(r"\D", "", value)
        return digits if len(digits) == 6 else None

    @property
    def is_complete(self) -> bool:
        """True when the address carries enough detail to serve a notice on."""
        return bool(self.street_line and self.city and self.pin_code)


class EntityDeclaration(_VisionModel):
    """Name, role and address of a declared legal person -- Rule 6(1)(a)."""

    name: Optional[str] = Field(description="Registered / trade name exactly as printed.")
    role: Optional[EntityRole] = Field(
        description=(
            "Role as labelled on the package ('Manufactured by' -> manufacturer, "
            "'Packed by' -> packer, 'Imported by' -> importer, "
            "'Marketed by' -> marketer). Null if the label does not say."
        )
    )
    role_prefix_as_printed: Optional[str] = Field(
        description="The literal prefix, e.g. 'Mfd. by', 'Packed & Marketed by'."
    )
    address: Optional[PostalAddress] = Field(
        description="Address block associated with this entity."
    )
    confidence: Confidence = Field(description="Confidence in this entity block.")


class NetQuantityDeclaration(DeclarationEvidence):
    """
    Net quantity declaration -- Rule 6(1)(c) read with Rules 12 and 13.

    ``unit_as_printed`` is preserved verbatim because the *abbreviation itself*
    is regulated: "500 gms" is a contravention while "500 g" is not, and the
    difference disappears once the unit has been normalised.
    """

    value: Optional[QuantityAmount] = Field(
        description="Numeric magnitude as printed, as a decimal string."
    )
    unit_as_printed: Optional[str] = Field(
        description="Unit exactly as printed, e.g. 'g', 'gms', 'ML', 'Ltr'."
    )
    si_unit: Optional[SIUnit] = Field(
        description=(
            "Normalised legal unit. Use 'non_standard' when the printed unit is "
            "readable but is not a lawful SI symbol. Null if unreadable."
        )
    )
    quantity_kind: Optional[QuantityKind] = Field(
        description="Physical dimension being declared."
    )
    qualifier_as_printed: Optional[str] = Field(
        description=(
            "Qualifying words preceding the quantity, e.g. 'Net Quantity', "
            "'Net Wt.', 'Net Vol.'. Null if the quantity carries no qualifier."
        )
    )
    number_of_units_in_package: Optional[int] = Field(
        description=(
            "For multi-piece packages ('10 x 20 g' or '12 N'), the count of "
            "retail units. Null for a single-unit package."
        )
    )


class RetailSalePriceDeclaration(DeclarationEvidence):
    """
    Retail sale price declaration -- Rule 6(1)(e) read with Rule 2(m).

    The Rules mandate not merely a price, but a price expressed in the form
    "Maximum retail price Rs. <amount> inclusive of all taxes". Both the price
    caption and the tax phrase are therefore captured verbatim and separately
    from the amount, so the rules engine can test each requirement on its own.
    """

    amount: Optional[MonetaryAmount] = Field(
        description="The price figure as a decimal string, without any symbol."
    )
    currency_symbol_as_printed: Optional[str] = Field(
        description="Currency token as printed, e.g. '₹', 'Rs.', 'INR'."
    )
    label_prefix_as_printed: Optional[str] = Field(
        description=(
            "Price caption exactly as printed, e.g. 'MRP', 'M.R.P.', "
            "'Maximum Retail Price'."
        )
    )
    tax_declaration_text: Optional[str] = Field(
        description=(
            "Verbatim tax phrase if present, e.g. 'Inclusive of all taxes', "
            "'Incl. of all taxes'. Null if no such phrase appears."
        )
    )
    is_inclusive_of_all_taxes: bool = Field(
        description=(
            "True ONLY if a phrase stating the price is inclusive of all taxes "
            "is actually printed. Never infer this from the presence of a price."
        )
    )


class DateDeclaration(DeclarationEvidence):
    """
    A date declaration -- Rule 6(1)(d) (month and year of manufacture, as
    substituted on 31.10.2021) and Rule 6(1)(da) (best before / use by date).

    Day / month / year are carried as integers rather than a ``date`` so a
    label printed as "MFD 03/2024" is represented faithfully, with no
    fabricated day-of-month. ``as_date`` resolves only when all three
    components were genuinely printed.
    """

    day: Optional[int] = Field(
        ge=1, le=31, description="Day of month; null if only month/year printed."
    )
    month: Optional[int] = Field(
        ge=1, le=12, description="Month as 1-12, resolving names like 'MAR' to 3."
    )
    year: Optional[int] = Field(
        ge=1900, le=2200, description="Four-digit year; expand '24' to 2024."
    )
    date_label_as_printed: Optional[str] = Field(
        description="Caption as printed, e.g. 'MFD', 'PKD', 'Best Before', 'USE BY'."
    )
    relative_period_text: Optional[str] = Field(
        description=(
            "For relative shelf-life declarations, the verbatim phrase, e.g. "
            "'Best before 9 months from packaging'. Null otherwise."
        )
    )
    relative_period_months: Optional[int] = Field(
        ge=0,
        le=600,
        description="Shelf life in whole months, if stated as a relative period.",
    )

    @field_validator("year", mode="before")
    @classmethod
    def _expand_two_digit_year(cls, value: object) -> object:
        """Expand a two-digit year ('24') into a four-digit one (2024)."""
        if isinstance(value, int) and 0 <= value <= 99:
            return 2000 + value
        return value

    @property
    def as_date(self) -> Optional[date]:
        """Full calendar date, or None when the label printed only month/year."""
        if self.day is None or self.month is None or self.year is None:
            return None
        try:
            return date(self.year, self.month, self.day)
        except ValueError:
            return None

    @property
    def has_month_and_year(self) -> bool:
        """Rule 6(1)(d) is satisfied by month + year; the day is optional."""
        return self.month is not None and self.year is not None


class ConsumerCareDeclaration(DeclarationEvidence):
    """
    Consumer care details -- Rule 6(2): every package shall bear the name,
    address, telephone number and e-mail address of the person, or the office,
    which can be contacted in case of complaints.
    """

    contact_person_or_designation: Optional[str] = Field(
        description="e.g. 'Customer Care Executive', 'Manager - Consumer Affairs'."
    )
    phone_numbers: list[str] = Field(
        description=(
            "Every telephone number printed, verbatim. Empty list if none. "
            "Never invent or complete a partially visible number."
        )
    )
    email_addresses: list[str] = Field(
        description="Every e-mail address printed, verbatim. Empty list if none."
    )
    is_toll_free: Optional[bool] = Field(
        description="True if the label describes the number as toll-free / 1800-series."
    )
    address: Optional[PostalAddress] = Field(
        description="Consumer-care address, if printed separately from the manufacturer."
    )

    #: Indian mobile (ten digits starting 6-9), optionally carrying a +91 / 0
    #: prefix, or a landline / short-code of 8-13 digits.
    _PHONE_RE = re.compile(r"^(?:\+?91|0)?[6-9]\d{9}$|^\d{8,13}$")
    _EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")

    @property
    def normalised_phone_numbers(self) -> list[str]:
        """Digit-only forms of every printed number that parses as dialable."""
        cleaned: list[str] = []
        for raw in self.phone_numbers:
            digits = re.sub(r"[^\d+]", "", raw).lstrip("+")
            if self._PHONE_RE.match(digits):
                cleaned.append(digits)
        return cleaned

    @property
    def has_valid_phone(self) -> bool:
        """True when at least one printed number is structurally dialable."""
        return bool(self.normalised_phone_numbers)

    @property
    def valid_email_addresses(self) -> list[str]:
        """E-mail addresses that are at least structurally well-formed."""
        return [e for e in self.email_addresses if self._EMAIL_RE.match(e.strip())]

    @property
    def has_valid_email(self) -> bool:
        """True when at least one printed e-mail address is well-formed."""
        return bool(self.valid_email_addresses)


class ExtractedLabelData(_VisionModel):
    """
    The complete structured-output contract handed to the Vision LLM.

    A ``None`` return from the extractor means *"the image could not be read"*.
    An instance of this model with mostly-null declarations means something
    entirely different and far more actionable: *"the image was read, and the
    package does not carry these declarations"* -- i.e. a contravention. The
    two situations must never be conflated, which is why legibility is modelled
    explicitly through ``is_label_legible`` and ``unreadable_fields``.
    """

    # -- Identity of the commodity ------------------------------------------ #
    commodity_name: Optional[TextDeclaration] = Field(
        description="Common or generic name of the commodity -- Rule 6(1)(b)."
    )
    brand_name: Optional[TextDeclaration] = Field(
        description="Brand or trade name, if distinct from the commodity name."
    )

    # -- Rule 6(1)(a): responsible entities ---------------------------------- #
    manufacturer: Optional[EntityDeclaration] = Field(
        description="Manufacturer or packer block."
    )
    importer: Optional[EntityDeclaration] = Field(
        description="Importer block, for imported packages."
    )
    marketer: Optional[EntityDeclaration] = Field(
        description="Marketer block, where the label declares one."
    )

    # -- Rules 6(1)(c) and 6(1)(e) ------------------------------------------- #
    net_quantity: Optional[NetQuantityDeclaration] = Field(
        description="Net quantity declaration."
    )
    retail_sale_price: Optional[RetailSalePriceDeclaration] = Field(
        description="Maximum retail price declaration."
    )

    # -- Rule 6(1)(d) and shelf life ----------------------------------------- #
    manufacture_date: Optional[DateDeclaration] = Field(
        description="Month and year of manufacture."
    )
    packing_date: Optional[DateDeclaration] = Field(
        description="Month and year of pre-packing."
    )
    import_date: Optional[DateDeclaration] = Field(
        description="Month and year of import, for imported packages."
    )
    best_before: Optional[DateDeclaration] = Field(
        description="Best-before / use-by declaration."
    )

    # -- Rule 6(2) ------------------------------------------------------------ #
    consumer_care: Optional[ConsumerCareDeclaration] = Field(
        description="Consumer care contact details."
    )

    # -- Origin and ancillary identifiers ------------------------------------- #
    country_of_origin: Optional[TextDeclaration] = Field(
        description="Country of origin -- mandatory for imported packages."
    )
    batch_number: Optional[TextDeclaration] = Field(
        description="Batch / lot / code number."
    )
    fssai_licence_number: Optional[TextDeclaration] = Field(
        description=(
            "FSSAI licence number for food articles. Informational only: "
            "evaluated under the Food Safety and Standards Act, not these Rules."
        )
    )

    # -- Rule 9: language of the declarations ---------------------------------- #
    languages_detected: list[str] = Field(
        description=(
            "Languages in which the mandatory declarations appear, as lowercase "
            "English names, e.g. ['english', 'hindi']. Empty list if unreadable."
        )
    )

    # -- Extraction self-assessment -------------------------------------------- #
    is_packaged_commodity_label: bool = Field(
        description=(
            "True only if the image actually shows the label of a pre-packaged "
            "commodity. False for unrelated photographs, screenshots or documents."
        )
    )
    is_label_legible: bool = Field(
        description=(
            "False when glare, blur, occlusion, cropping or resolution prevent "
            "reliable reading of the declaration panel as a whole."
        )
    )
    unreadable_fields: list[DeclarationField] = Field(
        description=(
            "Declarations that appear to be present on the package but could "
            "not be read reliably. Distinct from declarations that are absent."
        )
    )
    overall_confidence: Confidence = Field(
        description="Overall confidence in the extraction as a whole."
    )
    raw_text_dump: Optional[str] = Field(
        description=(
            "Every character read from the declaration panel, verbatim, in "
            "reading order. Serves as the evidentiary transcript."
        )
    )
    extraction_notes: Optional[str] = Field(
        description=(
            "Brief note on obstructions or ambiguities, e.g. 'lower third of "
            "panel obscured by specular glare'. Null when the read was clean."
        )
    )

    @property
    def declared_entity(self) -> Optional[EntityDeclaration]:
        """
        The entity primarily answerable under Rule 6(1)(a).

        Preference order mirrors enforcement practice: the importer answers for
        an imported package, the manufacturer/packer otherwise, and the
        marketer only where nothing else is declared.
        """
        return self.importer or self.manufacturer or self.marketer

    @property
    def is_imported_package(self) -> bool:
        """True when the label carries importer or country-of-origin details."""
        return bool(self.importer) or bool(
            self.country_of_origin and self.country_of_origin.value
        )


# --------------------------------------------------------------------------- #
# System-facing models                                                         #
# --------------------------------------------------------------------------- #


class ImageQualityMetrics(BaseModel):
    """
    Objective, reproducible measurements of the submitted image.

    Produced by OpenCV in :mod:`app.vision_parser`, never by the LLM. These
    numbers are what justifies a "could not be determined" outcome before an
    adjudicating officer: they are deterministic and re-computable from the
    archived image bytes.
    """

    model_config = ConfigDict(extra="forbid")

    width: int = Field(description="Decoded image width in pixels.")
    height: int = Field(description="Decoded image height in pixels.")
    blur_variance: float = Field(
        description="Variance of the Laplacian; lower means blurrier."
    )
    glare_ratio: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of pixels detected as specular highlight (plastic glare).",
    )
    mean_brightness: float = Field(description="Mean grayscale intensity, 0-255.")
    contrast_std: float = Field(description="Standard deviation of grayscale intensity.")
    is_usable: bool = Field(
        description="Whether the image clears the minimum thresholds for adjudication."
    )
    quality_notes: list[str] = Field(
        default_factory=list,
        description="Human-readable reasons behind an unusable verdict.",
    )


class PackageCategory(str, Enum):
    """
    Regulatory category of the commodity.

    The 2011 Rules repeatedly defer to other statutes for particular classes of
    goods -- Explanation III to Rule 6(1)(a) disapplies the manufacturer
    declaration for food articles in favour of the Food Safety and Standards
    Act, 2006, and the provisos to Rule 6(1)(d) defer to the food, seed and
    cosmetics regimes for the date declaration. The engine needs to know the
    category before it can decide whether a rule bites at all.
    """

    GENERAL = "general"
    FOOD = "food"
    COSMETIC = "cosmetic"
    DRUG = "drug"
    SEED = "seed"
    ALCOHOLIC_BEVERAGE = "alcoholic_beverage"


class PackageShape(str, Enum):
    """Package geometry, which selects the Rule 7(4) area formula."""

    RECTANGULAR = "rectangular"
    CYLINDRICAL = "cylindrical"
    OTHER = "other"


class ContainerSurface(str, Enum):
    """
    How the declarations are applied to the container.

    Table-I to Rule 7 prescribes larger minimum character heights where the
    text is blown, formed or moulded onto the surface of the container rather
    than printed on a label.
    """

    PRINTED = "printed"
    BLOWN_FORMED_MOULDED = "blown_formed_moulded"


class PackageMeasurements(BaseModel):
    """
    Physical measurements of the package, supplied by the inspecting officer or
    a calibrated capture rig.

    These cannot be derived from an uncalibrated photograph: without a scale
    reference in frame, an image carries no absolute millimetres. Rule 7 is
    therefore assessed only when these measurements are provided, and is
    reported as *not assessed* otherwise -- never as a passed or failed check.
    """

    model_config = ConfigDict(extra="forbid")

    shape: PackageShape = Field(
        default=PackageShape.OTHER, description="Geometry, selecting the Rule 7(4) formula."
    )
    surface: ContainerSurface = Field(
        default=ContainerSurface.PRINTED,
        description="Printed, or blown/formed/moulded onto the container.",
    )

    # Rule 7(4) inputs. Supply the set matching ``shape``.
    height_cm: Optional[float] = Field(default=None, gt=0)
    width_cm: Optional[float] = Field(default=None, gt=0)
    circumference_cm: Optional[float] = Field(default=None, gt=0)
    total_surface_area_cm2: Optional[float] = Field(default=None, gt=0)
    declared_pdp_area_cm2: Optional[float] = Field(
        default=None,
        gt=0,
        description="Directly measured PDP area, overriding the computed value.",
    )
    capacity_cubic_cm: Optional[float] = Field(
        default=None, gt=0, description="Package capacity, for the Rule 7(1) exemption."
    )

    # Measured character dimensions, in millimetres.
    measured_numeral_height_mm: Optional[float] = Field(default=None, gt=0)
    measured_letter_height_mm: Optional[float] = Field(default=None, gt=0)
    measured_numeral_width_mm: Optional[float] = Field(default=None, gt=0)

    @property
    def principal_display_panel_area_cm2(self) -> Optional[float]:
        """
        Area of the principal display panel, computed per Rule 7(4).

        Excludes the top, bottom, flanges of cans and the shoulders and neck of
        bottles and jars, as the sub-rule directs:

        * rectangular -- height x width of the panel side;
        * cylindrical -- 40% of height x circumference;
        * any other shape -- 40% of the total surface area.

        Returns ``None`` when the measurements needed for the applicable
        formula were not supplied.
        """
        if self.declared_pdp_area_cm2 is not None:
            return self.declared_pdp_area_cm2

        if self.shape is PackageShape.RECTANGULAR:
            if self.height_cm and self.width_cm:
                return self.height_cm * self.width_cm
            return None

        if self.shape is PackageShape.CYLINDRICAL:
            if self.height_cm and self.circumference_cm:
                return 0.40 * self.height_cm * self.circumference_cm
            return None

        if self.total_surface_area_cm2:
            return 0.40 * self.total_surface_area_cm2
        return None


class EvaluationContext(BaseModel):
    """
    Everything the rules engine needs that cannot be read off the label itself.

    Defaults are deliberately conservative: an unspecified category is treated
    as ``GENERAL`` (the strictest path, where no other statute displaces these
    Rules), and perishability is assumed false so that Rule 6(1)(da) is not
    asserted against a commodity that never needed a best-before date.
    """

    model_config = ConfigDict(extra="forbid")

    category: PackageCategory = Field(default=PackageCategory.GENERAL)
    is_perishable: Optional[bool] = Field(
        default=None,
        description=(
            "Whether the commodity may become unfit for consumption over time, "
            "which triggers Rule 6(1)(da). Null means not established."
        ),
    )
    is_imported: Optional[bool] = Field(
        default=None,
        description=(
            "Overrides the inference drawn from the label. Null means infer "
            "from the importer / country-of-origin declarations."
        ),
    )
    is_wholesale_package: bool = Field(
        default=False,
        description="True for wholesale packages, which are governed by Rule 24.",
    )
    measurements: Optional[PackageMeasurements] = Field(
        default=None, description="Physical measurements enabling the Rule 7 checks."
    )
    inspector_id: Optional[str] = Field(default=None)
    jurisdiction: Optional[str] = Field(default=None)


class RuleViolation(BaseModel):
    """
    A single contravention, anchored to the exact provision it offends.

    Every violation must be traceable to (a) the rule contravened, (b) the
    value actually observed on the package, and (c) what the rule required
    instead. Anything less is not defensible in adjudication proceedings.
    """

    model_config = ConfigDict(extra="forbid")

    violation_id: UUID = Field(default_factory=uuid4)
    field: DeclarationField = Field(description="Declaration to which this relates.")
    rule_reference: str = Field(description="Provision contravened, e.g. 'Rule 6(1)(e)'.")
    statute: str = Field(
        default=STATUTE_NAME, description="Instrument containing the provision."
    )
    title: str = Field(description="Short caption, e.g. 'Retail sale price not declared'.")
    description: str = Field(
        description="Narrative finding as it will appear in the statutory notice."
    )
    severity: Severity = Field(description="Enforcement priority.")
    observed_value: Optional[str] = Field(
        default=None,
        description="Verbatim value found on the package, or null if absent.",
    )
    expected_requirement: str = Field(description="What the provision requires.")
    penalty_reference: Optional[str] = Field(
        default=None,
        description=f"Penal provision, e.g. 'Section 36(1) of the {PARENT_ACT_NAME}'.",
    )
    suggested_remediation: Optional[str] = Field(
        default=None,
        description="Corrective action advised to the manufacturer or packer.",
    )


class ComplianceReport(BaseModel):
    """
    The adjudicable output of one inspection.

    Rendered as JSON by the Phase 3 API and as a legal notice by the Phase 4
    ReportLab generator. ``image_sha256`` binds the report to the exact bytes
    that were examined, which is what makes the document evidentially sound.
    """

    model_config = ConfigDict(extra="forbid")

    report_id: UUID = Field(default_factory=uuid4)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # -- Chain of custody ----------------------------------------------------- #
    image_sha256: str = Field(description="SHA-256 digest of the original uploaded bytes.")
    image_filename: Optional[str] = Field(
        default=None, description="Original filename, if supplied."
    )
    image_quality: Optional[ImageQualityMetrics] = Field(default=None)

    # -- Findings ------------------------------------------------------------- #
    extracted_data: Optional[ExtractedLabelData] = Field(
        default=None,
        description=(
            "Null when the image was unreadable; the verdict is then INDETERMINATE."
        ),
    )
    status: ComplianceStatus = Field(description="Terminal verdict.")
    violations: list[RuleViolation] = Field(default_factory=list)
    compliance_score: float = Field(
        ge=0.0,
        le=100.0,
        description="Weighted conformity score across the mandatory declarations.",
    )
    summary: Optional[str] = Field(
        default=None, description="One-paragraph verdict summary."
    )

    # -- Provenance and audit -------------------------------------------------- #
    vision_model_id: Optional[str] = Field(
        default=None, description="Model that performed the extraction."
    )
    rules_engine_version: Optional[str] = Field(default=None)
    statute_reference: str = Field(default=STATUTE_NAME)
    jurisdiction: Optional[str] = Field(
        default=None, description="State / district of inspection."
    )
    inspector_id: Optional[str] = Field(
        default=None, description="Legal Metrology Officer identifier."
    )
    processing_time_ms: Optional[float] = Field(default=None, ge=0.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def violation_count(self) -> int:
        """Total number of contraventions recorded."""
        return len(self.violations)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def critical_violation_count(self) -> int:
        """Contraventions where a mandatory declaration is wholly absent."""
        return sum(1 for v in self.violations if v.severity is Severity.CRITICAL)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_compliant(self) -> bool:
        """True only on an affirmative compliant verdict -- never on INDETERMINATE."""
        return self.status is ComplianceStatus.COMPLIANT

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_actionable(self) -> bool:
        """Whether this report can support the issue of a statutory notice."""
        return self.status is ComplianceStatus.NON_COMPLIANT and bool(self.violations)

    @classmethod
    def indeterminate(
        cls,
        *,
        image_sha256: str,
        image_filename: Optional[str] = None,
        image_quality: Optional[ImageQualityMetrics] = None,
        summary: str = (
            "The submitted image could not be read with sufficient reliability. "
            "No finding of compliance or contravention has been recorded."
        ),
        **kwargs: object,
    ) -> "ComplianceReport":
        """
        Build the report issued when extraction returns ``None``.

        Deliberately carries a zero score and an empty violation list: an
        unreadable image is an evidentiary failure on the inspecting side, and
        must never be presented as an adverse finding against the manufacturer.
        """
        return cls(
            image_sha256=image_sha256,
            image_filename=image_filename,
            image_quality=image_quality,
            extracted_data=None,
            status=ComplianceStatus.INDETERMINATE,
            violations=[],
            compliance_score=0.0,
            summary=summary,
            **kwargs,  # type: ignore[arg-type]
        )
