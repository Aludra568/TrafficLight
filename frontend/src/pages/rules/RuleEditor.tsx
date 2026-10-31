import { MinusCircleOutlined, PlusOutlined, SafetyCertificateOutlined } from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Collapse,
  Divider,
  Drawer,
  Form,
  Input,
  Radio,
  Select,
  Space,
  Spin,
  Tag,
  Timeline,
  Typography,
} from "antd";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, ApiError } from "../../api/client";
import { keys, useRule } from "../../api/hooks";
import { useAuth } from "../../auth/AuthProvider";
import { TrafficLight } from "../../components/TrafficLight";
import { CATEGORIES, DOC_TYPES, NUMERIC_FACTS, patternToPhrase, RULE_TYPES } from "../../lib/catalog";
import { when } from "../../lib/format";
import { type Body, toBody } from "./ruleBody";
import { RuleSandbox } from "./RuleSandbox";
import { VerifyRuleModal } from "./VerifyRuleModal";

const NEW_RULE: Body = {
  id: "",
  title: "",
  category: "terms",
  law: ["44-ФЗ"],
  applies_to: ["contract"],
  type: "llm_check",
  group: "custom",
  question: "",
  violation_if: "",
  ok_if: "",
  search_hints: [],
  examples: [],
  severity: "yellow",
  absent_behavior: "ignore",
  message: "",
  why_risk: "",
  recommendation: "",
  legal_basis: [],
  status: "draft",
};

// цвет замечания — со значком светофора, как в отчёте: цвет никогда не передаётся только цветом
const SEVERITIES = [
  {
    value: "red",
    label: (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
        <TrafficLight light="red" iconOnly /> Критично
      </span>
    ),
  },
  {
    value: "yellow",
    label: (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
        <TrafficLight light="yellow" iconOnly /> Внимание
      </span>
    ),
  },
];

/** Поля формы ← описание правила (как в YAML). Фразы поиска — простым текстом, по строке. */
function toForm(body: Body) {
  return {
    ...body,
    phrases: ((body.patterns as string[] | undefined) ?? []).map(patternToPhrase).join("\n"),
    excludes: ((body.exclude_patterns as string[] | undefined) ?? []).map(patternToPhrase).join("\n"),
    conditions: (body.conditions as Body[] | undefined) ?? [{ when: "value > 30", severity: "red" }],
  };
}

