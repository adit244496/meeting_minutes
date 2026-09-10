/** Inline SVG icons.
 *
 *  Hand-rolled rather than pulling in an icon package: the app needs a dozen
 *  glyphs, and a dependency would ship hundreds. All of them inherit
 *  currentColor so they follow the theme without extra wiring.
 */

type Props = { size?: number; className?: string };

const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.7,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
});

export const IconMeetings = ({ size = 17, className }: Props) => (
  <svg {...base(size)} className={className}>
    <path d="M4 5h16v11H7l-3 3z" />
    <path d="M8 9h8M8 12.5h5" />
  </svg>
);

export const IconUsers = ({ size = 17, className }: Props) => (
  <svg {...base(size)} className={className}>
    <circle cx="9" cy="8" r="3.2" />
    <path d="M3.5 19c0-3 2.5-4.8 5.5-4.8s5.5 1.8 5.5 4.8" />
    <path d="M16.5 6.4a3.2 3.2 0 0 1 0 6M17.5 14.6c2.1.5 3.5 2.1 3.5 4.4" />
  </svg>
);

export const IconMic = ({ size = 17, className }: Props) => (
  <svg {...base(size)} className={className}>
    <rect x="9" y="3" width="6" height="11" rx="3" />
    <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3" />
  </svg>
);

export const IconStop = ({ size = 17, className }: Props) => (
  <svg {...base(size)} className={className}>
    <rect x="6.5" y="6.5" width="11" height="11" rx="2" fill="currentColor" stroke="none" />
  </svg>
);

export const IconUpload = ({ size = 17, className }: Props) => (
  <svg {...base(size)} className={className}>
    <path d="M12 16V4M8 7.5 12 3.5l4 4" />
    <path d="M4 16v2.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V16" />
  </svg>
);

export const IconSearch = ({ size = 17, className }: Props) => (
  <svg {...base(size)} className={className}>
    <circle cx="10.5" cy="10.5" r="6" />
    <path d="m15 15 4.5 4.5" />
  </svg>
);

export const IconChevronLeft = ({ size = 15, className }: Props) => (
  <svg {...base(size)} className={className}>
    <path d="m14 6-6 6 6 6" />
  </svg>
);

export const IconMenu = ({ size = 20, className }: Props) => (
  <svg {...base(size)} className={className}>
    <path d="M4 7h16M4 12h16M4 17h16" />
  </svg>
);

export const IconClose = ({ size = 18, className }: Props) => (
  <svg {...base(size)} className={className}>
    <path d="m6 6 12 12M18 6 6 18" />
  </svg>
);

export const IconSun = ({ size = 17, className }: Props) => (
  <svg {...base(size)} className={className}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </svg>
);

export const IconMoon = ({ size = 17, className }: Props) => (
  <svg {...base(size)} className={className}>
    <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z" />
  </svg>
);

export const IconSignOut = ({ size = 16, className }: Props) => (
  <svg {...base(size)} className={className}>
    <path d="M14 6V4.5A1.5 1.5 0 0 0 12.5 3h-7A1.5 1.5 0 0 0 4 4.5v15A1.5 1.5 0 0 0 5.5 21h7a1.5 1.5 0 0 0 1.5-1.5V18" />
    <path d="M17 8.5 20.5 12 17 15.5M20 12H9.5" />
  </svg>
);

export const IconAlert = ({ size = 17, className }: Props) => (
  <svg {...base(size)} className={className}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7.5v5.5M12 16.2v.3" />
  </svg>
);

export const IconRefresh = ({ size = 16, className }: Props) => (
  <svg {...base(size)} className={className}>
    <path d="M20 12a8 8 0 1 1-2.6-5.9M20 4v4h-4" />
  </svg>
);

export const IconSparkle = ({ size = 16, className }: Props) => (
  <svg {...base(size)} className={className}>
    <path d="M12 3.5 13.7 9l5.3 1.7-5.3 1.7L12 18l-1.7-5.6L5 10.7 10.3 9z" />
  </svg>
);
