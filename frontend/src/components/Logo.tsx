import { brand, lights } from "../theme";

/** Знак: светофор с тремя сигналами — красный, жёлтый, зелёный (цвета итога проверки). */
export function Logo({ inverted = false, size = 28 }: { inverted?: boolean; size?: number }) {
  const text = inverted ? "#FFFFFF" : brand.navy;
  const muted = inverted ? "rgba(255,255,255,0.6)" : brand.gray;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
      <svg width={size * 0.7} height={size} viewBox="0 0 22 32" aria-hidden>
        <rect width="22" height="32" rx="7" fill={inverted ? "rgba(255,255,255,0.12)" : brand.navy} />
        <circle cx="11" cy="7.5" r="4" fill={lights.red.solid} />
        <circle cx="11" cy="16" r="4" fill={lights.yellow.solid} />
        <circle cx="11" cy="24.5" r="4" fill={lights.green.solid} />
      </svg>
      <span style={{ display: "flex", flexDirection: "column", lineHeight: 1.1, whiteSpace: "nowrap" }}>
        <span style={{ color: text, fontWeight: 700, fontSize: 16, letterSpacing: 0.2 }}>
          Светофор
        </span>
        <span className="logo-tagline" style={{ color: muted, fontSize: 11, fontWeight: 500 }}>
          риски закупки для поставщика
        </span>
      </span>
    </span>
  );
}