export function RuleEditor({ code, open, onClose }: { code: string | null; open: boolean; onClose: () => void }) {
  const { message } = App.useApp();
  const { can } = useAuth();
  const client = useQueryClient();
  const detail = useRule(code);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [verifyOpen, setVerifyOpen] = useState(false);
  const isNew = code === null;
  const original: Body = isNew ? NEW_RULE : ((detail.data?.body as Body | undefined) ?? NEW_RULE);
  const type = Form.useWatch("type", form) as string | undefined;
  const absent = Form.useWatch("absent_behavior", form) as string | undefined;

  useEffect(() => {
    if (!open) return;
    if (isNew) form.setFieldsValue(toForm(NEW_RULE));
    else if (detail.data) form.setFieldsValue(toForm(detail.data.body as Body));
  }, [open, isNew, detail.data, form]);

  const current = (): Body | null => {
    try {
      return toBody(original, form.getFieldsValue(true) as Body);
    } catch {
      return null;
    }
  };

  const save = async () => {
    try {
      await form.validateFields();
    } catch {
      return; // ошибки уже показаны под полями
    }
    const body = current();
    if (!body) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await api<{ code: string; version: number; ruleset_version: number | null }>(
        isNew ? "/api/rules" : `/api/rules/${code}`,
        { method: isNew ? "POST" : "PUT", json: { rule: body, change_note: isNew ? "новое правило" : "правка в редакторе" } },
      );
      message.success(
        `Правило ${saved.code} сохранено (версия ${saved.version})${saved.ruleset_version ? ` и опубликовано в набор v${saved.ruleset_version}` : ""} — следующие проверки его учтут`,
      );
      await client.invalidateQueries({ queryKey: keys.rules });
      await client.invalidateQueries({ queryKey: keys.rule(saved.code) });
      onClose();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Не удалось сохранить правило");
    } finally {
      setSaving(false);
    }
  };

  const verified = () => {
    void client.invalidateQueries({ queryKey: keys.rules });
    void client.invalidateQueries({ queryKey: keys.rule(code ?? "") });
  };

  return (
    <Drawer
      open={open}
      onClose={() => {
        setError(null);
        onClose();
      }}
      width={760}
      destroyOnClose
      title={isNew ? "Новое правило" : `Правило ${code}`}
      extra={
        <Space>
          {!isNew && can("methodologist") && (
            <Button icon={<SafetyCertificateOutlined />} onClick={() => setVerifyOpen(true)}>
              Проверено юристом
            </Button>
          )}
          <Button type="primary" loading={saving} onClick={save}>
            Сохранить и опубликовать
          </Button>
        </Space>
      }
    >
      {!isNew && detail.isLoading ? (
        <Spin />
      ) : (
        <>
          {error && (
            <Alert type="error" showIcon message="Правило не сохранено" description={<span style={{ whiteSpace: "pre-wrap" }}>{error}</span>} style={{ marginBottom: 16 }} />
          )}
          <Form form={form} layout="vertical" requiredMark="optional">
            <Space.Compact block>
              <Form.Item name="id" label="Код" rules={[{ required: true, message: "Например, TRM-04" }]} style={{ width: 160 }}>
                <Input placeholder="TRM-04" disabled={!isNew} />
              </Form.Item>
              <Form.Item name="title" label="Название" rules={[{ required: true, message: "Коротко: что за риск" }]} style={{ flex: 1 }}>
                <Input placeholder="Поставка в выходные дни" />
              </Form.Item>
            </Space.Compact>
            <Space wrap size={16}>
              <Form.Item name="type" label="Как проверять" rules={[{ required: true }]} style={{ minWidth: 260 }}>
                <Select options={Object.entries(RULE_TYPES).slice(0, 3).map(([value, label]) => ({ value, label }))} disabled={!isNew && !["llm_check", "pattern", "threshold"].includes(String(original.type))} />
              </Form.Item>
              <Form.Item name="category" label="Категория" rules={[{ required: true }]} style={{ minWidth: 220 }}>
                <Select options={Object.entries(CATEGORIES).map(([value, label]) => ({ value, label }))} />
              </Form.Item>
              <Form.Item name="law" label="Закон" rules={[{ required: true, message: "Хотя бы один закон" }]} style={{ minWidth: 180 }}>
                <Select mode="multiple" options={[{ value: "44-ФЗ" }, { value: "223-ФЗ" }]} />
              </Form.Item>
            </Space>
            <Form.Item name="applies_to" label="В каких документах искать" rules={[{ required: true, message: "Хотя бы один тип документа" }]}>
              <Select mode="multiple" options={DOC_TYPES} />
            </Form.Item>

            {type === "llm_check" && (
              <>
                <Form.Item name="question" label="Что ищем" rules={[{ required: true, message: "Вопрос к документу" }]} extra="Вопрос к документу простыми словами">
                  <Input.TextArea rows={2} placeholder="Предусмотрена ли поставка в выходные и праздничные дни?" />
                </Form.Item>
                <Form.Item name="violation_if" label="Когда это риск" rules={[{ required: true }]}>
                  <Input.TextArea rows={2} placeholder="Поставка или приёмка допускаются в выходные или праздничные дни" />
                </Form.Item>
                <Form.Item name="ok_if" label="Когда всё в порядке" rules={[{ required: true }]}>
                  <Input.TextArea rows={2} placeholder="Поставка только в рабочие дни и часы" />
                </Form.Item>
                <Form.Item name="search_hints" label="Ключевые слова для поиска" rules={[{ required: true, message: "Хотя бы одно слово" }]} extra="Введите слово и нажмите Enter">
                  <Select mode="tags" tokenSeparators={[","]} placeholder="выходные дни, праздничные дни, график поставки" />
                </Form.Item>
                <Form.Item
                  label="Пограничные примеры"
                  extra="Короткая формулировка из договора и вывод: риск или нет. Помогают отличать похожие случаи. До 6 примеров"
                >
                  <Form.List name="examples">
                    {(fields, { add, remove }) => (
                      <div style={{ marginTop: 8 }}>
                        {fields.map((field, index) => (
                          <div
                            key={field.key}
                            style={{ display: "flex", flexWrap: "wrap", gap: "0 8px", alignItems: "flex-start", marginBottom: 8 }}
                          >
                            <Form.Item
                              {...field}
                              name={[field.name, "text"]}
                              rules={[
                                { required: true, message: "Введите формулировку" },
                                { min: 10, max: 600, message: "От 10 до 600 символов" },
                              ]}
                              style={{ flex: "2 1 260px", marginBottom: 8 }}
                            >
                              <Input.TextArea
                                rows={2}
                                placeholder="Формулировка"
                                aria-label={`Формулировка ${index + 1}`}
                                maxLength={600}
                              />
                            </Form.Item>
                            <Form.Item
                              {...field}
                              name={[field.name, "verdict"]}
                              rules={[{ required: true, message: "Выберите вывод" }]}
                              style={{ width: 130, marginBottom: 8 }}
                            >
                              <Select
                                options={[
                                  { value: "violation", label: "Риск" },
                                  { value: "ok", label: "Не риск" },
                                ]}
                                placeholder="Вывод"
                                aria-label={`Вывод ${index + 1}`}
                              />
                            </Form.Item>
                            <Form.Item
                              {...field}
                              name={[field.name, "why"]}
                              rules={[
                                { required: true, message: "Укажите причину" },
                                { min: 5, max: 300, message: "От 5 до 300 символов" },
                              ]}
                              style={{ flex: "2 1 200px", marginBottom: 8 }}
                            >
                              <Input
                                placeholder="Почему"
                                aria-label={`Почему ${index + 1}`}
                                maxLength={300}
                              />
                            </Form.Item>
                            <Button
                              type="text"
                              icon={<MinusCircleOutlined />}
                              aria-label={`Убрать пример ${index + 1}`}
                              title="Убрать пример"
                              onClick={() => remove(field.name)}
                            />
                          </div>
                        ))}
                        <Button
                          type="dashed"
                          icon={<PlusOutlined />}
                          disabled={fields.length >= 6}
                          onClick={() => add({ verdict: "violation" })}
                        >
                          Добавить пример
                        </Button>
                      </div>
                    )}
                  </Form.List>
                </Form.Item>
              </>
            )}

            {type === "pattern" && (
              <>
                <Form.Item name="phrases" label="Формулировки — каждая с новой строки" rules={[{ required: true, message: "Хотя бы одна формулировка" }]} extra="Регистр и лишние пробелы не важны. Сложное выражение можно вписать как есть">
                  <Input.TextArea rows={3} placeholder={"по мере необходимости\nв выходные дни"} />
                </Form.Item>
                <Form.Item name="excludes" label="Не считать риском, если рядом есть">
                  <Input.TextArea rows={2} placeholder="кроме выходных" />
                </Form.Item>
              </>
            )}

            {type === "threshold" && (
              <>
                <Form.Item name="fact" label="Условие закупки" rules={[{ required: true }]}>
                  <Select options={NUMERIC_FACTS.map((f) => ({ value: f.value, label: `${f.label}${f.unit ? `, ${f.unit}` : ""}` }))} />
                </Form.Item>
                <Typography.Text strong>Пороги — сверху вниз, срабатывает первый подходящий</Typography.Text>
                <Form.List name="conditions">
                  {(fields, { add, remove }) => (
                    <div style={{ marginTop: 8 }}>
                      {fields.map((field, index) => (
                        // строка порога: условие, цвет и кнопка «убрать»; на телефоне переносится
                        <div key={field.key} style={{ display: "flex", flexWrap: "wrap", gap: "0 8px", alignItems: "flex-start" }}>
                          <Form.Item
                            {...field}
                            name={[field.name, "when"]}
                            rules={[{ required: true }]}
                            style={{ flex: "1 1 220px", maxWidth: 280 }}
                          >
                            <Input placeholder="value > 30" aria-label={`Условие порога ${index + 1}`} />
                          </Form.Item>
                          <Form.Item {...field} name={[field.name, "severity"]} style={{ width: 160 }}>
                            <Select options={SEVERITIES} aria-label={`Цвет порога ${index + 1}`} />
                          </Form.Item>
                          <Button
                            type="text"
                            icon={<MinusCircleOutlined />}
                            aria-label={`Убрать порог ${index + 1}`}
                            title="Убрать порог"
                            disabled={fields.length === 1}
                            onClick={() => remove(field.name)}
                          />
                        </div>
                      ))}
                      {/* отдельной строкой под списком: не слипается с кнопкой «убрать» последнего порога */}
                      <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({ when: "value > 10", severity: "yellow" })}>
                        Добавить порог
                      </Button>
                    </div>
                  )}
                </Form.List>
              </>
            )}

            {type !== "threshold" && (
              <Form.Item name="severity" label="Цвет замечания" rules={[{ required: true }]}>
                <Radio.Group options={SEVERITIES} optionType="button" buttonStyle="solid" />
              </Form.Item>
            )}
            {type === "llm_check" && (
              <Form.Item name="absent_behavior" label="Если условия в документах нет">
                <Radio.Group
                  options={[
                    { value: "ignore", label: "Это нормально" },
                    { value: "report", label: "Это риск — сообщить" },
                  ]}
                />
              </Form.Item>
            )}
            {type === "llm_check" && absent === "report" && (
              <Space.Compact block>
                <Form.Item name="absent_severity" label="Цвет" style={{ width: 160 }} rules={[{ required: true }]}>
                  <Select options={SEVERITIES} />
                </Form.Item>
                <Form.Item name="absent_message" label="Сообщение" style={{ flex: 1 }} rules={[{ required: true }]}>
                  <Input placeholder="В контракте не найдено условие о …" />
                </Form.Item>
              </Space.Compact>
            )}

            <Divider style={{ margin: "8px 0 16px" }} />
            <Form.Item name="message" label="Текст замечания" rules={[{ required: true }]} extra="Можно вставить {value} — найденное значение, {quote} — цитату">
              <Input placeholder="Поставка допускается в выходные дни" />
            </Form.Item>
            <Form.Item name="why_risk" label="Почему это риск" rules={[{ required: true }]}>
              <Input.TextArea rows={2} />
            </Form.Item>
            <Form.Item name="recommendation" label="Что делать" rules={[{ required: true }]}>
              <Input.TextArea rows={2} />
            </Form.Item>
            <Form.Item name="clarification" label="Вопрос заказчику для черновика запроса">
              <Input.TextArea rows={2} placeholder="Просим разъяснить, допускается ли поставка в выходные дни…" />
            </Form.Item>
            <Typography.Text strong>Норма закона</Typography.Text>
            <Form.List name="legal_basis">
              {(fields, { add, remove }) => (
                <div style={{ marginTop: 8 }}>
                  {fields.map((field, index) => (
                    // строкой-блоком: кнопка «Добавить норму» ниже, а не вплотную к «убрать»
                    <div key={field.key} style={{ display: "flex", flexWrap: "wrap", gap: "0 8px", alignItems: "flex-start" }}>
                      <Form.Item {...field} name={[field.name, "act"]} rules={[{ required: true }]} style={{ width: 160 }}>
                        <Input placeholder="44-ФЗ" aria-label={`Закон, норма ${index + 1}`} />
                      </Form.Item>
                      <Form.Item {...field} name={[field.name, "ref"]} style={{ flex: "1 1 160px", maxWidth: 200 }}>
                        <Input placeholder="ч. 13.1 ст. 34" aria-label={`Статья и часть, норма ${index + 1}`} />
                      </Form.Item>
                      <Button
                        type="text"
                        icon={<MinusCircleOutlined />}
                        aria-label={`Убрать норму ${index + 1}`}
                        title="Убрать норму"
                        onClick={() => remove(field.name)}
                      />
                    </div>
                  ))}
                  <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({ act: "44-ФЗ", ref: "" })}>
                    Добавить норму
                  </Button>
                </div>
              )}
            </Form.List>
          </Form>

          <Collapse
            style={{ marginTop: 24 }}
            items={[
              { key: "sandbox", label: "Проверить на тексте", children: <RuleSandbox getRule={current} /> },
              ...(!isNew && detail.data
                ? [
                    {
                      key: "versions",
                      label: `История версий (${detail.data.versions.length})`,
                      children: (
                        <Timeline
                          items={detail.data.versions.map((v) => ({
                            children: (
                              <div>
                                <strong>Версия {v.version}</strong>{" "}
                                <Tag bordered={false} color={v.status === "verified" ? "success" : "default"}>
                                  {v.status === "verified" ? "проверено юристом" : "черновик"}
                                </Tag>
                                <div className="muted" style={{ fontSize: 12 }}>
                                  {when(v.created_at)}
                                  {v.author ? ` · ${v.author}` : ""}
                                  {v.change_note ? ` · ${v.change_note}` : ""}
                                </div>
                              </div>
                            ),
                          }))}
                        />
                      ),
                    },
                  ]
                : []),
            ]}
          />
        </>
      )}
      {code && (
        <VerifyRuleModal
          code={code}
          open={verifyOpen}
          onClose={() => setVerifyOpen(false)}
          onDone={verified}
        />
      )}
    </Drawer>
  );
}
