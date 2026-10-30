import {
  CheckCircleFilled,
  MinusCircleFilled,
  StopFilled,
  WarningFilled,
} from "@ant-design/icons";
import type { CSSProperties } from "react";

import { LIGHT_LABELS, lightOf } from "../lib/lights";
import { lights, type LightKey } from "../theme";

const ICONS: Record<LightKey, typeof StopFilled> = {
  red: StopFilled,
  yellow: WarningFilled,
  green: CheckCircleFilled,
  gray: MinusCircleFilled,
};

interface Props {
  light: string | null | undefined;
  size?: "sm" | "md" | "lg";
  label?: string;
  /** Только иконка (подпись — в title и для экранных дикторов). */
  iconOnly?: boolean;
  style?: CSSProperties;
}

const SIZES = {
  sm: { font: 12, icon: 13, padY: 1, padX: 8, gap: 5 },
  md: { font: 13, icon: 15, padY: 3, padX: 10, gap: 6 },
  lg: { font: 18, icon: 22, padY: 8, padX: 16, gap: 10 },
};

export function TrafficLight({ light, size = "md", label, iconOnly, style }: Props) {
  const key = lightOf(light);
  const palette = lights[key];
  const dims = SIZES[size];
  const Icon = ICONS[key];
  const text = label ?? LIGHT_LABELS[key];
  if (iconOnly) {
    return (
      <span role="img" aria-label={text} title={text} style={{ color: palette.solid, ...style }}>
        <Icon style={{ fontSize: dims.icon }} />
      </span>
    );
  }
  return (
    <span
      className="traffic-light"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: dims.gap,
        padding: `${dims.padY}px ${dims.padX}px`,
        borderRadius: size === "lg" ? 8 : 999,
        background: palette.bg,
        border: `1px solid ${palette.border}`,
        color: palette.text,
        fontSize: dims.font,
        fontWeight: 600,
        lineHeight: 1.4,
        whiteSpace: "nowrap",
        letterSpacing: size === "lg" ? 0.2 : 0,
        ...style,
      }}
    >
      <Icon style={{ fontSize: dims.icon, color: palette.solid }} aria-hidden />
      {text}
    </span>
  );
}
