import { Alert, Input, Space, Typography } from "antd";

import { LIMITS, type EisParseResult } from "./portfolioUtils";

export function EisSources({ text, onChange, validation }: {
  text: string; onChange: (value: string) => void; validation: EisParseResult | null;
}) {
  return (
    <div>
      <Typography.Text className="muted" style={{ display: "block", marginBottom: 6, fontSize: 13 }}>
        Укажите по одному номеру на строку · 19 цифр (44-ФЗ) или 11 цифр (223-ФЗ)
      </Typography.Text>
      <Input.TextArea aria-label="Номера закупок ЕИС" rows={6} value={text}
        onChange={(e) => onChange(e.target.value)} placeholder={"0362100026126000023\n32312345678"} />
      {validation && (
        <Space direction="vertical" size={10} style={{ width: "100%", marginTop: 12 }}>
          {validation.valid.length > LIMITS.maxFiles && (
            <Alert type="error" showIcon message={`Можно проверить до ${LIMITS.maxFiles} закупок за один запуск`} />
          )}
          {validation.duplicates.length > 0 && (
            <Alert type="info" showIcon message={`Исключены дубликаты (${validation.duplicates.length}): ${validation.duplicates.join(", ")}`} />
          )}
          {validation.invalid.length > 0 && (
            <Alert type="error" showIcon message="Ошибки в номерах (запуск заблокирован)"
              description={<ul style={{ margin: 0, paddingLeft: 16 }}>
                {validation.invalid.map((message, index) => <li key={index}>{message}</li>)}
              </ul>} />
          )}
          {validation.valid.length > 0 && validation.valid.length <= LIMITS.maxFiles
            && validation.invalid.length === 0 && (
              <Typography.Text type="success">Готово к проверке уникальных закупок: {validation.valid.length}</Typography.Text>
            )}
        </Space>
      )}
    </div>
  );
}
