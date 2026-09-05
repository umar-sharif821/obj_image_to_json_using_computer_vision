import {
  NOT_LEGIBLE,
  absenceText,
  declaredEntity,
  formatAddress,
  formatConsumerCare,
  formatDate,
  formatEntity,
  formatPrice,
  formatQuantity,
  formatText,
} from '../lib/format.js'

/**
 * The declarations read off the label, with their governing rule.
 *
 * Every mandatory particular is listed whether or not it was found: the absence
 * of a declaration is the material fact in most inspections, so a row that is
 * missing must still appear and say why it is missing.
 *
 * @param {object} props
 * @param {object|null} props.extracted The `extracted_data` object.
 * @returns {JSX.Element}
 */
export default function DeclarationsCard({ extracted }) {
  if (!extracted) {
    return (
      <section className="card" aria-labelledby="declarations-heading">
        <h3 id="declarations-heading" className="card__heading">
          Declarations found
        </h3>
        <p className="card__empty">
          No declarations were extracted — the photograph could not be read
          reliably, so nothing has been recorded against this package.
        </p>
      </section>
    )
  }

  const entity = declaredEntity(extracted)

  /** @type {{label: string, rule: string|null, value: string|string[]}[]} */
  const rows = [
    {
      label: 'Product name',
      rule: 'Rule 6(1)(b)',
      value: formatText(extracted.commodity_name) || absenceText(extracted, 'commodity_name'),
    },
    {
      label: 'Brand',
      rule: null,
      value: formatText(extracted.brand_name) || 'Not declared',
    },
    {
      label: 'Maximum retail price',
      rule: 'Rule 6(1)(e)',
      value:
        formatPrice(extracted.retail_sale_price) ||
        absenceText(extracted, 'retail_sale_price', 'tax_inclusive_declaration'),
    },
    {
      label: 'Net quantity',
      rule: 'Rule 6(1)(c)',
      value: formatQuantity(extracted.net_quantity) || absenceText(extracted, 'net_quantity'),
    },
    {
      label: 'Date of manufacture',
      rule: 'Rule 6(1)(d)',
      value: formatDate(extracted.manufacture_date) || absenceText(extracted, 'manufacture_date'),
    },
    {
      label: 'Date of packing',
      rule: null,
      value: formatDate(extracted.packing_date) || absenceText(extracted, 'packing_date'),
    },
    {
      label: 'Best before / use by',
      rule: 'Rule 6(1)(da)',
      value: formatDate(extracted.best_before) || absenceText(extracted, 'best_before'),
    },
    {
      label: 'Manufacturer / packer',
      rule: 'Rule 6(1)(a)',
      value: formatEntity(entity) || absenceText(extracted, 'manufacturer_name'),
    },
    {
      label: 'Address',
      rule: 'Rule 6(1)(a)',
      value: formatAddress(entity?.address) || absenceText(extracted, 'manufacturer_address'),
    },
    {
      label: 'Consumer care',
      rule: 'Rule 6(2)',
      value:
        formatConsumerCare(extracted.consumer_care).length > 0
          ? formatConsumerCare(extracted.consumer_care)
          : absenceText(
              extracted,
              'consumer_care_phone',
              'consumer_care_email',
              'consumer_care_address',
            ),
    },
  ]

  if (extracted.country_of_origin?.value || extracted.importer) {
    rows.push({
      label: 'Country of origin',
      rule: 'Rule 6(1)(aa)',
      value:
        formatText(extracted.country_of_origin) || absenceText(extracted, 'country_of_origin'),
    })
  }
  if (extracted.batch_number?.value) {
    rows.push({ label: 'Batch number', rule: null, value: formatText(extracted.batch_number) })
  }

  return (
    <section className="card" aria-labelledby="declarations-heading">
      <h3 id="declarations-heading" className="card__heading">
        Declarations found on the label
      </h3>

      <dl className="declarations">
        {rows.map((row) => {
          const values = Array.isArray(row.value) ? row.value : [row.value]
          const missing =
            values.length === 1 &&
            (values[0].startsWith('Not declared') || values[0] === NOT_LEGIBLE)

          return (
            <div className="declarations__row" key={row.label}>
              <dt className="declarations__label">
                {row.label}
                {row.rule && <span className="declarations__rule">{row.rule}</span>}
              </dt>
              <dd className={`declarations__value${missing ? ' declarations__value--missing' : ''}`}>
                {values.map((line) => (
                  <span key={line}>{line}</span>
                ))}
              </dd>
            </div>
          )
        })}
      </dl>

      {extracted.unreadable_fields?.length > 0 && (
        <p className="card__note">
          <strong>Not legible in this photo:</strong>{' '}
          {extracted.unreadable_fields.map((f) => f.replace(/_/g, ' ')).join(', ')}. These have
          not been assessed and nothing has been recorded against them.
        </p>
      )}

      {extracted.extraction_notes && (
        <p className="card__note">
          <strong>Capture note:</strong> {extracted.extraction_notes}
        </p>
      )}
    </section>
  )
}
