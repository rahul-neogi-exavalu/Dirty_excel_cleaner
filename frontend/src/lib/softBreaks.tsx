import { Fragment, type ReactNode } from "react";

/**
 * A long identifier that may wrap where its words meet, never mid-word unless it has no
 * words to break at: after underscores, slashes and dots, and between camelCase words
 * (producer_eo_insurance_company_name, InsuranceCompanyID, Producer/Agency Name).
 * Used with ``[overflow-wrap:anywhere]`` as the last resort, so nothing is ever cut off.
 */
export function softBreaks(text: string): ReactNode {
  const parts = text.split(/(?<=[_/.])|(?<=[a-z0-9])(?=[A-Z])/);
  if (parts.length < 2) return text;
  return parts.map((part, index) => (
    <Fragment key={index}>
      {index > 0 && <wbr />}
      {part}
    </Fragment>
  ));
}
