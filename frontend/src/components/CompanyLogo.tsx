import { useState } from "react";

/** The customer's own logo, shown beside the app's.
 *
 *  Drop the file in `frontend/public/` as `company-logo.svg` (preferred - it
 *  stays sharp) or `company-logo.png`. Nothing is rendered until one exists, so
 *  an install without a logo simply shows no gap.
 */
const SOURCES = ["/company-logo.svg", "/company-logo.png"];

export function CompanyLogo({ className = "" }: { className?: string }) {
  const [attempt, setAttempt] = useState(0);

  if (attempt >= SOURCES.length) return null;
  return (
    <span className={`company-logo ${className}`}>
      <img
        src={SOURCES[attempt]}
        alt="Company logo"
        loading="lazy"
        decoding="async"
        onError={() => setAttempt((n) => n + 1)}
      />
    </span>
  );
}
