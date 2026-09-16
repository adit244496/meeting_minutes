import { useState } from "react";

/** The customer's own logo, shown beside the app's.
 *
 *  Drop the file in `frontend/public/` as `company-logo.<ext>`, keeping it
 *  exactly as supplied - svg stays sharp at any size, the rest are used as
 *  they are. The first one that loads wins, and nothing is rendered until one
 *  exists, so an install without a logo simply shows no gap.
 */
const SOURCES = [
  "/company-logo.svg",
  "/company-logo.png",
  "/company-logo.webp",
  "/company-logo.jpg",
  "/company-logo.jpeg",
];

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
