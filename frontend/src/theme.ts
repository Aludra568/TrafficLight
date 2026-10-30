import type { ThemeConfig } from "antd";

/**
 * Дизайн-система.
 * Цвета бренда B2B-РТС — из таблиц стилей b2b-rts.ru. Красный бренда — только в знаке логотипа:
 * красный и зелёный светофора несут смысл риска и не должны путаться с кнопками.
 */
export const brand = {
  navy: "#03102D",
  red: "#EE0022",
  graphite: "#2D313C",
  slate: "#354057",
  // текст: вторичный и третичный — не ниже 4,5:1 на белом и на светлых подложках (WCAG AA)
  gray: "#5F6677",
  textTertiary: "#636A7A",
  // только рамки и декоративные значки: как цвет текста и значимых иконок не проходит
  // по контрасту (2,5:1; для значков со смыслом нужно 3:1 — для них brand.gray)
  grayLight: "#9A9FAB",
  page: "#F3F3F4",
  border: "#E6E7EA",
  borderStrong: "#CDCFD5",
  link: "#0B6E99",
  surface: "#FFFFFF",
  subtle: "#F8F8F9",
} as const;

export type LightKey = "red" | "yellow" | "green" | "gray";

/** Светофор: цвет всегда вместе с иконкой и словом (контраст текста ≥ 4,5:1). */
export const lights: Record<LightKey, { text: string; bg: string; border: string; solid: string }> = {
  red: { text: "#B42318", bg: "#FEF3F2", border: "#FECDCA", solid: "#D92D20" },
  yellow: { text: "#B54708", bg: "#FFFAEB", border: "#FEDF89", solid: "#F79009" },
  green: { text: "#067647", bg: "#ECFDF3", border: "#ABEFC6", solid: "#17B26A" },
  gray: { text: "#475467", bg: "#F2F4F7", border: "#D0D5DD", solid: "#98A2B3" },
};

/**
 * Крупные цифры (24 px полужирным — «крупный текст», нужен контраст 3:1, а не 4,5:1). Тёмный
 * жёлтый текста (#B54708) по яркости почти как красный (#B42318) — рядом их не различить;
 * янтарный #D17A00: 3,05:1 на фоне полосы итога и вдвое дальше от красного по яркости.
 */
export const bigNumber: Record<LightKey, string> = {
  red: lights.red.text,
  yellow: "#D17A00",
  green: lights.green.text,
  gray: lights.gray.text,
};

export const FONT = "Onest, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";

export const theme: ThemeConfig = {
  token: {
    colorPrimary: brand.navy,
    colorLink: brand.link,
    colorText: brand.navy,
    colorTextSecondary: brand.gray,
    colorTextTertiary: brand.textTertiary,
    colorTextDescription: brand.textTertiary,
    colorTextPlaceholder: "#6B7180",
    colorBorder: brand.borderStrong,
    colorBorderSecondary: brand.border,
    colorBgLayout: brand.page,
    colorError: lights.red.text,
    colorWarning: lights.yellow.text,
    colorSuccess: lights.green.text,
    // фоны и рамки статусов — явно из светофора: выведенные из тёмного текста, они получались
    // мутными (серо-зелёный тег «В порядке», контраст текста ~3:1)
    colorErrorBg: lights.red.bg,
    colorErrorBorder: lights.red.border,
    colorWarningBg: lights.yellow.bg,
    colorWarningBorder: lights.yellow.border,
    colorSuccessBg: lights.green.bg,
    colorSuccessBorder: lights.green.border,
    colorInfo: brand.link,
    fontFamily: FONT,
    fontSize: 14,
    borderRadius: 6,
    borderRadiusLG: 8,
    controlHeight: 36,
    wireframe: false,
  },
  components: {
    Layout: { headerBg: brand.page, headerHeight: 64, headerPadding: "0 24px", bodyBg: brand.page },
    Menu: {
      darkItemBg: brand.navy,
      darkItemColor: "rgba(255,255,255,0.72)",
      darkItemHoverColor: "#FFFFFF",
      darkItemSelectedBg: "rgba(255,255,255,0.12)",
      darkItemSelectedColor: "#FFFFFF",
      horizontalItemSelectedColor: "#FFFFFF",
    },
    Card: { headerFontSize: 16, paddingLG: 20 },
    Table: { headerBg: brand.subtle, headerColor: brand.slate, rowHoverBg: brand.subtle },
    Tabs: { itemSelectedColor: brand.navy, inkBarColor: brand.navy, titleFontSize: 14 },
    Button: { primaryShadow: "none", defaultShadow: "none", fontWeight: 500 },
  },
};
