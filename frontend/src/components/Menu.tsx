import { useEffect, useRef } from "react";

import { IconChevronDown } from "./icons";

/** A dropdown built on <details>, so it opens and closes with the keyboard
 *  without any focus-trap code. Clicking outside, or on a button inside the
 *  panel, closes it. */
export function Menu({
  label,
  icon,
  align = "right",
  className,
  children,
}: {
  label: string;
  icon: JSX.Element;
  align?: "left" | "right";
  className?: string;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDetailsElement>(null);

  useEffect(() => {
    function close(event: MouseEvent) {
      if (ref.current?.open && !ref.current.contains(event.target as Node)) ref.current.open = false;
    }
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, []);

  return (
    <details className={`menu ${className ?? ""}`} ref={ref}>
      <summary className="btn btn-sm">
        {icon}
        <span className="menu-label">{label}</span>
        <IconChevronDown size={13} />
      </summary>
      <div
        className={`menu-panel ${align === "left" ? "align-left" : ""}`}
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("button[data-close]") && ref.current) ref.current.open = false;
        }}
      >
        {children}
      </div>
    </details>
  );
}

export function MenuGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="menu-group">
      <span className="menu-title">{title}</span>
      {children}
    </div>
  );
}

export function MenuItem({
  onClick,
  disabled,
  note,
  checked,
  keepOpen,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  note?: string;
  /** Renders a radio mark; use with keepOpen for a choice inside the menu. */
  checked?: boolean;
  keepOpen?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      className={`menu-item ${checked ? "checked" : ""}`}
      data-close={keepOpen ? undefined : ""}
      onClick={onClick}
      disabled={disabled}
      role={checked === undefined ? undefined : "menuitemradio"}
      aria-checked={checked === undefined ? undefined : checked}
    >
      {checked !== undefined && <span className={`menu-radio ${checked ? "on" : ""}`} aria-hidden="true" />}
      <span className="grow">{children}</span>
      {note && <span className="dim tiny">{note}</span>}
    </button>
  );
}
